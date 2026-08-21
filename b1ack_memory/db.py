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

from .models import AuditRun, CandidateRecord, MEMORY_KINDS, MemoryRecord, ReviewItem
from .security import secure_directory, secure_file

SCHEMA_VERSION = 8


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


def eligible_memory_predicate(alias: str = "m", *, now: str | None = None) -> tuple[str, list[str]]:
    """Return the one authoritative SQL predicate for injectable memories."""
    prefix = f"{alias}." if alias else ""
    current = now or utc_now()
    predicate = (
        f"{prefix}status='active' AND {prefix}temporal_status='current' "
        f"AND ({prefix}valid_until IS NULL OR datetime({prefix}valid_until)>datetime(?)) "
        f"AND ({prefix}valid_from IS NULL OR CASE WHEN length({prefix}valid_from)=10 "
        f"THEN date({prefix}valid_from)<=date(?) ELSE datetime({prefix}valid_from)<=datetime(?) END) "
        f"AND ({prefix}valid_to IS NULL OR CASE WHEN length({prefix}valid_to)=10 "
        f"THEN date({prefix}valid_to)>=date(?) ELSE datetime({prefix}valid_to)>=datetime(?) END)"
    )
    return predicate, [current, current, current, current, current]


class MemoryDatabase:
    def __init__(self, path: Path):
        self.path = path
        secure_directory(self.path.parent)
        self._local = threading.local()
        self.migrate()
        self._secure_database_files()

    def _secure_database_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            secure_file(Path(f"{self.path}{suffix}"))

    def schema_version(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT version FROM schema_meta").fetchone()[0])

    def _backup_before_schema_upgrade(self) -> None:
        """Create an online backup immediately before a supported schema upgrade."""
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return
        try:
            with contextlib.closing(sqlite3.connect(self.path)) as source:
                table = source.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
                ).fetchone()
                if not table:
                    return
                version = int(source.execute("SELECT version FROM schema_meta").fetchone()[0])
                if version not in {4, 5, 6, 7}:
                    return
                backup_dir = self.path.parent / "backups"
                secure_directory(backup_dir)
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
                target = backup_dir / f"{stamp}-pre-schema-v{version + 1}.db"
                with contextlib.closing(sqlite3.connect(target)) as destination:
                    source.backup(destination)
                secure_file(target)
        except (OSError, sqlite3.Error, TypeError, ValueError) as error:
            raise RuntimeError(
                f"Unable to create pre-schema upgrade backup: {error}"
            ) from error

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
        self._secure_database_files()
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
        self._backup_before_schema_upgrade()
        with self.transaction(immediate=True) as conn:
            self._execute_schema(conn, """
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
                    valid_from TEXT,
                    valid_to TEXT,
                    temporal_status TEXT NOT NULL DEFAULT 'current',
                    temporal_reason TEXT,
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
                    secret_redacted INTEGER NOT NULL DEFAULT 0,
                    subject_id TEXT,
                    ingest_status TEXT NOT NULL DEFAULT 'pending',
                    ingest_attempts INTEGER NOT NULL DEFAULT 0,
                    ingest_cursor INTEGER NOT NULL DEFAULT 0,
                    last_ingest_error TEXT,
                    next_retry_at TEXT
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
                    promoted_memory_id TEXT REFERENCES memories(id) ON DELETE CASCADE,
                    rem_status TEXT NOT NULL DEFAULT 'unreviewed',
                    rem_reason TEXT,
                    rem_reviewed_at TEXT,
                    admission_state TEXT NOT NULL DEFAULT 'admitted',
                    source_type TEXT NOT NULL DEFAULT 'dream_user',
                    admission_reason TEXT,
                    subject_id TEXT
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

                -- v0.6: these tables are the local, non-injectable recent layer.
                -- They deliberately do not reference Hermes' native Markdown files.
                CREATE TABLE IF NOT EXISTS recent_signals (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'fact',
                    status TEXT NOT NULL DEFAULT 'active',
                    confidence REAL NOT NULL DEFAULT 0.0,
                    strength INTEGER NOT NULL DEFAULT 1,
                    sensitive INTEGER NOT NULL DEFAULT 0,
                    content_hash TEXT NOT NULL,
                    subject_id TEXT REFERENCES subjects(id) ON DELETE SET NULL,
                    source_raw_turn_id TEXT REFERENCES raw_turns(id) ON DELETE SET NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_recent_signals_active
                    ON recent_signals(status, expires_at, last_seen_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_recent_signals_live_hash
                    ON recent_signals(content_hash) WHERE status='active';

                CREATE TABLE IF NOT EXISTS daily_memories (
                    id TEXT PRIMARY KEY,
                    memory_date TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    subject_id TEXT REFERENCES subjects(id) ON DELETE SET NULL,
                    content TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(memory_date, scope_key)
                );
                CREATE INDEX IF NOT EXISTS idx_daily_memories_active
                    ON daily_memories(status, memory_date DESC, scope_key);

                CREATE TABLE IF NOT EXISTS recent_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id TEXT REFERENCES recent_signals(id) ON DELETE CASCADE,
                    daily_memory_id TEXT REFERENCES daily_memories(id) ON DELETE CASCADE,
                    raw_turn_id TEXT REFERENCES raw_turns(id) ON DELETE SET NULL,
                    recall_event_id INTEGER REFERENCES recall_events(id) ON DELETE SET NULL,
                    excerpt TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT 'user',
                    observed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_recent_evidence_signal ON recent_evidence(signal_id);
                CREATE INDEX IF NOT EXISTS idx_recent_evidence_daily ON recent_evidence(daily_memory_id);
                CREATE INDEX IF NOT EXISTS idx_recent_evidence_raw ON recent_evidence(raw_turn_id);

                CREATE TABLE IF NOT EXISTS rem_reflections (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    reflection_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    confidence REAL NOT NULL DEFAULT 0.0,
                    sensitive INTEGER NOT NULL DEFAULT 0,
                    signal_ids_json TEXT NOT NULL DEFAULT '[]',
                    daily_memory_ids_json TEXT NOT NULL DEFAULT '[]',
                    dream_run_id TEXT REFERENCES dream_runs(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    handled_at TEXT,
                    outcome TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_rem_reflections_active
                    ON rem_reflections(status, created_at DESC);

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
                    created_at TEXT NOT NULL,
                    project_id TEXT,
                    project_confidence REAL,
                    project_reason TEXT
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
                    admitted_count INTEGER NOT NULL DEFAULT 0,
                    observed_count INTEGER NOT NULL DEFAULT 0,
                    discarded_count INTEGER NOT NULL DEFAULT 0,
                    review_count INTEGER NOT NULL DEFAULT 0,
                    work_item_count INTEGER NOT NULL DEFAULT 0,
                    assignment_count INTEGER NOT NULL DEFAULT 0,
                    summary_count INTEGER NOT NULL DEFAULT 0,
                    projection_count INTEGER NOT NULL DEFAULT 0,
                    blocked_count INTEGER NOT NULL DEFAULT 0,
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

                CREATE TABLE IF NOT EXISTS admission_decisions (
                    id TEXT PRIMARY KEY,
                    disposition TEXT NOT NULL,
                    content TEXT NOT NULL,
                    evidence_quote TEXT,
                    reason TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.0,
                    raw_turn_id TEXT REFERENCES raw_turns(id) ON DELETE SET NULL,
                    candidate_id TEXT REFERENCES candidates(id) ON DELETE SET NULL,
                    dream_run_id TEXT REFERENCES dream_runs(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_admission_decisions_created
                    ON admission_decisions(created_at);
                CREATE INDEX IF NOT EXISTS idx_admission_decisions_candidate
                    ON admission_decisions(candidate_id);

                CREATE TABLE IF NOT EXISTS memory_audit_runs (
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    status TEXT NOT NULL,
                    checked_count INTEGER NOT NULL DEFAULT 0,
                    issue_count INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_memory_audit_runs_scope_time
                    ON memory_audit_runs(scope, started_at DESC);

                CREATE TABLE IF NOT EXISTS memory_review_items (
                    id TEXT PRIMARY KEY,
                    issue_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    proposed_action TEXT NOT NULL,
                    proposed_content TEXT,
                    reason TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.0,
                    candidate_id TEXT REFERENCES candidates(id) ON DELETE CASCADE,
                    related_candidate_id TEXT REFERENCES candidates(id) ON DELETE CASCADE,
                    primary_memory_id TEXT REFERENCES memories(id) ON DELETE CASCADE,
                    related_memory_id TEXT REFERENCES memories(id) ON DELETE CASCADE,
                    source TEXT NOT NULL,
                    basis_hash TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    dream_run_id TEXT REFERENCES dream_runs(id) ON DELETE SET NULL,
                    audit_run_id TEXT REFERENCES memory_audit_runs(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    resolved_at TEXT,
                    resolution TEXT,
                    subject_id TEXT,
                    queue TEXT NOT NULL DEFAULT 'decision',
                    proposal_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_memory_review_items_status
                    ON memory_review_items(status, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_memory_review_items_candidate
                    ON memory_review_items(candidate_id, status);
                CREATE INDEX IF NOT EXISTS idx_memory_review_items_memory
                    ON memory_review_items(primary_memory_id, related_memory_id, status);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_review_items_fingerprint
                    ON memory_review_items(fingerprint);

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

                CREATE TABLE IF NOT EXISTS subjects (
                    id TEXT PRIMARY KEY,
                    subject_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    slug TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'active',
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_subjects_type_status
                    ON subjects(subject_type,status,updated_at DESC);

                CREATE TABLE IF NOT EXISTS subject_aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject_id TEXT NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
                    alias TEXT NOT NULL,
                    alias_normalized TEXT NOT NULL,
                    alias_type TEXT NOT NULL DEFAULT 'name',
                    created_at TEXT NOT NULL,
                    UNIQUE(subject_id,alias_normalized,alias_type)
                );
                CREATE INDEX IF NOT EXISTS idx_subject_alias_lookup
                    ON subject_aliases(alias_normalized,alias_type);

                CREATE TABLE IF NOT EXISTS subject_relations (
                    id TEXT PRIMARY KEY,
                    source_subject_id TEXT NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
                    relation_type TEXT NOT NULL,
                    target_subject_id TEXT NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_subject_id,relation_type,target_subject_id)
                );

                CREATE TABLE IF NOT EXISTS subject_links (
                    id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
                    object_type TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    assignment_status TEXT NOT NULL DEFAULT 'confirmed',
                    method TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(subject_id,object_type,object_id)
                );
                CREATE INDEX IF NOT EXISTS idx_subject_links_object
                    ON subject_links(object_type,object_id,assignment_status);

                CREATE TABLE IF NOT EXISTS work_items (
                    id TEXT PRIMARY KEY,
                    item_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'suggested',
                    confirmed INTEGER NOT NULL DEFAULT 0,
                    confidence REAL NOT NULL DEFAULT 0.0,
                    subject_id TEXT REFERENCES subjects(id) ON DELETE SET NULL,
                    raw_turn_id TEXT REFERENCES raw_turns(id) ON DELETE SET NULL,
                    admission_decision_id TEXT REFERENCES admission_decisions(id) ON DELETE SET NULL,
                    evidence_quote TEXT,
                    source TEXT NOT NULL DEFAULT 'dream_observe',
                    expires_at TEXT,
                    resolved_at TEXT,
                    promoted_memory_id TEXT REFERENCES memories(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_work_items_live_hash
                    ON work_items(content_hash,coalesce(subject_id,''))
                    WHERE status IN ('suggested','active');
                CREATE INDEX IF NOT EXISTS idx_work_items_subject_status
                    ON work_items(subject_id,status,item_type,updated_at DESC);

                CREATE TABLE IF NOT EXISTS work_item_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    work_item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
                    snapshot_json TEXT NOT NULL,
                    changed_at TEXT NOT NULL,
                    change_reason TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS summary_versions (
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    subject_id TEXT REFERENCES subjects(id) ON DELETE CASCADE,
                    content TEXT NOT NULL,
                    source_ids_json TEXT NOT NULL DEFAULT '[]',
                    change_reason TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL DEFAULT 'automatic',
                    status TEXT NOT NULL DEFAULT 'current',
                    source_revision TEXT NOT NULL DEFAULT '',
                    newest_source_updated_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_summary_versions_target
                    ON summary_versions(scope,subject_id,status,created_at DESC);

                CREATE TABLE IF NOT EXISTS session_subject_affinity (
                    session_id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
                    confidence REAL NOT NULL,
                    confirmed INTEGER NOT NULL DEFAULT 0,
                    method TEXT NOT NULL,
                    workspace TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS projection_jobs (
                    id TEXT PRIMARY KEY,
                    projection_type TEXT NOT NULL,
                    target_id TEXT,
                    revision TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(projection_type,target_id,revision)
                );
                CREATE INDEX IF NOT EXISTS idx_projection_jobs_status
                    ON projection_jobs(status,created_at);

                CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
                    record_id UNINDEXED,
                    source UNINDEXED,
                    pool UNINDEXED,
                    content,
                    search_text,
                    tokenize='unicode61 remove_diacritics 2'
                );
                """)
            current = int(conn.execute("SELECT version FROM schema_meta").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise RuntimeError(f"Database schema {current} is newer than supported {SCHEMA_VERSION}")
            memory_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(memories)").fetchall()
            }
            for column, definition in {
                "valid_from": "TEXT",
                "valid_to": "TEXT",
                "temporal_status": "TEXT NOT NULL DEFAULT 'current'",
                "temporal_reason": "TEXT",
            }.items():
                if column not in memory_columns:
                    conn.execute(f"ALTER TABLE memories ADD COLUMN {column} {definition}")
            conn.execute(
                "UPDATE memories SET temporal_status='current' "
                "WHERE temporal_status IS NULL OR temporal_status=''"
            )
            for table, migrations in {
                "raw_turns": {"subject_id": "TEXT"},
                "recall_events": {
                    "project_id": "TEXT",
                    "project_confidence": "REAL",
                    "project_reason": "TEXT",
                },
                "memory_review_items": {
                    "subject_id": "TEXT",
                    "queue": "TEXT NOT NULL DEFAULT 'decision'",
                    "proposal_json": "TEXT NOT NULL DEFAULT '{}'",
                },
                "summary_versions": {
                    "source_revision": "TEXT NOT NULL DEFAULT ''",
                    "newest_source_updated_at": "TEXT",
                },
                "evidence": {
                    "recent_signal_id": "TEXT REFERENCES recent_signals(id) ON DELETE SET NULL",
                    "daily_memory_id": "TEXT REFERENCES daily_memories(id) ON DELETE SET NULL",
                },
            }.items():
                columns = {
                    row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                for column, definition in migrations.items():
                    if column not in columns:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            raw_turn_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(raw_turns)").fetchall()
            }
            for column, definition in {
                "ingest_status": "TEXT NOT NULL DEFAULT 'pending'",
                "ingest_attempts": "INTEGER NOT NULL DEFAULT 0",
                "ingest_cursor": "INTEGER NOT NULL DEFAULT 0",
                "last_ingest_error": "TEXT",
                "next_retry_at": "TEXT",
            }.items():
                if column not in raw_turn_columns:
                    conn.execute(f"ALTER TABLE raw_turns ADD COLUMN {column} {definition}")
            conn.execute(
                "UPDATE raw_turns SET ingest_status=CASE WHEN ingested_at IS NULL THEN 'pending' "
                "ELSE 'processed' END WHERE ingest_status IS NULL OR ingest_status=''"
            )
            fts_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(search_fts)").fetchall()
            }
            if "pool" not in fts_columns:
                conn.execute("DROP TABLE search_fts")
                conn.execute(
                    "CREATE VIRTUAL TABLE search_fts USING fts5("
                    "record_id UNINDEXED,source UNINDEXED,pool UNINDEXED,content,search_text,"
                    "tokenize='unicode61 remove_diacritics 2')"
                )
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
                "promoted_memory_id": "TEXT REFERENCES memories(id) ON DELETE CASCADE",
                "rem_status": "TEXT NOT NULL DEFAULT 'unreviewed'",
                "rem_reason": "TEXT",
                "rem_reviewed_at": "TEXT",
                "admission_state": "TEXT NOT NULL DEFAULT 'admitted'",
                "source_type": "TEXT NOT NULL DEFAULT 'dream_user'",
                "admission_reason": "TEXT",
                "subject_id": "TEXT",
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
            for column in (
                "merged_count",
                "filtered_count",
                "expired_count",
                "admitted_count",
                "observed_count",
                "discarded_count",
                "review_count",
                "work_item_count",
                "assignment_count",
                "summary_count",
                "projection_count",
                "blocked_count",
                "recent_count",
                "daily_count",
                "reflection_count",
                "updated_memory_count",
            ):
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
            if current < 5:
                self._migrate_promoted_memory_links(conn)
            if current < 6:
                conn.execute("DROP INDEX IF EXISTS idx_candidates_promoted_memory")
                conn.execute(
                    "UPDATE candidates SET admission_state='legacy_review',source_type='legacy',"
                    "admission_reason=coalesce(admission_reason,'升级至 v0.4.0 后需重新准入') "
                    "WHERE status='pending'"
                )
            if current < 7:
                legacy_observations = conn.execute(
                    "SELECT ad.id,ad.content,ad.confidence,ad.raw_turn_id,ad.evidence_quote,"
                    "ad.created_at FROM admission_decisions ad "
                    "JOIN raw_turns rt ON rt.id=ad.raw_turn_id "
                    "WHERE ad.disposition='observe'",
                ).fetchall()
                for observation in legacy_observations:
                    work_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"b1ack:observe:{observation['id']}"))
                    created = observation["created_at"]
                    expires = (
                        datetime.fromisoformat(created) + timedelta(days=14)
                    ).isoformat(timespec="seconds")
                    legacy_status = "suggested" if expires > utc_now() else "expired"
                    conn.execute(
                        "INSERT OR IGNORE INTO work_items("
                        "id,item_type,content,content_hash,status,confirmed,confidence,raw_turn_id,"
                        "admission_decision_id,evidence_quote,source,expires_at,created_at,updated_at"
                        ") VALUES(?, 'proposal', ?, ?, ?, 0, ?, ?, ?, ?, 'migration_v7', ?, ?, ?)",
                        (
                            work_id,
                            observation["content"],
                            content_hash(observation["content"]),
                            legacy_status,
                            float(observation["confidence"]),
                            observation["raw_turn_id"],
                            observation["id"],
                            observation["evidence_quote"],
                            expires,
                            created,
                            created,
                        ),
                    )
                revision = content_hash("schema-v7:initial-projection")
                conn.execute(
                    "INSERT OR IGNORE INTO projection_jobs("
                    "id,projection_type,target_id,revision,status,created_at,updated_at"
                    ") VALUES(?, 'all', '', ?, 'pending', ?, ?)",
                    (str(uuid.uuid5(uuid.NAMESPACE_URL, "b1ack:schema-v7:initial-projection")), revision, utc_now(), utc_now()),
                )
            if current < 8:
                # v0.5 candidates remain available for audit, export and privacy
                # deletion, but must never enter the v0.6 Dream pipeline again.
                conn.execute(
                    "UPDATE candidates SET admission_state='legacy_history' "
                    "WHERE admission_state<>'legacy_history'"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_candidates_promoted_memory "
                "ON candidates(promoted_memory_id) WHERE promoted_memory_id IS NOT NULL"
            )
            conn.execute("UPDATE schema_meta SET version=?", (SCHEMA_VERSION,))

    @staticmethod
    def _execute_schema(conn: sqlite3.Connection, script: str) -> None:
        """Execute DDL without sqlite3.executescript's implicit pre-COMMIT."""
        statement = ""
        for line in script.splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                sql = statement.strip()
                if sql:
                    conn.execute(sql)
                statement = ""
        if statement.strip():
            raise sqlite3.OperationalError("Incomplete schema statement")

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
                "recent_signal_days": 14,
                "daily_memory_days": 30,
            },
            "recall": {"limit": 5, "durable_limit": 6, "max_context_chars": 4000},
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

    def _migrate_promoted_memory_links(self, conn: sqlite3.Connection) -> None:
        """Link legacy promoted candidates only when one memory is provably unique."""
        used_memory_ids = {
            row[0]
            for row in conn.execute(
                "SELECT promoted_memory_id FROM candidates WHERE promoted_memory_id IS NOT NULL"
            )
        }

        def unique_available(rows: list[sqlite3.Row]) -> str | None:
            ids = {str(row[0]) for row in rows if row[0]}
            if len(ids) != 1:
                return None
            memory_id = next(iter(ids))
            return None if memory_id in used_memory_ids else memory_id

        promoted_rows = conn.execute(
            "SELECT id,content,content_hash,promotion_origin,promoted_at "
            "FROM candidates WHERE status='promoted' ORDER BY promoted_at,id"
        ).fetchall()
        for candidate in promoted_rows:
            candidate_id = candidate["id"]
            queries: list[tuple[str, tuple[Any, ...]]] = [
                (
                    "SELECT DISTINCT me.memory_id FROM memory_events me "
                    "JOIN memories m ON m.id=me.memory_id "
                    "WHERE me.candidate_id=? AND me.event_type='candidate_promoted' "
                    "AND me.memory_id IS NOT NULL",
                    (candidate_id,),
                ),
                (
                    "SELECT DISTINCT e.memory_id FROM evidence e "
                    "JOIN memories m ON m.id=e.memory_id "
                    "WHERE e.candidate_id=? AND e.memory_id IS NOT NULL",
                    (candidate_id,),
                ),
                (
                    "SELECT DISTINCT memory_ref.record_id FROM model_call_records candidate_ref "
                    "JOIN model_call_records memory_ref ON memory_ref.call_id=candidate_ref.call_id "
                    "AND memory_ref.record_type='memory' "
                    "JOIN memories m ON m.id=memory_ref.record_id "
                    "WHERE candidate_ref.record_type='candidate' AND candidate_ref.record_id=?",
                    (candidate_id,),
                ),
            ]
            memory_id = None
            for sql, args in queries:
                memory_id = unique_available(conn.execute(sql, args).fetchall())
                if memory_id:
                    break
            if not memory_id and candidate["promoted_at"] and candidate["promotion_origin"]:
                memory_id = unique_available(
                    conn.execute(
                        "SELECT id FROM memories WHERE content_hash=? AND origin=? "
                        "AND abs((julianday(created_at)-julianday(?))*86400)<=600",
                        (
                            candidate["content_hash"],
                            candidate["promotion_origin"],
                            candidate["promoted_at"],
                        ),
                    ).fetchall()
                )
            if memory_id:
                conn.execute(
                    "UPDATE candidates SET promoted_memory_id=? WHERE id=?",
                    (memory_id, candidate_id),
                )
                used_memory_ids.add(memory_id)

        orphan_rows = conn.execute(
            "SELECT id,content_hash FROM candidates WHERE status='promoted' AND promoted_memory_id IS NULL"
        ).fetchall()
        if not orphan_rows:
            return
        now = utc_now()
        for orphan in orphan_rows:
            orphan_id = str(orphan["id"])
            pending = conn.execute(
                "SELECT id FROM candidates WHERE content_hash=? AND status='pending' LIMIT 1",
                (orphan["content_hash"],),
            ).fetchone()
            if pending:
                canonical_id = str(pending["id"])
                conn.execute("UPDATE evidence SET candidate_id=? WHERE candidate_id=?", (canonical_id, orphan_id))
                conn.execute("UPDATE memory_events SET candidate_id=? WHERE candidate_id=?", (canonical_id, orphan_id))
                conn.execute("UPDATE admission_decisions SET candidate_id=? WHERE candidate_id=?", (canonical_id, orphan_id))
                conn.execute("UPDATE memory_review_items SET candidate_id=? WHERE candidate_id=?", (canonical_id, orphan_id))
                conn.execute("UPDATE memory_review_items SET related_candidate_id=? WHERE related_candidate_id=?", (canonical_id, orphan_id))
                conn.execute(
                    "UPDATE recall_events SET record_id=? WHERE source='candidate' AND record_id=?",
                    (canonical_id, orphan_id),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO model_call_records(call_id,record_type,record_id) "
                    "SELECT call_id,'candidate',? FROM model_call_records "
                    "WHERE record_type='candidate' AND record_id=?",
                    (canonical_id, orphan_id),
                )
                conn.execute(
                    "DELETE FROM model_call_records WHERE record_type='candidate' AND record_id=?",
                    (orphan_id,),
                )
                for link in conn.execute(
                    "SELECT * FROM subject_links WHERE object_type='candidate' AND object_id=?", (orphan_id,)
                ).fetchall():
                    conn.execute(
                        "INSERT OR IGNORE INTO subject_links(id,subject_id,object_type,object_id,confidence,"
                        "assignment_status,method,created_at,updated_at) VALUES(?,?, 'candidate',?,?,?,?,?,?)",
                        (str(uuid.uuid4()), link["subject_id"], canonical_id, link["confidence"],
                         link["assignment_status"], "migration_merge", link["created_at"], now),
                    )
                conn.execute("DELETE FROM subject_links WHERE object_type='candidate' AND object_id=?", (orphan_id,))
                conn.execute("DELETE FROM embeddings WHERE source='candidate' AND record_id=?", (orphan_id,))
                conn.execute("DELETE FROM search_fts WHERE source='candidate' AND record_id=?", (orphan_id,))
                conn.execute("DELETE FROM candidates WHERE id=?", (orphan_id,))
                action = "migration-merge-unlinked-promoted"
            else:
                conn.execute(
                    "UPDATE candidates SET status='pending',admission_state='legacy_review',"
                    "source_type='legacy',admission_reason='Legacy promotion could not be linked safely',"
                    "promoted_at=NULL,promotion_origin=NULL,rem_status='unreviewed',rem_reason=NULL,"
                    "rem_reviewed_at=NULL WHERE id=?",
                    (orphan_id,),
                )
                action = "migration-review-unlinked-promoted"
            conn.execute(
                "INSERT INTO audit_events(action,record_id,created_at) VALUES(?,?,?)",
                (action, orphan_id, now),
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
        subject_id: str | None = None,
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
            if subject_id:
                self._link_subject_in_tx(
                    conn,
                    subject_id,
                    "memory",
                    record_id,
                    assignment_status="confirmed",
                    method="explicit_remember",
                    now=now,
                )
            if supersedes_id:
                conn.execute(
                    "UPDATE memories SET status='superseded',temporal_status='historical',"
                    "valid_to=coalesce(valid_to,?), updated_at=? WHERE id=?",
                    (now, now, supersedes_id),
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

    def update_memory(
        self,
        record_id: str,
        *,
        content: str,
        kind: str,
        valid_from: str | None = None,
        valid_to: str | None = None,
        temporal_status: str | None = None,
        temporal_reason: str | None = None,
        subject_id: str | None = None,
    ) -> MemoryRecord:
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
            next_temporal = temporal_status or old["temporal_status"]
            if next_temporal not in {"current", "historical", "disputed"}:
                raise ValueError("Unsupported temporal status")
            conn.execute(
                "UPDATE memories SET content=?,kind=?,content_hash=?,valid_from=?,valid_to=?,"
                "temporal_status=?,temporal_reason=?,updated_at=? WHERE id=?",
                (
                    content.strip(),
                    kind,
                    content_hash(content),
                    valid_from if valid_from is not None else old["valid_from"],
                    valid_to if valid_to is not None else old["valid_to"],
                    next_temporal,
                    temporal_reason if temporal_reason is not None else old["temporal_reason"],
                    now,
                    record_id,
                ),
            )
            if subject_id:
                self._link_subject_in_tx(
                    conn,
                    subject_id,
                    "memory",
                    record_id,
                    assignment_status="confirmed",
                    method="memory_edit",
                    now=now,
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
                "UPDATE memories SET status=?,temporal_status=CASE "
                "WHEN ?='superseded' THEN 'historical' "
                "WHEN ?='active' THEN 'current' ELSE temporal_status END, updated_at=? WHERE id=?",
                (status, status, status, now, record_id),
            ).rowcount
            if not changed:
                raise KeyError(record_id)
            obsolete_reviews = 0
            if status == "trashed":
                # A review whose proposed action depends on a trashed memory is
                # no longer actionable.  Do this in the same transaction as the
                # lifecycle change so it cannot remain in the review queue.
                obsolete_reviews = conn.execute(
                    "UPDATE memory_review_items SET status='obsolete',"
                    "resolution='memory_trashed',resolved_at=?,updated_at=? "
                    "WHERE status='open' AND (primary_memory_id=? OR related_memory_id=?)",
                    (now, now, record_id, record_id),
                ).rowcount
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
                data={
                    "previous_status": old["status"],
                    "status": status,
                    "obsolete_reviews": obsolete_reviews,
                },
            )

    def purge_memory(self, record_id: str) -> dict[str, int]:
        # Potentially expensive legacy model-text scanning is deliberately read-only.
        with self.connect() as scan:
            memory = scan.execute("SELECT id,content FROM memories WHERE id=?", (record_id,)).fetchone()
            if not memory:
                raise KeyError(record_id)
            candidate_rows = scan.execute(
                "SELECT DISTINCT c.id,c.content FROM candidates c "
                "LEFT JOIN evidence e ON e.candidate_id=c.id "
                "LEFT JOIN memory_events me ON me.candidate_id=c.id AND me.event_type='candidate_promoted' "
                "WHERE c.promoted_memory_id=? OR e.memory_id=? OR me.memory_id=?",
                (record_id, record_id, record_id),
            ).fetchall()
            candidate_ids = [row["id"] for row in candidate_rows]
            raw_ids = [row[0] for row in scan.execute(
                "SELECT DISTINCT raw_turn_id FROM evidence WHERE memory_id=? AND raw_turn_id IS NOT NULL",
                (record_id,),
            )]
            references = [("memory", record_id), *[("candidate", item) for item in candidate_ids], *[("raw_turn", item) for item in raw_ids]]
            dream_run_ids: set[str] = set()
            call_ids: set[str] = set()
            for record_type, linked_id in references:
                linked_calls = scan.execute(
                    "SELECT DISTINCT mc.id,mc.dream_run_id FROM model_calls mc JOIN model_call_records mcr ON mcr.call_id=mc.id "
                    "WHERE mcr.record_type=? AND mcr.record_id=?",
                    (record_type, linked_id),
                ).fetchall()
                call_ids.update(row["id"] for row in linked_calls)
                dream_run_ids.update(row["dream_run_id"] for row in linked_calls if row["dream_run_id"])
            raw_rows = [] if not raw_ids else scan.execute(
                f"SELECT user_content,assistant_content FROM raw_turns WHERE id IN ({','.join('?' for _ in raw_ids)})",
                raw_ids,
            ).fetchall()
            terms = [memory["content"], *[row["content"] for row in candidate_rows], *[value for row in raw_rows for value in row if value]]
            terms = [term for term in terms if len(term.strip()) >= 4]
            if terms:
                for call in scan.execute("SELECT dream_run_id,request_json,response_json FROM model_calls WHERE dream_run_id IS NOT NULL"):
                    stored = f"{call['request_json']}\n{call['response_json'] or ''}"
                    if any(term in stored for term in terms):
                        dream_run_ids.add(call["dream_run_id"])
        with self.transaction(immediate=True) as conn:
            if not conn.execute("SELECT 1 FROM memories WHERE id=?", (record_id,)).fetchone():
                raise KeyError(record_id)
            for run_id in dream_run_ids:
                conn.execute("DELETE FROM dream_runs WHERE id=?", (run_id,))
            for record_type, linked_id in references:
                conn.execute(
                    "DELETE FROM model_call_records WHERE record_type=? AND record_id=?",
                    (record_type, linked_id),
                )
            for candidate_id in candidate_ids:
                conn.execute(
                    "DELETE FROM subject_links WHERE object_type='candidate' AND object_id=?",
                    (candidate_id,),
                )
                conn.execute(
                    "DELETE FROM admission_decisions WHERE candidate_id=?", (candidate_id,)
                )
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
            conn.execute(
                "DELETE FROM subject_links WHERE object_type='memory' AND object_id=?",
                (record_id,),
            )
            conn.execute(
                "DELETE FROM projection_jobs WHERE target_id=?",
                (record_id,),
            )
            # Summary text is derived, but its source list can itself disclose a
            # permanently deleted identifier. Remove affected versions so the
            # next projection/summary refresh can recreate a privacy-clean view.
            conn.execute(
                "DELETE FROM summary_versions WHERE source_ids_json LIKE ?",
                (f'%\"{record_id}\"%',),
            )
            conn.execute("DELETE FROM recall_events WHERE record_id=?", (record_id,))
            conn.execute("DELETE FROM embeddings WHERE record_id=? AND source='memory'", (record_id,))
            conn.execute("DELETE FROM search_fts WHERE record_id=? AND source='memory'", (record_id,))
            deleted = conn.execute("DELETE FROM memories WHERE id=?", (record_id,)).rowcount
            if deleted != 1:
                raise KeyError(record_id)
            deleted_work_items = self._purge_work_items_for_raw_ids(conn, raw_ids)
            for raw_id in raw_ids:
                conn.execute(
                    "DELETE FROM subject_links WHERE object_type='raw_turn' AND object_id=?",
                    (raw_id,),
                )
                conn.execute("DELETE FROM raw_turns WHERE id=?", (raw_id,))
            conn.execute(
                "INSERT INTO audit_events(action,record_id,created_at) VALUES('purge',?,?)",
                (record_id, utc_now()),
            )
            for call_id in call_ids:
                conn.execute(
                    "DELETE FROM model_calls WHERE id=? AND NOT EXISTS "
                    "(SELECT 1 FROM model_call_records WHERE call_id=?)",
                    (call_id, call_id),
                )
        return {
            "memories": 1,
            "candidates": len(candidate_ids),
            "raw_turns": len(raw_ids),
            "work_items": deleted_work_items,
            "dream_runs": len(dream_run_ids),
        }

    def add_raw_turn(self, session_id: str, user: str, assistant: str, *, redacted: bool) -> str:
        record_id = str(uuid.uuid4())
        with self.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO raw_turns(id,session_id,user_content,assistant_content,observed_at,ingested_at,secret_redacted) "
                "VALUES(?,?,?,?,?,?,?)",
                (record_id, session_id, user, assistant, utc_now(), None, int(redacted)),
            )
        return record_id

    def pending_raw_turns(self, limit: int = 500) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM raw_turns WHERE ingested_at IS NULL "
                "AND ingest_status IN ('pending','retrying') "
                "AND (next_retry_at IS NULL OR next_retry_at<=?) ORDER BY observed_at LIMIT ?",
                (utc_now(), limit),
            ).fetchall()

    def mark_turns_ingested(self, ids: list[str]) -> None:
        if not ids:
            return
        with self.transaction(immediate=True) as conn:
            conn.executemany(
                "UPDATE raw_turns SET ingested_at=?,ingest_status='processed',last_ingest_error=NULL,next_retry_at=NULL,"
                "ingest_cursor=length(user_content) WHERE id=?", [(utc_now(), item) for item in ids]
            )

    def advance_turn_ingestion(self, progress: dict[str, int]) -> None:
        if not progress:
            return
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            for turn_id, cursor in progress.items():
                row = conn.execute("SELECT length(user_content) FROM raw_turns WHERE id=?", (turn_id,)).fetchone()
                if not row:
                    continue
                finished = int(cursor) >= int(row[0])
                conn.execute(
                    "UPDATE raw_turns SET ingest_cursor=?,ingest_status=?,ingested_at=?,"
                    "last_ingest_error=NULL,next_retry_at=NULL WHERE id=?",
                    (int(cursor), "processed" if finished else "pending", now if finished else None, turn_id),
                )

    def mark_turn_ingestion_failed(self, ids: list[str], error: str) -> None:
        if not ids:
            return
        with self.transaction(immediate=True) as conn:
            for turn_id in ids:
                row = conn.execute("SELECT ingest_attempts FROM raw_turns WHERE id=?", (turn_id,)).fetchone()
                if not row:
                    continue
                attempts = int(row[0]) + 1
                status = "quarantined" if attempts >= 3 else "retrying"
                retry_at = None if status == "quarantined" else (
                    datetime.now(UTC) + timedelta(minutes=2 ** attempts)
                ).isoformat(timespec="seconds")
                conn.execute(
                    "UPDATE raw_turns SET ingest_status=?,ingest_attempts=?,last_ingest_error=?,next_retry_at=? WHERE id=?",
                    (status, attempts, error[:500], retry_at, turn_id),
                )

    def ingestion_issues(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id,session_id,observed_at,ingest_status,ingest_attempts,ingest_cursor,"
                "last_ingest_error,next_retry_at,length(user_content) AS content_length "
                "FROM raw_turns WHERE ingest_status IN ('retrying','quarantined') "
                "ORDER BY observed_at DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def retry_ingestion(self, turn_id: str) -> dict[str, Any]:
        with self.transaction(immediate=True) as conn:
            changed = conn.execute(
                "UPDATE raw_turns SET ingest_status='pending',ingest_attempts=0,last_ingest_error=NULL,next_retry_at=NULL "
                "WHERE id=? AND ingest_status IN ('retrying','quarantined')", (turn_id,),
            ).rowcount
            if not changed:
                raise KeyError(turn_id)
            return dict(conn.execute("SELECT * FROM raw_turns WHERE id=?", (turn_id,)).fetchone())

    # ------------------------------------------------------------------
    # v0.6 recent layer.  These records are intentionally separate from
    # candidates and long-term memories: they are Dream input, never default
    # prompt injection, and only Deep may turn their evidence into a memory.

    @staticmethod
    def _recent_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        result = dict(row)
        result["sensitive"] = bool(result.get("sensitive"))
        return result

    def upsert_recent_signal(
        self,
        content: str,
        *,
        kind: str = "fact",
        confidence: float = 0.0,
        sensitive: bool = False,
        raw_turn_id: str | None = None,
        excerpt: str = "",
        observed_at: str | None = None,
        subject_id: str | None = None,
        retention_days: int = 14,
    ) -> tuple[dict[str, Any], bool]:
        """Create or reinforce one active recent signal.

        The boolean is true only when a new signal was created.  Exact
        repetitions reinforce a single record instead of creating review
        noise.  Semantic consolidation is performed by Light/REM before this
        method is called.
        """
        text = content.strip()
        if not text:
            raise ValueError("Recent signal content cannot be empty")
        if kind not in MEMORY_KINDS:
            kind = "fact"
        now = utc_now()
        seen_at = observed_at or now
        expires = (datetime.now(UTC) + timedelta(days=max(1, int(retention_days)))).isoformat(
            timespec="seconds"
        )
        digest = content_hash(text)
        with self.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM recent_signals WHERE content_hash=? AND status='active'",
                (digest,),
            ).fetchone()
            created = row is None
            if row:
                signal_id = str(row["id"])
                conn.execute(
                    "UPDATE recent_signals SET strength=strength+1,confidence=max(confidence,?),"
                    "sensitive=max(sensitive,?),last_seen_at=?,expires_at=?,updated_at=?,"
                    "subject_id=coalesce(?,subject_id),source_raw_turn_id=coalesce(?,source_raw_turn_id) "
                    "WHERE id=?",
                    (
                        max(0.0, min(1.0, float(confidence))),
                        int(sensitive),
                        seen_at,
                        expires,
                        now,
                        subject_id,
                        raw_turn_id,
                        signal_id,
                    ),
                )
            else:
                signal_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO recent_signals("
                    "id,content,kind,status,confidence,strength,sensitive,content_hash,subject_id,"
                    "source_raw_turn_id,first_seen_at,last_seen_at,expires_at,created_at,updated_at"
                    ") VALUES(?,?,?,'active',?,1,?,?,?,?,?,?,?,?,?)",
                    (
                        signal_id,
                        text,
                        kind,
                        max(0.0, min(1.0, float(confidence))),
                        int(sensitive),
                        digest,
                        subject_id,
                        raw_turn_id,
                        seen_at,
                        seen_at,
                        expires,
                        now,
                        now,
                    ),
                )
            if raw_turn_id and not conn.execute(
                "SELECT 1 FROM recent_evidence WHERE signal_id=? AND raw_turn_id=?",
                (signal_id, raw_turn_id),
            ).fetchone():
                conn.execute(
                    "INSERT INTO recent_evidence(signal_id,raw_turn_id,excerpt,role,observed_at) "
                    "VALUES(?,?,?,'user',?)",
                    (signal_id, raw_turn_id, excerpt[:1000], seen_at),
                )
            if subject_id:
                self._link_subject_in_tx(
                    conn, subject_id, "recent_signal", signal_id,
                    assignment_status="automatic", method="dream_light", now=now,
                )
            result = conn.execute("SELECT * FROM recent_signals WHERE id=?", (signal_id,)).fetchone()
        return self._recent_row(result) or {}, created

    def revise_recent_signal(
        self,
        signal_id: str,
        content: str,
        *,
        kind: str | None = None,
        confidence: float | None = None,
        retention_days: int = 14,
    ) -> dict[str, Any]:
        text = content.strip()
        if not text:
            raise ValueError("Recent signal content cannot be empty")
        now = utc_now()
        expires = (datetime.now(UTC) + timedelta(days=max(1, int(retention_days)))).isoformat(
            timespec="seconds"
        )
        with self.transaction(immediate=True) as conn:
            old = conn.execute("SELECT * FROM recent_signals WHERE id=?", (signal_id,)).fetchone()
            if not old or old["status"] != "active":
                raise KeyError(signal_id)
            next_kind = kind if kind in MEMORY_KINDS else old["kind"]
            next_confidence = old["confidence"] if confidence is None else max(
                0.0, min(1.0, float(confidence))
            )
            conn.execute(
                "UPDATE recent_signals SET content=?,kind=?,content_hash=?,confidence=?,strength=strength+1,"
                "last_seen_at=?,expires_at=?,updated_at=? WHERE id=?",
                (text, next_kind, content_hash(text), next_confidence, now, expires, now, signal_id),
            )
            row = conn.execute("SELECT * FROM recent_signals WHERE id=?", (signal_id,)).fetchone()
        return self._recent_row(row) or {}

    def merge_recent_signals(self, target_id: str, source_id: str, *, retention_days: int = 14) -> dict[str, Any]:
        if target_id == source_id:
            raise ValueError("A signal cannot merge into itself")
        now = utc_now()
        expires = (datetime.now(UTC) + timedelta(days=max(1, int(retention_days)))).isoformat(
            timespec="seconds"
        )
        with self.transaction(immediate=True) as conn:
            target = conn.execute("SELECT * FROM recent_signals WHERE id=? AND status='active'", (target_id,)).fetchone()
            source = conn.execute("SELECT * FROM recent_signals WHERE id=? AND status='active'", (source_id,)).fetchone()
            if not target or not source:
                raise KeyError(source_id if not source else target_id)
            conn.execute(
                "UPDATE recent_signals SET strength=strength+?,confidence=max(confidence,?),"
                "sensitive=max(sensitive,?),last_seen_at=max(last_seen_at,?),expires_at=?,updated_at=? WHERE id=?",
                (source["strength"], source["confidence"], source["sensitive"], source["last_seen_at"], expires, now, target_id),
            )
            conn.execute("UPDATE recent_evidence SET signal_id=? WHERE signal_id=?", (target_id, source_id))
            conn.execute("UPDATE recent_signals SET status='merged',updated_at=? WHERE id=?", (now, source_id))
            row = conn.execute("SELECT * FROM recent_signals WHERE id=?", (target_id,)).fetchone()
        return self._recent_row(row) or {}

    def add_daily_memory(
        self,
        content: str,
        *,
        observed_at: str | None = None,
        timezone_name: str = "system",
        subject_id: str | None = None,
        raw_turn_id: str | None = None,
        signal_id: str | None = None,
        retention_days: int = 30,
    ) -> dict[str, Any]:
        """Append one compact, de-duplicated item to a day/global-or-project log."""
        item = content.strip().lstrip("- ").strip()
        if not item:
            raise ValueError("Daily memory content cannot be empty")
        now = utc_now()
        recorded_at = observed_at or now
        day = local_date(recorded_at, timezone_name)
        scope = subject_id or "global"
        expires = (datetime.now(UTC) + timedelta(days=max(1, int(retention_days)))).isoformat(
            timespec="seconds"
        )
        with self.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM daily_memories WHERE memory_date=? AND scope_key=?",
                (day, scope),
            ).fetchone()
            line = f"- {item}"
            if row:
                existing_lines = [value.strip() for value in str(row["content"]).splitlines() if value.strip()]
                normalized = {content_hash(value.lstrip("- ").strip()) for value in existing_lines}
                combined = str(row["content"])
                if content_hash(item) not in normalized:
                    combined = f"{combined.rstrip()}\n{line}".strip()
                daily_id = str(row["id"])
                conn.execute(
                    "UPDATE daily_memories SET content=?,content_hash=?,status='active',expires_at=?,updated_at=? WHERE id=?",
                    (combined, content_hash(combined), expires, now, daily_id),
                )
            else:
                daily_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO daily_memories("
                    "id,memory_date,scope_key,subject_id,content,content_hash,status,expires_at,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,'active',?,?,?)",
                    (daily_id, day, scope, subject_id, line, content_hash(line), expires, now, now),
                )
            if raw_turn_id or signal_id:
                existing = conn.execute(
                    "SELECT 1 FROM recent_evidence WHERE daily_memory_id=? AND "
                    "coalesce(raw_turn_id,'')=coalesce(?, '') AND coalesce(signal_id,'')=coalesce(?, '')",
                    (daily_id, raw_turn_id, signal_id),
                ).fetchone()
                if not existing:
                    conn.execute(
                        "INSERT INTO recent_evidence(signal_id,daily_memory_id,raw_turn_id,excerpt,role,observed_at) "
                        "VALUES(?,?,?,?,'user',?)",
                        (signal_id, daily_id, raw_turn_id, item[:1000], recorded_at),
                    )
            row = conn.execute("SELECT * FROM daily_memories WHERE id=?", (daily_id,)).fetchone()
        return dict(row)

    def list_recent_signals(
        self, *, status: str | None = "active", subject_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM recent_signals WHERE 1=1"
        args: list[Any] = []
        if status:
            sql += " AND status=?"
            args.append(status)
        if subject_id:
            sql += " AND subject_id=?"
            args.append(subject_id)
        sql += " ORDER BY last_seen_at DESC LIMIT ?"
        args.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._recent_row(row) or {} for row in rows]

    def list_daily_memories(
        self,
        *,
        status: str | None = "active",
        subject_id: str | None = None,
        since_date: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM daily_memories WHERE 1=1"
        args: list[Any] = []
        if status:
            sql += " AND status=?"
            args.append(status)
        if subject_id is not None:
            sql += " AND subject_id=?"
            args.append(subject_id)
        if since_date:
            sql += " AND memory_date>=?"
            args.append(since_date)
        sql += " ORDER BY memory_date DESC,scope_key ASC LIMIT ?"
        args.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    def list_rem_reflections(
        self, *, status: str | None = "active", limit: int = 200
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM rem_reflections"
        args: list[Any] = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["sensitive"] = bool(item["sensitive"])
            item["signal_ids"] = json.loads(item.pop("signal_ids_json") or "[]")
            item["daily_memory_ids"] = json.loads(item.pop("daily_memory_ids_json") or "[]")
            result.append(item)
        return result

    def create_rem_reflection(
        self,
        content: str,
        *,
        reflection_type: str,
        confidence: float,
        signal_ids: list[str],
        daily_memory_ids: list[str],
        sensitive: bool = False,
        dream_run_id: str | None = None,
    ) -> dict[str, Any]:
        text = content.strip()
        if not text:
            raise ValueError("REM reflection content cannot be empty")
        allowed = {"theme", "evolution", "repetition", "conflict", "conclusion"}
        if reflection_type not in allowed:
            reflection_type = "theme"
        with self.transaction(immediate=True) as conn:
            signal_placeholders = ",".join("?" for _ in signal_ids) or "''"
            daily_placeholders = ",".join("?" for _ in daily_memory_ids) or "''"
            active_signal_ids = {
                row[0] for row in conn.execute(
                    f"SELECT id FROM recent_signals WHERE status='active' AND id IN ({signal_placeholders})",
                    signal_ids,
                )
            }
            active_daily_ids = {
                row[0] for row in conn.execute(
                    f"SELECT id FROM daily_memories WHERE status='active' AND id IN ({daily_placeholders})",
                    daily_memory_ids,
                )
            }
            if not active_signal_ids and not active_daily_ids:
                raise ValueError("REM reflection requires active recent evidence")
            reflection_id = str(uuid.uuid4())
            now = utc_now()
            conn.execute(
                "INSERT INTO rem_reflections("
                "id,content,reflection_type,status,confidence,sensitive,signal_ids_json,daily_memory_ids_json,"
                "dream_run_id,created_at,updated_at"
                ") VALUES(?,?,?,'active',?,?,?,?,?,?,?)",
                (
                    reflection_id, text, reflection_type, max(0.0, min(1.0, float(confidence))),
                    int(sensitive), json.dumps(sorted(active_signal_ids)), json.dumps(sorted(active_daily_ids)),
                    dream_run_id, now, now,
                ),
            )
            row = conn.execute("SELECT * FROM rem_reflections WHERE id=?", (reflection_id,)).fetchone()
        if not row:
            return {}
        result = dict(row)
        result["sensitive"] = bool(result["sensitive"])
        result["signal_ids"] = json.loads(result.pop("signal_ids_json") or "[]")
        result["daily_memory_ids"] = json.loads(result.pop("daily_memory_ids_json") or "[]")
        return result

    def active_recent_for_rem(self, *, daily_days: int = 30, limit: int = 300) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        cutoff = (datetime.now(UTC) - timedelta(days=max(1, int(daily_days)))).date().isoformat()
        return (
            self.list_recent_signals(status="active", limit=limit),
            self.list_daily_memories(status="active", since_date=cutoff, limit=limit),
        )

    def expire_recent_layer(self, *, recent_days: int = 14, daily_days: int = 30) -> dict[str, int]:
        now = utc_now()
        recent_cutoff = (datetime.now(UTC) - timedelta(days=max(1, int(recent_days)))).isoformat(
            timespec="seconds"
        )
        daily_cutoff = (datetime.now(UTC) - timedelta(days=max(1, int(daily_days)))).date().isoformat()
        with self.transaction(immediate=True) as conn:
            signals = conn.execute(
                "UPDATE recent_signals SET status='expired',updated_at=? "
                "WHERE status='active' AND (expires_at<=? OR last_seen_at<?)",
                (now, now, recent_cutoff),
            ).rowcount
            daily = conn.execute(
                "UPDATE daily_memories SET status='expired',updated_at=? "
                "WHERE status='active' AND (expires_at<=? OR memory_date<?)",
                (now, now, daily_cutoff),
            ).rowcount
        return {"recent_signals": signals, "daily_memories": daily}

    def reinforce_recent_search_hits(self, signal_ids: list[str], *, retention_days: int = 14) -> int:
        """Treat an explicit recall as weak recency reinforcement, never evidence."""
        ids = list(dict.fromkeys(item for item in signal_ids if item))
        if not ids:
            return 0
        now = utc_now()
        expires = (datetime.now(UTC) + timedelta(days=max(1, int(retention_days)))).isoformat(
            timespec="seconds"
        )
        with self.transaction(immediate=True) as conn:
            return conn.executemany(
                "UPDATE recent_signals SET strength=strength+1,last_seen_at=?,expires_at=?,updated_at=? "
                "WHERE id=? AND status='active'",
                [(now, expires, now, signal_id) for signal_id in ids],
            ).rowcount

    def _create_evidence_impact_review_in_tx(
        self, conn: sqlite3.Connection, memory_id: str, *, source: str, basis_hash: str
    ) -> None:
        fingerprint = self.make_review_fingerprint(
            "evidence_affected", primary_memory_id=memory_id, basis_hash=basis_hash
        )
        if conn.execute("SELECT 1 FROM memory_review_items WHERE fingerprint=?", (fingerprint,)).fetchone():
            return
        now = utc_now()
        conn.execute(
            "INSERT INTO memory_review_items("
            "id,issue_type,status,proposed_action,reason,confidence,primary_memory_id,source,"
            "basis_hash,fingerprint,created_at,updated_at,queue,proposal_json"
            ") VALUES(?,?,'open',?,?,?, ?,?,?,?,?,?,'decision','{}')",
            (
                str(uuid.uuid4()), "evidence_affected", "retain_or_edit",
                "Permanent deletion removed the only or a key recent-layer evidence source; long-term memory was preserved.",
                1.0, memory_id, source, basis_hash, fingerprint, now, now,
            ),
        )

    def remove_recent_record(self, record_type: str, record_id: str, *, permanent: bool = False) -> dict[str, int]:
        """Stop future Dream/recall immediately; preserve long-term conclusions for review."""
        table, evidence_column, recent_evidence_column, source = {
            "recent_signal": ("recent_signals", "recent_signal_id", "signal_id", "recent_signal"),
            "daily_memory": ("daily_memories", "daily_memory_id", "daily_memory_id", "daily_memory"),
        }.get(record_type, ("", "", "", ""))
        if not table:
            raise ValueError("Unsupported recent record type")
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
            if not row:
                raise KeyError(record_id)
            reflection_column = (
                "signal_ids_json" if record_type == "recent_signal" else "daily_memory_ids_json"
            )
            conn.execute(
                f"UPDATE rem_reflections SET status='discarded',outcome='source_deleted',updated_at=? "
                f"WHERE status='active' AND {reflection_column} LIKE ?",
                (now, f'%"{record_id}"%'),
            )
            if not permanent:
                conn.execute(f"UPDATE {table} SET status='trashed',updated_at=? WHERE id=?", (now, record_id))
                return {"trashed": 1, "reviews": 0, "purged": 0}
            # A permanent recent-layer deletion is a privacy operation.  The
            # source turn and every model run explicitly linked to it must no
            # longer retain the deleted conversation; normal trashing above
            # intentionally does not take this irreversible path.
            raw_ids = [
                str(item[0])
                for item in conn.execute(
                    f"SELECT DISTINCT raw_turn_id FROM recent_evidence "
                    f"WHERE {recent_evidence_column}=? AND raw_turn_id IS NOT NULL",
                    (record_id,),
                )
            ]
            record_refs = [(source, record_id), *[("raw_turn", raw_id) for raw_id in raw_ids]]
            dream_run_ids: set[str] = set()
            call_ids: set[str] = set()
            for record_kind, referenced_id in record_refs:
                for call in conn.execute(
                    "SELECT mc.id,mc.dream_run_id FROM model_calls mc "
                    "JOIN model_call_records mcr ON mcr.call_id=mc.id "
                    "WHERE mcr.record_type=? AND mcr.record_id=?",
                    (record_kind, referenced_id),
                ):
                    call_ids.add(str(call["id"]))
                    if call["dream_run_id"]:
                        dream_run_ids.add(str(call["dream_run_id"]))
            clauses = [f"COALESCE({evidence_column}=?,0)"]
            params: list[Any] = [record_id]
            if raw_ids:
                placeholders = ",".join("?" for _ in raw_ids)
                clauses.append(f"COALESCE(raw_turn_id IN ({placeholders}),0)")
                params.extend(raw_ids)
            linked = conn.execute(
                "SELECT DISTINCT memory_id FROM evidence WHERE ("
                + " OR ".join(clauses)
                + ") AND memory_id IS NOT NULL",
                params,
            ).fetchall()
            reviews = 0
            for linked_row in linked:
                memory_id = str(linked_row["memory_id"])
                remaining = conn.execute(
                    "SELECT count(*) FROM evidence WHERE memory_id=? AND NOT ("
                    + " OR ".join(clauses)
                    + ")",
                    [memory_id, *params],
                ).fetchone()[0]
                if int(remaining) <= 1:
                    self._create_evidence_impact_review_in_tx(
                        conn, memory_id, source="recent_privacy_delete", basis_hash=f"{record_type}:{record_id}"
                    )
                    reviews += 1
            conn.execute("DELETE FROM recall_events WHERE source=? AND record_id=?", (source, record_id))
            conn.execute("DELETE FROM embeddings WHERE source=? AND record_id=?", (source, record_id))
            conn.execute("DELETE FROM search_fts WHERE source=? AND record_id=?", (source, record_id))
            conn.execute("DELETE FROM model_call_records WHERE record_type=? AND record_id=?", (source, record_id))
            conn.execute(f"DELETE FROM {table} WHERE id=?", (record_id,))
            for run_id in dream_run_ids:
                conn.execute("DELETE FROM dream_runs WHERE id=?", (run_id,))
            for record_kind, referenced_id in record_refs:
                conn.execute(
                    "DELETE FROM model_call_records WHERE record_type=? AND record_id=?",
                    (record_kind, referenced_id),
                )
            if raw_ids:
                conn.executemany("DELETE FROM raw_turns WHERE id=?", [(raw_id,) for raw_id in raw_ids])
            for call_id in call_ids:
                conn.execute(
                    "DELETE FROM model_calls WHERE id=? AND NOT EXISTS "
                    "(SELECT 1 FROM model_call_records WHERE call_id=?)",
                    (call_id, call_id),
                )
        return {"trashed": 0, "reviews": reviews, "purged": 1}

    def _create_deep_review_in_tx(
        self,
        conn: sqlite3.Connection,
        *,
        reflection_id: str,
        action: str,
        content: str,
        reason: str,
        confidence: float,
        target_memory_id: str | None,
        dream_run_id: str | None,
    ) -> None:
        basis_hash = content_hash(f"{reflection_id}\n{content}\n{target_memory_id or ''}")
        fingerprint = self.make_review_fingerprint(
            "deep_review", proposed_content=content, primary_memory_id=target_memory_id,
            basis_hash=basis_hash,
        )
        if conn.execute("SELECT 1 FROM memory_review_items WHERE fingerprint=?", (fingerprint,)).fetchone():
            return
        now = utc_now()
        conn.execute(
            "INSERT INTO memory_review_items("
            "id,issue_type,status,proposed_action,proposed_content,reason,confidence,primary_memory_id,"
            "source,basis_hash,fingerprint,dream_run_id,created_at,updated_at,queue,proposal_json"
            ") VALUES(?,?,'open',?,?,?,?,?,?,?,?,?,?,?,'decision',?)",
            (
                str(uuid.uuid4()), "deep_review", action, content, reason[:2000],
                max(0.0, min(1.0, confidence)), target_memory_id, "deep", basis_hash,
                fingerprint, dream_run_id, now, now,
                json.dumps({"reflection_id": reflection_id, "action": action}, ensure_ascii=False),
            ),
        )

    def apply_deep_integrations(
        self, integrations: list[dict[str, Any]], *, dream_run_id: str
    ) -> dict[str, int]:
        """Validate and atomically apply a Deep batch.

        Normal duplicates converge into the existing memory.  Sensitive,
        conflict, or low-confidence items stay in the review queue; no Deep
        branch can silently delete a long-term conclusion.
        """
        allowed = {"create", "update", "merge", "supersede", "expire", "defer", "review"}
        outcomes = {key: 0 for key in allowed}
        if not integrations:
            return outcomes
        with self.transaction(immediate=True) as conn:
            event_run_id = (
                dream_run_id
                if conn.execute("SELECT 1 FROM dream_runs WHERE id=?", (dream_run_id,)).fetchone()
                else None
            )
            reflection_ids = [str(item.get("reflection_id", "")) for item in integrations]
            if len(set(reflection_ids)) != len(reflection_ids) or not all(reflection_ids):
                raise ValueError("Deep must contain each reflection at most once")
            placeholders = ",".join("?" for _ in reflection_ids)
            reflections = {
                str(row["id"]): row for row in conn.execute(
                    f"SELECT * FROM rem_reflections WHERE id IN ({placeholders}) AND status='active'",
                    reflection_ids,
                )
            }
            if set(reflections) != set(reflection_ids):
                raise ValueError("Deep referenced an unavailable reflection")
            now = utc_now()
            for item in integrations:
                reflection_id = str(item["reflection_id"])
                reflection = reflections[reflection_id]
                action = str(item.get("action", "defer")).strip().lower()
                if action not in allowed:
                    raise ValueError(f"Unsupported Deep action: {action}")
                raw_target_id = item.get("target_memory_id")
                target_id = str(raw_target_id).strip() if raw_target_id else None
                target = None
                if target_id:
                    target = conn.execute(
                        "SELECT * FROM memories WHERE id=? AND status='active'", (target_id,)
                    ).fetchone()
                    if not target:
                        raise ValueError(f"Deep referenced an unavailable memory: {target_id}")
                if action in {"update", "merge", "supersede", "expire"} and not target:
                    raise ValueError(f"Deep action {action} requires a target memory")
                content = str(item.get("content", "")).strip() or str(reflection["content"])
                kind = str(item.get("kind", "fact")).strip()
                if kind not in MEMORY_KINDS:
                    kind = "fact"
                try:
                    confidence = max(0.0, min(1.0, float(item.get("confidence", reflection["confidence"]))))
                except (TypeError, ValueError):
                    confidence = float(reflection["confidence"])
                reason = str(item.get("reason", "")).strip() or "Deep integration decision"
                signal_ids = json.loads(reflection["signal_ids_json"] or "[]")
                daily_ids = json.loads(reflection["daily_memory_ids_json"] or "[]")
                signal_placeholders = ",".join("?" for _ in signal_ids) or "''"
                daily_placeholders = ",".join("?" for _ in daily_ids) or "''"
                source_times = [
                    row[0] for row in conn.execute(
                        "SELECT observed_at FROM recent_evidence WHERE signal_id IN "
                        f"({signal_placeholders}) OR daily_memory_id IN "
                        f"({daily_placeholders})",
                        [*signal_ids, *daily_ids],
                    )
                ]
                evidence_days = {
                    str(value)[:10] for value in source_times if isinstance(value, str) and len(value) >= 10
                }
                if action in {"create", "update", "merge", "supersede", "expire"} and len(evidence_days) < 2:
                    action = "defer"
                    reason = "Deep deferred: recent evidence has not yet appeared on two dates"
                requires_review = (
                    action == "review" or bool(reflection["sensitive"]) or confidence < 0.70
                )
                if requires_review:
                    self._create_deep_review_in_tx(
                        conn, reflection_id=reflection_id, action=action, content=content,
                        reason=reason, confidence=confidence, target_memory_id=target_id,
                        dream_run_id=event_run_id,
                    )
                    conn.execute(
                        "UPDATE rem_reflections SET status='review',outcome=?,handled_at=?,updated_at=? WHERE id=?",
                        (action, now, now, reflection_id),
                    )
                    outcomes["review"] += 1
                    continue
                memory_id: str | None = None
                if action == "create":
                    duplicate = conn.execute(
                        "SELECT * FROM memories WHERE content_hash=? AND status='active'",
                        (content_hash(content),),
                    ).fetchone()
                    if duplicate:
                        memory_id = str(duplicate["id"])
                        action = "merge"
                    else:
                        memory_id = str(uuid.uuid4())
                        conn.execute(
                            "INSERT INTO memories("
                            "id,content,kind,status,origin,confidence,importance,sensitive,content_hash,created_at,updated_at"
                            ") VALUES(?,?,?,'active','dream-deep',?,?,0,?,?,?)",
                            (memory_id, content, kind, confidence, 0.5, content_hash(content), now, now),
                        )
                        self._add_event(
                            conn, "memory_created", memory_id=memory_id, dream_run_id=event_run_id,
                            occurred_at=now, data={"content": content, "kind": kind, "origin": "dream-deep"},
                        )
                elif action in {"update", "merge"}:
                    assert target is not None
                    memory_id = str(target["id"])
                    if action == "update" or content_hash(content) != target["content_hash"]:
                        conn.execute(
                            "INSERT INTO memory_revisions(memory_id,content,kind,changed_at) VALUES(?,?,?,?)",
                            (memory_id, target["content"], target["kind"], now),
                        )
                        conn.execute(
                            "UPDATE memories SET content=?,kind=?,content_hash=?,updated_at=? WHERE id=?",
                            (content, kind, content_hash(content), now, memory_id),
                        )
                        self._add_event(
                            conn, "memory_updated", memory_id=memory_id, dream_run_id=event_run_id,
                            occurred_at=now, data={"content": content, "reason": reason, "action": action},
                        )
                elif action == "supersede":
                    assert target is not None
                    memory_id = str(uuid.uuid4())
                    conn.execute(
                        "INSERT INTO memories("
                        "id,content,kind,status,origin,confidence,importance,sensitive,supersedes_id,content_hash,created_at,updated_at"
                        ") VALUES(?,?,?,'active','dream-deep',?,?,0,?,?,?,?)",
                        (memory_id, content, kind, confidence, 0.5, target["id"], content_hash(content), now, now),
                    )
                    conn.execute(
                        "UPDATE memories SET status='superseded',temporal_status='historical',valid_to=coalesce(valid_to,?),updated_at=? WHERE id=?",
                        (now, now, target["id"]),
                    )
                    self._add_event(
                        conn, "memory_superseded", memory_id=str(target["id"]), dream_run_id=event_run_id,
                        occurred_at=now, data={"replacement_memory_id": memory_id, "reason": reason},
                    )
                elif action == "expire":
                    assert target is not None
                    memory_id = str(target["id"])
                    conn.execute(
                        "UPDATE memories SET temporal_status='historical',valid_until=?,valid_to=coalesce(valid_to,?),updated_at=? WHERE id=?",
                        (now, now, now, memory_id),
                    )
                    self._add_event(
                        conn, "memory_expired", memory_id=memory_id, dream_run_id=event_run_id,
                        occurred_at=now, data={"reason": reason},
                    )
                elif action == "defer":
                    conn.execute(
                        "UPDATE rem_reflections SET status='deferred',outcome='defer',handled_at=?,updated_at=? WHERE id=?",
                        (now, now, reflection_id),
                    )
                    outcomes["defer"] += 1
                    continue
                if memory_id:
                    for signal_id in signal_ids:
                        conn.execute(
                            "INSERT INTO evidence(memory_id,recent_signal_id,excerpt,role,observed_at) VALUES(?,?,?,'recent',?)",
                            (memory_id, signal_id, str(reflection["content"])[:1000], now),
                        )
                    for daily_id in daily_ids:
                        conn.execute(
                            "INSERT INTO evidence(memory_id,daily_memory_id,excerpt,role,observed_at) VALUES(?,?,?,'daily',?)",
                            (memory_id, daily_id, str(reflection["content"])[:1000], now),
                        )
                conn.execute(
                    "UPDATE rem_reflections SET status='handled',outcome=?,handled_at=?,updated_at=? WHERE id=?",
                    (action, now, now, reflection_id),
                )
                outcomes[action] += 1
        return outcomes

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
        admission_state: str = "admitted",
        source_type: str = "dream_user",
        admission_reason: str | None = None,
        subject_id: str | None = None,
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
                    "rem_status='unreviewed',rem_reason=NULL,rem_reviewed_at=NULL,"
                    "admission_state=?,source_type=?,admission_reason=? WHERE id=?",
                    (
                        now,
                        now,
                        confidence,
                        admission_state,
                        source_type,
                        admission_reason,
                        candidate_id,
                    ),
                )
            else:
                candidate_id = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO candidates(
                        id,content,kind,status,model_confidence,sensitive,score,score_components,
                        evidence_days,content_hash,first_seen_at,last_seen_at,last_activity_at,
                        admission_state,source_type,admission_reason
                    ) VALUES(?,?,?,'pending',?,?,0,'{}',0,?,?,?,?,?,?,?)""",
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
                        admission_state,
                        source_type,
                        admission_reason,
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
                            "user",
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
            if subject_id:
                conn.execute("UPDATE candidates SET subject_id=? WHERE id=?", (subject_id, candidate_id))
                self._link_subject_in_tx(
                    conn,
                    subject_id,
                    "candidate",
                    candidate_id,
                    assignment_status=(
                        "confirmed" if source_type == "explicit_remember" else "automatic"
                    ),
                    method=source_type,
                    now=now,
                )
            row = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        return self._candidate_from_row(row)

    @staticmethod
    def _link_subject_in_tx(
        conn: sqlite3.Connection,
        subject_id: str,
        object_type: str,
        object_id: str,
        *,
        assignment_status: str,
        method: str,
        now: str,
        confidence: float = 1.0,
    ) -> None:
        subject = conn.execute(
            "SELECT subject_type,status FROM subjects WHERE id=?", (subject_id,)
        ).fetchone()
        if not subject or subject["subject_type"] != "project" or subject["status"] == "archived":
            raise KeyError(subject_id)
        conn.execute(
            "INSERT INTO subject_links(id,subject_id,object_type,object_id,confidence,"
            "assignment_status,method,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(subject_id,object_type,object_id) DO UPDATE SET "
            "confidence=max(confidence,excluded.confidence),assignment_status=excluded.assignment_status,"
            "method=excluded.method,updated_at=excluded.updated_at",
            (
                str(uuid.uuid4()), subject_id, object_type, object_id, confidence,
                assignment_status, method, now, now,
            ),
        )

    def link_subject_record(
        self,
        subject_id: str,
        object_type: str,
        object_id: str,
        *,
        method: str = "manual",
        assignment_status: str = "confirmed",
    ) -> None:
        with self.transaction(immediate=True) as conn:
            self._link_subject_in_tx(
                conn, subject_id, object_type, object_id,
                assignment_status=assignment_status, method=method, now=utc_now(),
            )

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

    def active_memory_by_content(self, content: str) -> MemoryRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE content_hash=? AND status='active'",
                (content_hash(content),),
            ).fetchone()
        return self._memory_from_row(row) if row else None

    def list_candidates(
        self,
        *,
        status: str = "pending",
        admission_state: str | None = None,
        limit: int = 500,
    ) -> list[CandidateRecord]:
        sql = "SELECT * FROM candidates WHERE status=?"
        args: list[Any] = [status]
        if admission_state:
            sql += " AND admission_state=?"
            args.append(admission_state)
        sql += " ORDER BY score DESC,last_seen_at DESC LIMIT ?"
        args.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._candidate_from_row(row) for row in rows]

    def candidates_due_for_rem(self, limit: int = 30) -> list[CandidateRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM candidates WHERE status='pending' "
                "AND admission_state='admitted' "
                "AND (rem_reviewed_at IS NULL OR rem_reviewed_at < last_activity_at) "
                "ORDER BY coalesce(rem_reviewed_at,'') ASC,last_activity_at ASC LIMIT ?",
                (limit,),
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
                "UPDATE admission_decisions SET candidate_id=? WHERE candidate_id=?",
                (canonical_id, duplicate_id),
            )
            conn.execute(
                "UPDATE memory_review_items SET candidate_id=? WHERE candidate_id=?",
                (canonical_id, duplicate_id),
            )
            conn.execute(
                "UPDATE memory_review_items SET related_candidate_id=? WHERE related_candidate_id=?",
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
            for link in conn.execute(
                "SELECT * FROM subject_links WHERE object_type='candidate' AND object_id=?",
                (duplicate_id,),
            ).fetchall():
                conn.execute(
                    "INSERT INTO subject_links(id,subject_id,object_type,object_id,confidence,"
                    "assignment_status,method,created_at,updated_at) VALUES(?,?, 'candidate',?,?,?,?,?,?) "
                    "ON CONFLICT(subject_id,object_type,object_id) DO UPDATE SET "
                    "confidence=max(confidence,excluded.confidence),updated_at=excluded.updated_at",
                    (str(uuid.uuid4()), link["subject_id"], canonical_id, link["confidence"],
                     link["assignment_status"], "candidate_merge", link["created_at"], utc_now()),
                )
            conn.execute(
                "DELETE FROM subject_links WHERE object_type='candidate' AND object_id=?", (duplicate_id,)
            )
            if not canonical["subject_id"] and duplicate["subject_id"]:
                conn.execute(
                    "UPDATE candidates SET subject_id=? WHERE id=?", (duplicate["subject_id"], canonical_id)
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
        review_id: str | None = None,
        review_resolution: str | None = None,
    ) -> MemoryRecord:
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            candidate = conn.execute(
                "SELECT * FROM candidates WHERE id=? AND status='pending'", (candidate_id,)
            ).fetchone()
            if not candidate:
                raise KeyError(candidate_id)
            memory_content = (edited_content or candidate["content"]).strip()
            row = self._promote_candidate_in_tx(
                conn, candidate, memory_content=memory_content, origin=origin,
                promotion_lane=promotion_lane, dream_run_id=dream_run_id, now=now,
            )
            if review_id:
                changed = conn.execute(
                    "UPDATE memory_review_items SET status='resolved',resolution=?,resolved_at=? "
                    "WHERE id=? AND status='open'",
                    (review_resolution or promotion_lane, now, review_id),
                ).rowcount
                if changed != 1:
                    raise KeyError(review_id)
        return self._memory_from_row(row)

    def _promote_candidate_in_tx(
        self,
        conn: sqlite3.Connection,
        candidate: sqlite3.Row,
        *,
        memory_content: str,
        origin: str,
        promotion_lane: str,
        dream_run_id: str | None,
        now: str,
    ) -> sqlite3.Row:
        """Promote and carry evidence, model lineage and subject scope atomically."""
        candidate_id = str(candidate["id"])
        existing = conn.execute(
            "SELECT * FROM memories WHERE content_hash=? AND status='active'",
            (content_hash(memory_content),),
        ).fetchone()
        memory_id = str(existing["id"]) if existing else str(uuid.uuid4())
        if not existing:
            conn.execute(
                """INSERT INTO memories(
                    id,content,kind,status,origin,confidence,importance,sensitive,
                    supersedes_id,content_hash,created_at,updated_at
                ) VALUES(?,?,?,'active',?,?,?,?,?,?,?,?)""",
                (memory_id, memory_content, candidate["kind"], origin,
                 float(candidate["model_confidence"]), 0.5, int(candidate["sensitive"]),
                 None, content_hash(memory_content), now, now),
            )
            self._add_event(
                conn, "memory_created", candidate_id=candidate_id, memory_id=memory_id,
                dream_run_id=dream_run_id, occurred_at=now,
                data={"content": memory_content, "kind": candidate["kind"], "origin": origin},
            )
        conn.execute(
            "UPDATE candidates SET status='promoted',promoted_at=?,promotion_origin=?,"
            "promoted_memory_id=? WHERE id=?", (now, origin, memory_id, candidate_id),
        )
        conn.execute("UPDATE evidence SET memory_id=? WHERE candidate_id=?", (memory_id, candidate_id))
        conn.execute(
            "INSERT OR IGNORE INTO model_call_records(call_id,record_type,record_id) "
            "SELECT call_id,'memory',? FROM model_call_records "
            "WHERE record_type='candidate' AND record_id=?", (memory_id, candidate_id),
        )
        self._add_event(
            conn, "candidate_promoted", candidate_id=candidate_id, memory_id=memory_id,
            dream_run_id=dream_run_id, occurred_at=now,
            data={"origin": origin, "promotion_lane": promotion_lane,
                  "candidate_content": candidate["content"], "memory_content": memory_content,
                  "model_confidence": float(candidate["model_confidence"]),
                  "evidence_days": int(candidate["evidence_days"]),
                  "recall_count": int(candidate["recall_count"]),
                  "unique_query_count": int(candidate["unique_query_count"])},
        )
        if existing:
            self._add_event(
                conn, "candidate_absorbed", candidate_id=candidate_id, memory_id=memory_id,
                dream_run_id=dream_run_id, occurred_at=now,
                data={"reason": "identical_content", "origin": origin},
            )
        existing_project_scope = bool(conn.execute(
            "SELECT 1 FROM subject_links sl JOIN subjects s ON s.id=sl.subject_id "
            "WHERE sl.object_type='memory' AND sl.object_id=? AND s.subject_type='project' LIMIT 1",
            (memory_id,),
        ).fetchone())
        links = conn.execute(
            "SELECT sl.*,s.subject_type FROM subject_links sl JOIN subjects s ON s.id=sl.subject_id "
            "WHERE sl.object_type='candidate' AND sl.object_id=?", (candidate_id,),
        ).fetchall()
        if not links and candidate["subject_id"]:
            subject = conn.execute("SELECT subject_type FROM subjects WHERE id=?", (candidate["subject_id"],)).fetchone()
            if subject:
                links = [{"subject_id": candidate["subject_id"], "subject_type": subject["subject_type"],
                          "confidence": 1.0, "assignment_status": (
                              "confirmed" if candidate["source_type"] == "explicit_remember" else "automatic"
                          ), "created_at": now}]
        for link in links:
            if existing and link["subject_type"] == "project" and not existing_project_scope:
                continue
            conn.execute(
                "INSERT INTO subject_links(id,subject_id,object_type,object_id,confidence,"
                "assignment_status,method,created_at,updated_at) VALUES(?,?, 'memory',?,?,?,?,?,?) "
                "ON CONFLICT(subject_id,object_type,object_id) DO UPDATE SET "
                "confidence=max(confidence,excluded.confidence),updated_at=excluded.updated_at",
                (str(uuid.uuid4()), link["subject_id"], memory_id, link["confidence"],
                 link["assignment_status"], "candidate_promotion", link["created_at"], now),
            )
        return conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()

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

    def add_admission_decision(
        self,
        *,
        disposition: str,
        content: str,
        reason: str,
        confidence: float = 0.0,
        evidence_quote: str | None = None,
        raw_turn_id: str | None = None,
        candidate_id: str | None = None,
        dream_run_id: str | None = None,
    ) -> str:
        if disposition not in {"admit", "observe", "discard", "invalid"}:
            raise ValueError(disposition)
        decision_id = str(uuid.uuid4())
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            conn.execute(
                """INSERT INTO admission_decisions(
                    id,disposition,content,evidence_quote,reason,confidence,
                    raw_turn_id,candidate_id,dream_run_id,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision_id,
                    disposition,
                    content[:4000],
                    (evidence_quote or "")[:1000] or None,
                    reason[:1000],
                    max(0.0, min(1.0, float(confidence))),
                    raw_turn_id,
                    candidate_id,
                    dream_run_id,
                    now,
                ),
            )
            self._add_event(
                conn,
                "admission_reviewed",
                candidate_id=candidate_id,
                dream_run_id=dream_run_id,
                occurred_at=now,
                data={"disposition": disposition, "reason": reason[:1000]},
            )
        return decision_id

    def list_admission_decisions(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM admission_decisions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def make_review_fingerprint(
        issue_type: str,
        *,
        proposed_content: str | None = None,
        candidate_id: str | None = None,
        related_candidate_id: str | None = None,
        primary_memory_id: str | None = None,
        related_memory_id: str | None = None,
        basis_hash: str = "",
    ) -> str:
        payload = "\n".join(
            [
                issue_type,
                content_hash(proposed_content or ""),
                candidate_id or "",
                related_candidate_id or "",
                primary_memory_id or "",
                related_memory_id or "",
                basis_hash,
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def create_review_item(
        self,
        *,
        issue_type: str,
        proposed_action: str,
        reason: str,
        proposed_content: str | None = None,
        confidence: float = 0.0,
        candidate_id: str | None = None,
        related_candidate_id: str | None = None,
        primary_memory_id: str | None = None,
        related_memory_id: str | None = None,
        source: str = "integration",
        basis_hash: str = "",
        dream_run_id: str | None = None,
        audit_run_id: str | None = None,
        subject_id: str | None = None,
        queue: str | None = None,
        proposal: dict[str, Any] | None = None,
    ) -> ReviewItem:
        fingerprint = self.make_review_fingerprint(
            issue_type,
            proposed_content=proposed_content,
            candidate_id=candidate_id,
            related_candidate_id=related_candidate_id,
            primary_memory_id=primary_memory_id,
            related_memory_id=related_memory_id,
            basis_hash=basis_hash,
        )
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            existing = conn.execute(
                "SELECT * FROM memory_review_items WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
            if existing:
                return self._review_from_row(existing)
            review_id = str(uuid.uuid4())
            if queue is None:
                queue = (
                    "suggestion"
                    if issue_type in {"duplicate", "legacy_admission", "project_assignment", "stale_work_item", "summary_refresh"}
                    else "decision"
                )
            conn.execute(
                """INSERT INTO memory_review_items(
                    id,issue_type,status,proposed_action,proposed_content,reason,confidence,
                    candidate_id,related_candidate_id,primary_memory_id,related_memory_id,
                    source,basis_hash,fingerprint,dream_run_id,audit_run_id,created_at,updated_at,
                    subject_id,queue,proposal_json
                ) VALUES(?,?,'open',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    review_id,
                    issue_type,
                    proposed_action,
                    proposed_content,
                    reason[:2000],
                    max(0.0, min(1.0, float(confidence))),
                    candidate_id,
                    related_candidate_id,
                    primary_memory_id,
                    related_memory_id,
                    source,
                    basis_hash,
                    fingerprint,
                    dream_run_id,
                    audit_run_id,
                    now,
                    now,
                    subject_id,
                    queue,
                    json.dumps(proposal or {}, ensure_ascii=False),
                ),
            )
            self._add_event(
                conn,
                "integration_review_created",
                candidate_id=candidate_id,
                memory_id=primary_memory_id or related_memory_id,
                dream_run_id=dream_run_id,
                occurred_at=now,
                data={
                    "review_id": review_id,
                    "issue_type": issue_type,
                    "proposed_action": proposed_action,
                    "reason": reason[:1000],
                },
            )
            row = conn.execute(
                "SELECT * FROM memory_review_items WHERE id=?", (review_id,)
            ).fetchone()
        return self._review_from_row(row)

    def get_review_item(self, review_id: str) -> ReviewItem | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM memory_review_items WHERE id=?", (review_id,)
            ).fetchone()
        return self._review_from_row(row) if row else None

    def list_review_items(
        self,
        *,
        status: str | None = "open",
        issue_type: str | None = None,
        queue: str | None = None,
        subject_id: str | None = None,
        limit: int = 500,
    ) -> list[ReviewItem]:
        sql = "SELECT * FROM memory_review_items WHERE 1=1"
        args: list[Any] = []
        if status:
            sql += " AND status=?"
            args.append(status)
        if issue_type:
            sql += " AND issue_type=?"
            args.append(issue_type)
        if queue:
            sql += " AND queue=?"
            args.append(queue)
        if subject_id:
            sql += " AND subject_id=?"
            args.append(subject_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._review_from_row(row) for row in rows]

    def count_open_reviews(self) -> int:
        with self.connect() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_review_items WHERE status='open'"
                ).fetchone()[0]
            )

    def has_open_review(
        self, *, candidate_id: str | None = None, memory_id: str | None = None
    ) -> bool:
        clauses = ["status='open'"]
        args: list[Any] = []
        if candidate_id:
            clauses.append("(candidate_id=? OR related_candidate_id=?)")
            args.extend([candidate_id, candidate_id])
        if memory_id:
            clauses.append("(primary_memory_id=? OR related_memory_id=?)")
            args.extend([memory_id, memory_id])
        if len(clauses) == 1:
            return False
        with self.connect() as conn:
            return bool(
                conn.execute(
                    "SELECT 1 FROM memory_review_items WHERE " + " AND ".join(clauses) + " LIMIT 1",
                    args,
                ).fetchone()
            )

    def close_review_item(self, review_id: str, *, resolution: str, dismissed: bool = False) -> None:
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM memory_review_items WHERE id=? AND status='open'", (review_id,)
            ).fetchone()
            if not row:
                raise KeyError(review_id)
            conn.execute(
                "UPDATE memory_review_items SET status=?,resolution=?,resolved_at=?,updated_at=? "
                "WHERE id=?",
                ("dismissed" if dismissed else "resolved", resolution[:1000], now, now, review_id),
            )
            self._add_event(
                conn,
                "integration_review_resolved",
                candidate_id=row["candidate_id"],
                memory_id=row["primary_memory_id"] or row["related_memory_id"],
                occurred_at=now,
                data={"review_id": review_id, "resolution": resolution[:1000]},
            )

    def obsolete_open_reviews(
        self, *, candidate_id: str | None = None, memory_id: str | None = None
    ) -> int:
        clauses = ["status='open'"]
        args: list[Any] = []
        if candidate_id:
            clauses.append("(candidate_id=? OR related_candidate_id=?)")
            args.extend([candidate_id, candidate_id])
        if memory_id:
            clauses.append("(primary_memory_id=? OR related_memory_id=?)")
            args.extend([memory_id, memory_id])
        if len(clauses) == 1:
            return 0
        with self.transaction(immediate=True) as conn:
            return conn.execute(
                "UPDATE memory_review_items SET status='obsolete',updated_at=? WHERE "
                + " AND ".join(clauses),
                [utc_now(), *args],
            ).rowcount

    def create_audit_run(self, scope: str) -> AuditRun:
        if scope not in {"incremental", "full", "legacy"}:
            raise ValueError(scope)
        run_id = str(uuid.uuid4())
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO memory_audit_runs(id,scope,status,started_at) VALUES(?,?,?,?)",
                (run_id, scope, "running", now),
            )
            row = conn.execute("SELECT * FROM memory_audit_runs WHERE id=?", (run_id,)).fetchone()
        return self._audit_run_from_row(row)

    def finish_audit_run(
        self,
        run_id: str,
        *,
        checked_count: int,
        issue_count: int,
        error: str | None = None,
    ) -> AuditRun:
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            changed = conn.execute(
                "UPDATE memory_audit_runs SET status=?,checked_count=?,issue_count=?,"
                "finished_at=?,error=? WHERE id=?",
                (
                    "failed" if error else "completed",
                    checked_count,
                    issue_count,
                    now,
                    error[:2000] if error else None,
                    run_id,
                ),
            ).rowcount
            if not changed:
                raise KeyError(run_id)
            row = conn.execute("SELECT * FROM memory_audit_runs WHERE id=?", (run_id,)).fetchone()
        return self._audit_run_from_row(row)

    def latest_audit_run(self, scope: str = "full") -> AuditRun | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM memory_audit_runs WHERE scope=? ORDER BY started_at DESC LIMIT 1",
                (scope,),
            ).fetchone()
        return self._audit_run_from_row(row) if row else None

    def merge_memories(self, canonical_id: str, duplicate_id: str) -> MemoryRecord:
        if canonical_id == duplicate_id:
            memory = self.get_memory(canonical_id)
            if not memory:
                raise KeyError(canonical_id)
            return memory
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            canonical = conn.execute("SELECT * FROM memories WHERE id=?", (canonical_id,)).fetchone()
            duplicate = conn.execute("SELECT * FROM memories WHERE id=?", (duplicate_id,)).fetchone()
            if not canonical or not duplicate:
                raise KeyError(canonical_id if not canonical else duplicate_id)
            if canonical["status"] != "active" or duplicate["status"] != "active":
                raise ValueError("Only active memories can be merged")
            conn.execute("UPDATE evidence SET memory_id=? WHERE memory_id=?", (canonical_id, duplicate_id))
            conn.execute(
                "UPDATE candidates SET promoted_memory_id=? WHERE promoted_memory_id=?",
                (canonical_id, duplicate_id),
            )
            for link in conn.execute(
                "SELECT * FROM subject_links WHERE object_type='memory' AND object_id=?",
                (duplicate_id,),
            ).fetchall():
                conn.execute(
                    "INSERT INTO subject_links(id,subject_id,object_type,object_id,confidence,"
                    "assignment_status,method,created_at,updated_at) VALUES(?,?, 'memory',?,?,?,?,?,?) "
                    "ON CONFLICT(subject_id,object_type,object_id) DO UPDATE SET "
                    "confidence=max(confidence,excluded.confidence),updated_at=excluded.updated_at",
                    (str(uuid.uuid4()), link["subject_id"], canonical_id, link["confidence"],
                     link["assignment_status"], "memory_merge", link["created_at"], now),
                )
            conn.execute(
                "DELETE FROM subject_links WHERE object_type='memory' AND object_id=?", (duplicate_id,)
            )
            for summary in conn.execute(
                "SELECT id,source_ids_json FROM summary_versions WHERE source_ids_json LIKE ?",
                (f'%\"{duplicate_id}\"%',),
            ).fetchall():
                source_ids = [
                    canonical_id if str(item) == duplicate_id else str(item)
                    for item in json.loads(summary["source_ids_json"])
                ]
                conn.execute(
                    "UPDATE summary_versions SET source_ids_json=? WHERE id=?",
                    (json.dumps(list(dict.fromkeys(source_ids))), summary["id"]),
                )
            conn.execute(
                "UPDATE memories SET status='superseded',temporal_status='historical',"
                "valid_to=coalesce(valid_to,?),temporal_reason='Merged into canonical memory',"
                "updated_at=? WHERE id=?",
                (now, now, duplicate_id),
            )
            self._add_event(
                conn,
                "memory_merged",
                memory_id=canonical_id,
                occurred_at=now,
                data={"absorbed_memory_id": duplicate_id},
            )
            self._add_event(
                conn,
                "memory_superseded",
                memory_id=duplicate_id,
                occurred_at=now,
                data={"canonical_memory_id": canonical_id, "reason": "merge"},
            )
            row = conn.execute("SELECT * FROM memories WHERE id=?", (canonical_id,)).fetchone()
        return self._memory_from_row(row)

    def update_memory_temporal(
        self,
        record_id: str,
        *,
        valid_from: str | None = None,
        valid_to: str | None = None,
        temporal_status: str = "current",
        temporal_reason: str | None = None,
    ) -> MemoryRecord:
        if temporal_status not in {"current", "historical", "disputed"}:
            raise ValueError("Unsupported temporal status")
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
                "UPDATE memories SET valid_from=?,valid_to=?,temporal_status=?,temporal_reason=?,updated_at=? WHERE id=?",
                (valid_from, valid_to, temporal_status, temporal_reason, now, record_id),
            )
            self._add_event(
                conn,
                "memory_temporal_updated",
                memory_id=record_id,
                occurred_at=now,
                data={
                    "valid_from": valid_from,
                    "valid_to": valid_to,
                    "temporal_status": temporal_status,
                    "temporal_reason": temporal_reason,
                },
            )
            row = conn.execute("SELECT * FROM memories WHERE id=?", (record_id,)).fetchone()
        return self._memory_from_row(row)

    def refresh_temporal_statuses(self, *, now: str | None = None) -> int:
        current = now or utc_now()
        changed = 0
        with self.transaction(immediate=True) as conn:
            rows = conn.execute(
                "SELECT id,valid_to FROM memories WHERE status='active' "
                "AND temporal_status='current' AND valid_to IS NOT NULL "
                "AND CASE WHEN length(valid_to)=10 THEN date(valid_to)<date(?) "
                "ELSE datetime(valid_to)<datetime(?) END",
                (current, current),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE memories SET temporal_status='historical',"
                    "temporal_reason=coalesce(temporal_reason,'Validity interval ended'),updated_at=? WHERE id=?",
                    (current, row["id"]),
                )
                self._add_event(
                    conn, "memory_temporal_changed", memory_id=row["id"], occurred_at=current,
                    data={"temporal_status": "historical", "reason": "valid_to_elapsed"},
                )
                changed += 1
        return changed

    def supersede_memory(self, new_memory_id: str, old_memory_id: str) -> MemoryRecord:
        if new_memory_id == old_memory_id:
            raise ValueError("A memory cannot supersede itself")
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            new = conn.execute("SELECT * FROM memories WHERE id=?", (new_memory_id,)).fetchone()
            old = conn.execute("SELECT * FROM memories WHERE id=?", (old_memory_id,)).fetchone()
            if not new or not old:
                raise KeyError(new_memory_id if not new else old_memory_id)
            conn.execute(
                "UPDATE memories SET supersedes_id=?,updated_at=? WHERE id=?",
                (old_memory_id, now, new_memory_id),
            )
            conn.execute(
                "UPDATE memories SET status='superseded',temporal_status='historical',"
                "valid_to=coalesce(valid_to,?),temporal_reason='Superseded by newer memory',"
                "updated_at=? WHERE id=?",
                (now, now, old_memory_id),
            )
            self._add_event(
                conn,
                "memory_superseded",
                memory_id=old_memory_id,
                occurred_at=now,
                data={"replacement_memory_id": new_memory_id},
            )
            row = conn.execute("SELECT * FROM memories WHERE id=?", (new_memory_id,)).fetchone()
        return self._memory_from_row(row)

    def apply_dream_consolidation(
        self,
        *,
        creates: list[dict[str, Any]],
        reviews: list[dict[str, Any]],
        dream_run_id: str,
    ) -> dict[str, Any]:
        """Validate and apply a complete Deep batch in one transaction."""
        candidate_ids = [str(item.get("candidate_id", "")) for item in [*creates, *reviews]]
        if not candidate_ids or any(not item for item in candidate_ids):
            if candidate_ids:
                raise ValueError("Deep batch contains an empty candidate ID")
            return {"promoted": [], "reviews": []}
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("Deep batch contains duplicate candidate IDs")
        now = utc_now()
        promoted: list[str] = []
        created_reviews: list[str] = []
        with self.transaction(immediate=True) as conn:
            candidates = {
                row["id"]: row
                for row in conn.execute(
                    f"SELECT * FROM candidates WHERE id IN ({','.join('?' for _ in candidate_ids)})",
                    candidate_ids,
                ).fetchall()
            }
            if set(candidates) != set(candidate_ids) or any(
                row["status"] != "pending" or row["admission_state"] != "admitted"
                for row in candidates.values()
            ):
                raise ValueError("Deep batch references an unavailable candidate")
            for item in reviews:
                target_value = item.get("target_memory_id")
                target_id = str(target_value).strip() if target_value else ""
                if target_id and not conn.execute(
                    "SELECT 1 FROM memories WHERE id=? AND status='active'", (target_id,)
                ).fetchone():
                    raise ValueError(f"Deep batch references unknown memory ID: {target_id}")

            for item in creates:
                candidate_id = str(item["candidate_id"])
                candidate = candidates[candidate_id]
                memory_content = str(item.get("content", "")).strip()
                if not memory_content:
                    raise ValueError("Deep create action has empty content")
                memory = self._promote_candidate_in_tx(
                    conn, candidate, memory_content=memory_content, origin="dream",
                    promotion_lane="different_dates", dream_run_id=dream_run_id, now=now,
                )
                memory_id = str(memory["id"])
                promoted.append(memory_id)

            for item in reviews:
                candidate_id = str(item["candidate_id"])
                target_value = item.get("target_memory_id")
                target_id = str(target_value).strip() if target_value else None
                proposed_content = str(item.get("content", "")).strip() or candidates[candidate_id]["content"]
                issue_type = str(item.get("issue_type", item.get("action", "defer")))
                basis_hash = str(item.get("basis_hash", ""))
                fingerprint = self.make_review_fingerprint(
                    issue_type,
                    proposed_content=proposed_content,
                    candidate_id=candidate_id,
                    related_memory_id=target_id,
                    basis_hash=basis_hash,
                )
                existing_review = conn.execute(
                    "SELECT id FROM memory_review_items WHERE fingerprint=?", (fingerprint,)
                ).fetchone()
                if existing_review:
                    created_reviews.append(existing_review["id"])
                    continue
                review_id = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO memory_review_items(
                        id,issue_type,status,proposed_action,proposed_content,reason,confidence,
                        candidate_id,related_memory_id,source,basis_hash,fingerprint,dream_run_id,
                        created_at,updated_at
                    ) VALUES(?,?,'open',?,?,?,?,?,?,'dream',?,?,?,?,?)""",
                    (
                        review_id,
                        issue_type,
                        str(item.get("action", "defer")),
                        proposed_content,
                        str(item.get("reason", "Deep integration requires review"))[:2000],
                        max(0.0, min(1.0, float(item.get("confidence", 0.0)))),
                        candidate_id,
                        target_id,
                        basis_hash,
                        fingerprint,
                        dream_run_id,
                        now,
                        now,
                    ),
                )
                self._add_event(
                    conn,
                    "integration_review_created",
                    candidate_id=candidate_id,
                    memory_id=target_id,
                    dream_run_id=dream_run_id,
                    occurred_at=now,
                    data={
                        "review_id": review_id,
                        "issue_type": issue_type,
                        "proposed_action": str(item.get("action", "defer")),
                    },
                )
                created_reviews.append(review_id)
        return {"promoted": promoted, "reviews": created_reviews}

    def purge_candidate(
        self,
        candidate_id: str,
        *,
        privacy: bool = False,
        timezone_name: str = "system",
    ) -> dict[str, int]:
        with self.connect() as scan:
            candidate = scan.execute("SELECT id,content FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            if not candidate:
                raise KeyError(candidate_id)
            raw_ids = [row[0] for row in scan.execute(
                "SELECT DISTINCT raw_turn_id FROM evidence WHERE candidate_id=? AND raw_turn_id IS NOT NULL",
                (candidate_id,),
            )]
            affected_candidate_ids: list[str] = []
            if privacy and raw_ids:
                placeholders = ",".join("?" for _ in raw_ids)
                affected_candidate_ids = [row[0] for row in scan.execute(
                    f"SELECT DISTINCT candidate_id FROM evidence WHERE raw_turn_id IN ({placeholders}) "
                    "AND candidate_id IS NOT NULL AND candidate_id<>?", [*raw_ids, candidate_id],
                )]
            dream_run_ids: set[str] = set()
            call_ids: set[str] = set()
            references = [("candidate", candidate_id), *[("raw_turn", raw_id) for raw_id in raw_ids]]
            if privacy:
                for record_type, record_id in references:
                    linked = scan.execute(
                        "SELECT DISTINCT mc.id,mc.dream_run_id FROM model_calls mc "
                        "JOIN model_call_records mcr ON mcr.call_id=mc.id "
                        "WHERE mcr.record_type=? AND mcr.record_id=?", (record_type, record_id),
                    ).fetchall()
                    call_ids.update(row["id"] for row in linked)
                    dream_run_ids.update(row["dream_run_id"] for row in linked if row["dream_run_id"])
                for call in scan.execute(
                    "SELECT id,dream_run_id,request_json,response_json FROM model_calls WHERE dream_run_id IS NOT NULL"
                ):
                    stored = f"{call['request_json']}\n{call['response_json'] or ''}"
                    if candidate["content"] in stored:
                        call_ids.add(call["id"])
                        dream_run_ids.add(call["dream_run_id"])
        with self.transaction(immediate=True) as conn:
            if not conn.execute("SELECT 1 FROM candidates WHERE id=?", (candidate_id,)).fetchone():
                raise KeyError(candidate_id)
            if privacy:
                linked_memories = conn.execute(
                    "SELECT DISTINCT memory_id FROM evidence WHERE candidate_id=? AND memory_id IS NOT NULL",
                    (candidate_id,),
                ).fetchall()
                for linked_memory in linked_memories:
                    memory_id = str(linked_memory["memory_id"])
                    remaining = int(conn.execute(
                        "SELECT count(*) FROM evidence WHERE memory_id=? AND candidate_id<>?",
                        (memory_id, candidate_id),
                    ).fetchone()[0])
                    if remaining <= 1:
                        self._create_evidence_impact_review_in_tx(
                            conn, memory_id, source="candidate_privacy_delete",
                            basis_hash=f"candidate:{candidate_id}",
                        )
            if privacy:
                for run_id in dream_run_ids:
                    conn.execute("DELETE FROM dream_runs WHERE id=?", (run_id,))
                for record_type, record_id in references:
                    conn.execute(
                        "DELETE FROM model_call_records WHERE record_type=? AND record_id=?",
                        (record_type, record_id),
                    )
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
            conn.execute(
                "DELETE FROM admission_decisions WHERE candidate_id=?", (candidate_id,)
            )
            conn.execute(
                "DELETE FROM subject_links WHERE object_type='candidate' AND object_id=?",
                (candidate_id,),
            )
            conn.execute("DELETE FROM candidates WHERE id=?", (candidate_id,))
            if privacy and raw_ids:
                deleted_work_items = self._purge_work_items_for_raw_ids(conn, raw_ids)
                conn.executemany(
                    "DELETE FROM subject_links WHERE object_type='raw_turn' AND object_id=?",
                    [(item,) for item in raw_ids],
                )
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
            for call_id in call_ids:
                conn.execute(
                    "DELETE FROM model_calls WHERE id=? AND NOT EXISTS "
                    "(SELECT 1 FROM model_call_records WHERE call_id=?)", (call_id, call_id),
                )
        return {
            "candidates": 1,
            "raw_turns": len(raw_ids) if privacy else 0,
            "work_items": deleted_work_items if privacy and raw_ids else 0,
            "dream_runs": len(dream_run_ids),
        }

    @staticmethod
    def _purge_work_items_for_raw_ids(conn: sqlite3.Connection, raw_ids: list[str]) -> int:
        if not raw_ids:
            return 0
        placeholders = ",".join("?" for _ in raw_ids)
        work_item_ids = [
            str(row[0])
            for row in conn.execute(
                f"SELECT id FROM work_items WHERE raw_turn_id IN ({placeholders})", raw_ids
            )
        ]
        for item_id in work_item_ids:
            conn.execute(
                "DELETE FROM subject_links WHERE object_type='work_item' AND object_id=?",
                (item_id,),
            )
            conn.execute(
                "DELETE FROM recall_events WHERE source='work_item' AND record_id=?", (item_id,)
            )
            conn.execute(
                "DELETE FROM embeddings WHERE source='work_item' AND record_id=?", (item_id,)
            )
            conn.execute(
                "DELETE FROM search_fts WHERE source='work_item' AND record_id=?", (item_id,)
            )
            conn.execute("DELETE FROM projection_jobs WHERE target_id=?", (item_id,))
            conn.execute(
                "DELETE FROM summary_versions WHERE source_ids_json LIKE ?",
                (f'%\"{item_id}\"%',),
            )
        if work_item_ids:
            item_placeholders = ",".join("?" for _ in work_item_ids)
            conn.execute(f"DELETE FROM work_items WHERE id IN ({item_placeholders})", work_item_ids)
        return len(work_item_ids)

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
        batch_size = 200
        with self.connect() as conn:
            expiring_ids = [row[0] for row in conn.execute(
                "SELECT id FROM candidates WHERE status='pending' AND coalesce(last_activity_at,last_seen_at) < ?",
                (inactive_cutoff,),
            )]
        reason = f"{candidate_inactive_days} 天无活动自动过期"
        expired = 0
        for start in range(0, len(expiring_ids), batch_size):
            with self.transaction(immediate=True) as conn:
                for candidate_id in expiring_ids[start : start + batch_size]:
                    changed = conn.execute(
                        "UPDATE candidates SET status='expired',expired_at=?,rem_reason=coalesce(rem_reason,?) "
                        "WHERE id=? AND status='pending'", (current_iso, reason, candidate_id),
                    ).rowcount
                    if changed:
                        expired += 1
                        self._add_event(conn, "candidate_expired", candidate_id=candidate_id,
                                        occurred_at=current_iso, data={"reason": reason, "source": "retention"})
        with self.connect() as conn:
            purge_ids = [row[0] for row in conn.execute(
                "SELECT id FROM candidates WHERE "
                "(status='expired' AND expired_at IS NOT NULL AND expired_at < ?) OR "
                "(status='rejected' AND rejected_at IS NOT NULL AND rejected_at < ?)",
                (expired_cutoff, rejected_cutoff),
            )]
        for start in range(0, len(purge_ids), batch_size):
            with self.transaction(immediate=True) as conn:
                for candidate_id in purge_ids[start : start + batch_size]:
                    conn.execute("DELETE FROM admission_decisions WHERE candidate_id=?", (candidate_id,))
                    conn.execute("DELETE FROM recall_events WHERE record_id=? AND source='candidate'", (candidate_id,))
                    conn.execute("DELETE FROM embeddings WHERE record_id=? AND source='candidate'", (candidate_id,))
                    conn.execute("DELETE FROM search_fts WHERE record_id=? AND source='candidate'", (candidate_id,))
                    conn.execute("DELETE FROM model_call_records WHERE record_type='candidate' AND record_id=?", (candidate_id,))
                    conn.execute("DELETE FROM subject_links WHERE object_type='candidate' AND object_id=?", (candidate_id,))
                    conn.execute("DELETE FROM candidates WHERE id=?", (candidate_id,))

        def delete_batched(table: str, column: str, cutoff: str) -> int:
            total = 0
            while True:
                with self.transaction(immediate=True) as conn:
                    changed = conn.execute(
                        f"DELETE FROM {table} WHERE rowid IN "
                        f"(SELECT rowid FROM {table} WHERE {column} < ? LIMIT {batch_size})",
                        (cutoff,),
                    ).rowcount
                total += changed
                if changed < batch_size:
                    return total

        raw = delete_batched("raw_turns", "observed_at", raw_cutoff)
        calls = delete_batched("model_calls", "created_at", model_cutoff)
        traces = delete_batched("recall_events", "created_at", model_cutoff)
        admission_logs = delete_batched("admission_decisions", "created_at", model_cutoff)
        with self.connect() as conn:
            evidence_days = [
                (self._count_evidence_days(conn, row[0], timezone_name), row[0])
                for row in conn.execute("SELECT id FROM candidates")
            ]
        for start in range(0, len(evidence_days), batch_size):
            with self.transaction(immediate=True) as conn:
                conn.executemany(
                    "UPDATE candidates SET evidence_days=? WHERE id=?",
                    evidence_days[start : start + batch_size],
                )
        return {
            "raw_turns": raw,
            "model_calls": calls,
            "recall_events": traces,
            "admission_decisions": admission_logs,
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
        secure_directory(target.parent)
        source = self.connect()
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        secure_file(target)

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
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            temporal_status=row["temporal_status"] or "current",
            temporal_reason=row["temporal_reason"],
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
            promoted_memory_id=row["promoted_memory_id"],
            rem_status=row["rem_status"] or "unreviewed",
            rem_reason=row["rem_reason"],
            rem_reviewed_at=row["rem_reviewed_at"],
            score_components=json.loads(row["score_components"] or "{}"),
            conflict_memory_id=row["conflict_memory_id"],
            conflict_reason=row["conflict_reason"],
            admission_state=row["admission_state"] or "admitted",
            source_type=row["source_type"] or "dream_user",
            admission_reason=row["admission_reason"],
        )

    def _review_from_row(self, row: sqlite3.Row) -> ReviewItem:
        return ReviewItem(
            id=row["id"],
            issue_type=row["issue_type"],
            status=row["status"],
            proposed_action=row["proposed_action"],
            proposed_content=row["proposed_content"],
            reason=row["reason"],
            confidence=float(row["confidence"]),
            candidate_id=row["candidate_id"],
            related_candidate_id=row["related_candidate_id"],
            primary_memory_id=row["primary_memory_id"],
            related_memory_id=row["related_memory_id"],
            source=row["source"],
            basis_hash=row["basis_hash"],
            fingerprint=row["fingerprint"],
            dream_run_id=row["dream_run_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            resolved_at=row["resolved_at"],
            resolution=row["resolution"],
            subject_id=row["subject_id"],
            queue=row["queue"] or "decision",
            proposal=json.loads(row["proposal_json"] or "{}"),
        )

    def _audit_run_from_row(self, row: sqlite3.Row) -> AuditRun:
        return AuditRun(
            id=row["id"],
            scope=row["scope"],
            status=row["status"],
            checked_count=int(row["checked_count"]),
            issue_count=int(row["issue_count"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error=row["error"],
        )
