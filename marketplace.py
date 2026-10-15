"""Marketplace money calculations shared by checkout, settlement, and reporting."""
from decimal import Decimal, ROUND_HALF_UP


def ugx_amount(value):
    return int(Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def percentage_amount(base, rate_percent):
    return ugx_amount(Decimal(base) * Decimal(rate_percent) / Decimal("100"))


def calculate_totals(subtotal, delivery_fee, commission_rate, tax_rate,
                     tax_applies_to_delivery=False, discount=0):
    subtotal = int(subtotal)
    discount = int(discount)
    delivery_fee = int(delivery_fee)
    if min(subtotal, discount, delivery_fee) < 0 or discount > subtotal:
        raise ValueError("Order amounts must be non-negative and discount cannot exceed subtotal.")

    taxable_base = subtotal - discount
    if tax_applies_to_delivery:
        taxable_base += delivery_fee
    tax = percentage_amount(taxable_base, tax_rate)
    commission = percentage_amount(subtotal - discount, commission_rate)
    return {
        "product_subtotal_ugx": subtotal,
        "discount_ugx": discount,
        "delivery_fee_ugx": delivery_fee,
        "tax_ugx": tax,
        "commission_ugx": commission,
        "total_ugx": subtotal - discount + delivery_fee + tax,
        "seller_proceeds_ugx": subtotal - discount - commission,
    }


def record_completion_ledger(database, order):
    """Create immutable order allocations once the order is delivered."""
    totals = calculate_totals(
        order["product_subtotal_ugx"], order["delivery_fee_ugx"],
        order["commission_rate_percent"], order["tax_rate_percent"],
        order["tax_applies_to_delivery"], order["discount_ugx"],
    )
    entries = (
        ("seller_proceeds", order["seller_id"], totals["seller_proceeds_ugx"], "pending"),
        ("platform_commission", None, totals["commission_ugx"], "not_applicable"),
        ("delivery_pass_through", None, order["delivery_fee_ugx"], "not_applicable"),
        ("payment_fee", None, -order["payment_fee_ugx"], "not_applicable"),
        ("tax", None, totals["tax_ugx"], "not_applicable"),
    )
    for entry_type, owner_id, amount, payout_status in entries:
        database.execute(
            """INSERT INTO marketplace_ledger(order_id,entry_type,owner_id,amount_ugx,payout_status)
               VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (order["id"], entry_type, owner_id, amount, payout_status),
        )


def record_refund_ledger(database, order, refund_id, refund_amount):
    """Reverse order allocations proportionally for an administrator-confirmed refund."""
    refunded_before = database.execute(
        "SELECT coalesce(sum(amount_ugx),0) AS amount FROM marketplace_refunds WHERE order_id=%s AND id<>%s",
        (order["id"], refund_id),
    ).fetchone()["amount"]
    total = order["total_ugx"]
    completed = database.execute(
        """SELECT 1 FROM marketplace_ledger WHERE order_id=%s
           AND entry_type='seller_proceeds' AND refund_id IS NULL""",
        (order["id"],),
    ).fetchone()
    if not completed:
        database.execute(
            """INSERT INTO marketplace_ledger(order_id,refund_id,entry_type,amount_ugx)
               VALUES(%s,%s,'refund',%s) ON CONFLICT DO NOTHING""",
            (order["id"], refund_id, -refund_amount),
        )
        return
    allocation_types = {
        "seller_proceeds": (order["seller_id"], order["product_subtotal_ugx"] - order["discount_ugx"]
                            - percentage_amount(order["product_subtotal_ugx"] - order["discount_ugx"],
                                                order["commission_rate_percent"]), "pending"),
        "platform_commission": (None, percentage_amount(
            order["product_subtotal_ugx"] - order["discount_ugx"], order["commission_rate_percent"]),
            "not_applicable"),
        "delivery_pass_through": (None, order["delivery_fee_ugx"], "not_applicable"),
        "tax": (None, order["tax_ugx"], "not_applicable"),
    }
    for entry_type, (owner_id, allocation, payout_status) in allocation_types.items():
        cumulative = ugx_amount(Decimal(refunded_before + refund_amount) * allocation / total)
        previous = ugx_amount(Decimal(refunded_before) * allocation / total)
        database.execute(
            """INSERT INTO marketplace_ledger
               (order_id,refund_id,entry_type,owner_id,amount_ugx,payout_status)
               VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (order["id"], refund_id, entry_type, owner_id, -(cumulative - previous), payout_status),
        )
    database.execute(
        """INSERT INTO marketplace_ledger(order_id,refund_id,entry_type,amount_ugx)
           VALUES(%s,%s,'refund',%s) ON CONFLICT DO NOTHING""",
        (order["id"], refund_id, -refund_amount),
    )
