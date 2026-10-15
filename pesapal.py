"""Pesapal API v3 client. The only module that knows about the gateway."""
import os, time
import requests

_tok = {"v": None, "exp": 0.0}
_ipn = {"id": None}

def _base():
    return "https://pay.pesapal.com/v3" if os.environ.get("PESAPAL_ENV") == "live" else "https://cybqa.pesapal.com/pesapalv3"

def configured():
    return bool(os.environ.get("PESAPAL_CONSUMER_KEY") and os.environ.get("PESAPAL_CONSUMER_SECRET"))

def _call(path, method="GET", body=None, auth=True):
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    if auth:
        h["Authorization"] = "Bearer " + _token()
    r = requests.request(method, _base() + path, json=body, headers=h, timeout=20)
    try:
        j = r.json()
    except ValueError:
        j = {}
    if not r.ok or j.get("error"):
        raise RuntimeError(f"Pesapal {path} failed: {r.status_code} {j.get('error') or j}")
    return j

def _token():
    if _tok["v"] and time.time() < _tok["exp"]:
        return _tok["v"]
    if not configured():
        raise RuntimeError("Pesapal credentials not configured")
    j = _call("/api/Auth/RequestToken", "POST", {"consumer_key": os.environ["PESAPAL_CONSUMER_KEY"], "consumer_secret": os.environ["PESAPAL_CONSUMER_SECRET"]}, auth=False)
    _tok.update(v=j["token"], exp=time.time() + 240)
    return _tok["v"]

def _ipn_id():
    if os.environ.get("PESAPAL_IPN_ID"):
        return os.environ["PESAPAL_IPN_ID"]
    if not _ipn["id"]:
        url = f"{os.environ['APP_URL']}/payment/ipn/{os.environ['IPN_SECRET']}"
        _ipn["id"] = _call("/api/URLSetup/RegisterIPN", "POST", {"url": url, "ipn_notification_type": "GET"})["ipn_id"]
    return _ipn["id"]

def submit_order(o):
    return _call("/api/Transactions/SubmitOrderRequest", "POST", {
        "id": str(o["id"]), "currency": o["currency"], "amount": float(o["amount"]), "description": o["description"][:100],
        "callback_url": os.environ["APP_URL"] + "/payment/return", "notification_id": _ipn_id(),
        "billing_address": {"email_address": o["email"], "phone_number": o.get("phone") or "", "first_name": o["name"]}})

def get_status(tracking_id):
    """Authoritative status. status_code: 1 completed, 2 failed, 3 reversed, 0 invalid/pending."""
    return _call("/api/Transactions/GetTransactionStatus?orderTrackingId=" + requests.utils.quote(tracking_id, safe=""))
