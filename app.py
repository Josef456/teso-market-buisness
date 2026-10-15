from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import csv, hashlib, io, logging, os, secrets, smtplib
import psycopg
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
from psycopg.rows import dict_row
from email.message import EmailMessage
from html import escape
from functools import wraps
import ssl
from dotenv import load_dotenv
from psycopg.types.json import Jsonb
load_dotenv()
from flask import Flask, g, jsonify, redirect, render_template, request, session, flash, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect, CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
import marketplace as market_money
import payments, pesapal, payment_providers
from db import conn, ex, init_app, q

E = os.environ
for k in ("SESSION_SECRET", "IPN_SECRET"):
    if not E.get(k):
        raise RuntimeError(f"{k} must be set")
PROD = E.get("FLASK_ENV") == "production"
logging.basicConfig(level=logging.INFO, format='{"t":"%(asctime)s","lvl":"%(levelname)s","msg":"%(message)s"}')
log = logging.getLogger("teso")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(SECRET_KEY=E["SESSION_SECRET"], SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=PROD,
                  PERMANENT_SESSION_LIFETIME=7 * 86400, MAX_CONTENT_LENGTH=6 * 1024 * 1024, RATELIMIT_ENABLED=E.get("DISABLE_RATE_LIMIT") != "1")
csrf = CSRFProtect(app)
limiter = Limiter(get_remote_address, app=app, default_limits=["300/minute"], storage_uri="memory://")
init_app(app)

MARKET_CATEGORIES = {
    "shoes": {"label": "Shoes", "icon": "👟"},
    "clothes": {"label": "Clothes", "icon": "👕"},
    "cars": {"label": "Cars", "icon": "🚗"},
    "electronics": {"label": "Electronics", "icon": "📱"},
}
MAX_LISTING_IMAGE_BYTES = 5 * 1024 * 1024


@app.context_processor
def marketplace_context():
    return {
        "unread_messages": getattr(g, "unread_messages", 0),
        "unread_order_notifications": getattr(g, "unread_order_notifications", 0),
    }


@app.template_filter("money")
def money(a, cur):
    return f"UGX {int(a):,}" if cur == "UGX" else f"{cur} {float(a):,.2f}"


@app.template_filter("category_name")
def category_name(value):
    return MARKET_CATEGORIES.get(value, {}).get("label", "Marketplace")


@app.template_filter("category_icon")
def category_icon(value):
    return MARKET_CATEGORIES.get(value, {}).get("icon", "🛍️")

@app.template_filter("day")
def day(d):
    return d.strftime("%Y-%m-%d") if d else "-"

@app.after_request
def headers(r):
    r.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin",
                      "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; base-uri 'self'; frame-ancestors 'none'"})
    if PROD:
        r.headers["Strict-Transport-Security"] = "max-age=31536000"
    return r

@app.before_request
def load_user():
    g.user = None
    g.unread_messages = 0
    g.unread_order_notifications = 0
    if session.get("uid"):
        u = q("SELECT id,email,name,phone,role,disabled FROM users WHERE id=%s", (session["uid"],), one=True)
        if u and not u["disabled"]:
            g.user = u
            unread = q("SELECT count(*) AS count FROM messages WHERE recipient_id=%s AND read_at IS NULL",
                       (u["id"],), one=True)
            g.unread_messages = unread["count"]
            notification_count = q(
                "SELECT count(*) AS count FROM marketplace_order_notifications WHERE user_id=%s AND read_at IS NULL",
                (u["id"],), one=True,
            )
            g.unread_order_notifications = notification_count["count"]
        else:
            session.clear()

def need(f):
    @wraps(f)
    def w(*a, **k):
        return f(*a, **k) if g.user else redirect(url_for("login_form", next=request.full_path.rstrip("?")))
    return w

def admin(f):
    @wraps(f)
    def w(*a, **k):
        return f(*a, **k) if g.user["role"] == "admin" else (render_template("error.html", title="Not allowed", msg="Administrators only."), 403)
    return w


def role_required(*roles):
    def decorate(f):
        @wraps(f)
        def w(*args, **kwargs):
            if g.user and (g.user["role"] in roles or g.user["role"] == "admin"):
                return f(*args, **kwargs)
            if not g.user:
                return redirect(url_for("login_form", next=request.full_path.rstrip("?")))
            return render_template("error.html", title="Not allowed", msg="You do not have access to this page."), 403
        return w
    return decorate


def marketplace_order_visible(order, user):
    return (
        user["role"] == "admin"
        or order["buyer_id"] == user["id"]
        or order["seller_id"] == user["id"]
        or order["assigned_rider_id"] == user["id"]
    )


def payment_redirect_url(value):
    parsed = urlsplit(value or "")
    allowed_hosts = {"pay.pesapal.com", "cybqa.pesapal.com"}
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
        raise RuntimeError("Payment provider returned an unsafe checkout URL.")
    return value


def csv_cell(value):
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def create_marketplace_order(buyer, listing_id, form):
    method = form.get("delivery_method", "").strip().lower()
    if method not in {"seller", "courier", "pickup"}:
        raise ValueError("Choose a valid delivery or pickup method.")
    recipient = clean_text(form.get("recipient_name", ""), 80)
    phone = normalize_phone(form.get("phone", ""), 20)
    if not recipient or len("".join(ch for ch in phone if ch.isdigit())) < 9:
        raise ValueError("Enter the recipient's name and a valid phone number.")
    map_pin = clean_text(form.get("map_pin", ""), 500)
    if map_pin and (urlsplit(map_pin).scheme != "https" or not urlsplit(map_pin).netloc):
        raise ValueError("Map pins must be secure https links.")

    district = clean_text(form.get("district", ""), 80)
    town = clean_text(form.get("town", ""), 100)
    neighbourhood = clean_text(form.get("neighbourhood", ""), 120)
    landmark = clean_text(form.get("landmark", ""), 200)
    zone_id = None
    delivery_fee = 0
    with conn() as database:
        enabled = database.execute(
            "SELECT enabled FROM marketplace_settings WHERE key='checkout_enabled'"
        ).fetchone()
        if not enabled or not enabled["enabled"]:
            raise ValueError("Marketplace checkout is currently unavailable.")
        listing = database.execute(
            "SELECT * FROM listings WHERE id=%s AND status='active' FOR UPDATE",
            (listing_id,),
        ).fetchone()
        if not listing or listing["seller_id"] == buyer["id"]:
            raise ValueError("This listing is not available for purchase.")
        if listing["currency"] != "UGX" or Decimal(listing["price"]) != Decimal(listing["price"]).to_integral_value():
            raise ValueError("Online checkout currently supports UGX-priced listings only.")
        open_order = database.execute(
            """SELECT 1 FROM marketplace_orders WHERE listing_id=%s
               AND status NOT IN ('cancelled','refunded')""",
            (listing_id,),
        ).fetchone()
        if open_order:
            raise ValueError("This listing already has an active order.")

        if method == "pickup":
            district = town = neighbourhood = landmark = map_pin = ""
        else:
            if not district or not town or not neighbourhood:
                raise ValueError("Enter the district, town and village or neighbourhood for delivery.")
            zone = database.execute(
                "SELECT * FROM delivery_zones WHERE id=%s AND active",
                (to_int(form.get("delivery_zone_id"), -1),),
            ).fetchone()
            if not zone or zone["district"].casefold() != district.casefold() or zone["town"].casefold() != town.casefold():
                raise ValueError("Choose an active delivery zone matching the entered district and town.")
            zone_id, delivery_fee = zone["id"], zone["fee_ugx"]

        commission = database.execute(
            """SELECT rate_percent FROM marketplace_commission_rates
               WHERE active AND (seller_id=%s OR category=%s OR (seller_id IS NULL AND category IS NULL))
               ORDER BY CASE WHEN seller_id=%s THEN 2 WHEN category=%s THEN 1 ELSE 0 END DESC
               LIMIT 1""",
            (listing["seller_id"], listing["category"], listing["seller_id"], listing["category"]),
        ).fetchone()
        tax = database.execute(
            """SELECT rate_percent,applies_to_delivery FROM marketplace_tax_rates
               WHERE active AND (category=%s OR category IS NULL)
               ORDER BY CASE WHEN category=%s THEN 1 ELSE 0 END DESC LIMIT 1""",
            (listing["category"], listing["category"]),
        ).fetchone()
        commission_rate = commission["rate_percent"] if commission else Decimal("0")
        tax_rate = tax["rate_percent"] if tax else Decimal("0")
        tax_on_delivery = bool(tax and tax["applies_to_delivery"])
        totals = market_money.calculate_totals(
            int(listing["price"]), delivery_fee, commission_rate, tax_rate, tax_on_delivery
        )
        reference = f"TM-{uuid4().hex[:12].upper()}"
        order = database.execute(
            """INSERT INTO marketplace_orders
               (reference,buyer_id,seller_id,listing_id,listing_title,currency,
                product_subtotal_ugx,delivery_fee_ugx,tax_rate_percent,tax_applies_to_delivery,
                tax_ugx,total_ugx,commission_rate_percent,delivery_method,delivery_zone_id,
                recipient_name,phone,district,town,neighbourhood,landmark,map_pin)
               VALUES(%s,%s,%s,%s,%s,'UGX',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING *""",
            (
                reference, buyer["id"], listing["seller_id"], listing["id"], listing["title"],
                totals["product_subtotal_ugx"], totals["delivery_fee_ugx"], tax_rate, tax_on_delivery,
                totals["tax_ugx"], totals["total_ugx"], commission_rate, method, zone_id,
                recipient, phone, district, town, neighbourhood, landmark, map_pin,
            ),
        ).fetchone()
        database.execute(
            """INSERT INTO marketplace_order_status_history(order_id,new_status,changed_by,note)
               VALUES(%s,'pending_payment',%s,'Order created')""",
            (order["id"], buyer["id"]),
        )
        database.execute(
            """INSERT INTO marketplace_order_notifications(order_id,user_id,message)
               VALUES(%s,%s,%s),(%s,%s,%s)""",
            (
                order["id"], buyer["id"], f"Order {reference} was created and is awaiting payment.",
                order["id"], listing["seller_id"], f"New order {reference} is awaiting payment.",
            ),
        )
        database.execute(
            """INSERT INTO marketplace_audit_events(actor_id,order_id,event_type,details)
               VALUES(%s,%s,'order_created',%s)""",
            (buyer["id"], order["id"], Jsonb({"listing_id": listing["id"], "delivery_method": method})),
        )
        return order


