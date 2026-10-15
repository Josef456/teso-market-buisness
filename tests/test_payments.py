"""Integration tests use the throw-away PostgreSQL database in .env.test."""
import base64, os, random
from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest.mock import MagicMock
from pathlib import Path

import pytest

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env.test")
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="needs TEST_DATABASE_URL")

@pytest.fixture(scope="module")
def ctx():
    os.environ.update(DATABASE_URL=URL, SESSION_SECRET="s" * 32, IPN_SECRET="ipnsecret", APP_URL="http://x",
                      PESAPAL_CONSUMER_KEY="k", PESAPAL_CONSUMER_SECRET="s", DISABLE_RATE_LIMIT="1")
    import migrate; migrate.run()
    import app as a, pesapal, payments
    a.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return a.app, pesapal, payments

def mk_order(email, amount=20000):
    from db import conn
    with conn() as c:
        u = c.execute("INSERT INTO users(email,name,password_hash) VALUES(%s,'T','x') ON CONFLICT (email) DO UPDATE SET name='T' RETURNING id", (email,)).fetchone()
        t = f"trk-{random.random()}"
        o = c.execute("INSERT INTO orders(user_id,plan_id,amount,currency,tracking_id) VALUES(%s,1,%s,'UGX',%s) RETURNING id", (u["id"], amount, t)).fetchone()
    return u["id"], str(o["id"]), t

def gw(oid, **kw):
    return lambda t: {"status_code": 1, "payment_status_description": "Completed", "amount": 20000, "currency": "UGX", "confirmation_code": "C1", "merchant_reference": oid, **kw}

def one(sql, *p):
    from db import conn
    with conn() as c: return c.execute(sql, p).fetchone()

def mk_market_user(email, plan_id=1):
    from db import conn
    with conn() as database:
        user_id = database.execute(
            "INSERT INTO users(email,name,password_hash) VALUES(%s,'Market Test','x') RETURNING id",
            (email,),
        ).fetchone()["id"]
        order_id = database.execute(
            """INSERT INTO orders(user_id,plan_id,amount,currency,status,paid_at)
               SELECT %s,id,price_ugx,'UGX','paid',now() FROM plans WHERE id=%s RETURNING id""",
            (user_id, plan_id),
        ).fetchone()["id"]
        database.execute(
            """INSERT INTO subscriptions(user_id,plan_id,order_id,starts_at,ends_at)
               SELECT %s,id,%s,now(),now()+make_interval(days=>duration_days)
               FROM plans WHERE id=%s""",
            (user_id, order_id, plan_id),
        )
        return user_id

def logged_in_client(app, user_id):
    client = app.test_client()
    with client.session_transaction() as session:
        session["uid"] = user_id
    return client

def test_register_login_logout(ctx):
    app = ctx[0]; c = app.test_client(); email = f"a{random.random()}@t.co"
    r = c.post("/register", data={"name": "Ann", "email": email, "password": "longenoughpw1"}); assert r.headers["Location"] == "/dashboard"
    assert b"Hello, Ann" in c.get("/dashboard").data
    c.post("/logout"); assert c.get("/dashboard").status_code == 302
    assert c.post("/login", data={"email": email, "password": "wrong-password"}).headers["Location"] == "/login"
    assert c.post("/login", data={"email": email, "password": "longenoughpw1"}).headers["Location"] == "/dashboard"
    c.post("/logout")
    login_url = c.get("/listings/new").headers["Location"]
    assert login_url == "/login?next=/listings/new"
    assert c.post(login_url, data={"email": email, "password": "longenoughpw1"}).headers["Location"] == "/listings/new"

def test_csrf_enforced(ctx):
    app = ctx[0]; app.config["WTF_CSRF_ENABLED"] = True
    try: assert app.test_client().post("/register", data={"name": "x"}).status_code == 400
    finally: app.config["WTF_CSRF_ENABLED"] = False

