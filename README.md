# Teso Market (Flask)

Marketplace for shoes, clothes, cars and electronics, with seller listings, product photos, private buyer/seller messages, subscriptions and Pesapal payments (UGX/USD).
Stack: Python 3.12, Flask 3, PostgreSQL (psycopg 3), Gunicorn, server-rendered Jinja templates.

## Files
`app.py` routes · `payments.py` verified settlement · `pesapal.py` gateway client · `db.py` · `migrate.py` + `migrations/` · `templates/` · `static/` · `tests/` · `requirements.txt` · `Procfile` · `render.yaml` · `.env.example`

## Run locally
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
createdb teso_market
cp .env.example .env                                    # fill in SESSION_SECRET, IPN_SECRET, ADMIN_*
python migrate.py                                       # creates tables + first admin
python app.py                                           # http://localhost:5000
```

## Pesapal setup (you must do this)
1. Create sandbox keys at https://developer.pesapal.com → `PESAPAL_CONSUMER_KEY` / `PESAPAL_CONSUMER_SECRET`, `PESAPAL_ENV=sandbox`.
2. Register a live merchant account at pesapal.com and complete their approval; then use the live keys with `PESAPAL_ENV=live`.
3. `APP_URL` must be the public HTTPS URL. Pesapal calls `APP_URL/payment/ipn/<IPN_SECRET>` (registered automatically, or set `PESAPAL_IPN_ID`).
4. Locally Pesapal cannot reach localhost: use a tunnel (`ngrok http 5000`) and set `APP_URL` to it.

Until keys are set, checkout says "Payments are not configured": nothing is faked.

## Payment safety
Prices come from the database. Orders start `pending`; only a gateway status lookup (from the webhook or the return page) can mark them `paid`. Redirect query strings are ignored. Amount/currency must match (else `review`). Settlement is a locked transaction with a unique `subscriptions.order_id`, so duplicate webhooks never extend twice. Every verification is logged in `payments`. No card data is received or stored.

## Deploy on Render
1. Push to GitHub → Render → New → Blueprint (uses `render.yaml`), or create a Web Service manually: build `pip install -r requirements.txt`, start `python migrate.py && gunicorn app:app --workers 2 --bind 0.0.0.0:$PORT`.
2. Set `APP_URL`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`, Pesapal keys. `SESSION_SECRET`/`IPN_SECRET` are generated.
3. **Persistence:** the blueprint uses Render's free Postgres, which expires after ~30 days. Use a paid database plan for real customers. Sessions are signed cookies, so restarts don't log anyone out.
4. Domain: Service → Settings → Custom Domains, add DNS records Render shows, then update `APP_URL`. HTTPS is automatic.

## Tests
`TEST_DATABASE_URL=postgresql://user:pass@localhost/teso_test pytest` (throw-away DB). Covers register/login/CSRF, duplicate webhooks, amount mismatch, redirect-only payments, renewal and expiry. The gateway is stubbed; also do a real sandbox payment.

For Windows, copy `.env.test.example` to the ignored `.env.test` file, then run `.\.venv\Scripts\python.exe -m pytest tests\test_payments.py -q`. The test module loads `.env.test` automatically and runs migrations against that database. Never point `TEST_DATABASE_URL` at development or production data. The local test server listens only on `127.0.0.1:5433`; after a reboot, start it with `& 'C:\Program Files\PostgreSQL\16\bin\pg_ctl.exe' -D "$env:LOCALAPPDATA\teso-market-test-postgres" -o '-h 127.0.0.1 -p 5433' -l "$env:LOCALAPPDATA\teso-market-test-postgres\server.log" start`.

The integration suite checks Gmail SMTP delivery, listing creation and photo validation, category browsing, private buyer/seller conversations and read status, in addition to the payment scenarios below.

## Gmail password-reset email
Set `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER` to the full Gmail/Workspace address, and `SMTP_PASS` to a Google App Password (not the account password). In Google Account security, enable 2-Step Verification and create an App Password for this app. Set `MAIL_FROM` to the same Gmail address, or leave it blank to use `SMTP_USER`. Keep these values in the ignored local `.env`; on Render, add them as environment variables. Restart the app after changing local environment values. Password-reset links are never written to application logs.

## Marketplace
Anyone can browse and search active shoe, clothes, car and electronics listings. Signed-in sellers can post listings with UGX/USD prices, descriptions, locations and an optional JPG, PNG or WebP photo (maximum 5 MB), and can mark listings sold or archive them. Signed-in buyers can privately message a seller from a listing; replies, unread counts and threads are available in Messages. Photos currently live under `static/uploads/` on the server and are excluded from source control. Local-disk photo storage is temporary on hosts with ephemeral disks such as Render; configure durable image/object storage before deploying there.

Publishing and keeping listings visible requires an active seller subscription. The Starter plan allows 50 active listings; plans with a blank listing limit are unlimited. Sold and archived items do not use an active listing slot. Configure plan limits from Admin → Plans.

## Notes
Renewal is manual (time is added after current expiry). Rate-limit storage is per worker (in memory). Password-reset emails require working SMTP settings. Seed plans are placeholders; edit them in /admin.