def start_marketplace_payment(order):
    providers = payment_providers.configured_providers()
    if not providers:
        raise RuntimeError("No approved online payment provider is configured.")
    provider = providers[0]
    user = q("SELECT email,name,phone FROM users WHERE id=%s", (order["buyer_id"],), one=True)
    merchant_reference = str(uuid4())
    with conn() as database:
        locked_order = database.execute(
            "SELECT status,total_ugx FROM marketplace_orders WHERE id=%s AND buyer_id=%s FOR UPDATE",
            (order["id"], order["buyer_id"]),
        ).fetchone()
        if not locked_order or locked_order["status"] != "pending_payment":
            raise ValueError("This order is no longer awaiting payment.")
        previous = database.execute(
            """SELECT status FROM marketplace_payment_transactions WHERE order_id=%s
               ORDER BY id DESC LIMIT 1 FOR UPDATE""",
            (order["id"],),
        ).fetchone()
        if previous and previous["status"] not in {"failed", "reversed"}:
            raise ValueError("An existing payment attempt must be verified before another can start.")
        attempt = database.execute(
            """INSERT INTO marketplace_payment_transactions
               (order_id,provider,merchant_reference,status,amount_ugx,currency)
               VALUES(%s,%s,%s,'initiating',%s,'UGX') RETURNING id""",
            (order["id"], provider.name, merchant_reference, locked_order["total_ugx"]),
        ).fetchone()
        database.execute(
            """INSERT INTO marketplace_audit_events(actor_id,order_id,event_type,details)
               VALUES(%s,%s,'payment_attempt_started',%s)""",
            (order["buyer_id"], order["id"], Jsonb({"provider": provider.name, "attempt_id": attempt["id"]})),
        )
    try:
        response = provider.submit_order({
        "id": merchant_reference,
        "currency": "UGX",
        "amount": locked_order["total_ugx"],
        "description": f"Teso Market order {order['reference']}",
        "email": user["email"],
        "phone": order["phone"] or user["phone"],
        "name": order["recipient_name"] or user["name"],
        })
    except Exception:
        with conn() as database:
            database.execute(
                """UPDATE marketplace_payment_transactions SET status='submission_review',
                   reconciliation_status='review' WHERE id=%s""",
                (attempt["id"],),
            )
            database.execute(
                """INSERT INTO marketplace_audit_events(actor_id,order_id,event_type,details)
                   VALUES(%s,%s,'payment_submission_uncertain',%s)""",
                (order["buyer_id"], order["id"], Jsonb({"provider": provider.name, "attempt_id": attempt["id"]})),
            )
        raise
    tracking_id = clean_text(response.get("order_tracking_id", ""), 200)
    if not tracking_id:
        with conn() as database:
            database.execute(
                """UPDATE marketplace_payment_transactions SET status='submission_review',
                   reconciliation_status='review',raw=%s WHERE id=%s""",
                (Jsonb({"submission": response}), attempt["id"]),
            )
        raise RuntimeError("Payment provider did not return a transaction reference.")
    with conn() as database:
        database.execute(
            """UPDATE marketplace_payment_transactions
               SET tracking_id=%s,status='pending',raw=%s WHERE id=%s""",
            (
                tracking_id, Jsonb({"submission": response}), attempt["id"],
            ),
        )
    return payment_redirect_url(response.get("redirect_url"))

def email_ok(e):
    import re
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]{2,}", e)) and len(e) <= 254


def clean_text(value, limit=80):
    return (value or "").strip()[:limit]


def normalize_email(value):
    return (value or "").strip().lower()


def normalize_phone(value, limit=20):
    digits = "".join(ch for ch in (value or "") if ch.isdigit() or ch in "+()- .")
    return digits.strip()[:limit]


