import os
import psycopg
from flask import g
from psycopg.rows import dict_row


def conn(autocommit=False):
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL must be set before using the database.")
    return psycopg.connect(dsn, row_factory=dict_row, autocommit=autocommit, connect_timeout=10)

def _db():
    if "db" not in g:
        g.db = conn(autocommit=True)
    return g.db

def q(sql, params=(), one=False):
    cur = _db().execute(sql, params)
    rows = cur.fetchall() if cur.description else []
    return (rows[0] if rows else None) if one else rows

def ex(sql, params=()):
    return _db().execute(sql, params).rowcount

def init_app(app):
    @app.teardown_appcontext
    def close(_):
        c = g.pop("db", None)
        if c is not None:
            c.close()
