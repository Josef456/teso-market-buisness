from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from psycopg.types.json import Jsonb
from db import conn
from payment_providers import provider_for


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _apply(c, ref, tid, st):
    tracker = (tid or "").strip()
    ref = str(ref or "").strip()
    o = c.execute("SELECT * FROM orders WHERE id::text=%s FOR UPDATE", (ref,)).fetchone() if ref else None
    if not o:
        o = c.execute("SELECT * FROM orders WHERE tracking_id=%s ORDER BY created_at DESC LIMIT 1 FOR UPDATE", (tracker,)).fetchone() if tracker else None
    if not o or o["tracking_id"] != tracker:
        return None
    c.execute("INSERT INTO payments(order_id,tracking_id,status,confirmation_code,raw) VALUES(%s,%s,%s,%s,%s)",
              (o["id"], tracker, st.get("payment_status_description") or "unknown", st.get("confirmation_code"), Jsonb(st)))
    if o["status"] == "paid":  # idempotent: duplicate webhooks never extend twice
        return o
    code, nxt = _safe_int(st.get("status_code"), 0), o["status"]
    if code == 1:
        ok = _safe_float(st.get("amount"), 0.0) == _safe_float(o["amount"], 0.0) and str(st.get("currency") or "") == str(o["currency"])
        nxt = "paid" if ok else "review"
    elif code in (2, 3):
        nxt = "failed"
    if nxt == "paid":
        days = c.execute("SELECT duration_days FROM plans WHERE id=%s", (o["plan_id"],)).fetchone()["duration_days"]
        c.execute("""INSERT INTO subscriptions(user_id,plan_id,order_id,starts_at,ends_at)
            SELECT %(u)s,%(p)s,%(o)s,s,s+make_interval(days=>%(d)s)
            FROM (SELECT greatest(now(),coalesce((SELECT max(ends_at) FROM subscriptions WHERE user_id=%(u)s),now())) s) x
            ON CONFLICT (order_id) DO NOTHING""", {"u": o["user_id"], "p": o["plan_id"], "o": o["id"], "d": days})
        c.execute("UPDATE users SET role='seller' WHERE id=%s AND role='customer'", (o["user_id"],))
        c.execute("UPDATE orders SET status='paid', paid_at=now() WHERE id=%s", (o["id"],))
    elif nxt != o["status"]:
        c.execute("UPDATE orders SET status=%s WHERE id=%s", (nxt, o["id"]))
    return {**o, "status": nxt}

def settle(tracking_id):
    """Ask the gateway (never trust redirects/query strings), then settle in one locked transaction."""
    tracker = (tracking_id or "").strip()
    if not tracker:
        return None
    st = provider_for("pesapal").verify_transaction(tracker)
    with conn() as c:  # commits on success, rolls back on error
        return _apply(c, st.get("merchant_reference"), tracker, st)


def settle_marketplace(tracking_id):
    """Verify a marketplace payment with the provider before changing order state."""
    tracker = (tracking_id or "").strip()
    if not tracker:
        return None
    provider = provider_for("pesapal")
    status = provider.verify_transaction(tracker)
    with conn() as database:
        payment = database.execute(
            """SELECT p.*,o.status AS order_status,o.total_ugx,o.currency,o.reference,o.buyer_id,o.seller_id,
                      o.id AS marketplace_order_id
               FROM marketplace_payment_transactions p
               JOIN marketplace_orders o ON o.id=p.order_id
               WHERE p.provider=%s AND p.tracking_id=%s FOR UPDATE OF p,o""",
            (provider.name, tracker),
        ).fetchone()
        if not payment:
            return None
        if payment["order_status"] not in ("pending_payment", "payment_review"):
            return {**payment, "status": payment["order_status"]}

        code = _safe_int(status.get("status_code"))
        transaction_ref = str(status.get("confirmation_code") or "").strip()[:200] or None
        reported_amount = status.get("amount")
        try:
            amount_matches = (reported_amount is not None
                              and Decimal(str(reported_amount)) == Decimal(payment["total_ugx"]))
        except (InvalidOperation, TypeError, ValueError):
            amount_matches = False
        currency_matches = str(status.get("currency") or "").upper() == payment["currency"]
        reference_matches = str(status.get("merchant_reference") or "") == payment["merchant_reference"]
        next_status = payment["order_status"]
        reconciliation = "pending"
        transaction_status = "pending"
        if code == 1:
            if amount_matches and currency_matches and reference_matches:
                next_status, reconciliation, transaction_status = "paid", "matched", "completed"
            else:
                next_status, reconciliation, transaction_status = "payment_review", "review", "review"
        elif code == 2:
            next_status, reconciliation, transaction_status = "pending_payment", "matched", "failed"
        elif code == 3:
            next_status, reconciliation, transaction_status = "pending_payment", "matched", "reversed"

        database.execute(
            """UPDATE marketplace_payment_transactions
               SET transaction_reference=%s,status=%s,reconciliation_status=%s,raw=%s,verified_at=now()
               WHERE id=%s""",
            (
                transaction_ref,
                transaction_status,
                reconciliation,
                Jsonb(status),
                payment["id"],
            ),
        )
        order_changed = next_status != payment["order_status"]
        attempt_changed = transaction_status != payment["status"]
        if order_changed:
            database.execute(
                """UPDATE marketplace_orders SET status=%s,paid_at=CASE WHEN %s='paid' THEN now() ELSE paid_at END,
                   updated_at=now() WHERE id=%s""",
                (next_status, next_status, payment["marketplace_order_id"]),
            )
            database.execute(
                """INSERT INTO marketplace_order_status_history(order_id,old_status,new_status,note)
                   VALUES(%s,%s,%s,%s)""",
                (payment["marketplace_order_id"], payment["order_status"], next_status, "Provider status verification"),
            )
            database.execute(
                """INSERT INTO marketplace_order_notifications(order_id,user_id,message)
                   VALUES(%s,%s,%s),(%s,%s,%s)""",
                (
                    payment["marketplace_order_id"], payment["buyer_id"],
                    f"Order {payment['reference']} payment/order status is now {next_status.replace('_',' ')}.",
                    payment["marketplace_order_id"], payment["seller_id"],
                    f"Order {payment['reference']} payment/order status is now {next_status.replace('_',' ')}.",
                ),
            )
        elif attempt_changed and transaction_status in {"failed", "reversed"}:
            database.execute(
                """INSERT INTO marketplace_order_notifications(order_id,user_id,message)
                   VALUES(%s,%s,%s)""",
                (
                    payment["marketplace_order_id"], payment["buyer_id"],
                    f"Payment for order {payment['reference']} was not completed; you may retry.",
                ),
            )
        if order_changed or attempt_changed:
            database.execute(
                """INSERT INTO marketplace_audit_events(order_id,event_type,details)
                   VALUES(%s,'payment_verified',%s)""",
                (
                    payment["marketplace_order_id"],
                    Jsonb({"provider": provider.name, "status_code": code, "reconciliation": reconciliation}),
                ),
            )
        return {**payment, "status": next_status}


def settle_tracking(tracking_id):
    """Route verification to the legacy subscription or marketplace order model."""
    tracker = (tracking_id or "").strip()
    if not tracker:
        return None
    with conn() as database:
        marketplace_payment = database.execute(
            "SELECT 1 FROM marketplace_payment_transactions WHERE tracking_id=%s",
            (tracker,),
        ).fetchone()
    if marketplace_payment:
        return settle_marketplace(tracker)
    return settle(tracker)


def sub_state(s):
    if not s:
        return "none"
    return "active" if s["ends_at"] > datetime.now(timezone.utc) else "expired"
