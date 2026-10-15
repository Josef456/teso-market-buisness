"""Transactional queueing and SMTP delivery for marketplace order updates."""
from email.message import EmailMessage
from html import escape
import os
import smtplib
import ssl


def enqueue_order_email(database, order_id, user_id, event_key, status, message):
    database.execute(
        """INSERT INTO marketplace_order_email_outbox
           (order_id,user_id,event_key,status,message)
           VALUES(%s,%s,%s,%s,%s) ON CONFLICT(order_id,user_id,event_key) DO NOTHING""",
        (order_id, user_id, event_key, status, message),
    )


def send_order_email(recipient, recipient_name, reference, listing_title, status, message):
    host = os.environ.get("SMTP_HOST", "").strip()
    username = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASS", "")
    sender = os.environ.get("MAIL_FROM", "").strip() or username
    if not all((host, username, password, sender)):
        raise RuntimeError("Configure SMTP_HOST, SMTP_USER, SMTP_PASS and MAIL_FROM for order email delivery.")

    app_url = os.environ.get("APP_URL", "").rstrip("/")
    order_url = f"{app_url}/orders/{reference}" if app_url else ""
    subject = f"Teso Market order {reference}: {status.replace('_', ' ')}"
    text = (
        f"Hello {recipient_name},\n\n{message}\n"
        f"Item: {listing_title}\nOrder: {reference}\nStatus: {status.replace('_', ' ')}\n"
    )
    if order_url:
        text += f"\nView your order: {order_url}\n"

    html_link = (
        f'<p><a href="{escape(order_url, quote=True)}">View order</a></p>'
        if order_url else ""
    )
    html = (
        '<div style="font-family:Arial,sans-serif;max-width:560px;margin:24px auto;'
        'padding:28px;border:1px solid #e6e3d8;border-radius:16px;color:#14211a">'
        '<h1 style="color:#168442">Teso Market</h1>'
        f"<p>Hello {escape(recipient_name)},</p><p>{escape(message)}</p>"
        f"<p><b>Item:</b> {escape(listing_title)}<br>"
        f"<b>Order:</b> {escape(reference)}<br>"
        f"<b>Status:</b> {escape(status.replace('_', ' '))}</p>{html_link}</div>"
    )
    email = EmailMessage()
    email["From"], email["To"], email["Subject"] = sender, recipient, subject
    email.set_content(text)
    email.add_alternative(html, subtype="html")

    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
    except ValueError as error:
        raise ValueError("SMTP_PORT must be a valid port number.") from error
    with smtplib.SMTP(host, port, timeout=15) as smtp:
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
        smtp.login(username, password)
        smtp.send_message(email)
