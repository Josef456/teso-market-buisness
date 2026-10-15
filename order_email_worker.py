"""Deliver queued order emails; run as a separate background worker."""
import logging
import smtplib
import time

from dotenv import load_dotenv

from db import conn
from order_emails import send_order_email


logging.basicConfig(level=logging.INFO)
log = logging.getLogger("teso.order_email_worker")


def process_one_email(outbox_id=None):
    with conn() as database:
        email = database.execute(
            """SELECT e.*,u.email AS recipient_email,u.name AS recipient_name,
                      o.reference,o.listing_title
               FROM marketplace_order_email_outbox e
               JOIN users u ON u.id=e.user_id
               JOIN marketplace_orders o ON o.id=e.order_id
               WHERE e.sent_at IS NULL AND e.next_attempt_at<=now()
                 AND (%s::bigint IS NULL OR e.id=%s)
               ORDER BY e.id
               LIMIT 1 FOR UPDATE OF e SKIP LOCKED""",
            (outbox_id, outbox_id),
        ).fetchone()
        if not email:
            return False

        try:
            send_order_email(
                email["recipient_email"], email["recipient_name"], email["reference"],
                email["listing_title"], email["status"], email["message"],
            )
        except (OSError, ValueError, smtplib.SMTPException) as error:
            attempts = email["attempts"] + 1
            delay_seconds = min(21600, 30 * 2 ** min(email["attempts"], 10))
            database.execute(
                """UPDATE marketplace_order_email_outbox
                   SET attempts=%s,next_attempt_at=now()+(%s * interval '1 second'),
                       last_error=%s WHERE id=%s""",
                (attempts, delay_seconds, type(error).__name__, email["id"]),
            )
            log.warning(
                "Order email delivery failed; queued for retry (outbox_id=%s error=%s).",
                email["id"], type(error).__name__,
            )
            return True

        database.execute(
            """UPDATE marketplace_order_email_outbox
               SET attempts=attempts+1,sent_at=now(),last_error=NULL WHERE id=%s""",
            (email["id"],),
        )
        log.info("Order email delivered (outbox_id=%s).", email["id"])
        return True


def run():
    load_dotenv()
    while True:
        if not process_one_email():
            time.sleep(5)


if __name__ == "__main__":
    run()
