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

from .models import CandidateRecord, MEMORY_KINDS, MemoryRecord

SCHEMA_VERSION = 2


class _ClosingConnection(sqlite3.Connection):
    """Make `with db.connect()` commit/rollback and release the OS handle."""

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc, traceback))
        finally:
            self.close()


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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
                    evidence_days INTEGER NOT NULL DEFAULT 1,
                    conflict_memory_id TEXT REFERENCES memories(id) ON DELETE SET NULL,
                    conflict_reason TEXT,
                    content_hash TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
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
            conn.execute("UPDATE schema_meta SET version=?", (SCHEMA_VERSION,))

    def default_settings(self) -> dict[str, Any]:
        return {
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
            },
            "retention": {"raw_turn_days": 30, "model_call_days": 30, "backup_count": 7},
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
            if supersedes_id:
                conn.execute(
                    "UPDATE memories SET status='superseded', updated_at=? WHERE id=?", (now, supersedes_id)
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
            row = conn.execute("SELECT * FROM memories WHERE id=?", (record_id,)).fetchone()
        return self._memory_from_row(row)

    def set_memory_status(self, record_id: str, status: str) -> None:
        if status not in {"active", "superseded", "trashed"}:
            raise ValueError(status)
        with self.transaction(immediate=True) as conn:
            changed = conn.execute(
                "UPDATE memories SET status=?, updated_at=? WHERE id=?",
                (status, utc_now(), record_id),
            ).rowcount
            if not changed:
                raise KeyError(record_id)

    def purge_memory(self, record_id: str) -> dict[str, int]:
        with self.transaction(immediate=True) as conn:
            memory = conn.execute(
                "SELECT id,content FROM memories WHERE id=?", (record_id,)
            ).fetchone()
            if not memory:
                raise KeyError(record_id)
            candidate_rows = conn.execute(
                "SELECT DISTINCT c.id,c.content FROM candidates c "
                "JOIN evidence e ON e.candidate_id=c.id WHERE e.memory_id=?",
                (record_id,),
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
    ) -> CandidateRecord:
        if kind not in MEMORY_KINDS:
            kind = "fact"
        digest = content_hash(content)
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM candidates WHERE content_hash=? AND status='pending'", (digest,)
            ).fetchone()
            if row:
                candidate_id = row["id"]
                conn.execute(
                    "UPDATE candidates SET last_seen_at=?, model_confidence=max(model_confidence, ?) WHERE id=?",
                    (now, confidence, candidate_id),
                )
            else:
                candidate_id = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO candidates(
                        id,content,kind,status,model_confidence,sensitive,score,score_components,
                        content_hash,first_seen_at,last_seen_at
                    ) VALUES(?,?,?,'pending',?,?,0,'{}',?,?,?)""",
                    (candidate_id, content.strip(), kind, confidence, int(sensitive), digest, now, now),
                )
            if raw_turn_id:
                exists = conn.execute(
                    "SELECT 1 FROM evidence WHERE candidate_id=? AND raw_turn_id=?",
                    (candidate_id, raw_turn_id),
                ).fetchone()
                if not exists:
                    conn.execute(
                        "INSERT INTO evidence(candidate_id,raw_turn_id,excerpt,role,observed_at) VALUES(?,?,?,?,?)",
                        (
                            candidate_id,
                            raw_turn_id,
                            excerpt[:1000],
                            "conversation",
                            observed_at or now,
                        ),
                    )
            evidence_days = conn.execute(
                "SELECT COUNT(DISTINCT substr(observed_at,1,10)) FROM evidence WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE candidates SET evidence_days=max(1, ?) WHERE id=?",
                (evidence_days, candidate_id),
            )
            row = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        return self._candidate_from_row(row)

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
    ) -> None:
        if not reviewed_ids:
            return
        reviewed = set(reviewed_ids)
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
                conn.execute(
                    "UPDATE candidates SET conflict_memory_id=?,conflict_reason=? "
                    "WHERE id=? AND status='pending'",
                    (memory_id, reason, candidate_id),
                )

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
    ) -> MemoryRecord:
        with self.connect() as conn:
            candidate = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if not candidate:
            raise KeyError(candidate_id)
        memory = self.add_memory(
            edited_content or candidate["content"],
            kind=candidate["kind"],
            origin=origin,
            confidence=float(candidate["model_confidence"]),
            sensitive=bool(candidate["sensitive"]),
            supersedes_id=candidate["conflict_memory_id"],
        )
        with self.transaction(immediate=True) as conn:
            conn.execute("UPDATE candidates SET status='promoted' WHERE id=?", (candidate_id,))
            conn.execute(
                "UPDATE evidence SET memory_id=? WHERE candidate_id=?", (memory.id, candidate_id)
            )
            conn.execute(
                "INSERT OR IGNORE INTO model_call_records(call_id,record_type,record_id) "
                "SELECT call_id,'memory',? FROM model_call_records "
                "WHERE record_type='candidate' AND record_id=?",
                (memory.id, candidate_id),
            )
        return memory

    def reject_candidate(self, candidate_id: str) -> None:
        with self.transaction(immediate=True) as conn:
            changed = conn.execute(
                "UPDATE candidates SET status='rejected' WHERE id=?", (candidate_id,)
            ).rowcount
            if not changed:
                raise KeyError(candidate_id)

    def retention_cleanup(self, raw_days: int, model_days: int) -> dict[str, int]:
        raw_cutoff = (datetime.now(UTC) - timedelta(days=raw_days)).isoformat()
        model_cutoff = (datetime.now(UTC) - timedelta(days=model_days)).isoformat()
        with self.transaction(immediate=True) as conn:
            raw = conn.execute("DELETE FROM raw_turns WHERE observed_at < ?", (raw_cutoff,)).rowcount
            calls = conn.execute("DELETE FROM model_calls WHERE created_at < ?", (model_cutoff,)).rowcount
            traces = conn.execute("DELETE FROM recall_events WHERE created_at < ?", (model_cutoff,)).rowcount
        return {"raw_turns": raw, "model_calls": calls, "recall_events": traces}

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
            last_seen_at=row["last_seen_at"],
            score_components=json.loads(row["score_components"] or "{}"),
            conflict_memory_id=row["conflict_memory_id"],
            conflict_reason=row["conflict_reason"],
        )
