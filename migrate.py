"""Applies SQL migrations and creates the first admin. Safe to run on every start."""
import glob, os
import psycopg
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash
load_dotenv()

def run():
    with psycopg.connect(os.environ["DATABASE_URL"]) as c:
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations(name text PRIMARY KEY, applied_at timestamptz DEFAULT now())")
        done = {r[0] for r in c.execute("SELECT name FROM schema_migrations")}
        for f in sorted(glob.glob(os.path.join(os.path.dirname(__file__), "migrations", "*.sql"))):
            name = os.path.basename(f)
            if name in done:
                continue
            c.execute(open(f, encoding="utf8").read())
            c.execute("INSERT INTO schema_migrations(name) VALUES(%s)", (name,))
            print("migrated", name)
        email, pw = os.environ.get("ADMIN_EMAIL"), os.environ.get("ADMIN_PASSWORD")
        if email and pw:
            c.execute("INSERT INTO users(email,name,password_hash,role) VALUES(%s,'Administrator',%s,'admin') ON CONFLICT (email) DO NOTHING",
                      (email.lower(), generate_password_hash(pw)))

if __name__ == "__main__":
    run()
