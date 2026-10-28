"""Password accounts and opaque, revocable browser sessions.

Guest data is migrated transactionally after authentication. The former guest
identity is retired, so possession of its cookie never grants account access.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
import secrets
import time
from uuid import uuid4

from .storage.sqlite import fingerprint

AUTH_COOKIE = "travel_account"
SESSION_SECONDS = 30 * 24 * 60 * 60
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**17, 8, 1
DUMMY_HASH = "scrypt$131072$8$1$df13191ef1f9c3be1cb71b81d0541f45$" + "00" * 32


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                            p=SCRYPT_P, dklen=32, maxmem=256 * 1024 * 1024)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt" or (int(n), int(r), int(p)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt),
                                n=int(n), r=int(r), p=int(p), dklen=32, maxmem=256 * 1024 * 1024)
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError, UnicodeError):
        return False


class AuthConflict(Exception):
    pass


class AuthRateLimit(Exception):
    def __init__(self, retry_after):
        self.retry_after = max(1, int(retry_after))


class AuthStore:
    def __init__(self, repository, *, clock=time.time):
        self.repo, self.db, self.clock = repository, repository.conn, clock
        self.db.execute("""CREATE TABLE IF NOT EXISTS accounts (
            account_id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL, name TEXT NOT NULL, created_at BIGINT NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS auth_sessions (
            token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL,
            created_at BIGINT NOT NULL, expires_at BIGINT NOT NULL)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS auth_session_account ON auth_sessions(account_id)")
        self.db.execute("CREATE INDEX IF NOT EXISTS auth_session_expiry ON auth_sessions(expires_at)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS auth_rate_limits (
            bucket TEXT PRIMARY KEY, attempts INTEGER NOT NULL, started_at BIGINT NOT NULL)""")
        self.db.execute("CREATE TABLE IF NOT EXISTS retired_guests (guest_id TEXT PRIMARY KEY, retired_at BIGINT NOT NULL)")
        self.db.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (int(self.clock()),))
        self.db.execute("DELETE FROM auth_rate_limits WHERE started_at<?", (int(self.clock()) - 86400,))

    @contextmanager
    def transaction(self):
        if getattr(self.repo, "dialect", "sqlite") == "postgres":
            with self.db.raw.transaction():
                yield
        else:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    @staticmethod
    def public_user(row):
        return {"id": row["account_id"], "email": row["email"], "name": row["name"], "email_verified": False}

    def account_by_email(self, email):
        return self.db.execute("SELECT * FROM accounts WHERE email=?", (email,)).fetchone()

    def session(self, token):
        if not isinstance(token, str) or len(token) != 64 or any(char not in "0123456789abcdef" for char in token):
            return None
        row = self.db.execute("""SELECT accounts.*,auth_sessions.expires_at FROM auth_sessions
            JOIN accounts ON accounts.account_id=auth_sessions.account_id
            WHERE token_hash=? AND expires_at>?""", (hashlib.sha256(token.encode()).hexdigest(), int(self.clock()))).fetchone()
        return self.public_user(row) if row else None

    def revoke(self, token):
        if token:
            self.db.execute("DELETE FROM auth_sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def guest_retired(self, guest):
        return self.db.execute("SELECT 1 FROM retired_guests WHERE guest_id=?", (guest,)).fetchone() is not None

    def rate_limit(self, key, *, limit, seconds):
        # Only HMAC/hash keys enter this table; raw addresses/IPs are not retained.
        now = int(self.clock())
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO auth_rate_limits VALUES(?,?,?)", (key, 0, now))
            row = self.db.execute("SELECT attempts,started_at FROM auth_rate_limits WHERE bucket=?", (key,)).fetchone()
            if now - row["started_at"] >= seconds:
                self.db.execute("UPDATE auth_rate_limits SET attempts=1,started_at=? WHERE bucket=?", (now, key))
            elif row["attempts"] >= limit:
                raise AuthRateLimit(seconds - (now - row["started_at"]))
            else:
                self.db.execute("UPDATE auth_rate_limits SET attempts=attempts+1 WHERE bucket=?", (key,))

    def issue(self, account_id, *, guest=None, old_token=None, signup=None):
        now, token = int(self.clock()), secrets.token_hex(32)
        with self.transaction():
            if signup:
                if self.account_by_email(signup["email"]):
                    raise AuthConflict("Этот адрес уже зарегистрирован. Войдите в аккаунт.")
                self.db.execute("INSERT INTO accounts VALUES(?,?,?,?,?)", (account_id, signup["email"], signup["password_hash"], signup["name"], now))
            migrated = self._migrate_guest(guest, account_id) if guest else {"moved_sessions": 0, "session_id_map": {}}
            self.revoke(old_token)
            self.db.execute("INSERT INTO auth_sessions VALUES(?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), account_id, now, now + SESSION_SECONDS))
            row = self.db.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        return token, self.public_user(row), migrated

    def _migrate_guest(self, guest, account_id):
        if self.guest_retired(guest):
            raise AuthConflict("Гостевой сеанс уже изменился. Обновите страницу и войдите снова.")
        if self.db.execute("SELECT 1 FROM web_jobs WHERE user_id=? AND status='running'", (guest,)).fetchone():
            raise AuthConflict("Дождитесь ответа по текущей поездке, затем войдите в аккаунт.")
        ids = {row[0] for row in self.db.execute("SELECT session_id FROM sessions WHERE user_id=? UNION SELECT session_id FROM web_jobs WHERE user_id=?", (guest, guest)).fetchall()}
        mapping = {}
        for original_id in ids:
            collision = self.db.execute("SELECT 1 FROM sessions WHERE user_id=? AND session_id=? UNION SELECT 1 FROM web_jobs WHERE user_id=? AND session_id=?", (account_id, original_id, account_id, original_id)).fetchone()
            target_id = uuid4().hex if collision else original_id
            mapping[original_id] = target_id
            state_row = self.db.execute("SELECT body FROM sessions WHERE user_id=? AND session_id=?", (guest, original_id)).fetchone()
            if state_row:
                state = json.loads(state_row[0])
                state.update(user_id=account_id, session_id=target_id)
                self.db.execute("UPDATE sessions SET user_id=?,session_id=?,body=? WHERE user_id=? AND session_id=?", (account_id, target_id, json.dumps(state, ensure_ascii=False), guest, original_id))
            jobs = self.db.execute("SELECT * FROM web_jobs WHERE user_id=? AND session_id=?", (guest, original_id)).fetchall()
            for job in jobs:
                turn = json.loads(job["turn_json"])
                turn.update(user_id=account_id, session_id=target_id)
                result = json.loads(job["result"]) if job["result"] else None
                if result:
                    result["session_id"] = target_id
                body = {key: turn[key] for key in ("session_id", "request_id", "expected_state_version", "input", "payload")}
                self.db.execute("UPDATE web_jobs SET user_id=?,session_id=?,turn_json=?,body_hash=?,result=? WHERE job_id=?", (account_id, target_id, json.dumps(turn, ensure_ascii=False), fingerprint(body), json.dumps(result, ensure_ascii=False) if result else None, job["job_id"]))
                # Re-key only the identity envelope. Action/plan hashes, prices
                # and request IDs are unchanged, so retries remain idempotent.
                receipt = self.repo.turn(guest, original_id, job["request_id"])
                if receipt:
                    receipt_result = receipt[1]
                    receipt_result["session_id"] = target_id
                    self.db.execute("UPDATE turns SET user_id=?,session_id=?,fingerprint=?,result=? WHERE user_id=? AND session_id=? AND request_id=?", (account_id, target_id, fingerprint(turn), json.dumps(receipt_result, ensure_ascii=False), guest, original_id, job["request_id"]))
            self.db.execute("UPDATE demo_orders SET user_id=?,session_id=? WHERE user_id=? AND session_id=?", (account_id, target_id, guest, original_id))
            self.db.execute("UPDATE events SET user_id=?,session_id=? WHERE user_id=? AND session_id=?", (account_id, target_id, guest, original_id))
        guest_profile = self.db.execute("SELECT body FROM profiles WHERE user_id=?", (guest,)).fetchone()
        omitted = 0
        if guest_profile:
            account_profile = self.db.execute("SELECT body FROM profiles WHERE user_id=?", (account_id,)).fetchone()
            incoming = json.loads(guest_profile[0])
            saved = json.loads(account_profile[0]) if account_profile else {"origin": "", "preferences": []}
            saved["origin"] = saved.get("origin") or incoming.get("origin", "")
            combined = list(dict.fromkeys([*saved.get("preferences", []), *incoming.get("preferences", [])]))
            saved["preferences"], omitted = combined[:10], max(0, len(combined) - 10)
            self.db.execute("INSERT INTO profiles VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET body=excluded.body", (account_id, json.dumps(saved, ensure_ascii=False)))
            # Retain the original profile under the retired guest identity for
            # internal recovery. That cookie can no longer access any API data.
        self.db.execute("INSERT INTO retired_guests VALUES(?,?)", (guest, int(self.clock())))
        return {"moved_sessions": len(ids), "session_id_map": mapping, "profile_preferences_not_imported": omitted}
