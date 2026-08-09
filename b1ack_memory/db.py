from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import CandidateRecord, MEMORY_KINDS, MemoryRecord

SCHEMA_VERSION = 4


class _ClosingConnection(sqlite3.Connection):
    """Make `with db.connect()` commit/rollback and release the OS handle."""

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc, traceback))
        finally:
            self.close()


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def resolve_timezone(name: str | None):
    value = (name or "system").strip()
    if value == "system":
        return datetime.now().astimezone().tzinfo or UTC
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"Unknown IANA timezone: {value}") from error


def local_date(value: str, timezone_name: str | None = "system") -> str:
    return datetime.fromisoformat(value).astimezone(resolve_timezone(timezone_name)).date().isoformat()


def content_hash(content: str) -> str:
    normalized = " ".join(content.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class MemoryDatabase:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path, timeout=5, isolation_level=None, factory=_ClosingConnection
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA secure_delete=ON")
        return conn

    @contextlib.contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self) -> None:
        with self.transaction(immediate=True) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    version INTEGER NOT NULL
                );
                INSERT INTO schema_meta(version)
                SELECT 0 WHERE NOT EXISTS (SELECT 1 FROM schema_meta);

                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    origin TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    importance REAL NOT NULL DEFAULT 0.5,
                    sensitive INTEGER NOT NULL DEFAULT 0,
                    valid_until TEXT,
                    supersedes_id TEXT REFERENCES memories(id) ON DELETE SET NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
                CREATE INDEX IF NOT EXISTS idx_memories_hash ON memories(content_hash);

                CREATE TABLE IF NOT EXISTS memory_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                    content TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    changed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS raw_turns (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    user_content TEXT NOT NULL,
                    assistant_content TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    ingested_at TEXT,
                    secret_redacted INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS candidates (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    model_confidence REAL NOT NULL DEFAULT 0.0,
                    sensitive INTEGER NOT NULL DEFAULT 0,
                    score REAL NOT NULL DEFAULT 0.0,
                    score_components TEXT NOT NULL DEFAULT '{}',
                    recall_count INTEGER NOT NULL DEFAULT 0,
                    unique_query_count INTEGER NOT NULL DEFAULT 0,
                    evidence_days INTEGER NOT NULL DEFAULT 0,
                    conflict_memory_id TEXT REFERENCES memories(id) ON DELETE SET NULL,
                    conflict_reason TEXT,
                    content_hash TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_activity_at TEXT NOT NULL,
                    last_recalled_at TEXT,
                    expired_at TEXT,
                    rejected_at TEXT,
                    promoted_at TEXT,
                    promotion_origin TEXT,
                    rem_status TEXT NOT NULL DEFAULT 'unreviewed',
                    rem_reason TEXT,
                    rem_reviewed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_candidates_pending_hash
                    ON candidates(content_hash) WHERE status = 'pending';

                CREATE TABLE IF NOT EXISTS evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id TEXT REFERENCES candidates(id) ON DELETE CASCADE,
                    memory_id TEXT REFERENCES memories(id) ON DELETE CASCADE,
                    raw_turn_id TEXT REFERENCES raw_turns(id) ON DELETE CASCADE,
                    excerpt TEXT NOT NULL,
                    role TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS recall_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    query_text TEXT NOT NULL,
                    query_hash TEXT NOT NULL,
                    keyword_rank INTEGER,
                    vector_rank INTEGER,
                    final_score REAL NOT NULL,
                    injected INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS embeddings (
                    record_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    model_fingerprint TEXT NOT NULL,
                    vector_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(record_id, source)
                );

                CREATE TABLE IF NOT EXISTS dream_runs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    light_summary TEXT,
                    rem_summary TEXT,
                    deep_summary TEXT,
                    input_count INTEGER NOT NULL DEFAULT 0,
                    candidate_count INTEGER NOT NULL DEFAULT 0,
                    merged_count INTEGER NOT NULL DEFAULT 0,
                    filtered_count INTEGER NOT NULL DEFAULT 0,
                    expired_count INTEGER NOT NULL DEFAULT 0,
                    promoted_count INTEGER NOT NULL DEFAULT 0,
                    model TEXT,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS model_calls (
                    id TEXT PRIMARY KEY,
                    dream_run_id TEXT REFERENCES dream_runs(id) ON DELETE CASCADE,
                    phase TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS model_call_records (
                    call_id TEXT NOT NULL REFERENCES model_calls(id) ON DELETE CASCADE,
                    record_type TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    PRIMARY KEY(call_id, record_type, record_id)
                );
                CREATE INDEX IF NOT EXISTS idx_model_call_records_record
                    ON model_call_records(record_type, record_id);

                CREATE TABLE IF NOT EXISTS memory_events (
                    id TEXT PRIMARY KEY,
                    event_key TEXT UNIQUE,
                    event_type TEXT NOT NULL,
                    candidate_id TEXT REFERENCES candidates(id) ON DELETE CASCADE,
                    memory_id TEXT REFERENCES memories(id) ON DELETE CASCADE,
                    dream_run_id TEXT REFERENCES dream_runs(id) ON DELETE SET NULL,
                    occurred_at TEXT NOT NULL,
                    data_json TEXT NOT NULL DEFAULT '{}',
                    backfilled INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_memory_events_candidate
                    ON memory_events(candidate_id, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_memory_events_memory
                    ON memory_events(memory_id, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_memory_events_type_time
                    ON memory_events(event_type, occurred_at);

                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    record_id TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS leases (
                    name TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
                    record_id UNINDEXED,
                    source UNINDEXED,
                    content,
                    search_text,
                    tokenize='unicode61 remove_diacritics 2'
                );
                """
            )
            current = int(conn.execute("SELECT version FROM schema_meta").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise RuntimeError(f"Database schema {current} is newer than supported {SCHEMA_VERSION}")
            candidate_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(candidates)").fetchall()
            }
            if "conflict_reason" not in candidate_columns:
                conn.execute("ALTER TABLE candidates ADD COLUMN conflict_reason TEXT")
            candidate_migrations = {
                "last_activity_at": "TEXT",
                "last_recalled_at": "TEXT",
                "expired_at": "TEXT",
                "rejected_at": "TEXT",
                "promoted_at": "TEXT",
                "promotion_origin": "TEXT",
                "rem_status": "TEXT NOT NULL DEFAULT 'unreviewed'",
                "rem_reason": "TEXT",
                "rem_reviewed_at": "TEXT",
            }
            for column, definition in candidate_migrations.items():
                if column not in candidate_columns:
                    conn.execute(f"ALTER TABLE candidates ADD COLUMN {column} {definition}")
            conn.execute(
                "UPDATE candidates SET last_activity_at=last_seen_at "
                "WHERE last_activity_at IS NULL OR last_activity_at=''"
            )
            dream_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(dream_runs)").fetchall()
            }
            for column in ("merged_count", "filtered_count", "expired_count"):
                if column not in dream_columns:
                    conn.execute(
                        f"ALTER TABLE dream_runs ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                    )
            if current < 4:
                self._backfill_memory_events(conn)
                timezone_name = "system"
                general_row = conn.execute(
                    "SELECT value_json FROM settings WHERE key='general'"
                ).fetchone()
                if general_row:
                    try:
                        timezone_name = str(
                            json.loads(general_row["value_json"]).get("timezone", "system")
                        )
                        resolve_timezone(timezone_name)
                    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                        timezone_name = "system"
                for candidate_row in conn.execute("SELECT id FROM candidates").fetchall():
                    evidence_days = self._count_evidence_days(
                        conn, candidate_row["id"], timezone_name
                    )
                    conn.execute(
                        "UPDATE candidates SET evidence_days=? WHERE id=?",
                        (evidence_days, candidate_row["id"]),
                    )
            conn.execute("UPDATE schema_meta SET version=?", (SCHEMA_VERSION,))

    def default_settings(self) -> dict[str, Any]:
        return {
            "general": {"timezone": "system"},
            "llm": {
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-v4-flash",
                "timeout_seconds": 60,
                "max_output_tokens": 1200,
            },
            "embedding": {"enabled": False, "base_url": "", "model": ""},
            "dream": {
                "enabled": True,
                "daily_at": "03:00",
                "max_light_batches": 3,
                "batch_chars": 12000,
                "max_auto_promotions": 3,
                "max_new_candidates": 8,
            },
            "retention": {
                "raw_turn_days": 30,
                "model_call_days": 30,
                "backup_count": 7,
                "candidate_inactive_days": 14,
                "candidate_expired_days": 30,
                "rejected_candidate_days": 30,
            },
            "recall": {"limit": 5, "max_context_chars": 4000},
        }

    def get_settings(self) -> dict[str, Any]:
        result = self.default_settings()
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value_json FROM settings").fetchall()
        for row in rows:
            value = json.loads(row["value_json"])
            if isinstance(result.get(row["key"]), dict) and isinstance(value, dict):
                result[row["key"]].update(value)
            else:
                result[row["key"]] = value
        return result

    def save_setting(self, key: str, value: Any) -> None:
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False), now),
            )

    @staticmethod
    def _add_event(
        conn: sqlite3.Connection,
        event_type: str,
        *,
        candidate_id: str | None = None,
        memory_id: str | None = None,
        dream_run_id: str | None = None,
        occurred_at: str | None = None,
        data: dict[str, Any] | None = None,
        backfilled: bool = False,
        event_key: str | None = None,
    ) -> None:
        values = (
            str(uuid.uuid4()),
            event_key,
            event_type,
            candidate_id,
            memory_id,
            dream_run_id,
            occurred_at or utc_now(),
            json.dumps(data or {}, ensure_ascii=False),
            int(backfilled),
        )
        conn.execute(
            "INSERT OR IGNORE INTO memory_events("
            "id,event_key,event_type,candidate_id,memory_id,dream_run_id,occurred_at,data_json,backfilled"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            values,
        )

    def _backfill_memory_events(self, conn: sqlite3.Connection) -> None:
        for row in conn.execute("SELECT * FROM candidates"):
            self._add_event(
                conn,
                "candidate_created",
                candidate_id=row["id"],
                occurred_at=row["first_seen_at"],
                data={"content": row["content"], "kind": row["kind"]},
                backfilled=True,
                event_key=f"backfill:candidate:create:{row['id']}",
            )
        for row in conn.execute("SELECT * FROM evidence"):
            self._add_event(
                conn,
                "evidence_added",
                candidate_id=row["candidate_id"],
                memory_id=row["memory_id"],
                occurred_at=row["observed_at"],
                data={"excerpt": row["excerpt"], "raw_turn_id": row["raw_turn_id"]},
                backfilled=True,
                event_key=f"backfill:evidence:{row['id']}",
            )
        for row in conn.execute("SELECT * FROM candidates"):
            if row["rem_reviewed_at"]:
                self._add_event(
                    conn,
                    "rem_reviewed",
                    candidate_id=row["id"],
                    occurred_at=row["rem_reviewed_at"],
                    data={"decision": row["rem_status"], "reason": row["rem_reason"]},
                    backfilled=True,
                    event_key=f"backfill:candidate:rem:{row['id']}",
                )
            state_time = {
                "promoted": row["promoted_at"],
                "expired": row["expired_at"],
                "rejected": row["rejected_at"],
            }.get(row["status"])
            if state_time:
                memory_row = conn.execute(
                    "SELECT memory_id FROM evidence WHERE candidate_id=? AND memory_id IS NOT NULL LIMIT 1",
                    (row["id"],),
                ).fetchone()
                self._add_event(
                    conn,
                    f"candidate_{row['status']}",
                    candidate_id=row["id"],
                    memory_id=memory_row["memory_id"] if memory_row else None,
                    occurred_at=state_time,
                    data={
                        "origin": row["promotion_origin"] if row["status"] == "promoted" else None,
                        "promotion_lane": "unknown" if row["status"] == "promoted" else None,
                    },
                    backfilled=True,
                    event_key=f"backfill:candidate:{row['status']}:{row['id']}",
                )
        for row in conn.execute("SELECT * FROM memories"):
            self._add_event(
                conn,
                "memory_created",
                memory_id=row["id"],
                occurred_at=row["created_at"],
                data={"content": row["content"], "kind": row["kind"], "origin": row["origin"]},
                backfilled=True,
                event_key=f"backfill:memory:create:{row['id']}",
            )
        for row in conn.execute("SELECT * FROM memory_revisions"):
            self._add_event(
                conn,
                "memory_updated",
                memory_id=row["memory_id"],
                occurred_at=row["changed_at"],
                data={"previous_content": row["content"], "previous_kind": row["kind"]},
                backfilled=True,
                event_key=f"backfill:memory:revision:{row['id']}",
            )

    @staticmethod
    def _count_evidence_days(
        conn: sqlite3.Connection, candidate_id: str, timezone_name: str = "system"
    ) -> int:
        dates: set[str] = set()
        for row in conn.execute(
            "SELECT observed_at FROM evidence WHERE candidate_id=?", (candidate_id,)
        ):
            try:
                dates.add(local_date(row["observed_at"], timezone_name))
            except (TypeError, ValueError):
                continue
        return len(dates)

    def recompute_evidence_days(self, timezone_name: str = "system") -> int:
        resolve_timezone(timezone_name)
        updated = 0
        with self.transaction(immediate=True) as conn:
            ids = [row[0] for row in conn.execute("SELECT id FROM candidates")]
            for candidate_id in ids:
                days = self._count_evidence_days(conn, candidate_id, timezone_name)
                updated += conn.execute(
                    "UPDATE candidates SET evidence_days=? WHERE id=? AND evidence_days<>?",
                    (days, candidate_id, days),
                ).rowcount
        return updated

    def add_memory(
        self,
        content: str,
        *,
        kind: str = "fact",
        origin: str = "manual",
        confidence: float = 1.0,
        importance: float = 0.5,
        sensitive: bool = False,
        supersedes_id: str | None = None,
    ) -> MemoryRecord:
        if kind not in MEMORY_KINDS:
            raise ValueError(f"Unsupported memory kind: {kind}")
        now = utc_now()
        record_id = str(uuid.uuid4())
        digest = content_hash(content)
        with self.transaction(immediate=True) as conn:
            existing = conn.execute(
                "SELECT * FROM memories WHERE content_hash=? AND status='active'", (digest,)
            ).fetchone()
            if existing:
                return self._memory_from_row(existing)
            conn.execute(
                """INSERT INTO memories(
                    id,content,kind,status,origin,confidence,importance,sensitive,
                    supersedes_id,content_hash,created_at,updated_at
                ) VALUES(?,?,?,'active',?,?,?,?,?,?,?,?)""",
                (
                    record_id,
                    content.strip(),
                    kind,
                    origin,
                    max(0.0, min(1.0, confidence)),
                    max(0.0, min(1.0, importance)),
                    int(sensitive),
                    supersedes_id,
                    digest,
                    now,
                    now,
                ),
            )
            self._add_event(
                conn,
                "memory_created",
                memory_id=record_id,
                occurred_at=now,
                data={"content": content.strip(), "kind": kind, "origin": origin},
            )
            if supersedes_id:
                conn.execute(
                    "UPDATE memories SET status='superseded', updated_at=? WHERE id=?", (now, supersedes_id)
                )
                self._add_event(
                    conn,
                    "memory_superseded",
                    memory_id=supersedes_id,
                    occurred_at=now,
                    data={"replacement_memory_id": record_id},
                )
            row = conn.execute("SELECT * FROM memories WHERE id=?", (record_id,)).fetchone()
        return self._memory_from_row(row)

    def list_memories(self, *, status: str | None = "active", limit: int = 500) -> list[MemoryRecord]:
        sql = "SELECT * FROM memories"
        args: list[Any] = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._memory_from_row(row) for row in rows]

    def get_memory(self, record_id: str) -> MemoryRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM memories WHERE id=?", (record_id,)).fetchone()
        return self._memory_from_row(row) if row else None

    def update_memory(self, record_id: str, *, content: str, kind: str) -> MemoryRecord:
        if kind not in MEMORY_KINDS:
            raise ValueError(f"Unsupported memory kind: {kind}")
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            old = conn.execute("SELECT * FROM memories WHERE id=?", (record_id,)).fetchone()
            if not old:
                raise KeyError(record_id)
            conn.execute(
                "INSERT INTO memory_revisions(memory_id,content,kind,changed_at) VALUES(?,?,?,?)",
                (record_id, old["content"], old["kind"], now),
            )
            conn.execute(
                "UPDATE memories SET content=?, kind=?, content_hash=?, updated_at=? WHERE id=?",
                (content.strip(), kind, content_hash(content), now, record_id),
            )
            self._add_event(
                conn,
                "memory_updated",
                memory_id=record_id,
                occurred_at=now,
                data={
                    "previous_content": old["content"],
                    "previous_kind": old["kind"],
                    "content": content.strip(),
                    "kind": kind,
                },
            )
            row = conn.execute("SELECT * FROM memories WHERE id=?", (record_id,)).fetchone()
        return self._memory_from_row(row)

    def set_memory_status(self, record_id: str, status: str) -> None:
        if status not in {"active", "superseded", "trashed"}:
            raise ValueError(status)
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            old = conn.execute("SELECT status FROM memories WHERE id=?", (record_id,)).fetchone()
            if not old:
                raise KeyError(record_id)
            changed = conn.execute(
                "UPDATE memories SET status=?, updated_at=? WHERE id=?",
                (status, now, record_id),
            ).rowcount
            if not changed:
                raise KeyError(record_id)
            event_type = {
                "active": "memory_restored",
                "trashed": "memory_trashed",
                "superseded": "memory_superseded",
            }[status]
            self._add_event(
                conn,
                event_type,
                memory_id=record_id,
                occurred_at=now,
                data={"previous_status": old["status"], "status": status},
            )

    def purge_memory(self, record_id: str) -> dict[str, int]:
        with self.transaction(immediate=True) as conn:
            memory = conn.execute(
                "SELECT id,content FROM memories WHERE id=?", (record_id,)
            ).fetchone()
            if not memory:
                raise KeyError(record_id)
            candidate_rows = conn.execute(
                "SELECT DISTINCT c.id,c.content FROM candidates c "
                "LEFT JOIN evidence e ON e.candidate_id=c.id "
                "LEFT JOIN memory_events me ON me.candidate_id=c.id "
                "AND me.event_type='candidate_promoted' "
                "WHERE e.memory_id=? OR me.memory_id=?",
                (record_id, record_id),
            ).fetchall()
            candidate_ids = [row["id"] for row in candidate_rows]
            raw_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT raw_turn_id FROM evidence WHERE memory_id=? AND raw_turn_id IS NOT NULL",
                    (record_id,),
                )
            ]
            references = [("memory", record_id)]
            references.extend(("candidate", item) for item in candidate_ids)
            references.extend(("raw_turn", item) for item in raw_ids)
            dream_run_ids: set[str] = set()
            for record_type, linked_id in references:
                dream_run_ids.update(
                    row[0]
                    for row in conn.execute(
                        "SELECT DISTINCT mc.dream_run_id FROM model_calls mc "
                        "JOIN model_call_records mcr ON mcr.call_id=mc.id "
                        "WHERE mcr.record_type=? AND mcr.record_id=? "
                        "AND mc.dream_run_id IS NOT NULL",
                        (record_type, linked_id),
                    )
                )

            # Databases created before schema v2 have no explicit call links.
            # Remove legacy runs whose stored request/response contains deleted content.
            raw_rows = []
            if raw_ids:
                placeholders = ",".join("?" for _ in raw_ids)
                raw_rows = conn.execute(
                    f"SELECT user_content,assistant_content FROM raw_turns WHERE id IN ({placeholders})",
                    raw_ids,
                ).fetchall()
            terms = [memory["content"]]
            terms.extend(row["content"] for row in candidate_rows)
            terms.extend(value for row in raw_rows for value in row if value)
            terms = [term for term in terms if len(term.strip()) >= 4]
            if terms:
                for call in conn.execute(
                    "SELECT dream_run_id,request_json,response_json FROM model_calls "
                    "WHERE dream_run_id IS NOT NULL"
                ):
                    stored = f"{call['request_json']}\n{call['response_json'] or ''}"
                    if any(term in stored for term in terms):
                        dream_run_ids.add(call["dream_run_id"])

            for run_id in dream_run_ids:
                conn.execute("DELETE FROM dream_runs WHERE id=?", (run_id,))
            for candidate_id in candidate_ids:
                conn.execute(
                    "DELETE FROM recall_events WHERE record_id=? AND source='candidate'",
                    (candidate_id,),
                )
                conn.execute(
                    "DELETE FROM embeddings WHERE record_id=? AND source='candidate'",
                    (candidate_id,),
                )
                conn.execute(
                    "DELETE FROM search_fts WHERE record_id=? AND source='candidate'",
                    (candidate_id,),
                )
                conn.execute("DELETE FROM candidates WHERE id=?", (candidate_id,))
            conn.execute("DELETE FROM recall_events WHERE record_id=?", (record_id,))
            conn.execute("DELETE FROM embeddings WHERE record_id=? AND source='memory'", (record_id,))
            conn.execute("DELETE FROM search_fts WHERE record_id=? AND source='memory'", (record_id,))
            deleted = conn.execute("DELETE FROM memories WHERE id=?", (record_id,)).rowcount
            if deleted != 1:
                raise KeyError(record_id)
            for raw_id in raw_ids:
                conn.execute("DELETE FROM raw_turns WHERE id=?", (raw_id,))
            conn.execute(
                "INSERT INTO audit_events(action,record_id,created_at) VALUES('purge',?,?)",
                (record_id, utc_now()),
            )
        return {
            "memories": 1,
            "candidates": len(candidate_ids),
            "raw_turns": len(raw_ids),
            "dream_runs": len(dream_run_ids),
        }

    def add_raw_turn(self, session_id: str, user: str, assistant: str, *, redacted: bool) -> str:
        record_id = str(uuid.uuid4())
        with self.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO raw_turns VALUES(?,?,?,?,?,?,?)",
                (record_id, session_id, user, assistant, utc_now(), None, int(redacted)),
            )
        return record_id

    def pending_raw_turns(self, limit: int = 500) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM raw_turns WHERE ingested_at IS NULL ORDER BY observed_at LIMIT ?", (limit,)
            ).fetchall()

    def mark_turns_ingested(self, ids: list[str]) -> None:
        if not ids:
            return
        with self.transaction(immediate=True) as conn:
            conn.executemany(
                "UPDATE raw_turns SET ingested_at=? WHERE id=?", [(utc_now(), item) for item in ids]
            )

    def upsert_candidate(
        self,
        content: str,
        *,
        kind: str,
        confidence: float,
        sensitive: bool,
        raw_turn_id: str | None,
        excerpt: str,
        observed_at: str | None = None,
        timezone_name: str = "system",
        dream_run_id: str | None = None,
    ) -> CandidateRecord:
        if kind not in MEMORY_KINDS:
            kind = "fact"
        digest = content_hash(content)
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM candidates WHERE content_hash=? "
                "ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'expired' THEN 1 "
                "WHEN 'rejected' THEN 2 ELSE 3 END LIMIT 1",
                (digest,),
            ).fetchone()
            if row and row["status"] in {"rejected", "promoted"}:
                return self._candidate_from_row(row)
            restored = bool(row and row["status"] == "expired")
            if row:
                candidate_id = row["id"]
                conn.execute(
                    "UPDATE candidates SET status='pending',last_seen_at=?,last_activity_at=?, "
                    "model_confidence=max(model_confidence,?),expired_at=NULL,rejected_at=NULL, "
                    "rem_status='unreviewed',rem_reason=NULL,rem_reviewed_at=NULL WHERE id=?",
                    (now, now, confidence, candidate_id),
                )
            else:
                candidate_id = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO candidates(
                        id,content,kind,status,model_confidence,sensitive,score,score_components,
                        evidence_days,content_hash,first_seen_at,last_seen_at,last_activity_at
                    ) VALUES(?,?,?,'pending',?,?,0,'{}',0,?,?,?,?)""",
                    (
                        candidate_id,
                        content.strip(),
                        kind,
                        confidence,
                        int(sensitive),
                        digest,
                        now,
                        now,
                        now,
                    ),
                )
                self._add_event(
                    conn,
                    "candidate_created",
                    candidate_id=candidate_id,
                    dream_run_id=dream_run_id,
                    occurred_at=now,
                    data={"content": content.strip(), "kind": kind},
                )
            if restored:
                self._add_event(
                    conn,
                    "candidate_restored",
                    candidate_id=candidate_id,
                    dream_run_id=dream_run_id,
                    occurred_at=now,
                    data={"reason": "new_evidence"},
                )
            if raw_turn_id:
                exists = conn.execute(
                    "SELECT 1 FROM evidence WHERE candidate_id=? AND raw_turn_id=?",
                    (candidate_id, raw_turn_id),
                ).fetchone()
                if not exists:
                    evidence_time = observed_at or now
                    conn.execute(
                        "INSERT INTO evidence(candidate_id,raw_turn_id,excerpt,role,observed_at) VALUES(?,?,?,?,?)",
                        (
                            candidate_id,
                            raw_turn_id,
                            excerpt[:1000],
                            "conversation",
                            evidence_time,
                        ),
                    )
                    self._add_event(
                        conn,
                        "evidence_added",
                        candidate_id=candidate_id,
                        dream_run_id=dream_run_id,
                        occurred_at=evidence_time,
                        data={"excerpt": excerpt[:1000], "raw_turn_id": raw_turn_id},
                    )
            evidence_days = self._count_evidence_days(conn, candidate_id, timezone_name)
            conn.execute(
                "UPDATE candidates SET evidence_days=? WHERE id=?",
                (evidence_days, candidate_id),
            )
            row = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        return self._candidate_from_row(row)

    def get_candidate(self, candidate_id: str) -> CandidateRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        return self._candidate_from_row(row) if row else None

    def candidate_by_content(self, content: str) -> CandidateRecord | None:
        digest = content_hash(content)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM candidates WHERE content_hash=? "
                "ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'expired' THEN 1 "
                "WHEN 'rejected' THEN 2 ELSE 3 END LIMIT 1",
                (digest,),
            ).fetchone()
        return self._candidate_from_row(row) if row else None

    def active_memory_has_content(self, content: str) -> bool:
        with self.connect() as conn:
            return bool(
                conn.execute(
                    "SELECT 1 FROM memories WHERE content_hash=? AND status='active'",
                    (content_hash(content),),
                ).fetchone()
            )

    def list_candidates(self, *, status: str = "pending", limit: int = 500) -> list[CandidateRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM candidates WHERE status=? ORDER BY score DESC,last_seen_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        return [self._candidate_from_row(row) for row in rows]

    def update_candidate_score(self, candidate_id: str, score: float, components: dict[str, float]) -> None:
        with self.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE candidates SET score=?, score_components=? WHERE id=?",
                (score, json.dumps(components), candidate_id),
            )

    def update_candidate_conflicts(
        self,
        conflicts: list[dict[str, Any]],
        *,
        reviewed_ids: list[str],
        dream_run_id: str | None = None,
    ) -> set[str]:
        if not reviewed_ids:
            return set()
        reviewed = set(reviewed_ids)
        conflicted: set[str] = set()
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            conn.executemany(
                "UPDATE candidates SET conflict_memory_id=NULL,conflict_reason=NULL "
                "WHERE id=? AND status='pending'",
                [(item,) for item in reviewed_ids],
            )
            for conflict in conflicts:
                candidate_id = str(conflict.get("candidate_id", ""))
                memory_id = str(conflict.get("memory_id", "")).strip() or None
                reason = str(conflict.get("explanation", "")).strip()[:1000]
                if candidate_id not in reviewed or not reason:
                    continue
                if memory_id and not conn.execute(
                    "SELECT 1 FROM memories WHERE id=? AND status='active'", (memory_id,)
                ).fetchone():
                    memory_id = None
                changed = conn.execute(
                    "UPDATE candidates SET conflict_memory_id=?,conflict_reason=?,"
                    "rem_status='conflict',rem_reason=?,rem_reviewed_at=? "
                    "WHERE id=? AND status='pending'",
                    (memory_id, reason, reason, now, candidate_id),
                ).rowcount
                if changed:
                    conflicted.add(candidate_id)
                    self._add_event(
                        conn,
                        "rem_reviewed",
                        candidate_id=candidate_id,
                        memory_id=memory_id,
                        dream_run_id=dream_run_id,
                        occurred_at=now,
                        data={"decision": "conflict", "reason": reason},
                    )
        return conflicted

    def update_candidate_rem_review(
        self,
        *,
        reviewed_ids: list[str],
        durable_ids: list[str],
        noise: dict[str, str],
        reasons: dict[str, str] | None = None,
        dream_run_id: str | None = None,
    ) -> int:
        reviewed = set(reviewed_ids)
        durable = reviewed.intersection(durable_ids)
        review_reasons = reasons or {}
        now = utc_now()
        expired = 0
        with self.transaction(immediate=True) as conn:
            for candidate_id in reviewed:
                if candidate_id in noise:
                    changed = conn.execute(
                        "UPDATE candidates SET status='expired',expired_at=?,rem_status='noise',"
                        "rem_reason=?,rem_reviewed_at=? WHERE id=? AND status='pending'",
                        (now, noise[candidate_id][:1000], now, candidate_id),
                    ).rowcount
                    expired += changed
                    if changed:
                        self._add_event(
                            conn,
                            "rem_reviewed",
                            candidate_id=candidate_id,
                            dream_run_id=dream_run_id,
                            occurred_at=now,
                            data={"decision": "noise", "reason": noise[candidate_id][:1000]},
                        )
                        self._add_event(
                            conn,
                            "candidate_expired",
                            candidate_id=candidate_id,
                            dream_run_id=dream_run_id,
                            occurred_at=now,
                            data={"reason": noise[candidate_id][:1000], "source": "rem"},
                        )
                else:
                    decision = "approved" if candidate_id in durable else "deferred"
                    reason = review_reasons.get(candidate_id, "")[:1000] or None
                    conn.execute(
                        "UPDATE candidates SET rem_status=?,rem_reason=?,rem_reviewed_at=? "
                        "WHERE id=? AND status='pending'",
                        (decision, reason, now, candidate_id),
                    )
                    self._add_event(
                        conn,
                        "rem_reviewed",
                        candidate_id=candidate_id,
                        dream_run_id=dream_run_id,
                        occurred_at=now,
                        data={"decision": decision, "reason": reason},
                    )
        return expired

    def expire_candidate(
        self, candidate_id: str, reason: str, *, dream_run_id: str | None = None
    ) -> bool:
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            changed = conn.execute(
                    "UPDATE candidates SET status='expired',expired_at=?,rem_status='duplicate',"
                    "rem_reason=?,rem_reviewed_at=? WHERE id=? AND status='pending'",
                    (now, reason[:1000], now, candidate_id),
                ).rowcount
            if changed:
                self._add_event(
                    conn,
                    "rem_reviewed",
                    candidate_id=candidate_id,
                    dream_run_id=dream_run_id,
                    occurred_at=now,
                    data={"decision": "duplicate", "reason": reason[:1000]},
                )
                self._add_event(
                    conn,
                    "candidate_expired",
                    candidate_id=candidate_id,
                    dream_run_id=dream_run_id,
                    occurred_at=now,
                    data={"reason": reason[:1000], "source": "rem"},
                )
            return bool(changed)

    def merge_candidates(
        self,
        canonical_id: str,
        duplicate_id: str,
        *,
        timezone_name: str = "system",
        dream_run_id: str | None = None,
        reason: str = "同义候选合并",
    ) -> CandidateRecord:
        if canonical_id == duplicate_id:
            candidate = self.get_candidate(canonical_id)
            if not candidate:
                raise KeyError(canonical_id)
            return candidate
        with self.transaction(immediate=True) as conn:
            canonical = conn.execute(
                "SELECT * FROM candidates WHERE id=? AND status='pending'", (canonical_id,)
            ).fetchone()
            duplicate = conn.execute(
                "SELECT * FROM candidates WHERE id=? AND status='pending'", (duplicate_id,)
            ).fetchone()
            if not canonical or not duplicate:
                raise KeyError(duplicate_id if canonical else canonical_id)
            conn.execute(
                "UPDATE evidence SET candidate_id=? WHERE candidate_id=?",
                (canonical_id, duplicate_id),
            )
            conn.execute(
                "UPDATE memory_events SET candidate_id=? WHERE candidate_id=?",
                (canonical_id, duplicate_id),
            )
            conn.execute(
                "UPDATE recall_events SET record_id=? WHERE record_id=? AND source='candidate'",
                (canonical_id, duplicate_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO model_call_records(call_id,record_type,record_id) "
                "SELECT call_id,'candidate',? FROM model_call_records "
                "WHERE record_type='candidate' AND record_id=?",
                (canonical_id, duplicate_id),
            )
            conn.execute(
                "DELETE FROM model_call_records WHERE record_type='candidate' AND record_id=?",
                (duplicate_id,),
            )
            conn.execute(
                "DELETE FROM embeddings WHERE record_id=? AND source='candidate'", (duplicate_id,)
            )
            conn.execute(
                "DELETE FROM search_fts WHERE record_id=? AND source='candidate'", (duplicate_id,)
            )
            conn.execute("DELETE FROM candidates WHERE id=?", (duplicate_id,))
            evidence_days = self._count_evidence_days(conn, canonical_id, timezone_name)
            recall_count = conn.execute(
                "SELECT COUNT(*) FROM recall_events WHERE record_id=? "
                "AND source='candidate' AND injected=1",
                (canonical_id,),
            ).fetchone()[0]
            unique_queries = conn.execute(
                "SELECT COUNT(DISTINCT query_hash) FROM recall_events WHERE record_id=? "
                "AND source='candidate' AND injected=1",
                (canonical_id,),
            ).fetchone()[0]
            conn.execute(
                """UPDATE candidates SET model_confidence=max(model_confidence,?),
                sensitive=max(sensitive,?),first_seen_at=min(first_seen_at,?),
                last_seen_at=max(last_seen_at,?),last_activity_at=max(last_activity_at,?),
                evidence_days=?,recall_count=?,unique_query_count=?,
                rem_status='unreviewed',rem_reason=NULL,rem_reviewed_at=NULL WHERE id=?""",
                (
                    duplicate["model_confidence"],
                    duplicate["sensitive"],
                    duplicate["first_seen_at"],
                    duplicate["last_seen_at"],
                    duplicate["last_activity_at"] or duplicate["last_seen_at"],
                    evidence_days,
                    recall_count,
                    unique_queries,
                    canonical_id,
                ),
            )
            self._add_event(
                conn,
                "candidate_merged",
                candidate_id=canonical_id,
                dream_run_id=dream_run_id,
                data={
                    "duplicate_candidate_id": duplicate_id,
                    "duplicate_content": duplicate["content"],
                    "reason": reason[:1000],
                },
            )
            row = conn.execute("SELECT * FROM candidates WHERE id=?", (canonical_id,)).fetchone()
        return self._candidate_from_row(row)

    def add_model_call_refs(
        self, call_id: str, references: list[tuple[str, str]]
    ) -> None:
        if not references:
            return
        with self.transaction(immediate=True) as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO model_call_records(call_id,record_type,record_id) "
                "VALUES(?,?,?)",
                [(call_id, record_type, record_id) for record_type, record_id in references],
            )

    def promote_candidate(
        self,
        candidate_id: str,
        *,
        edited_content: str | None = None,
        origin: str = "review",
        promotion_lane: str = "manual",
        dream_run_id: str | None = None,
    ) -> MemoryRecord:
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            candidate = conn.execute(
                "SELECT * FROM candidates WHERE id=? AND status='pending'", (candidate_id,)
            ).fetchone()
            if not candidate:
                raise KeyError(candidate_id)
            memory_content = (edited_content or candidate["content"]).strip()
            existing = conn.execute(
                "SELECT * FROM memories WHERE content_hash=? AND status='active'",
                (content_hash(memory_content),),
            ).fetchone()
            if existing:
                memory_id = existing["id"]
            else:
                memory_id = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO memories(
                        id,content,kind,status,origin,confidence,importance,sensitive,
                        supersedes_id,content_hash,created_at,updated_at
                    ) VALUES(?,?,?,'active',?,?,?,?,?,?,?,?)""",
                    (
                        memory_id,
                        memory_content,
                        candidate["kind"],
                        origin,
                        float(candidate["model_confidence"]),
                        0.5,
                        int(candidate["sensitive"]),
                        candidate["conflict_memory_id"],
                        content_hash(memory_content),
                        now,
                        now,
                    ),
                )
                self._add_event(
                    conn,
                    "memory_created",
                    candidate_id=candidate_id,
                    memory_id=memory_id,
                    dream_run_id=dream_run_id,
                    occurred_at=now,
                    data={
                        "content": memory_content,
                        "kind": candidate["kind"],
                        "origin": origin,
                    },
                )
                if candidate["conflict_memory_id"]:
                    conn.execute(
                        "UPDATE memories SET status='superseded',updated_at=? WHERE id=?",
                        (now, candidate["conflict_memory_id"]),
                    )
            conn.execute(
                "UPDATE candidates SET status='promoted',promoted_at=?,promotion_origin=? WHERE id=?",
                (now, origin, candidate_id),
            )
            conn.execute(
                "UPDATE evidence SET memory_id=? WHERE candidate_id=?", (memory_id, candidate_id)
            )
            conn.execute(
                "INSERT OR IGNORE INTO model_call_records(call_id,record_type,record_id) "
                "SELECT call_id,'memory',? FROM model_call_records "
                "WHERE record_type='candidate' AND record_id=?",
                (memory_id, candidate_id),
            )
            self._add_event(
                conn,
                "candidate_promoted",
                candidate_id=candidate_id,
                memory_id=memory_id,
                dream_run_id=dream_run_id,
                occurred_at=now,
                data={
                    "origin": origin,
                    "promotion_lane": promotion_lane,
                    "candidate_content": candidate["content"],
                    "memory_content": memory_content,
                    "model_confidence": float(candidate["model_confidence"]),
                    "evidence_days": int(candidate["evidence_days"]),
                    "recall_count": int(candidate["recall_count"]),
                    "unique_query_count": int(candidate["unique_query_count"]),
                },
            )
            row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return self._memory_from_row(row)

    def reject_candidate(self, candidate_id: str) -> None:
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            changed = conn.execute(
                "UPDATE candidates SET status='rejected',rejected_at=?,last_activity_at=?,"
                "rem_status='rejected',rem_reason='人工拒绝' WHERE id=? AND status='pending'",
                (now, now, candidate_id),
            ).rowcount
            if not changed:
                raise KeyError(candidate_id)
            self._add_event(
                conn,
                "candidate_rejected",
                candidate_id=candidate_id,
                occurred_at=now,
                data={"reason": "人工拒绝"},
            )

    def restore_candidate(self, candidate_id: str) -> CandidateRecord:
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            changed = conn.execute(
                "UPDATE candidates SET status='pending',last_activity_at=?,expired_at=NULL,"
                "rejected_at=NULL,rem_status='unreviewed',rem_reason=NULL,rem_reviewed_at=NULL "
                "WHERE id=? AND status IN ('expired','rejected')",
                (now, candidate_id),
            ).rowcount
            if not changed:
                raise KeyError(candidate_id)
            self._add_event(
                conn,
                "candidate_restored",
                candidate_id=candidate_id,
                occurred_at=now,
                data={"reason": "manual"},
            )
            row = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        return self._candidate_from_row(row)

    def count_auto_promotions_since(self, since: str) -> int:
        with self.connect() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM candidates WHERE status='promoted' "
                    "AND promotion_origin='dream' AND promoted_at>=?",
                    (since,),
                ).fetchone()[0]
            )

    def purge_candidate(
        self,
        candidate_id: str,
        *,
        privacy: bool = False,
        timezone_name: str = "system",
    ) -> dict[str, int]:
        with self.transaction(immediate=True) as conn:
            candidate = conn.execute(
                "SELECT id,content FROM candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            if not candidate:
                raise KeyError(candidate_id)
            raw_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT raw_turn_id FROM evidence WHERE candidate_id=? "
                    "AND raw_turn_id IS NOT NULL",
                    (candidate_id,),
                )
            ]
            affected_candidate_ids: list[str] = []
            if privacy and raw_ids:
                placeholders = ",".join("?" for _ in raw_ids)
                affected_candidate_ids = [
                    row[0]
                    for row in conn.execute(
                        f"SELECT DISTINCT candidate_id FROM evidence WHERE raw_turn_id IN ({placeholders}) "
                        "AND candidate_id IS NOT NULL AND candidate_id<>?",
                        [*raw_ids, candidate_id],
                    )
                ]
            dream_run_ids: set[str] = set()
            if privacy:
                references = [("candidate", candidate_id)]
                references.extend(("raw_turn", raw_id) for raw_id in raw_ids)
                for record_type, record_id in references:
                    dream_run_ids.update(
                        row[0]
                        for row in conn.execute(
                            "SELECT DISTINCT mc.dream_run_id FROM model_calls mc "
                            "JOIN model_call_records mcr ON mcr.call_id=mc.id "
                            "WHERE mcr.record_type=? AND mcr.record_id=? "
                            "AND mc.dream_run_id IS NOT NULL",
                            (record_type, record_id),
                        )
                    )
                for call in conn.execute(
                    "SELECT dream_run_id,request_json,response_json FROM model_calls "
                    "WHERE dream_run_id IS NOT NULL"
                ):
                    stored = f"{call['request_json']}\n{call['response_json'] or ''}"
                    if candidate["content"] in stored:
                        dream_run_ids.add(call["dream_run_id"])
                for run_id in dream_run_ids:
                    conn.execute("DELETE FROM dream_runs WHERE id=?", (run_id,))
            conn.execute(
                "DELETE FROM recall_events WHERE record_id=? AND source='candidate'",
                (candidate_id,),
            )
            conn.execute(
                "DELETE FROM embeddings WHERE record_id=? AND source='candidate'", (candidate_id,)
            )
            conn.execute(
                "DELETE FROM search_fts WHERE record_id=? AND source='candidate'", (candidate_id,)
            )
            conn.execute(
                "DELETE FROM model_call_records WHERE record_type='candidate' AND record_id=?",
                (candidate_id,),
            )
            conn.execute("DELETE FROM candidates WHERE id=?", (candidate_id,))
            if privacy and raw_ids:
                conn.executemany("DELETE FROM raw_turns WHERE id=?", [(item,) for item in raw_ids])
                for affected_id in affected_candidate_ids:
                    evidence_days = self._count_evidence_days(
                        conn, affected_id, timezone_name
                    )
                    conn.execute(
                        "UPDATE candidates SET evidence_days=? WHERE id=?",
                        (evidence_days, affected_id),
                    )
            conn.execute(
                "INSERT INTO audit_events(action,record_id,created_at) VALUES(?,?,?)",
                ("purge-candidate" if privacy else "cleanup-candidate", candidate_id, utc_now()),
            )
        return {
            "candidates": 1,
            "raw_turns": len(raw_ids) if privacy else 0,
            "dream_runs": len(dream_run_ids),
        }

    def retention_cleanup(
        self,
        raw_days: int,
        model_days: int,
        candidate_inactive_days: int = 14,
        candidate_expired_days: int = 30,
        rejected_candidate_days: int = 30,
        *,
        now: datetime | None = None,
        timezone_name: str = "system",
    ) -> dict[str, int]:
        current = now or datetime.now(UTC)
        raw_cutoff = (current - timedelta(days=raw_days)).isoformat()
        model_cutoff = (current - timedelta(days=model_days)).isoformat()
        inactive_cutoff = (current - timedelta(days=candidate_inactive_days)).isoformat()
        expired_cutoff = (current - timedelta(days=candidate_expired_days)).isoformat()
        rejected_cutoff = (current - timedelta(days=rejected_candidate_days)).isoformat()
        current_iso = current.isoformat(timespec="seconds")
        with self.transaction(immediate=True) as conn:
            expiring_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM candidates WHERE status='pending' "
                    "AND coalesce(last_activity_at,last_seen_at) < ?",
                    (inactive_cutoff,),
                )
            ]
            for candidate_id in expiring_ids:
                reason = f"{candidate_inactive_days} 天无活动自动过期"
                conn.execute(
                    "UPDATE candidates SET status='expired',expired_at=?,rem_reason=coalesce(rem_reason,?) "
                    "WHERE id=? AND status='pending'",
                    (current_iso, reason, candidate_id),
                )
                self._add_event(
                    conn,
                    "candidate_expired",
                    candidate_id=candidate_id,
                    occurred_at=current_iso,
                    data={"reason": reason, "source": "retention"},
                )
            expired = len(expiring_ids)
            purge_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM candidates WHERE "
                    "(status='expired' AND expired_at IS NOT NULL AND expired_at < ?) OR "
                    "(status='rejected' AND rejected_at IS NOT NULL AND rejected_at < ?)",
                    (expired_cutoff, rejected_cutoff),
                )
            ]
            for candidate_id in purge_ids:
                conn.execute(
                    "DELETE FROM recall_events WHERE record_id=? AND source='candidate'",
                    (candidate_id,),
                )
                conn.execute(
                    "DELETE FROM embeddings WHERE record_id=? AND source='candidate'", (candidate_id,)
                )
                conn.execute(
                    "DELETE FROM search_fts WHERE record_id=? AND source='candidate'", (candidate_id,)
                )
                conn.execute(
                    "DELETE FROM model_call_records WHERE record_type='candidate' AND record_id=?",
                    (candidate_id,),
                )
                conn.execute("DELETE FROM candidates WHERE id=?", (candidate_id,))
            raw = conn.execute("DELETE FROM raw_turns WHERE observed_at < ?", (raw_cutoff,)).rowcount
            remaining_ids = [row[0] for row in conn.execute("SELECT id FROM candidates")]
            for candidate_id in remaining_ids:
                days = self._count_evidence_days(conn, candidate_id, timezone_name)
                conn.execute(
                    "UPDATE candidates SET evidence_days=? WHERE id=?",
                    (days, candidate_id),
                )
            calls = conn.execute("DELETE FROM model_calls WHERE created_at < ?", (model_cutoff,)).rowcount
            traces = conn.execute("DELETE FROM recall_events WHERE created_at < ?", (model_cutoff,)).rowcount
        return {
            "raw_turns": raw,
            "model_calls": calls,
            "recall_events": traces,
            "expired_candidates": expired,
            "purged_candidates": len(purge_ids),
        }

    def acquire_lease(self, name: str, owner: str, ttl_seconds: int) -> bool:
        now = datetime.now(UTC)
        expires = (now + timedelta(seconds=ttl_seconds)).isoformat()
        with self.transaction(immediate=True) as conn:
            row = conn.execute("SELECT owner,expires_at FROM leases WHERE name=?", (name,)).fetchone()
            if row and datetime.fromisoformat(row["expires_at"]) > now and row["owner"] != owner:
                return False
            conn.execute(
                "INSERT INTO leases(name,owner,expires_at) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at",
                (name, owner, expires),
            )
        return True

    def release_lease(self, name: str, owner: str) -> None:
        with self.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM leases WHERE name=? AND owner=?", (name, owner))

    def backup(self, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        source = self.connect()
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()

    def maintain(self, *, vacuum: bool = False) -> dict[str, Any]:
        conn = self.connect()
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("ANALYZE")
            if vacuum:
                conn.execute("VACUUM")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return {"integrity": integrity, "vacuumed": vacuum}
        finally:
            conn.close()

    def _memory_from_row(self, row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"],
            content=row["content"],
            kind=row["kind"],
            status=row["status"],
            origin=row["origin"],
            confidence=float(row["confidence"]),
            importance=float(row["importance"]),
            sensitive=bool(row["sensitive"]),
            valid_until=row["valid_until"],
            supersedes_id=row["supersedes_id"],
            content_hash=row["content_hash"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _candidate_from_row(self, row: sqlite3.Row) -> CandidateRecord:
        last_seen_at = row["last_seen_at"]
        return CandidateRecord(
            id=row["id"],
            content=row["content"],
            kind=row["kind"],
            status=row["status"],
            model_confidence=float(row["model_confidence"]),
            sensitive=bool(row["sensitive"]),
            score=float(row["score"]),
            recall_count=int(row["recall_count"]),
            unique_query_count=int(row["unique_query_count"]),
            evidence_days=int(row["evidence_days"]),
            first_seen_at=row["first_seen_at"],
            last_seen_at=last_seen_at,
            last_activity_at=row["last_activity_at"] or last_seen_at,
            last_recalled_at=row["last_recalled_at"],
            expired_at=row["expired_at"],
            rejected_at=row["rejected_at"],
            promoted_at=row["promoted_at"],
            promotion_origin=row["promotion_origin"],
            rem_status=row["rem_status"] or "unreviewed",
            rem_reason=row["rem_reason"],
            rem_reviewed_at=row["rem_reviewed_at"],
            score_components=json.loads(row["score_components"] or "{}"),
            conflict_memory_id=row["conflict_memory_id"],
            conflict_reason=row["conflict_reason"],
        )
