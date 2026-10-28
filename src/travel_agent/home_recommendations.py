"""Durable, account-isolated inputs for the homepage recommendation feed.

Only canonical topic IDs and the owner's travel constraints are retained here.
Community ranking never exposes source messages, identities or private budgets.
The existing web-job receipt remains the authority for whether a turn succeeded.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re

from .recommendations import EXTRACTOR_VERSION, build_recommendations, extract_signals

RETENTION_DAYS = 90
MIN_COMMUNITY_USERS = 3


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class RecommendationStore:
    def __init__(self, repo):
        self.repo = repo
        self.db = repo.conn
        self.postgres = getattr(repo, "dialect", "sqlite") == "postgres"
        self.db.execute("""CREATE TABLE IF NOT EXISTS recommendation_signals (
            user_id TEXT NOT NULL, session_id TEXT NOT NULL, request_id TEXT NOT NULL,
            topic_id TEXT NOT NULL, occurred_at TEXT NOT NULL, context TEXT NOT NULL,
            rejected INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(user_id, session_id, request_id, topic_id))""")
        if self.postgres:
            self.db.execute("ALTER TABLE recommendation_signals ADD COLUMN IF NOT EXISTS rejected INTEGER NOT NULL DEFAULT 0")
        elif "rejected" not in {row[1] for row in self.db.execute("PRAGMA table_info(recommendation_signals)").fetchall()}:
            self.db.execute("ALTER TABLE recommendation_signals ADD COLUMN rejected INTEGER NOT NULL DEFAULT 0")
        self.db.execute("CREATE INDEX IF NOT EXISTS recommendation_signals_owner_time ON recommendation_signals(user_id,occurred_at)")
        self.db.execute("CREATE INDEX IF NOT EXISTS recommendation_signals_topic_time ON recommendation_signals(topic_id,occurred_at)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS recommendation_processed_jobs (
            user_id TEXT NOT NULL, job_id TEXT NOT NULL, processed_at TEXT NOT NULL,
            PRIMARY KEY(user_id,job_id))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS recommendation_dismissals (
            user_id TEXT NOT NULL, recommendation_id TEXT NOT NULL, dismissed_at TEXT NOT NULL,
            PRIMARY KEY(user_id,recommendation_id))""")
        self.db.execute("CREATE TABLE IF NOT EXISTS recommendation_metadata (name TEXT PRIMARY KEY,value TEXT NOT NULL)")
        version = self.db.execute("SELECT value FROM recommendation_metadata WHERE name='extractor_version'").fetchone()
        if not version or version[0] != str(EXTRACTOR_VERSION):
            # Derived data must be re-evaluated after extraction corrections.
            # Original trip history and explicit card dismissals stay untouched.
            self.db.execute("BEGIN" if self.postgres else "BEGIN IMMEDIATE")
            try:
                self.db.execute("DELETE FROM recommendation_signals")
                self.db.execute("DELETE FROM recommendation_processed_jobs")
                self.db.execute("INSERT INTO recommendation_metadata VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", ("extractor_version", str(EXTRACTOR_VERSION)))
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def record_job(self, row, *, state=None, now=None):
        """Idempotently derive signals from a committed successful user message."""
        now = utc(now or datetime.now(timezone.utc))
        user_id = row["user_id"]
        if row["status"] != "completed" or not row["result"]:
            return
        # Guests and pre-account local data must never enter community ranking.
        if not self.db.execute("SELECT 1 FROM accounts WHERE account_id=?", (user_id,)).fetchone():
            return
        if self.db.execute("SELECT 1 FROM recommendation_processed_jobs WHERE user_id=? AND job_id=?", (user_id, row["job_id"])).fetchone():
            return
        receipt = self.repo.turn(user_id, row["session_id"], row["request_id"])
        if receipt is None:
            return
        try:
            turn = json.loads(row["turn_json"])
            result = json.loads(row["result"])
            occurred = utc(row["created_at"])
        except (ValueError, TypeError):
            self._mark_processed(row, now)
            return
        if (not isinstance(turn, dict) or not isinstance(result, dict)
                or not isinstance(turn.get("payload"), dict)
                or not isinstance(receipt[1], dict)):
            # Malformed legacy rows are permanent skips, not a poison pill that
            # breaks the homepage or starves valid older history on every read.
            self._mark_processed(row, now)
            return
        result = receipt[1]  # The committed domain receipt is authoritative.
        eligible = (turn.get("input") == "message" and result.get("status") != "error"
                    and not result.get("error")
                    and now - timedelta(days=RETENTION_DAYS) <= occurred <= now + timedelta(minutes=5))
        if eligible:
            snapshot = state if state is not None else self.repo.session(user_id, row["session_id"])
            # A final session snapshot is not a historical snapshot. Earlier turns
            # are derived from their explicit text instead of newer requirements.
            if not snapshot or snapshot.get("state_version") != result.get("state_version"):
                snapshot = {"request": {}}
            for signal in extract_signals(snapshot, str(turn.get("payload", {}).get("text", ""))):
                topic = signal.get("topic_id")
                if not isinstance(topic, str) or not re.fullmatch(r"[A-Za-z0-9_:-]{1,100}", topic):
                    continue
                context = dict(signal.get("context") or {})
                rejected = context.get("rejected") is True
                # A model may classify a short refusal as conversational chat.
                # Honour an explicit negative signal, never a positive chat match.
                if result.get("response_kind") == "chat" and not rejected:
                    continue
                if not rejected and not {"nights", "duration_days"}.intersection(context):
                    previous = self.db.execute("""SELECT s.context,s.rejected FROM recommendation_signals s
                        JOIN web_jobs j ON j.user_id=s.user_id AND j.session_id=s.session_id AND j.request_id=s.request_id
                        WHERE s.user_id=? AND s.session_id=? AND s.topic_id=?
                        AND (s.occurred_at<? OR (s.occurred_at=? AND j.rowid<(SELECT rowid FROM web_jobs WHERE job_id=?)))
                        ORDER BY s.occurred_at DESC,j.rowid DESC LIMIT 1""",
                        (user_id,row["session_id"],topic,occurred.isoformat(),occurred.isoformat(),row["job_id"])).fetchone()
                    if previous and not previous["rejected"]:
                        prior_context = json.loads(previous["context"])
                        # Duration can be known before exact dates exist in
                        # TripRequest. Preserve its original unit and explicit
                        # reset, never removed budgets or party constraints.
                        for key in ("nights", "duration_days"):
                            if key in prior_context:
                                context[key] = prior_context[key]
                self.db.execute("""INSERT OR IGNORE INTO recommendation_signals
                    (user_id,session_id,request_id,topic_id,occurred_at,context,rejected) VALUES(?,?,?,?,?,?,?)""",
                    (user_id, row["session_id"], row["request_id"], topic,
                     occurred.isoformat(), json.dumps(context, ensure_ascii=False), 1 if rejected else 0))
        # Insert last: interruption during extraction leaves the job retryable.
        self._mark_processed(row, now)

    def _mark_processed(self, row, now):
        self.db.execute("INSERT OR IGNORE INTO recommendation_processed_jobs VALUES(?,?,?)",
                        (row["user_id"], row["job_id"], now.isoformat()))

    def backfill(self, *, now, user_id=None, limit=300):
        """Incrementally process legacy/recovered jobs with bounded request work."""
        cutoff = (utc(now) - timedelta(days=RETENTION_DAYS + 1)).isoformat()
        owner_filter = " AND j.user_id=?" if user_id else ""
        params = (cutoff, user_id, limit) if user_id else (cutoff, limit)
        rows = self.db.execute("""SELECT j.* FROM web_jobs j
            JOIN accounts a ON a.account_id=j.user_id
            JOIN turns t ON t.user_id=j.user_id AND t.session_id=j.session_id AND t.request_id=j.request_id
            LEFT JOIN recommendation_processed_jobs p ON p.user_id=j.user_id AND p.job_id=j.job_id
            WHERE j.status='completed' AND j.result IS NOT NULL AND p.job_id IS NULL
            AND j.created_at>=?""" + owner_filter + " ORDER BY j.created_at DESC,j.rowid DESC LIMIT ?", params).fetchall()
        # Replay the selected window in chronological order so follow-ups can
        # inherit an explicitly stated duration from the same trip.
        for row in reversed(rows):
            self.record_job(row, now=now)

    def feed(self, user_id, profile, *, now=None):
        now = utc(now or datetime.now(timezone.utc))
        self.backfill(now=now, user_id=user_id, limit=100)
        self.backfill(now=now, limit=300)
        cutoff = (now - timedelta(days=RETENTION_DAYS)).isoformat()
        # Remove expired derived data; the account's original trip history stays intact.
        self.db.execute("DELETE FROM recommendation_signals WHERE occurred_at<?", (cutoff,))
        # Limit distinct interests, not raw turns: one frequently refined trip
        # must not evict another valid interest from the personal history window.
        rows = self.db.execute("""SELECT topic_id,context,occurred_at,session_id,request_id,job_order FROM (
            SELECT s.topic_id,s.context,s.occurred_at,s.session_id,s.request_id,j.rowid AS job_order,
                ROW_NUMBER() OVER (PARTITION BY s.topic_id ORDER BY s.occurred_at DESC,j.rowid DESC,s.request_id DESC) AS position
            FROM recommendation_signals s
            LEFT JOIN web_jobs j ON j.user_id=s.user_id AND j.session_id=s.session_id AND j.request_id=s.request_id
            WHERE s.user_id=? AND s.occurred_at>=? AND s.occurred_at<=?
            ) AS latest WHERE position=1 ORDER BY occurred_at DESC,topic_id LIMIT 500""",
            (user_id, cutoff, now.isoformat())).fetchall()
        signals = []
        for row in rows:
            context = json.loads(row["context"])
            if context.get("rejected") is not True and not {"nights", "duration_days"}.intersection(context):
                # An older initial message may arrive in a later bounded-backfill
                # batch. Resolve its duration without copying any newer budget,
                # and stop at an explicit duration reset or rejected interest.
                has_duration = ("jsonb_extract_path(s.context::jsonb,'nights') IS NOT NULL OR jsonb_extract_path(s.context::jsonb,'duration_days') IS NOT NULL" if self.postgres else "json_type(s.context,'$.nights') IS NOT NULL OR json_type(s.context,'$.duration_days') IS NOT NULL")
                prior = self.db.execute("""SELECT s.context,s.rejected FROM recommendation_signals s
                    LEFT JOIN web_jobs j ON j.user_id=s.user_id AND j.session_id=s.session_id AND j.request_id=s.request_id
                    WHERE s.user_id=? AND s.session_id=? AND s.topic_id=?
                    AND (s.occurred_at<? OR (s.occurred_at=? AND j.rowid<?))
                    AND (s.rejected=1 OR """ + has_duration + """)
                    ORDER BY s.occurred_at DESC,j.rowid DESC LIMIT 1""",
                    (user_id,row["session_id"],row["topic_id"],row["occurred_at"],row["occurred_at"],row["job_order"] or 0)).fetchone()
                if prior and not prior["rejected"]:
                    prior_context = json.loads(prior["context"])
                    for key in ("nights", "duration_days"):
                        if key in prior_context:
                            context[key] = prior_context[key]
            signals.append({"topic_id": row["topic_id"], "context": context,
                            "occurred_at": row["occurred_at"]})
        # The latest statement wins per account/topic, including a refusal.
        # Repeated searches cannot inflate popularity; rejected interests do not
        # retain a historical vote after the user changes their mind.
        popular = self.db.execute("""SELECT topic_id, COUNT(*) AS users,
            SUM(CASE WHEN occurred_at>=? THEN 1.0 WHEN occurred_at>=? THEN 0.7 ELSE 0.4 END) AS score
            FROM (SELECT s.topic_id,s.user_id,s.occurred_at,s.rejected,
                ROW_NUMBER() OVER (PARTITION BY s.topic_id,s.user_id ORDER BY s.occurred_at DESC,j.rowid DESC,s.request_id DESC) AS position
                FROM recommendation_signals s JOIN accounts a ON a.account_id=s.user_id
                LEFT JOIN web_jobs j ON j.user_id=s.user_id AND j.session_id=s.session_id AND j.request_id=s.request_id
                WHERE s.occurred_at>=? AND s.occurred_at<=?) AS votes
            WHERE position=1 AND rejected=0
            GROUP BY topic_id HAVING COUNT(*)>=? ORDER BY score DESC,users DESC,topic_id LIMIT 50""",
            ((now - timedelta(days=7)).isoformat(), (now - timedelta(days=30)).isoformat(),
             cutoff, now.isoformat(), MIN_COMMUNITY_USERS)).fetchall()
        dismissed = {row[0] for row in self.db.execute(
            "SELECT recommendation_id FROM recommendation_dismissals WHERE user_id=?", (user_id,)).fetchall()}
        return build_recommendations(signals, [dict(row) for row in popular], profile, now, dismissed=dismissed)

    def dismiss(self, user_id, recommendation_id, *, now=None):
        self.db.execute("""INSERT INTO recommendation_dismissals VALUES(?,?,?)
            ON CONFLICT(user_id,recommendation_id) DO UPDATE SET dismissed_at=excluded.dismissed_at""",
            (user_id, recommendation_id, utc(now or datetime.now(timezone.utc)).isoformat()))
