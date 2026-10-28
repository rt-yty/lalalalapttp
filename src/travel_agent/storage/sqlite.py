"""Domain transactions. The domain DB is authoritative, not the execution graph."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()).hexdigest()


class Repository:
    def __init__(self, path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript('''
        CREATE TABLE IF NOT EXISTS sessions (
          user_id TEXT, session_id TEXT, version INTEGER NOT NULL, body TEXT NOT NULL,
          PRIMARY KEY(user_id, session_id));
        CREATE TABLE IF NOT EXISTS turns (
          user_id TEXT, session_id TEXT, request_id TEXT, fingerprint TEXT NOT NULL, result TEXT NOT NULL,
          PRIMARY KEY(user_id, session_id, request_id));
        CREATE TABLE IF NOT EXISTS demo_orders (
          user_id TEXT, session_id TEXT, action_id TEXT, local_id TEXT UNIQUE, body TEXT NOT NULL,
          PRIMARY KEY(user_id, session_id, action_id));
        CREATE TABLE IF NOT EXISTS events (
          event_id TEXT PRIMARY KEY, user_id TEXT, session_id TEXT, body TEXT NOT NULL);
        ''')

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
        """CAS, result receipt, order and consumed action persist together."""
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            prior = self.turn(turn.user_id, turn.session_id, turn.request_id)
            if prior:
                c.rollback()
                return ("repeat" if prior[0] == digest else "request_id_conflict", prior[1])
            row = c.execute("SELECT version FROM sessions WHERE user_id=? AND session_id=?", (turn.user_id, turn.session_id)).fetchone()
            actual = row[0] if row else 0
            if actual != turn.expected_state_version:
                c.rollback()
                return "state_conflict", None
            c.execute("INSERT INTO sessions VALUES(?,?,?,?) ON CONFLICT(user_id,session_id) DO UPDATE SET version=excluded.version,body=excluded.body", (turn.user_id, turn.session_id, state["state_version"], json.dumps(state, ensure_ascii=False)))
            order = result.get("demo_order") if result["status"] == "completed_demo" else None
            if order:
                c.execute("INSERT OR IGNORE INTO demo_orders VALUES(?,?,?,?,?)", (turn.user_id, turn.session_id, order["action_id"], order["local_id"], json.dumps(order, ensure_ascii=False)))
                saved = self.order(turn.user_id, turn.session_id, order["action_id"])
                if saved != order:
                    raise RuntimeError("Заказ с таким действием уже существует с другим составом")
            c.execute("INSERT INTO turns VALUES(?,?,?,?,?)", (turn.user_id, turn.session_id, turn.request_id, digest, json.dumps(result, ensure_ascii=False)))
            for event in events:
                c.execute("INSERT INTO events VALUES(?,?,?,?)", (event["event_id"], turn.user_id, turn.session_id, json.dumps(event, ensure_ascii=False)))
            c.commit()
            return "committed", result
        except BaseException:
            c.rollback()
            raise

    def close(self):
        self.conn.close()