def to_int(v, d=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def seller_entitlement(user_id):
    plan = q(
        """SELECT p.id AS plan_id,p.name AS plan_name,p.listing_limit,s.ends_at
           FROM subscriptions s JOIN plans p ON p.id=s.plan_id
           WHERE s.user_id=%s AND s.ends_at>now()
           ORDER BY s.ends_at DESC LIMIT 1""",
        (user_id,),
        one=True,
    )
    if plan:
        active = q("SELECT count(*) AS count FROM listings WHERE seller_id=%s AND status='active'",
                   (user_id,), one=True)
        plan["active_listings"] = active["count"]
    return plan


def F(name, label, type="text", attrs=""):
    return {"name": name, "label": label, "type": type, "attrs": attrs}

def auth_page(title, action, fields, button, extra=""):
    return render_template("auth.html", title=title, action=action, fields=fields, button=button, extra=extra)

def start_session(uid):
    session.clear(); session["uid"] = uid; session.permanent = True


def listing_image_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def save_listing_image(upload):
    if not upload or not upload.filename:
        return None
    data = upload.stream.read(MAX_LISTING_IMAGE_BYTES + 1)
    if not data:
        return None
    if len(data) > MAX_LISTING_IMAGE_BYTES:
        return False
    extension = listing_image_type(data)
    if not extension:
        return False
    folder = Path(app.static_folder) / "uploads"
    filename = f"{uuid4().hex}{extension}"
    path = folder / filename
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError:
        log.exception("Could not save marketplace listing photo.")
        return False
    return f"uploads/{filename}"


def send_password_reset_email(address, link):
    host, username, password = E.get("SMTP_HOST"), E.get("SMTP_USER"), E.get("SMTP_PASS")
    sender = E.get("MAIL_FROM") or username
    if not all((host, username, password, sender)):
        log.error("Password reset email not sent: configure SMTP_HOST, SMTP_USER, SMTP_PASS and MAIL_FROM.")
        return False

    message = EmailMessage()
    message["From"], message["To"] = sender, address
    message["Subject"] = "Reset your Teso Market password"
    message.set_content(f"Reset your Teso Market password using this link (valid for 1 hour):\n\n{link}\n")
    message.add_alternative(
        f"""<div style="font-family:Arial,sans-serif;max-width:560px;margin:24px auto;padding:28px;border:1px solid #e6e3d8;border-radius:16px;color:#14211a">
        <h1 style="color:#168442">Teso Market</h1><h2>Reset your password</h2>
        <p>We received a request to reset your Teso Market password. This link expires in one hour.</p>
        <p><a href="{escape(link, quote=True)}" style="display:inline-block;padding:12px 20px;border-radius:9px;background:#168442;color:#fff;text-decoration:none">Choose a new password</a></p>
        <p style="color:#5d6b63">If you didn't request this, you can ignore this email.</p></div>""",
        subtype="html",
    )
    try:
        port = int(E.get("SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(username, password)
            smtp.send_message(message)
    except (OSError, ValueError, smtplib.SMTPException):
        log.exception("Password reset email delivery failed.")
        return False
    return True


@app.get("/health")
@limiter.exempt
def health():
    q("SELECT 1"); return "ok"

@app.get("/")
def landing():
    return render_template("landing.html", plans=q("SELECT * FROM plans WHERE active ORDER BY price_ugx"))


@app.get("/marketplace")
def marketplace():
    category = request.args.get("category", "").strip().lower()
    if category not in MARKET_CATEGORIES:
        category = ""
    search = clean_text(request.args.get("q", ""), 100)
    featured_setting = q(
        "SELECT enabled FROM marketplace_settings WHERE key='featured_listings_enabled'",
        one=True,
    )
    featured_enabled = bool(featured_setting and featured_setting["enabled"])
    clauses = ["active_position<=coalesce(listing_limit,2147483647)"]
    params = []
    if category:
        clauses.append("l.category=%s")
        params.append(category)
    if search:
        clauses.append("(l.title ILIKE %s OR l.description ILIKE %s OR l.location ILIKE %s)")
        term = f"%{search}%"
        params.extend((term, term, term))
    params.append(featured_enabled)
    listings = q(
        f"""WITH published AS (
              SELECT l.*,u.name AS seller_name,plan.listing_limit,
                     row_number() OVER (PARTITION BY l.seller_id ORDER BY l.created_at DESC,l.id DESC) AS active_position
              FROM listings l JOIN users u ON u.id=l.seller_id
              JOIN LATERAL (
                  SELECT p.listing_limit FROM subscriptions s JOIN plans p ON p.id=s.plan_id
                  WHERE s.user_id=l.seller_id AND s.ends_at>now()
                  ORDER BY s.ends_at DESC LIMIT 1
              ) plan ON true
              WHERE l.status='active' AND u.disabled=false
            )
            SELECT * FROM published l WHERE {' AND '.join(clauses)}
            ORDER BY CASE WHEN %s AND l.featured THEN 0 ELSE 1 END,l.created_at DESC,l.id DESC LIMIT 60""",
        params,
    )
    return render_template(
        "marketplace.html",
        listings=listings,
        categories=MARKET_CATEGORIES,
        selected_category=category,
        search=search,
        featured_enabled=featured_enabled,
    )


@app.get("/listings/new")
@need
def listing_new():
    entitlement = seller_entitlement(g.user["id"])
    if not entitlement:
        flash("Choose an active seller plan before posting a listing.", "err")
        return redirect(url_for("dashboard") + "#pricing")
    limit = entitlement["listing_limit"]
    if limit is not None and entitlement["active_listings"] >= limit:
        flash("Your active listing limit is full. Archive or sell a listing, or choose another plan.", "err")
        return redirect(url_for("my_listings"))
    return render_template("listing_new.html", categories=MARKET_CATEGORIES, entitlement=entitlement)


@app.post("/listings")
@need
@limiter.limit("20/hour")
def listing_create():
    form = request.form
    category = form.get("category", "").strip().lower()
    title = clean_text(form.get("title", ""), 120)
    description = clean_text(form.get("description", ""), 3000)
    location = clean_text(form.get("location", ""), 100)
    currency = form.get("currency", "UGX").strip().upper()
    try:
        price = Decimal(form.get("price", "").strip())
    except (InvalidOperation, ValueError):
        price = Decimal("0")
    if (category not in MARKET_CATEGORIES or len(title) < 3 or not description
            or not price.is_finite() or price <= 0 or price > Decimal("9999999999.99")
            or currency not in ("UGX", "USD")
            or (currency == "UGX" and price != price.to_integral_value())):
        flash("Enter a valid category, title, description, price and currency.", "err")
        return redirect(url_for("listing_new"))

    entitlement = seller_entitlement(g.user["id"])
    if not entitlement:
        flash("Choose an active seller plan before posting a listing.", "err")
        return redirect(url_for("dashboard") + "#pricing")
    limit = entitlement["listing_limit"]
    if limit is not None and entitlement["active_listings"] >= limit:
        flash("Your active listing limit is full. Archive or sell a listing, or choose another plan.", "err")
        return redirect(url_for("my_listings"))

    image = save_listing_image(request.files.get("image"))
    if image is False:
        flash("Use a JPG, PNG or WebP photo smaller than 5 MB.", "err")
        return redirect(url_for("listing_new"))
    denial = None
    row = None
    try:
        with psycopg.connect(E["DATABASE_URL"], row_factory=dict_row) as database:
            database.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (g.user["id"],))
            entitlement = database.execute(
                """SELECT p.listing_limit FROM subscriptions s JOIN plans p ON p.id=s.plan_id
                   WHERE s.user_id=%s AND s.ends_at>now() ORDER BY s.ends_at DESC LIMIT 1""",
                (g.user["id"],),
            ).fetchone()
            if not entitlement:
                denial = "Choose an active seller plan before posting a listing."
            else:
                active_count = database.execute(
                    "SELECT count(*) AS count FROM listings WHERE seller_id=%s AND status='active'",
                    (g.user["id"],),
                ).fetchone()["count"]
                limit = entitlement["listing_limit"]
                if limit is not None and active_count >= limit:
                    denial = "Your active listing limit is full. Archive or sell a listing, or choose another plan."
                else:
                    row = database.execute(
                        """INSERT INTO listings(seller_id,category,title,description,price,currency,location,image)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (g.user["id"], category, title, description, price, currency, location, image),
                    ).fetchone()
    except psycopg.Error:
        if image:
            (Path(app.static_folder) / image).unlink(missing_ok=True)
        raise
    if denial:
        if image:
            (Path(app.static_folder) / image).unlink(missing_ok=True)
        flash(denial, "err")
        return redirect(url_for("dashboard") + "#pricing" if "active seller plan" in denial else url_for("my_listings"))
    flash("Your listing is live on the marketplace.", "ok")
    return redirect(url_for("listing_detail", listing_id=row["id"]))


@app.route("/listings/<int:listing_id>", methods=["GET", "POST"])
def listing_detail(listing_id):
    listing = q(
        """SELECT l.*, u.name AS seller_name FROM listings l
           JOIN users u ON u.id=l.seller_id
           WHERE l.id=%s AND
             ((l.status='active' AND EXISTS (
                 SELECT 1 FROM subscriptions s JOIN plans p ON p.id=s.plan_id
                 WHERE s.user_id=l.seller_id AND s.ends_at>now()
                   AND (p.listing_limit IS NULL OR (
                     SELECT count(*) FROM listings newer
                     WHERE newer.seller_id=l.seller_id AND newer.status='active'
                       AND (newer.created_at>l.created_at OR (newer.created_at=l.created_at AND newer.id>l.id))
                   ) < p.listing_limit)
              )) OR l.seller_id=%s) AND u.disabled=false""",
        (listing_id, g.user["id"] if g.user else -1),
        one=True,
    )
    if not listing:
        return render_template("error.html", title="Not found", msg="This listing is not available."), 404

    if request.method == "POST":
        if not g.user:
            return redirect(url_for("login_form", next=request.path))
        if g.user["id"] == listing["seller_id"]:
            flash("You cannot message yourself about your own listing.", "err")
            return redirect(url_for("listing_detail", listing_id=listing_id))
        body = clean_text(request.form.get("body", ""), 2000)
        if not body:
            flash("Write a message before sending.", "err")
            return redirect(url_for("listing_detail", listing_id=listing_id))
        with psycopg.connect(E["DATABASE_URL"], row_factory=dict_row) as database:
            conversation = database.execute(
                """INSERT INTO conversations(listing_id,buyer_id,seller_id)
                   VALUES(%s,%s,%s) ON CONFLICT (listing_id,buyer_id)
                   DO UPDATE SET last_message_at=now() RETURNING id""",
                (listing_id, g.user["id"], listing["seller_id"]),
            ).fetchone()
            database.execute(
                """INSERT INTO messages(conversation_id,sender_id,recipient_id,body)
                   VALUES(%s,%s,%s,%s)""",
                (conversation["id"], g.user["id"], listing["seller_id"], body),
            )
            database.execute("UPDATE conversations SET last_message_at=now() WHERE id=%s",
                             (conversation["id"],))
        flash("Your message was sent to the seller.", "ok")
        return redirect(url_for("conversation_detail", conversation_id=conversation["id"]))

    conversation = None
    if g.user:
        conversation = q(
            "SELECT id FROM conversations WHERE listing_id=%s AND buyer_id=%s",
            (listing_id, g.user["id"]),
            one=True,
        )
    return render_template("listing_detail.html", listing=listing, conversation=conversation)


@app.get("/listings/<int:listing_id>/checkout")
@need
def listing_checkout(listing_id):
    listing = q(
        """SELECT l.*,u.name AS seller_name FROM listings l JOIN users u ON u.id=l.seller_id
           WHERE l.id=%s AND l.status='active' AND u.disabled=false""",
        (listing_id,), one=True,
    )
    if not listing or listing["seller_id"] == g.user["id"]:
        return render_template("error.html", title="Not found", msg="This listing is not available for checkout."), 404
    zones = q("SELECT * FROM delivery_zones WHERE active ORDER BY district,town")
    providers = payment_providers.configured_providers()
    checkout_setting = q(
        "SELECT enabled FROM marketplace_settings WHERE key='checkout_enabled'",
        one=True,
    )
    checkout_enabled = bool(checkout_setting and checkout_setting["enabled"])
    return render_template(
        "checkout.html", listing=listing, zones=zones, providers=providers,
        checkout_enabled=checkout_enabled,
    )


@app.post("/listings/<int:listing_id>/checkout")
@need
@limiter.limit("15/hour")
def listing_checkout_create(listing_id):
    if not payment_providers.configured_providers():
        flash("Online payments are not configured. No payment has been taken.", "err")
        return redirect(url_for("listing_detail", listing_id=listing_id))
    order = None
    try:
        order = create_marketplace_order(g.user, listing_id, request.form)
    except ValueError as error:
        flash(str(error), "err")
        return redirect(url_for("listing_checkout", listing_id=listing_id))
    except Exception:
        log.exception("Marketplace checkout failed listing=%s buyer=%s", listing_id, g.user["id"])
        flash("Could not create this order. Please try again.", "err")
        return redirect(url_for("listing_detail", listing_id=listing_id))
    return redirect(url_for("market_order_detail", reference=order["reference"]), code=303)


@app.get("/orders")
@need
def customer_orders():
    orders = q(
        """SELECT o.*,u.name AS seller_name
           FROM marketplace_orders o JOIN users u ON u.id=o.seller_id
           WHERE o.buyer_id=%s ORDER BY o.created_at DESC LIMIT 100""",
        (g.user["id"],),
    )
    return render_template("market_orders.html", orders=orders, heading="My orders")


@app.get("/orders/<reference>")
@need
def market_order_detail(reference):
    order = q(
        """SELECT o.*,buyer.name AS buyer_display_name,seller.name AS seller_name,
                  rider.name AS rider_name,z.district AS zone_district,z.town AS zone_town,
                  (SELECT transaction_reference FROM marketplace_payment_transactions p
                   WHERE p.order_id=o.id ORDER BY p.id DESC LIMIT 1) AS transaction_reference,
                  (SELECT status FROM marketplace_payment_transactions p
                   WHERE p.order_id=o.id ORDER BY p.id DESC LIMIT 1) AS payment_attempt_status,
                  (SELECT tracking_id FROM marketplace_payment_transactions p
                   WHERE p.order_id=o.id ORDER BY p.id DESC LIMIT 1) AS payment_tracking_id
           FROM marketplace_orders o JOIN users buyer ON buyer.id=o.buyer_id
           JOIN users seller ON seller.id=o.seller_id
           LEFT JOIN users rider ON rider.id=o.assigned_rider_id
           LEFT JOIN delivery_zones z ON z.id=o.delivery_zone_id
           WHERE o.reference=%s AND (o.buyer_id=%s OR o.seller_id=%s OR o.assigned_rider_id=%s OR %s)""",
        (reference, g.user["id"], g.user["id"], g.user["id"], g.user["role"] == "admin"),
        one=True,
    )
    if not order:
        return render_template("error.html", title="Not found", msg="Order not found."), 404
    history = q(
        """SELECT h.*,u.name AS actor_name FROM marketplace_order_status_history h
           LEFT JOIN users u ON u.id=h.changed_by WHERE h.order_id=%s ORDER BY h.created_at,h.id""",
        (order["id"],),
    )
    return render_template("market_order_detail.html", order=order, history=history)


@app.post("/orders/<reference>/pay")
@need
@limiter.limit("10/hour")
def market_order_pay(reference):
    order = q(
        "SELECT * FROM marketplace_orders WHERE reference=%s AND buyer_id=%s",
        (reference, g.user["id"]), one=True,
    )
    if not order or order["status"] != "pending_payment":
        return render_template("error.html", title="Not found", msg="Pending order not found."), 404
    try:
        target = start_marketplace_payment(order)
    except ValueError as error:
        flash(str(error), "err")
        return redirect(url_for("market_order_detail", reference=reference))
    except Exception:
        log.exception("Marketplace payment retry failed order=%s", reference)
        flash("Payment could not be started safely. Check your order status or contact the administrator before retrying.", "err")
        return redirect(url_for("market_order_detail", reference=reference))
    return redirect(target, code=303)


@app.post("/orders/<reference>/verify")
@need
@limiter.limit("15/hour")
def market_order_verify(reference):
    transaction = q(
        """SELECT p.tracking_id FROM marketplace_payment_transactions p
           JOIN marketplace_orders o ON o.id=p.order_id
           WHERE o.reference=%s AND (o.buyer_id=%s OR %s)
           ORDER BY p.created_at DESC LIMIT 1""",
        (reference, g.user["id"], g.user["role"] == "admin"), one=True,
    )
    if not transaction:
        flash("No payment attempt exists for this order.", "err")
        return redirect(url_for("market_order_detail", reference=reference))
    if not transaction["tracking_id"]:
        flash("The provider submission has no tracking reference yet and needs administrator reconciliation.", "err")
        return redirect(url_for("market_order_detail", reference=reference))
    try:
        result = payments.settle_marketplace(transaction["tracking_id"])
    except Exception:
        log.exception("Customer payment verification failed order=%s", reference)
        flash("Provider verification failed. No payment status was changed.", "err")
        return redirect(url_for("market_order_detail", reference=reference))
    flash(
        f"Provider status checked: {result['status']}."
        if result else "Provider transaction could not be matched to this order.",
        "ok" if result else "err",
    )
    return redirect(url_for("market_order_detail", reference=reference))


@app.post("/orders/<reference>/status")
@need
def market_order_status(reference):
    requested = request.form.get("status", "").strip()
    proof = clean_text(request.form.get("proof_of_delivery", ""), 500)
    valid_statuses = {
        "pending_payment", "paid", "confirmed", "preparing", "ready_for_pickup",
        "out_for_delivery", "delivered", "cancelled", "refunded", "payment_review",
    }
    if requested not in valid_statuses:
        flash("Choose a valid order status.", "err")
        return redirect(url_for("market_order_detail", reference=reference))
    denial = None
    with conn() as database:
        order = database.execute(
            "SELECT * FROM marketplace_orders WHERE reference=%s FOR UPDATE",
            (reference,),
        ).fetchone()
        if not order or not marketplace_order_visible(order, g.user):
            return render_template("error.html", title="Not found", msg="Order not found."), 404
        current = order["status"]
        is_admin = g.user["role"] == "admin"
        is_buyer = order["buyer_id"] == g.user["id"]
        is_seller = order["seller_id"] == g.user["id"]
        is_assigned_rider = order["assigned_rider_id"] == g.user["id"] and g.user["role"] == "rider"
        transitions = {
            "pending_payment": {"cancelled"},
            "paid": {"confirmed", "cancelled"},
            "confirmed": {"preparing", "cancelled"},
            "preparing": {"ready_for_pickup", "out_for_delivery", "cancelled"},
            "ready_for_pickup": {"delivered"},
            "out_for_delivery": {"delivered"},
        }
        allowed = requested in transitions.get(current, set())
        if is_admin:
            allowed = allowed and requested in {
                "confirmed", "preparing", "ready_for_pickup", "out_for_delivery", "delivered", "cancelled",
            }
        elif is_buyer:
            allowed = requested == "cancelled" and current == "pending_payment"
        elif is_seller:
            allowed = allowed and requested in {"confirmed", "preparing", "ready_for_pickup", "out_for_delivery", "delivered", "cancelled"}
        elif is_assigned_rider:
            allowed = current == "out_for_delivery" and requested == "delivered" and order["delivery_method"] == "courier"
        else:
            allowed = False

        if allowed and requested == "cancelled" and current == "pending_payment":
            active_attempt = database.execute(
                """SELECT status FROM marketplace_payment_transactions WHERE order_id=%s
                   ORDER BY id DESC LIMIT 1""",
                (order["id"],),
            ).fetchone()
            if active_attempt and active_attempt["status"] not in {"failed", "reversed"}:
                denial = "A payment attempt is awaiting verification; do not cancel or retry until it is resolved."
        if not allowed:
            denial = "This order cannot be moved to that status by your account."
        elif not denial and requested == "ready_for_pickup" and order["delivery_method"] != "pickup":
            denial = "Only pickup orders can be marked ready for pickup."
        elif not denial and requested == "out_for_delivery" and order["delivery_method"] == "pickup":
            denial = "Pickup orders cannot be marked out for delivery."
        elif not denial and requested == "out_for_delivery" and order["delivery_method"] == "courier" and not order["assigned_rider_id"]:
            denial = "Assign a rider before sending this order out for delivery."
        elif not denial and requested == "delivered" and not proof:
            denial = "Enter proof of delivery or pickup confirmation before completing the order."
        elif not denial and requested in {"confirmed", "preparing", "ready_for_pickup", "out_for_delivery", "delivered"} and current == "payment_review":
            denial = "An administrator must resolve the payment review before fulfillment."

        if not denial:
            database.execute(
                """UPDATE marketplace_orders SET status=%s,proof_of_delivery=CASE WHEN %s='delivered' THEN %s ELSE proof_of_delivery END,
                   delivered_at=CASE WHEN %s='delivered' THEN now() ELSE delivered_at END,
                   updated_at=now() WHERE id=%s""",
                (requested, requested, proof, requested, order["id"]),
            )
            database.execute(
                """INSERT INTO marketplace_order_status_history(order_id,old_status,new_status,changed_by,note)
                   VALUES(%s,%s,%s,%s,%s)""",
                (order["id"], current, requested, g.user["id"], proof),
            )
            database.execute(
                """INSERT INTO marketplace_order_notifications(order_id,user_id,message)
                   VALUES(%s,%s,%s),(%s,%s,%s)""",
                (
                    order["id"], order["buyer_id"], f"Order {reference} status changed to {requested.replace('_',' ')}.",
                    order["id"], order["seller_id"], f"Order {reference} status changed to {requested.replace('_',' ')}.",
                ),
            )
            database.execute(
                """INSERT INTO marketplace_audit_events(actor_id,order_id,event_type,details)
                   VALUES(%s,%s,'order_status_changed',%s)""",
                (g.user["id"], order["id"], Jsonb({"from": current, "to": requested})),
            )
            if requested == "delivered":
                updated_order = {**order, "payment_fee_ugx": order["payment_fee_ugx"]}
                market_money.record_completion_ledger(database, updated_order)
                database.execute("UPDATE listings SET status='sold',updated_at=now() WHERE id=%s", (order["listing_id"],))
    if denial:
        flash(denial, "err")
    else:
        flash("Order status updated.", "ok")
    return redirect(url_for("market_order_detail", reference=reference))


@app.get("/notifications")
@need
def order_notifications():
    notices = q(
        """SELECT n.*,o.reference FROM marketplace_order_notifications n
           JOIN marketplace_orders o ON o.id=n.order_id
           WHERE n.user_id=%s ORDER BY n.created_at DESC LIMIT 100""",
        (g.user["id"],),
    )
    ex("UPDATE marketplace_order_notifications SET read_at=now() WHERE user_id=%s AND read_at IS NULL",
       (g.user["id"],))
    return render_template("order_notifications.html", notices=notices)


@app.get("/orders/<reference>/receipt")
@need
def market_order_receipt(reference):
    order = q(
        """SELECT o.*,buyer.name AS buyer_display_name,seller.name AS seller_name,
                  (SELECT transaction_reference FROM marketplace_payment_transactions p
                   WHERE p.order_id=o.id ORDER BY p.id DESC LIMIT 1) AS transaction_reference
           FROM marketplace_orders o JOIN users buyer ON buyer.id=o.buyer_id
           JOIN users seller ON seller.id=o.seller_id
           WHERE o.reference=%s AND (o.buyer_id=%s OR %s)""",
        (reference, g.user["id"], g.user["role"] == "admin"), one=True,
    )
    if not order:
        return render_template("error.html", title="Not found", msg="Receipt not found."), 404
    return render_template("market_order_receipt.html", order=order)


@app.get("/my-listings")
@need
def my_listings():
    listings = q("SELECT * FROM listings WHERE seller_id=%s ORDER BY created_at DESC LIMIT 100",
                 (g.user["id"],))
    return render_template("my_listings.html", listings=listings,
                           entitlement=seller_entitlement(g.user["id"]))


@app.post("/listings/<int:listing_id>/status")
@need
def listing_status(listing_id):
    status = request.form.get("status", "")
    if status not in ("active", "sold", "archived"):
        flash("Choose a valid listing status.", "err")
    else:
        denial = None
        updated = 0
        with psycopg.connect(E["DATABASE_URL"], row_factory=dict_row) as database:
            database.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (g.user["id"],))
            listing = database.execute(
                "SELECT status FROM listings WHERE id=%s AND seller_id=%s FOR UPDATE",
                (listing_id, g.user["id"]),
            ).fetchone()
            if listing and status == "active":
                entitlement = database.execute(
                    """SELECT p.listing_limit FROM subscriptions s JOIN plans p ON p.id=s.plan_id
                       WHERE s.user_id=%s AND s.ends_at>now() ORDER BY s.ends_at DESC LIMIT 1""",
                    (g.user["id"],),
                ).fetchone()
                if not entitlement:
                    denial = "Choose an active seller plan before making listings active."
                else:
                    active_count = database.execute(
                        """SELECT count(*) AS count FROM listings
                           WHERE seller_id=%s AND status='active' AND id<>%s""",
                        (g.user["id"], listing_id),
                    ).fetchone()["count"]
                    limit = entitlement["listing_limit"]
                    if limit is not None and active_count >= limit:
                        denial = "Your active listing limit is full. Sell or archive another listing first."
            if listing and not denial:
                updated = database.execute(
                    """UPDATE listings SET status=%s,updated_at=now()
                       WHERE id=%s AND seller_id=%s""",
                    (status, listing_id, g.user["id"]),
                ).rowcount
        if denial:
            flash(denial, "err")
        elif updated:
            flash("Listing status updated.", "ok")
        else:
            flash("Listing not found.", "err")
    return redirect(url_for("my_listings"))


@app.get("/messages")
@need
def messages():
    threads = q(
        """SELECT c.id,c.last_message_at,l.id AS listing_id,l.title,l.image,l.category,
                  CASE WHEN c.buyer_id=%s THEN seller.name ELSE buyer.name END AS other_name,
                  (SELECT body FROM messages m WHERE m.conversation_id=c.id
                   ORDER BY m.created_at DESC,m.id DESC LIMIT 1) AS last_message,
                  (SELECT count(*) FROM messages m WHERE m.conversation_id=c.id
                   AND m.recipient_id=%s AND m.read_at IS NULL) AS unread
           FROM conversations c JOIN listings l ON l.id=c.listing_id
           JOIN users buyer ON buyer.id=c.buyer_id JOIN users seller ON seller.id=c.seller_id
           WHERE c.buyer_id=%s OR c.seller_id=%s
           ORDER BY c.last_message_at DESC LIMIT 100""",
        (g.user["id"], g.user["id"], g.user["id"], g.user["id"]),
    )
    return render_template("messages.html", threads=threads)


@app.route("/messages/<int:conversation_id>", methods=["GET", "POST"])
@need
def conversation_detail(conversation_id):
    conversation = q(
        """SELECT c.*,l.title,l.status AS listing_status,l.image,l.category,l.id AS listing_id,
                  buyer.name AS buyer_name,seller.name AS seller_name
           FROM conversations c JOIN listings l ON l.id=c.listing_id
           JOIN users buyer ON buyer.id=c.buyer_id JOIN users seller ON seller.id=c.seller_id
           WHERE c.id=%s AND (c.buyer_id=%s OR c.seller_id=%s)""",
        (conversation_id, g.user["id"], g.user["id"]),
        one=True,
    )
    if not conversation:
        return render_template("error.html", title="Not found", msg="Conversation not found."), 404
    other_id = conversation["seller_id"] if conversation["buyer_id"] == g.user["id"] else conversation["buyer_id"]
    other_name = conversation["seller_name"] if conversation["buyer_id"] == g.user["id"] else conversation["buyer_name"]
    if request.method == "POST":
        body = clean_text(request.form.get("body", ""), 2000)
        if not body:
            flash("Write a message before sending.", "err")
        else:
            with psycopg.connect(E["DATABASE_URL"], row_factory=dict_row) as database:
                database.execute(
                    "INSERT INTO messages(conversation_id,sender_id,recipient_id,body) VALUES(%s,%s,%s,%s)",
                    (conversation_id, g.user["id"], other_id, body),
                )
                database.execute("UPDATE conversations SET last_message_at=now() WHERE id=%s",
                                 (conversation_id,))
        return redirect(url_for("conversation_detail", conversation_id=conversation_id))

    cleared = ex(
        """UPDATE messages SET read_at=now()
           WHERE conversation_id=%s AND recipient_id=%s AND read_at IS NULL""",
        (conversation_id, g.user["id"]),
    )
    g.unread_messages = max(0, g.unread_messages - cleared)
    thread_messages = q(
        "SELECT * FROM messages WHERE conversation_id=%s ORDER BY created_at,id LIMIT 500",
        (conversation_id,),
    )
    return render_template(
        "conversation.html",
        conversation=conversation,
        messages=thread_messages,
        other_name=other_name,
    )


@app.get("/login")
def login_form():
    action = url_for("login", next=request.args.get("next", ""))
    return auth_page("Log in", action, [F("email", "Email", "email", "required"), F("password", "Password", "password", "required")], "Log in",
                     '<p class="muted"><a href="/forgot">Forgot password?</a> · <a href="/register">Create account</a></p>')

@app.get("/register")
def register_form():
    return auth_page("Create account", "/register", [F("name", "Full name", "text", 'required maxlength="80"'), F("email", "Email", "email", "required"),
                     F("phone", "Phone (optional)", "tel", 'placeholder="+2567…"'), F("password", "Password (min 10 characters)", "password", 'required minlength="10"')],
                     "Register", '<p class="muted">Have an account? <a href="/login">Log in</a></p>')

@app.post("/register")
@limiter.limit("25/15 minutes")
def register():
    f = request.form
    name, email, phone, pw = clean_text(f.get("name", ""), 80), normalize_email(f.get("email", "")), normalize_phone(f.get("phone", ""), 20), f.get("password", "")
    if not name or not email_ok(email) or not 10 <= len(pw) <= 128:
        flash("Enter a name, a valid email and a password of at least 10 characters.", "err"); return redirect("/register")
    u = q("INSERT INTO users(email,name,phone,password_hash) VALUES(%s,%s,%s,%s) ON CONFLICT (email) DO NOTHING RETURNING id", (email, name, phone, generate_password_hash(pw)), one=True)
    if not u:
        flash("That email is already registered. Try logging in.", "err"); return redirect("/register")
    start_session(u["id"]); return redirect("/dashboard")

@app.post("/login")
@limiter.limit("25/15 minutes")
def login():
    email = normalize_email(request.form.get("email", ""))
    u = q("SELECT * FROM users WHERE email=%s", (email,), one=True)
    if not u or u["disabled"] or not check_password_hash(u["password_hash"], request.form.get("password", "")):
        flash("Invalid email or password.", "err"); return redirect("/login")
    start_session(u["id"])
    next_url = request.args.get("next", "")
    if next_url.startswith("/") and not next_url.startswith("//") and "\\" not in next_url:
        return redirect(next_url)
    return redirect("/admin" if u["role"] == "admin" else "/dashboard")

@app.post("/logout")
def logout():
    session.clear(); return redirect("/")

@app.get("/forgot")
def forgot_form():
    return auth_page("Reset password", "/forgot", [F("email", "Email", "email", "required")], "Send reset link")

@app.post("/forgot")
@limiter.limit("25/15 minutes")
def forgot():
    email, t = normalize_email(request.form.get("email", "")), secrets.token_urlsafe(32)
    if not email_ok(email):
        flash("Enter a valid email address.", "err"); return redirect("/forgot")
    n = ex("UPDATE users SET reset_hash=%s, reset_expires=now()+interval '1 hour' WHERE email=%s AND NOT disabled", (hashlib.sha256(t.encode()).hexdigest(), email))
    if n:
        link = f"{E.get('APP_URL')}/reset/{t}"
        if E.get("SMTP_HOST"):
            send_password_reset_email(email, link)
        else:
            log.error("Password reset email not sent: set SMTP_HOST to enable email delivery.")
    flash("If that email exists, a reset link has been sent.", "ok"); return redirect("/login")

@app.get("/reset/<t>")
def reset_form(t):
    return auth_page("Choose new password", f"/reset/{t}", [F("password", "New password (min 10 characters)", "password", 'required minlength="10"')], "Update password")

@app.post("/reset/<t>")
@limiter.limit("25/15 minutes")
def reset(t):
    pw = request.form.get("password", "")
    if not 10 <= len(pw) <= 128:
        flash("Password must be at least 10 characters.", "err"); return redirect(f"/reset/{t}")
    n = ex("UPDATE users SET password_hash=%s, reset_hash=NULL, reset_expires=NULL WHERE reset_hash=%s AND reset_expires>now()", (generate_password_hash(pw), hashlib.sha256(t.encode()).hexdigest()))
    flash("Password updated. Please log in." if n else "Reset link is invalid or expired.", "ok" if n else "err"); return redirect("/login")

@app.get("/dashboard")
@need
def dashboard():
    sub = q("SELECT s.*, p.name FROM subscriptions s JOIN plans p ON p.id=s.plan_id WHERE s.user_id=%s ORDER BY s.ends_at DESC LIMIT 1", (g.user["id"],), one=True)
    orders = q("SELECT o.*, p.name plan FROM orders o JOIN plans p ON p.id=o.plan_id WHERE o.user_id=%s ORDER BY o.created_at DESC LIMIT 50", (g.user["id"],))
    market_orders = q(
        """SELECT * FROM marketplace_orders WHERE buyer_id=%s
           ORDER BY created_at DESC LIMIT 20""",
        (g.user["id"],),
    )
    left = max(0, (sub["ends_at"] - datetime.now(timezone.utc)).days + 1) if sub else 0
    return render_template(
        "dashboard.html", sub=sub, state=payments.sub_state(sub), left=left,
        orders=orders, market_orders=market_orders,
        plans=q("SELECT * FROM plans WHERE active ORDER BY price_ugx"),
    )

@app.post("/account")
@need
def account():
    name = clean_text(request.form.get("name", ""), 80)
    phone = normalize_phone(request.form.get("phone", ""), 20)
    if not name:
        flash("Name is required.", "err")
    else:
        ex("UPDATE users SET name=%s, phone=%s WHERE id=%s", (name, phone, g.user["id"])); flash("Account updated.", "ok")
    return redirect("/dashboard")

@app.post("/account/password")
@need
@limiter.limit("25/15 minutes")
def account_password():
    u, pw = q("SELECT password_hash FROM users WHERE id=%s", (g.user["id"],), one=True), request.form.get("password", "")
    if not check_password_hash(u["password_hash"], request.form.get("current", "")) or not 10 <= len(pw) <= 128:
        flash("Current password is wrong or the new one is too short.", "err")
    else:
        ex("UPDATE users SET password_hash=%s WHERE id=%s", (generate_password_hash(pw), g.user["id"])); flash("Password changed.", "ok")
    return redirect("/dashboard")

# ---------------- Payments ----------------
@app.post("/checkout")
@need
def checkout():
    p = q("SELECT * FROM plans WHERE id=%s AND active", (to_int(request.form.get("plan")),), one=True)
    cur = "USD" if str(request.form.get("currency", "")).strip().upper() == "USD" else "UGX"
    if not p or (cur == "USD" and not p["price_usd"]):
        flash("That plan or currency is not available.", "err"); return redirect("/dashboard")
    if not pesapal.configured():
        flash("Payments are not configured yet. The administrator must add the Pesapal credentials.", "err"); return redirect("/dashboard")
    o = q("INSERT INTO orders(user_id,plan_id,amount,currency) VALUES(%s,%s,%s,%s) RETURNING *", (g.user["id"], p["id"], p["price_usd"] if cur == "USD" else p["price_ugx"], cur), one=True)  # price from DB, never the browser
    try:
        r = pesapal.submit_order({"id": o["id"], "currency": cur, "amount": o["amount"], "description": f"Teso Market {p['name']}", "email": g.user["email"], "phone": g.user["phone"], "name": g.user["name"]})
        ex("UPDATE orders SET tracking_id=%s WHERE id=%s", (r["order_tracking_id"], o["id"]))
        return redirect(r["redirect_url"], 303)
    except Exception as e:
        log.error("submit order failed order=%s err=%s", o["id"], e)
        ex("UPDATE orders SET status='failed' WHERE id=%s", (o["id"],))
        flash("Could not start the payment. Please try again later.", "err"); return redirect("/dashboard")

@app.get("/payment/return")
def payment_return():
    """Browser return. The query string is NOT trusted: we ask the gateway and show what it says."""
    t, o = request.args.get("OrderTrackingId", ""), None
    marketplace_order = q(
        """SELECT o.reference FROM marketplace_orders o
           JOIN marketplace_payment_transactions p ON p.order_id=o.id
           WHERE p.tracking_id=%s""",
        (t,), one=True,
    ) if t else None
    if marketplace_order:
        try:
            payments.settle_tracking(t)
        except Exception:
            log.exception("Marketplace return verification failed tracking_id=%s", t)
        return redirect(url_for("market_order_detail", reference=marketplace_order["reference"]))
    try:
        if t: o = payments.settle_tracking(t)
    except Exception:
        log.exception("Subscription return verification failed tracking_id=%s", t)
    if not o:
        o = q("SELECT * FROM orders WHERE tracking_id=%s", (t,), one=True)
    return render_template("status.html", kind=o["status"] if o else "pending", o=o)

@app.route("/payment/ipn/<secret>", methods=["GET", "POST"])
@csrf.exempt
@limiter.exempt
def ipn(secret):
    if not secrets.compare_digest(secret, E["IPN_SECRET"]):
        return "", 404
    provider = payment_providers.provider_for("pesapal")
    signature_valid = provider.verify_webhook_signature(request.get_data(), request.headers)
    if signature_valid is False:
        return "", 401
    src = request.values
    t = provider.webhook_tracking_id(src, request.headers)
    try:
        if t: payments.settle_tracking(t)
        return jsonify(orderNotificationType=src.get("OrderNotificationType", "IPNCHANGE"), orderTrackingId=t, orderMerchantReference=src.get("OrderMerchantReference"), status=200)
    except Exception as e:
        log.error("ipn failed t=%s err=%s", t, e)
        return jsonify(status=500), 500  # non-200 makes the gateway retry

@app.get("/invoice/<oid>")
@need
def invoice(oid):
    o = q("""SELECT o.*, p.name plan, u.name uname, u.email, s.starts_at, s.ends_at,
        (SELECT confirmation_code FROM payments WHERE order_id=o.id AND confirmation_code IS NOT NULL ORDER BY id DESC LIMIT 1) confirmation_code
        FROM orders o JOIN plans p ON p.id=o.plan_id JOIN users u ON u.id=o.user_id JOIN subscriptions s ON s.order_id=o.id
        WHERE o.id::text=%s AND o.status='paid' AND (%s OR o.user_id=%s)""", (oid, g.user["role"] == "admin", g.user["id"]), one=True)
    return render_template("invoice.html", o=o) if o else (render_template("error.html", title="Not found", msg="Invoice not found."), 404)


@app.get("/seller/orders")
@need
@role_required("seller")
def seller_orders():
    orders = q(
        """SELECT o.*,buyer.name AS buyer_display_name FROM marketplace_orders o
           JOIN users buyer ON buyer.id=o.buyer_id
           WHERE o.seller_id=%s ORDER BY o.created_at DESC LIMIT 200""",
        (g.user["id"],),
    )
    return render_template("market_orders.html", orders=orders, heading="Seller orders")


@app.get("/seller/earnings")
@need
@role_required("seller")
def seller_earnings():
    entries = q(
        """SELECT l.*,o.reference,o.listing_title,o.created_at AS order_created_at
           FROM marketplace_ledger l JOIN marketplace_orders o ON o.id=l.order_id
           WHERE l.owner_id=%s AND l.entry_type='seller_proceeds'
           ORDER BY l.created_at DESC LIMIT 500""",
        (g.user["id"],),
    )
    summary = q(
        """SELECT payout_status,coalesce(sum(amount_ugx),0) AS amount
           FROM marketplace_ledger WHERE owner_id=%s AND entry_type='seller_proceeds'
           GROUP BY payout_status""",
        (g.user["id"],),
    )
    return render_template("seller_earnings.html", entries=entries, summary=summary)


@app.get("/rider/deliveries")
@need
@role_required("rider")
def rider_deliveries():
    orders = q(
        """SELECT * FROM marketplace_orders WHERE assigned_rider_id=%s
           ORDER BY created_at DESC LIMIT 200""",
        (g.user["id"],),
    )
    return render_template("market_orders.html", orders=orders, heading="Assigned deliveries")


@app.get("/admin/marketplace")
@need
@admin
def admin_marketplace():
    zones = q("SELECT * FROM delivery_zones ORDER BY district,town")
    commissions = q(
        """SELECT r.*,u.name AS seller_name FROM marketplace_commission_rates r
           LEFT JOIN users u ON u.id=r.seller_id ORDER BY r.seller_id NULLS FIRST,r.category NULLS FIRST"""
    )
    taxes = q("SELECT * FROM marketplace_tax_rates ORDER BY category NULLS FIRST")
    riders = q("SELECT id,name,email,phone FROM users WHERE role='rider' AND NOT disabled ORDER BY name")
    sellers = q(
        """SELECT DISTINCT u.id,u.name,u.email FROM users u
           JOIN listings l ON l.seller_id=u.id ORDER BY u.name"""
    )
    orders = q(
        """SELECT o.*,buyer.name AS buyer_name,seller.name AS seller_name,rider.name AS rider_name,
                  (SELECT transaction_reference FROM marketplace_payment_transactions p
                   WHERE p.order_id=o.id ORDER BY p.id DESC LIMIT 1) AS transaction_reference,
                  (SELECT status FROM marketplace_payment_transactions p
                   WHERE p.order_id=o.id ORDER BY p.id DESC LIMIT 1) AS payment_attempt_status,
                  (SELECT tracking_id FROM marketplace_payment_transactions p
                   WHERE p.order_id=o.id ORDER BY p.id DESC LIMIT 1) AS payment_tracking_id,
                  (SELECT merchant_reference FROM marketplace_payment_transactions p
                   WHERE p.order_id=o.id ORDER BY p.id DESC LIMIT 1) AS payment_merchant_reference
           FROM marketplace_orders o JOIN users buyer ON buyer.id=o.buyer_id
           JOIN users seller ON seller.id=o.seller_id
           LEFT JOIN users rider ON rider.id=o.assigned_rider_id
           ORDER BY o.created_at DESC LIMIT 200"""
    )
    ledger_summary = q(
        """SELECT entry_type,payout_status,sum(amount_ugx) AS amount,count(*) AS entries
           FROM marketplace_ledger GROUP BY entry_type,payout_status ORDER BY entry_type,payout_status"""
    )
    payouts = q(
        """SELECT l.id,l.amount_ugx,l.payout_status,l.payout_reference,u.name AS seller_name,o.reference
           FROM marketplace_ledger l JOIN users u ON u.id=l.owner_id
           JOIN marketplace_orders o ON o.id=l.order_id
           WHERE l.entry_type='seller_proceeds' ORDER BY l.created_at DESC LIMIT 200"""
    )
    settings = {
        row["key"]: row["enabled"]
        for row in q("SELECT key,enabled FROM marketplace_settings")
    }
    featured_listings = q(
        """SELECT l.id,l.title,l.category,l.featured,u.name AS seller_name
           FROM listings l JOIN users u ON u.id=l.seller_id
           WHERE l.status='active' ORDER BY l.created_at DESC LIMIT 200"""
    )
    return render_template(
        "admin_marketplace.html", zones=zones, commissions=commissions, taxes=taxes,
        riders=riders, sellers=sellers, orders=orders, ledger_summary=ledger_summary,
        payouts=payouts, categories=MARKET_CATEGORIES,
        featured_enabled=settings.get("featured_listings_enabled", False),
        checkout_enabled=settings.get("checkout_enabled", False),
        featured_listings=featured_listings,
    )


@app.post("/admin/marketplace/zones")
@need
@admin
def admin_delivery_zone():
    district = clean_text(request.form.get("district", ""), 80)
    town = clean_text(request.form.get("town", ""), 100)
    fee = to_int(request.form.get("fee_ugx"), -1)
    zone_id = to_int(request.form.get("zone_id"), 0)
    active = bool(request.form.get("active"))
    if not district or not town or fee < 0 or fee > 10000000:
        flash("Enter a district, town and a non-negative UGX delivery fee.", "err")
        return redirect(url_for("admin_marketplace"))
    try:
        with conn() as database:
            if zone_id:
                changed = database.execute(
                    """UPDATE delivery_zones SET district=%s,town=%s,fee_ugx=%s,active=%s
                       WHERE id=%s""",
                    (district, town, fee, active, zone_id),
                ).rowcount
                if not changed:
                    flash("Delivery zone not found.", "err")
                    return redirect(url_for("admin_marketplace"))
            else:
                database.execute(
                    "INSERT INTO delivery_zones(district,town,fee_ugx,active) VALUES(%s,%s,%s,%s)",
                    (district, town, fee, active),
                )
        flash("Delivery zone saved.", "ok")
    except psycopg.errors.UniqueViolation:
        flash("A delivery zone already exists for that district and town.", "err")
    return redirect(url_for("admin_marketplace"))


@app.post("/admin/marketplace/settings/<key>")
@need
@admin
def admin_marketplace_setting(key):
    if key not in {"checkout_enabled", "featured_listings_enabled"}:
        return render_template("error.html", title="Not found", msg="Marketplace setting not found."), 404
    enabled = bool(request.form.get("enabled"))
    ex("UPDATE marketplace_settings SET enabled=%s WHERE key=%s", (enabled, key))
    ex(
        """INSERT INTO marketplace_audit_events(actor_id,event_type,details)
           VALUES(%s,'marketplace_setting_changed',%s)""",
        (g.user["id"], Jsonb({"setting": key, "enabled": enabled})),
    )
    flash("Marketplace setting updated.", "ok")
    return redirect(url_for("admin_marketplace"))


@app.post("/admin/marketplace/listings/<int:listing_id>/featured")
@need
@admin
def admin_featured_listing(listing_id):
    featured = bool(request.form.get("featured"))
    changed = ex(
        "UPDATE listings SET featured=%s WHERE id=%s AND status='active'",
        (featured, listing_id),
    )
    if changed:
        ex(
            """INSERT INTO marketplace_audit_events(actor_id,event_type,details)
               VALUES(%s,'listing_featured_changed',%s)""",
            (g.user["id"], Jsonb({"listing_id": listing_id, "featured": featured})),
        )
    flash("Featured listing setting saved." if changed else "Active listing not found.",
          "ok" if changed else "err")
    return redirect(url_for("admin_marketplace"))


def _validated_rate(raw):
    try:
        value = Decimal(raw)
    except (InvalidOperation, TypeError):
        return None
    if not value.is_finite() or value < 0 or value > 100:
        return None
    return value.quantize(Decimal("0.01"))


@app.post("/admin/marketplace/commission")
@need
@admin
def admin_commission_rate():
    rate = _validated_rate(request.form.get("rate_percent", ""))
    category = request.form.get("category", "").strip().lower() or None
    seller_id = to_int(request.form.get("seller_id"), 0) or None
    if rate is None or (category and category not in MARKET_CATEGORIES) or (category and seller_id):
        flash("Enter a valid commission percentage and choose one seller or category scope.", "err")
        return redirect(url_for("admin_marketplace"))
    if seller_id and not q("SELECT id FROM users WHERE id=%s", (seller_id,), one=True):
        flash("Choose an existing seller account.", "err")
        return redirect(url_for("admin_marketplace"))
    with conn() as database:
        if seller_id:
            existing = database.execute(
                "SELECT id FROM marketplace_commission_rates WHERE seller_id=%s FOR UPDATE",
                (seller_id,),
            ).fetchone()
        elif category:
            existing = database.execute(
                "SELECT id FROM marketplace_commission_rates WHERE category=%s FOR UPDATE",
                (category,),
            ).fetchone()
        else:
            existing = database.execute(
                """SELECT id FROM marketplace_commission_rates
                   WHERE seller_id IS NULL AND category IS NULL FOR UPDATE"""
            ).fetchone()
        if existing:
            database.execute(
                "UPDATE marketplace_commission_rates SET rate_percent=%s,active=true WHERE id=%s",
                (rate, existing["id"]),
            )
        else:
            database.execute(
                """INSERT INTO marketplace_commission_rates(category,seller_id,rate_percent)
                   VALUES(%s,%s,%s)""",
                (category, seller_id, rate),
            )
        database.execute(
            """INSERT INTO marketplace_audit_events(actor_id,event_type,details)
               VALUES(%s,'commission_rate_changed',%s)""",
            (g.user["id"], Jsonb({"category": category, "seller_id": seller_id, "rate_percent": str(rate)})),
        )
    flash("Commission policy saved. It applies to future orders; existing order rates remain unchanged.", "ok")
    return redirect(url_for("admin_marketplace"))


@app.post("/admin/marketplace/tax")
@need
@admin
def admin_tax_rate():
    rate = _validated_rate(request.form.get("rate_percent", ""))
    category = request.form.get("category", "").strip().lower() or None
    active = bool(request.form.get("active"))
    applies_to_delivery = bool(request.form.get("applies_to_delivery"))
    if rate is None or (category and category not in MARKET_CATEGORIES):
        flash("Enter a valid tax percentage and category scope.", "err")
        return redirect(url_for("admin_marketplace"))
    with conn() as database:
        if category:
            existing = database.execute(
                "SELECT id FROM marketplace_tax_rates WHERE category=%s FOR UPDATE",
                (category,),
            ).fetchone()
        else:
            existing = database.execute(
                "SELECT id FROM marketplace_tax_rates WHERE category IS NULL FOR UPDATE"
            ).fetchone()
        if existing:
            database.execute(
                """UPDATE marketplace_tax_rates SET rate_percent=%s,active=%s,applies_to_delivery=%s
                   WHERE id=%s""",
                (rate, active, applies_to_delivery, existing["id"]),
            )
        else:
            database.execute(
                """INSERT INTO marketplace_tax_rates(category,rate_percent,active,applies_to_delivery)
                   VALUES(%s,%s,%s,%s)""",
                (category, rate, active, applies_to_delivery),
            )
        database.execute(
            """INSERT INTO marketplace_audit_events(actor_id,event_type,details)
               VALUES(%s,'tax_rate_changed',%s)""",
            (g.user["id"], Jsonb({"category": category, "rate_percent": str(rate), "active": active})),
        )
    flash("Tax configuration saved. It applies to future orders only.", "ok")
    return redirect(url_for("admin_marketplace"))


@app.post("/admin/marketplace/orders/<reference>/assign")
@need
@admin
def admin_assign_rider(reference):
    rider_id = to_int(request.form.get("rider_id"), 0) or None
    with conn() as database:
        order = database.execute(
            "SELECT * FROM marketplace_orders WHERE reference=%s FOR UPDATE",
            (reference,),
        ).fetchone()
        if not order:
            flash("Order not found.", "err")
            return redirect(url_for("admin_marketplace"))
        if order["delivery_method"] != "courier":
            flash("Riders can only be assigned to courier deliveries.", "err")
            return redirect(url_for("admin_marketplace"))
        if rider_id and not database.execute(
            "SELECT id FROM users WHERE id=%s AND role='rider' AND NOT disabled",
            (rider_id,),
        ).fetchone():
            flash("Choose an enabled delivery rider.", "err")
            return redirect(url_for("admin_marketplace"))
        old_rider = order["assigned_rider_id"]
        database.execute(
            "UPDATE marketplace_orders SET assigned_rider_id=%s,updated_at=now() WHERE id=%s",
            (rider_id, order["id"]),
        )
        database.execute(
            """INSERT INTO marketplace_audit_events(actor_id,order_id,event_type,details)
               VALUES(%s,%s,'rider_assignment_changed',%s)""",
            (g.user["id"], order["id"], Jsonb({"from": old_rider, "to": rider_id})),
        )
        if rider_id:
            database.execute(
                """INSERT INTO marketplace_order_notifications(order_id,user_id,message)
                   VALUES(%s,%s,%s)""",
                (order["id"], rider_id, f"You have been assigned order {reference}."),
            )
    flash("Rider assignment saved.", "ok")
    return redirect(url_for("admin_marketplace"))


@app.post("/admin/marketplace/orders/<reference>/refund")
@need
@admin
def admin_refund_order(reference):
    amount = to_int(request.form.get("amount_ugx"), 0)
    provider = clean_text(request.form.get("provider", ""), 40).lower()
    transaction_reference = clean_text(request.form.get("transaction_reference", ""), 200)
    note = clean_text(request.form.get("note", ""), 500)
    if amount <= 0 or not provider or not transaction_reference:
        flash("Enter a refund amount, provider, and verified provider refund reference.", "err")
        return redirect(url_for("admin_marketplace"))
    with conn() as database:
        order = database.execute(
            "SELECT * FROM marketplace_orders WHERE reference=%s FOR UPDATE",
            (reference,),
        ).fetchone()
        if not order:
            flash("Order not found.", "err")
            return redirect(url_for("admin_marketplace"))
        if order["status"] in {"pending_payment", "payment_review", "refunded"}:
            flash("Only a successfully paid order can be reconciled as refunded.", "err")
            return redirect(url_for("admin_marketplace"))
        prior = database.execute(
            "SELECT coalesce(sum(amount_ugx),0) AS total FROM marketplace_refunds WHERE order_id=%s",
            (order["id"],),
        ).fetchone()["total"]
        if amount + prior > order["total_ugx"]:
            flash("Refund amount exceeds the remaining amount paid.", "err")
            return redirect(url_for("admin_marketplace"))
        payment = database.execute(
            """SELECT provider FROM marketplace_payment_transactions WHERE order_id=%s
               ORDER BY created_at DESC LIMIT 1""",
            (order["id"],),
        ).fetchone()
        if not payment or provider != payment["provider"]:
            flash("Refund provider must match the verified payment provider.", "err")
            return redirect(url_for("admin_marketplace"))
        refund = database.execute(
            """INSERT INTO marketplace_refunds
               (order_id,amount_ugx,provider,transaction_reference,note,created_by)
               VALUES(%s,%s,%s,%s,%s,%s) RETURNING id""",
            (order["id"], amount, provider, transaction_reference, note, g.user["id"]),
        ).fetchone()
        market_money.record_refund_ledger(database, order, refund["id"], amount)
        refunded_total = prior + amount
        if refunded_total == order["total_ugx"]:
            database.execute(
                "UPDATE marketplace_orders SET status='refunded',updated_at=now() WHERE id=%s",
                (order["id"],),
            )
            database.execute(
                "UPDATE marketplace_payment_transactions SET reconciliation_status='refunded' WHERE order_id=%s",
                (order["id"],),
            )
            database.execute(
                """INSERT INTO marketplace_order_status_history(order_id,old_status,new_status,changed_by,note)
                   VALUES(%s,%s,'refunded',%s,%s)""",
                (order["id"], order["status"], g.user["id"], note),
            )
        database.execute(
            """INSERT INTO marketplace_audit_events(actor_id,order_id,event_type,details)
               VALUES(%s,%s,'refund_recorded',%s)""",
            (g.user["id"], order["id"], Jsonb({"amount_ugx": amount, "reference": transaction_reference})),
        )
        database.execute(
            """INSERT INTO marketplace_order_notifications(order_id,user_id,message)
               VALUES(%s,%s,%s)""",
            (order["id"], order["buyer_id"], f"A refund of UGX {amount:,} was reconciled for order {reference}."),
        )
    flash("Refund recorded as reconciled. The refund must already have been completed and verified with the provider.", "ok")
    return redirect(url_for("admin_marketplace"))


@app.post("/admin/marketplace/orders/<reference>/verify")
@need
@admin
def admin_verify_marketplace_payment(reference):
    payment = q(
        """SELECT p.tracking_id FROM marketplace_payment_transactions p
           JOIN marketplace_orders o ON o.id=p.order_id
           WHERE o.reference=%s ORDER BY p.created_at DESC LIMIT 1""",
        (reference,), one=True,
    )
    if not payment:
        flash("No provider transaction is available to verify.", "err")
        return redirect(url_for("admin_marketplace"))
    if not payment["tracking_id"]:
        flash("This provider submission has no tracking ID. Reconcile it in the merchant console before retrying payment.", "err")
        return redirect(url_for("admin_marketplace"))
    try:
        order = payments.settle_marketplace(payment["tracking_id"])
    except Exception:
        log.exception("Admin payment verification failed order=%s", reference)
        flash("Provider verification failed. No payment status was changed.", "err")
        return redirect(url_for("admin_marketplace"))
    flash(
        f"Provider verification returned {order['status']} for {reference}."
        if order else "Provider transaction could not be matched to the order.",
        "ok" if order else "err",
    )
    return redirect(url_for("admin_marketplace"))


@app.post("/admin/marketplace/ledger/<int:entry_id>/payout")
@need
@admin
def admin_ledger_payout(entry_id):
    reference = clean_text(request.form.get("payout_reference", ""), 200)
    if not reference:
        flash("Enter the external payout reference before marking earnings paid.", "err")
        return redirect(url_for("admin_marketplace"))
    changed = ex(
        """UPDATE marketplace_ledger SET payout_status='paid',payout_reference=%s
           WHERE id=%s AND entry_type='seller_proceeds' AND payout_status='pending' AND amount_ugx>0""",
        (reference, entry_id),
    )
    if changed:
        ex(
            """INSERT INTO marketplace_audit_events(actor_id,order_id,event_type,details)
               SELECT %s,order_id,'seller_payout_recorded',%s FROM marketplace_ledger WHERE id=%s""",
            (g.user["id"], Jsonb({"payout_reference": reference}), entry_id),
        )
    flash("Seller payout recorded." if changed else "Pending seller payout entry not found.", "ok" if changed else "err")
    return redirect(url_for("admin_marketplace"))


@app.post("/admin/marketplace/orders/<reference>/fee")
@need
@admin
def admin_payment_fee(reference):
    fee = to_int(request.form.get("fee_ugx"), -1)
    note = clean_text(request.form.get("note", ""), 500)
    if fee < 0 or fee > 10000000:
        flash("Enter a valid non-negative payment fee in UGX.", "err")
        return redirect(url_for("admin_marketplace"))
    with conn() as database:
        order = database.execute(
            "SELECT * FROM marketplace_orders WHERE reference=%s FOR UPDATE",
            (reference,),
        ).fetchone()
        if not order or not order["paid_at"]:
            flash("Only a provider-verified paid order can have payment fees reconciled.", "err")
            return redirect(url_for("admin_marketplace"))
        payment = database.execute(
            """SELECT * FROM marketplace_payment_transactions WHERE order_id=%s
               ORDER BY created_at DESC LIMIT 1 FOR UPDATE""",
            (order["id"],),
        ).fetchone()
        if not payment:
            flash("No provider transaction is available for this order.", "err")
            return redirect(url_for("admin_marketplace"))
        database.execute(
            "UPDATE marketplace_payment_transactions SET fee_ugx=%s WHERE id=%s",
            (fee, payment["id"]),
        )
        database.execute(
            "UPDATE marketplace_orders SET payment_fee_ugx=%s WHERE id=%s",
            (fee, order["id"]),
        )
        if order["status"] == "delivered":
            database.execute(
                """UPDATE marketplace_ledger SET amount_ugx=%s
                   WHERE order_id=%s AND entry_type='payment_fee' AND refund_id IS NULL""",
                (-fee, order["id"]),
            )
        database.execute(
            """INSERT INTO marketplace_audit_events(actor_id,order_id,event_type,details)
               VALUES(%s,%s,'payment_fee_reconciled',%s)""",
            (g.user["id"], order["id"], Jsonb({"fee_ugx": fee, "note": note})),
        )
    flash("Provider payment fee recorded.", "ok")
    return redirect(url_for("admin_marketplace"))


@app.post("/admin/marketplace/orders/<reference>/efris")
@need
@admin
def admin_efris_record(reference):
    efris_reference = clean_text(request.form.get("efris_reference", ""), 200)
    efris_status = request.form.get("efris_status", "")
    if efris_status not in {"pending", "issued", "failed"}:
        flash("Choose a valid EFRIS record status.", "err")
        return redirect(url_for("admin_marketplace"))
    if efris_status == "issued" and not efris_reference:
        flash("An issued EFRIS record requires its external reference.", "err")
        return redirect(url_for("admin_marketplace"))
    with conn() as database:
        order = database.execute(
            "SELECT id FROM marketplace_orders WHERE reference=%s FOR UPDATE",
            (reference,),
        ).fetchone()
        if not order:
            flash("Order not found.", "err")
            return redirect(url_for("admin_marketplace"))
        database.execute(
            "UPDATE marketplace_orders SET efris_reference=%s,efris_status=%s WHERE id=%s",
            (efris_reference, efris_status, order["id"]),
        )
        database.execute(
            """INSERT INTO marketplace_audit_events(actor_id,order_id,event_type,details)
               VALUES(%s,%s,'efris_record_updated',%s)""",
            (g.user["id"], order["id"], Jsonb({"efris_status": efris_status, "reference": efris_reference})),
        )
    flash("EFRIS record reference saved.", "ok")
    return redirect(url_for("admin_marketplace"))


@app.get("/admin/marketplace.csv")
@need
@admin
def admin_marketplace_csv():
    rows = q(
        """SELECT o.reference,o.created_at,o.status,buyer.email AS buyer_email,seller.email AS seller_email,
                  o.listing_title,o.product_subtotal_ugx,o.discount_ugx,o.delivery_fee_ugx,o.tax_ugx,o.total_ugx,
                  coalesce(sum(l.amount_ugx) FILTER (WHERE l.entry_type='platform_commission'),0) AS commission_ugx,
                  o.payment_fee_ugx,o.efris_status,o.efris_reference
           FROM marketplace_orders o JOIN users buyer ON buyer.id=o.buyer_id
           JOIN users seller ON seller.id=o.seller_id
           LEFT JOIN marketplace_ledger l ON l.order_id=o.id
           GROUP BY o.id,buyer.email,seller.email ORDER BY o.created_at DESC"""
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "reference", "created_at", "status", "buyer_email", "seller_email", "listing",
        "product_subtotal_ugx", "discount_ugx", "delivery_fee_ugx", "tax_ugx", "total_ugx",
        "commission_ugx", "payment_fee_ugx", "efris_status", "efris_reference",
    ])
    for row in rows:
        writer.writerow([csv_cell(row[key]) for key in (
            "reference", "created_at", "status", "buyer_email", "seller_email", "listing_title",
            "product_subtotal_ugx", "discount_ugx", "delivery_fee_ugx", "tax_ugx", "total_ugx",
            "commission_ugx", "payment_fee_ugx", "efris_status", "efris_reference",
        )])
    return app.response_class(
        output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=teso-market-sales.csv"},
    )


@app.get("/seller/earnings.csv")
@need
@role_required("seller")
def seller_earnings_csv():
    rows = q(
        """SELECT o.reference,o.created_at,o.status,o.listing_title,l.amount_ugx,l.payout_status,l.payout_reference
           FROM marketplace_ledger l JOIN marketplace_orders o ON o.id=l.order_id
           WHERE l.owner_id=%s AND l.entry_type='seller_proceeds' ORDER BY o.created_at DESC""",
        (g.user["id"],),
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["reference", "created_at", "order_status", "listing", "seller_proceeds_ugx", "payout_status", "payout_reference"])
    for row in rows:
        writer.writerow([csv_cell(row[key]) for key in (
            "reference", "created_at", "status", "listing_title", "amount_ugx", "payout_status", "payout_reference",
        )])
    return app.response_class(
        output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=teso-market-seller-earnings.csv"},
    )


# ---------------- Admin ----------------
def plan_vals(f):
    raw_limit = f.get("listing_limit", "").strip()
    limit = to_int(raw_limit, -1) if raw_limit else None
    d = [f.get("name", "").strip()[:60], f.get("description", "")[:200], to_int(f.get("price_ugx")),
         to_int(f.get("price_usd")) or None, to_int(f.get("duration_days")), limit]
    return d if d[0] and d[2] > 0 and d[4] > 0 and (limit is None or limit > 0) else None

@app.get("/admin")
@need
@admin
def admin_home():
    return render_template("admin.html", plans=q("SELECT * FROM plans ORDER BY id"), users=q("SELECT * FROM users ORDER BY id"),
                           orders=q("SELECT o.*, p.name plan, u.email, (SELECT count(*) FROM payments WHERE order_id=o.id) events FROM orders o JOIN plans p ON p.id=o.plan_id JOIN users u ON u.id=o.user_id ORDER BY o.created_at DESC LIMIT 100"))

@app.post("/admin/plans")
@need
@admin
def admin_plan_add():
    d = plan_vals(request.form)
    if d: ex("INSERT INTO plans(name,description,price_ugx,price_usd,duration_days,listing_limit) VALUES(%s,%s,%s,%s,%s,%s)", d); flash("Plan added.", "ok")
    else: flash("Invalid plan values.", "err")
    return redirect("/admin")

@app.post("/admin/plans/<int:pid>")
@need
@admin
def admin_plan_edit(pid):
    d = plan_vals(request.form)
    if d: ex("UPDATE plans SET name=%s,description=%s,price_ugx=%s,price_usd=%s,duration_days=%s,listing_limit=%s,active=%s WHERE id=%s", (*d, bool(request.form.get("active")), pid)); flash("Plan saved.", "ok")
    else: flash("Invalid plan values.", "err")
    return redirect("/admin")

@app.post("/admin/users/<int:uid>")
@need
@admin
def admin_user(uid):
    if uid != g.user["id"]:
        act = request.form.get("act")
        if act == "toggle": ex("UPDATE users SET disabled = NOT disabled WHERE id=%s", (uid,))
        elif act == "role":
            target = request.form.get("role", "")
            if target in {"customer", "seller", "rider", "admin"}:
                ex("UPDATE users SET role=%s WHERE id=%s", (target, uid))
    return redirect("/admin")


@app.post("/admin/users/<int:uid>/role")
@need
@admin
def admin_user_role(uid):
    role = request.form.get("role", "")
    if uid == g.user["id"] or role not in {"customer", "seller", "rider", "admin"}:
        flash("Choose a valid role for another user.", "err")
    else:
        changed = ex("UPDATE users SET role=%s WHERE id=%s", (role, uid))
        flash("User role updated." if changed else "User not found.", "ok" if changed else "err")
    return redirect("/admin")

@app.errorhandler(CSRFError)
def csrf_err(_error):
    return render_template("error.html", title="Session expired", msg="Please go back, reload the page and try again."), 400

@app.errorhandler(404)
def nf(_error):
    return render_template("error.html", title="404", msg="Page not found."), 404

@app.errorhandler(429)
def rl(_error):
    return render_template("error.html", title="Slow down", msg="Too many attempts. Please wait a few minutes."), 429

@app.errorhandler(Exception)
def boom(e):
    if hasattr(e, "code") and isinstance(e.code, int) and e.code < 500:
        return e
    log.exception("unhandled: %s", e)
    return render_template("error.html", title="Something went wrong", msg="Please try again."), 500

if __name__ == "__main__":
    app.run(port=int(E.get("PORT", 5000)), debug=not PROD)