def test_password_reset_email_uses_gmail_starttls(ctx, monkeypatch):
    import app as a
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "seller@example.com")
    monkeypatch.setenv("SMTP_PASS", "test-app-password")
    monkeypatch.setenv("MAIL_FROM", "seller@example.com")
    smtp = MagicMock()
    smtp_context = MagicMock()
    smtp_context.__enter__.return_value = smtp
    smtp_factory = MagicMock(return_value=smtp_context)
    monkeypatch.setattr(a.smtplib, "SMTP", smtp_factory)

    assert a.send_password_reset_email("customer@example.com", "https://example.com/reset/test-token")

    smtp_factory.assert_called_once_with("smtp.gmail.com", 587, timeout=15)
    smtp.starttls.assert_called_once()
    smtp.login.assert_called_once_with("seller@example.com", "test-app-password")
    message = smtp.send_message.call_args.args[0]
    assert message["To"] == "customer@example.com"
    assert "https://example.com/reset/test-token" in message.get_body().get_content()
    html_body = next(part for part in message.walk() if part.get_content_type() == "text/html")
    assert "https://example.com/reset/test-token" in html_body.get_content()

def test_marketplace_seller_can_post_and_manage_listing_with_photo(ctx, tmp_path, monkeypatch):
    app = ctx[0]
    monkeypatch.setattr(app, "static_folder", str(tmp_path))
    seller_id = mk_market_user(f"seller{random.random()}@market.test")
    client = logged_in_client(app, seller_id)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j7uoAAAAASUVORK5CYII="
    )
    response = client.post(
        "/listings",
        data={
            "category": "shoes",
            "title": "Blue running shoes",
            "description": "Size 42, in great condition.",
            "price": "125000",
            "currency": "UGX",
            "location": "Entebbe",
            "image": (BytesIO(png), "shoes.png"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    assert "/listings/" in response.headers["Location"]
    assert b"Blue running shoes" in client.get("/marketplace?category=shoes").data
    assert b"Blue running shoes" not in client.get("/marketplace?category=cars").data
    assert b"Blue running shoes" in client.get("/my-listings").data

    listing = one("SELECT * FROM listings WHERE seller_id=%s AND title=%s", seller_id, "Blue running shoes")
    assert listing["category"] == "shoes"
    assert listing["image"].startswith("uploads/")
    image_response = client.get(f"/static/{listing['image']}")
    assert image_response.status_code == 200
    assert image_response.mimetype == "image/png"

    response = client.post(f"/listings/{listing['id']}/status", data={"status": "sold"})
    assert response.status_code == 302
    assert one("SELECT status FROM listings WHERE id=%s", listing["id"])["status"] == "sold"
    assert client.get(f"/listings/{listing['id']}").status_code == 200
    assert app.test_client().get(f"/listings/{listing['id']}").status_code == 404

def test_marketplace_messages_are_private_and_marked_read(ctx):
    app = ctx[0]
    seller_id = mk_market_user(f"seller{random.random()}@market.test")
    buyer_id = mk_market_user(f"buyer{random.random()}@market.test")
    other_id = mk_market_user(f"other{random.random()}@market.test")
    from db import conn
    with conn() as database:
        listing_id = database.execute(
            """INSERT INTO listings(seller_id,category,title,description,price,currency,location)
               VALUES(%s,'cars','Used family car','Well maintained.',12500000,'UGX','Kampala')
               RETURNING id""",
            (seller_id,),
        ).fetchone()["id"]
    buyer = logged_in_client(app, buyer_id)
    response = buyer.post(
        f"/listings/{listing_id}",
        data={"body": "Hi, is this car still available?"},
    )
    assert response.status_code == 302
    conversation_id = int(response.headers["Location"].rsplit("/", 1)[-1])
    assert b"Hi, is this car still available?" in buyer.get("/messages").data

    other = logged_in_client(app, other_id)
    assert other.get(f"/messages/{conversation_id}").status_code == 404

    seller = logged_in_client(app, seller_id)
    assert b"Hi, is this car still available?" in seller.get("/messages").data
    unread = one(
        "SELECT count(*) AS n FROM messages WHERE conversation_id=%s AND recipient_id=%s AND read_at IS NULL",
        conversation_id,
        seller_id,
    )["n"]
    assert unread == 1
    response = seller.get(f"/messages/{conversation_id}")
    assert response.status_code == 200
    assert b"unread-count" not in response.data
    assert one(
        "SELECT count(*) AS n FROM messages WHERE conversation_id=%s AND recipient_id=%s AND read_at IS NULL",
        conversation_id,
        seller_id,
    )["n"] == 0

    response = seller.post(f"/messages/{conversation_id}", data={"body": "Yes, it is available."})
    assert response.status_code == 302
    assert b"Yes, it is available." in buyer.get(f"/messages/{conversation_id}").data

def test_marketplace_rejects_invalid_photo_and_currency_input(ctx):
    app = ctx[0]
    seller_id = mk_market_user(f"seller{random.random()}@market.test")
    client = logged_in_client(app, seller_id)
    response = client.post(
        "/listings",
        data={
            "category": "electronics",
            "title": "Wireless headphones",
            "description": "Like new.",
            "price": "100.00",
            "currency": "USD",
            "image": (BytesIO(b"<script>alert(1)</script>"), "bad.png"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    assert one("SELECT count(*) AS n FROM listings WHERE seller_id=%s", seller_id)["n"] == 0
    response = client.post(
        "/listings",
        data={
            "category": "electronics",
            "title": "Wireless headphones",
            "description": "Like new.",
            "price": "100.50",
            "currency": "UGX",
        },
    )
    assert response.status_code == 302
    assert one("SELECT count(*) AS n FROM listings WHERE seller_id=%s", seller_id)["n"] == 0

def test_marketplace_requires_active_plan_and_enforces_active_listing_limit(ctx):
    app = ctx[0]
    from db import conn
    with conn() as database:
        plan_id = database.execute(
            """INSERT INTO plans(name,description,price_ugx,duration_days,listing_limit)
               VALUES(%s,'One active listing',10000,30,1) RETURNING id""",
            (f"Limit-{random.random()}",),
        ).fetchone()["id"]
    seller_id = mk_market_user(f"seller{random.random()}@market.test", plan_id)
    client = logged_in_client(app, seller_id)
    marker = str(random.random())
    listing_data = {
        "category": "clothes",
        "title": f"Everyday jacket {marker}",
        "description": "Warm and in good condition.",
        "price": "70000",
        "currency": "UGX",
    }
    first = client.post("/listings", data=listing_data)
    assert first.status_code == 302
    second = client.post("/listings", data={**listing_data, "title": f"Extra jacket {marker}"})
    assert second.status_code == 302
    assert one("SELECT count(*) AS n FROM listings WHERE seller_id=%s AND status='active'", seller_id)["n"] == 1

    listing_id = one("SELECT id FROM listings WHERE seller_id=%s", seller_id)["id"]
    assert client.post(f"/listings/{listing_id}/status", data={"status": "sold"}).status_code == 302
    assert client.post("/listings", data={**listing_data, "title": f"Next jacket {marker}"}).status_code == 302
    assert one("SELECT count(*) AS n FROM listings WHERE seller_id=%s AND status='active'", seller_id)["n"] == 1

    with conn() as database:
        database.execute("UPDATE plans SET listing_limit=2 WHERE id=%s", (plan_id,))
    assert client.post("/listings", data={**listing_data, "title": f"Overflow jacket {marker}"}).status_code == 302
    with conn() as database:
        database.execute("UPDATE plans SET listing_limit=1 WHERE id=%s", (plan_id,))
    next_listing_id = one(
        "SELECT id FROM listings WHERE seller_id=%s AND title=%s", seller_id, f"Next jacket {marker}"
    )["id"]
    overflow_listing_id = one(
        "SELECT id FROM listings WHERE seller_id=%s AND title=%s", seller_id, f"Overflow jacket {marker}"
    )["id"]
    public_client = app.test_client()
    marketplace = public_client.get("/marketplace")
    assert f"Overflow jacket {marker}".encode() in marketplace.data
    assert f"Next jacket {marker}".encode() not in marketplace.data
    assert public_client.get(f"/listings/{next_listing_id}").status_code == 404
    assert public_client.get(f"/listings/{overflow_listing_id}").status_code == 200

    expired_id = mk_market_user(f"expired{random.random()}@market.test")
    with conn() as database:
        database.execute(
            "UPDATE subscriptions SET ends_at=now()-interval '1 day' WHERE user_id=%s",
            (expired_id,),
        )
    expired = logged_in_client(app, expired_id)
    assert expired.get("/listings/new").headers["Location"] == "/dashboard#pricing"
    assert expired.post("/listings", data=listing_data).headers["Location"] == "/dashboard#pricing"

def test_duplicate_webhooks_activate_once(ctx):
    app, pesapal, _ = ctx; uid, oid, t = mk_order(f"b{random.random()}@t.co"); pesapal.get_status = gw(oid); c = app.test_client()
    for _ in range(3): assert c.get(f"/payment/ipn/ipnsecret?OrderTrackingId={t}").status_code == 200
    assert one("SELECT count(*) n FROM subscriptions WHERE order_id=%s", oid)["n"] == 1
    assert one("SELECT status FROM orders WHERE id=%s", oid)["status"] == "paid"
    assert c.get(f"/payment/ipn/wrong?OrderTrackingId={t}").status_code == 404

def test_amount_mismatch_goes_to_review(ctx):
    _, pesapal, payments = ctx; uid, oid, t = mk_order(f"c{random.random()}@t.co"); pesapal.get_status = gw(oid, amount=1); payments.settle(t)
    assert one("SELECT status FROM orders WHERE id=%s", oid)["status"] == "review"
    assert one("SELECT count(*) n FROM subscriptions WHERE order_id=%s", oid)["n"] == 0

def test_redirect_alone_never_marks_paid(ctx):
    app, pesapal, _ = ctx; uid, oid, t = mk_order(f"d{random.random()}@t.co"); pesapal.get_status = gw(oid, status_code=0, payment_status_description="Pending")
    assert b"Payment pending" in app.test_client().get(f"/payment/return?OrderTrackingId={t}&status=paid").data
    assert one("SELECT status FROM orders WHERE id=%s", oid)["status"] == "pending"

def test_renewal_extends_and_expiry(ctx):
    _, pesapal, payments = ctx; uid, oid, t = mk_order(f"e{random.random()}@t.co"); pesapal.get_status = gw(oid); payments.settle(t)
    uid2, oid2, t2 = mk_order(f"e{random.random()}@t.co")
    from db import conn
    with conn() as c:
        c.execute("UPDATE orders SET user_id=%s WHERE id=%s", (uid, oid2))
    pesapal.get_status = gw(oid2); payments.settle(t2)
    from db import conn as cn
    with cn() as c: s = c.execute("SELECT * FROM subscriptions WHERE user_id=%s ORDER BY ends_at", (uid,)).fetchall()
    assert len(s) == 2 and s[1]["starts_at"] == s[0]["ends_at"] and payments.sub_state(s[1]) == "active"
    assert payments.sub_state({"ends_at": datetime.now(timezone.utc) - timedelta(seconds=1)}) == "expired" and payments.sub_state(None) == "none"


def test_marketplace_money_calculation_uses_ugx_rounding():
    from marketplace import calculate_totals

    totals = calculate_totals(10001, 2500, "12.5", "5", True, discount=1)
    assert totals == {
        "product_subtotal_ugx": 10001,
        "discount_ugx": 1,
        "delivery_fee_ugx": 2500,
        "tax_ugx": 625,
        "commission_ugx": 1250,
        "total_ugx": 13125,
        "seller_proceeds_ugx": 8750,
    }


def test_marketplace_checkout_verification_delivery_ledger_and_refund(ctx, monkeypatch):
    app, pesapal, payments = ctx
    from db import conn

    with conn() as database:
        seller_id = database.execute(
            """INSERT INTO users(email,name,password_hash,role)
               VALUES(%s,'Market Seller','x','seller') RETURNING id""",
            (f"market-seller-{random.random()}@market.test",),
        ).fetchone()["id"]
        buyer_id = database.execute(
            """INSERT INTO users(email,name,password_hash,phone)
               VALUES(%s,'Market Buyer','x','+256700000001') RETURNING id""",
            (f"market-buyer-{random.random()}@market.test",),
        ).fetchone()["id"]
        rider_id = database.execute(
            """INSERT INTO users(email,name,password_hash,role)
               VALUES(%s,'Delivery Rider','x','rider') RETURNING id""",
            (f"market-rider-{random.random()}@market.test",),
        ).fetchone()["id"]
        admin_id = database.execute(
            """INSERT INTO users(email,name,password_hash,role)
               VALUES(%s,'Marketplace Admin','x','admin') RETURNING id""",
            (f"market-admin-{random.random()}@market.test",),
        ).fetchone()["id"]
        listing_id = database.execute(
            """INSERT INTO listings(seller_id,category,title,description,price,currency,location)
               VALUES(%s,'shoes','Uganda market shoes','New shoes.',120000,'UGX','Kampala')
               RETURNING id""",
            (seller_id,),
        ).fetchone()["id"]
        town = f"Kampala-{random.random()}"
        zone_id = database.execute(
            """INSERT INTO delivery_zones(district,town,fee_ugx)
               VALUES('Kampala',%s,5000) RETURNING id""",
            (town,),
        ).fetchone()["id"]
        database.execute("DELETE FROM marketplace_commission_rates WHERE category='shoes'")
        database.execute(
            "INSERT INTO marketplace_commission_rates(category,rate_percent) VALUES('shoes',10)"
        )
        database.execute("DELETE FROM marketplace_tax_rates WHERE category='shoes'")
        database.execute(
            """INSERT INTO marketplace_tax_rates(category,rate_percent,active)
               VALUES('shoes',5,true)"""
        )

    captured = {}

    def submit_order(order):
        captured.update(order)
        return {
            "order_tracking_id": tracking_id,
            "redirect_url": "https://cybqa.pesapal.com/pesapalv3/redirect",
        }

    tracking_id = f"market-tracking-{random.random()}"
    provider_status = {
        "status_code": 0,
        "payment_status_description": "Pending",
        "amount": 131000,
        "currency": "UGX",
        "merchant_reference": "",
    }
    monkeypatch.setattr(pesapal, "submit_order", submit_order)
    monkeypatch.setattr(pesapal, "get_status", lambda _: dict(provider_status))

    buyer = logged_in_client(app, buyer_id)
    with conn() as database:
        database.execute(
            "UPDATE marketplace_settings SET enabled=false WHERE key='checkout_enabled'"
        )
    assert b"Online marketplace checkout is disabled" in buyer.get(
        f"/listings/{listing_id}/checkout"
    ).data
    with conn() as database:
        database.execute(
            "UPDATE marketplace_settings SET enabled=true WHERE key='checkout_enabled'"
        )
    response = buyer.post(
        f"/listings/{listing_id}/checkout",
        data={
            "recipient_name": "Market Buyer",
            "phone": "+256700000001",
            "delivery_method": "courier",
            "delivery_zone_id": zone_id,
            "district": "Kampala",
            "town": town,
            "neighbourhood": "Nakawa",
            "landmark": "Near the market",
            "map_pin": "https://maps.google.com/?q=0.3,32.6",
            "price": "1",
            "total_ugx": "1",
        },
    )
    assert response.status_code == 303
    order = one("SELECT * FROM marketplace_orders WHERE listing_id=%s", listing_id)
    assert order["reference"].startswith("TM-")
    assert response.headers["Location"] == f"/orders/{order['reference']}"
    assert order["status"] == "pending_payment"
    assert order["delivery_fee_ugx"] == 5000
    assert order["tax_ugx"] == 6000
    assert order["total_ugx"] == 131000
    assert order["commission_rate_percent"] == 10
    assert b"UGX 131,000" in buyer.get(response.headers["Location"]).data
    assert buyer.post(
        f"/orders/{order['reference']}/status", data={"status": "paid"}
    ).status_code == 302
    assert one("SELECT status FROM marketplace_orders WHERE id=%s", order["id"])["status"] == "pending_payment"
    with conn() as database:
        outsider_id = database.execute(
            """INSERT INTO users(email,name,password_hash)
               VALUES(%s,'Unrelated User','x') RETURNING id""",
            (f"market-outsider-{random.random()}@market.test",),
        ).fetchone()["id"]
    outsider = logged_in_client(app, outsider_id)
    assert outsider.get(f"/orders/{order['reference']}").status_code == 404

    response = buyer.post(f"/orders/{order['reference']}/pay")
    assert response.status_code == 303
    assert response.headers["Location"] == "https://cybqa.pesapal.com/pesapalv3/redirect"
    assert captured["amount"] == 131000
    assert buyer.post(f"/orders/{order['reference']}/pay").status_code == 302
    assert one(
        "SELECT count(*) AS n FROM marketplace_payment_transactions WHERE order_id=%s", order["id"]
    )["n"] == 1

    provider_status["merchant_reference"] = captured["id"]
    tracking_id = one(
        "SELECT tracking_id FROM marketplace_payment_transactions WHERE order_id=%s", order["id"]
    )["tracking_id"]
    assert buyer.get(f"/payment/return?OrderTrackingId={tracking_id}&status=paid").status_code == 302
    assert one("SELECT status FROM marketplace_orders WHERE id=%s", order["id"])["status"] == "pending_payment"
    provider_status.update(status_code=1, payment_status_description="Completed", amount=1)
    assert payments.settle_tracking(tracking_id)["status"] == "payment_review"
    assert one("SELECT status FROM marketplace_orders WHERE id=%s", order["id"])["status"] == "payment_review"
    assert one(
        "SELECT count(*) AS n FROM marketplace_ledger WHERE order_id=%s", order["id"]
    )["n"] == 0

    provider_status["amount"] = 131000
    assert payments.settle_tracking(tracking_id)["status"] == "paid"
    assert payments.settle_tracking(tracking_id)["status"] == "paid"

    admin = logged_in_client(app, admin_id)
    assert admin.post(
        f"/admin/marketplace/orders/{order['reference']}/fee",
        data={"fee_ugx": "1500", "note": "Provider transaction statement"},
    ).status_code == 302
    assert one(
        "SELECT payment_fee_ugx FROM marketplace_orders WHERE id=%s", order["id"]
    )["payment_fee_ugx"] == 1500
    assert admin.post(
        f"/admin/marketplace/orders/{order['reference']}/assign",
        data={"rider_id": rider_id},
    ).status_code == 302
    seller = logged_in_client(app, seller_id)
    for status in ("confirmed", "preparing", "out_for_delivery"):
        assert seller.post(
            f"/orders/{order['reference']}/status", data={"status": status}
        ).status_code == 302
    rider = logged_in_client(app, rider_id)
    assert rider.post(
        f"/orders/{order['reference']}/status",
        data={"status": "delivered", "proof_of_delivery": "Recipient confirmed delivery"},
    ).status_code == 302
    assert one("SELECT status FROM marketplace_orders WHERE id=%s", order["id"])["status"] == "delivered"
    with conn() as database:
        ledger_rows = database.execute(
            """SELECT entry_type,amount_ugx FROM marketplace_ledger
               WHERE order_id=%s AND refund_id IS NULL""",
            (order["id"],),
        ).fetchall()
    ledger = {row["entry_type"]: row["amount_ugx"] for row in ledger_rows}
    assert ledger == {
        "seller_proceeds": 108000,
        "platform_commission": 12000,
        "delivery_pass_through": 5000,
        "payment_fee": -1500,
        "tax": 6000,
    }

    assert admin.post(
        f"/admin/marketplace/orders/{order['reference']}/refund",
        data={
            "amount_ugx": "131000",
            "provider": "pesapal",
            "transaction_reference": f"verified-refund-{random.random()}",
            "note": "Confirmed in merchant console",
        },
    ).status_code == 302
    assert one("SELECT status FROM marketplace_orders WHERE id=%s", order["id"])["status"] == "refunded"
    assert one(
        """SELECT sum(amount_ugx) AS amount FROM marketplace_ledger
           WHERE order_id=%s AND entry_type='platform_commission'""",
        order["id"],
    )["amount"] == 0
    assert one(
        "SELECT amount_ugx FROM marketplace_ledger WHERE order_id=%s AND entry_type='refund'",
        order["id"],
    )["amount_ugx"] == -131000
    assert admin.get("/admin/marketplace").status_code == 200
    with conn() as database:
        database.execute(
            "UPDATE marketplace_settings SET enabled=false WHERE key='checkout_enabled'"
        )
    admin_page = admin.get("/admin/marketplace").data
    checkout_feature = admin_page.split(b"<b>Product checkout</b>", 1)[1].split(b"</form>", 1)[0]
    assert b"checked" not in checkout_feature
    with conn() as database:
        database.execute(
            "UPDATE marketplace_settings SET enabled=true WHERE key='checkout_enabled'"
        )
    csv_response = admin.get("/admin/marketplace.csv")
    assert csv_response.status_code == 200
    assert b"commission_ugx" in csv_response.data and order["reference"].encode() in csv_response.data
