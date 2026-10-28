"""PostgreSQL persistence, retaining the domain repository contract.

All durable state lives here. The small SQL adapter supports the deliberately
limited SQL vocabulary used by the domain and HTTP repositories.
"""
from __future__ import annotations

import hashlib
import json
import re


class Row(dict):
    def __getitem__(self, key):
        return tuple(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


def row_factory(cursor):
    names = [column.name for column in (cursor.description or [])]
    return lambda values: Row(zip(names, values))


class Connection:
    def __init__(self, url):
        import psycopg
        self.raw = psycopg.connect(url, autocommit=True, row_factory=row_factory, connect_timeout=5)
        self.raw.execute("SET statement_timeout='10s'")
        self.raw.execute("SET lock_timeout='5s'")

    def execute(self, sql, params=()):
        sql = sql.replace("?", "%s")
        sql = re.sub(r"\browid\b", "sequence", sql)
        if "INSERT OR IGNORE INTO" in sql:
            sql = sql.replace("INSERT OR IGNORE INTO", "INSERT INTO") + " ON CONFLICT DO NOTHING"
        return self.raw.execute(sql, params)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


class PostgresRepository:
    dialect = "postgres"

    def __init__(self, url):
        self.path = str(url)
        self.conn = Connection(url)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
            user_id TEXT, session_id TEXT, version INTEGER NOT NULL, body TEXT NOT NULL,
            PRIMARY KEY(user_id,session_id))""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS turns (
            user_id TEXT, session_id TEXT, request_id TEXT, fingerprint TEXT NOT NULL, result TEXT NOT NULL,
            PRIMARY KEY(user_id,session_id,request_id))""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS demo_orders (
            user_id TEXT, session_id TEXT, action_id TEXT, local_id TEXT UNIQUE, body TEXT NOT NULL,
            PRIMARY KEY(user_id,session_id,action_id))""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,user_id TEXT,session_id TEXT,body TEXT NOT NULL)""")

    def session(self, user, session):
        row = self.conn.execute("SELECT body FROM sessions WHERE user_id=? AND session_id=?", (user, session)).fetchone()
        return json.loads(row[0]) if row else None

    def turn(self, user, session, request_id):
        row = self.conn.execute("SELECT fingerprint,result FROM turns WHERE user_id=? AND session_id=? AND request_id=?", (user, session, request_id)).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def order(self, user, session, action_id):
        row = self.conn.execute("SELECT body FROM demo_orders WHERE user_id=? AND session_id=? AND action_id=?", (user, session, action_id)).fetchone()
        return json.loads(row[0]) if row else None

    def commit(self, turn, digest, state, result, events=()):
        # An advisory transaction lock also protects the absent-row case; SELECT
        # FOR UPDATE alone cannot serialize two first turns for a new session.
        lock = int.from_bytes(hashlib.sha256(json.dumps([turn.user_id, turn.session_id]).encode()).digest()[:8], "big", signed=True)
        with self.conn.raw.transaction():
            self.conn.execute("SELECT pg_advisory_xact_lock(?)", (lock,))
            prior = self.turn(turn.user_id, turn.session_id, turn.request_id)
            if prior:
                return ("repeat" if prior[0] == digest else "request_id_conflict", prior[1])
            row = self.conn.execute("SELECT version FROM sessions WHERE user_id=? AND session_id=?", (turn.user_id, turn.session_id)).fetchone()
            if (row[0] if row else 0) != turn.expected_state_version:
                return "state_conflict", None
            self.conn.execute("INSERT INTO sessions VALUES(?,?,?,?) ON CONFLICT(user_id,session_id) DO UPDATE SET version=excluded.version,body=excluded.body", (turn.user_id, turn.session_id, state["state_version"], json.dumps(state, ensure_ascii=False)))
            order = result.get("demo_order") if result["status"] == "completed_demo" else None
            if order:
                self.conn.execute("INSERT INTO demo_orders VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING", (turn.user_id, turn.session_id, order["action_id"], order["local_id"], json.dumps(order, ensure_ascii=False)))
                if self.order(turn.user_id, turn.session_id, order["action_id"]) != order:
                    raise RuntimeError("Заказ уже существует с другим составом")
            self.conn.execute("INSERT INTO turns VALUES(?,?,?,?,?)", (turn.user_id, turn.session_id, turn.request_id, digest, json.dumps(result, ensure_ascii=False)))
            for event in events:
                self.conn.execute("INSERT INTO events VALUES(?,?,?,?)", (event["event_id"], turn.user_id, turn.session_id, json.dumps(event, ensure_ascii=False)))
        return "committed", result

    def close(self):
        self.conn.close()
