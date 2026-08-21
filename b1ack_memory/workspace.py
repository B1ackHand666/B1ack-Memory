from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
import uuid
from difflib import SequenceMatcher
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .db import SCHEMA_VERSION, MemoryDatabase, content_hash, eligible_memory_predicate, utc_now
from .llm import LlmError, OpenAICompatibleClient
from .security import permission_report, secure_directory, secure_file

SUBJECT_TYPES = {"project", "person", "organization", "tool", "topic"}
SUBJECT_STATUSES = {"active", "paused", "archived"}
WORK_ITEM_TYPES = {"decision", "current_state", "open_question", "proposal", "milestone"}
WORK_ITEM_STATUSES = {"suggested", "active", "resolved", "expired", "archived"}
WORK_ITEM_TTL_DAYS = {
    "decision": None,
    "current_state": 30,
    "open_question": 30,
    "proposal": 14,
    "milestone": 90,
}

SUMMARY_SYSTEM = """Create a compact, grounded project or topic brief. Return JSON only with:
summary, change_reason, and statements. statements is an array of objects with text and source_ids.
Every statement must cite one or more supplied source IDs. Never cite an unknown ID, invent a fact,
or turn a proposal into a confirmed decision. Prefer current state, confirmed decisions, open questions,
and durable facts. Historical and suggested items are context only and must not be stated as current."""


def _normal(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _alias_in_query(alias: str, query: str) -> bool:
    if not alias:
        return False
    if re.search(r"[\u3400-\u9fff\uf900-\ufaff]", alias):
        return alias == query if len(alias) == 1 else alias in query
    return bool(re.search(rf"(?<![\w-]){re.escape(alias)}(?![\w-])", query, re.UNICODE))


def _slug(value: str) -> str:
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", _normal(value)).strip("-")
    if ascii_slug:
        return ascii_slug[:60]
    return "subject-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]


class WorkspaceManager:
    def __init__(
        self,
        db: MemoryDatabase,
        root: Path,
        client_factory: Callable[[], OpenAICompatibleClient] | None = None,
    ):
        self.db = db
        self.root = root
        self.vault = root / "vault"
        self.indexes = root / "indexes"
        self.client_factory = client_factory

    # Subjects and project assignment -------------------------------------------------
    def create_subject(
        self,
        name: str,
        *,
        subject_type: str = "project",
        description: str = "",
        aliases: list[str] | None = None,
    ) -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise ValueError("Subject name is required")
        if subject_type not in SUBJECT_TYPES:
            raise ValueError("Unsupported subject type")
        now = utc_now()
        subject_id = str(uuid.uuid4())
        base = _slug(name)
        with self.db.transaction(immediate=True) as conn:
            slug = base
            suffix = 2
            while conn.execute("SELECT 1 FROM subjects WHERE slug=?", (slug,)).fetchone():
                slug = f"{base}-{suffix}"
                suffix += 1
            conn.execute(
                "INSERT INTO subjects(id,subject_type,name,slug,status,description,created_at,updated_at) "
                "VALUES(?,?,?,?, 'active',?,?,?)",
                (subject_id, subject_type, name, slug, description.strip(), now, now),
            )
            self._insert_alias(conn, subject_id, name, "name", now)
            for alias in aliases or []:
                self._insert_alias(conn, subject_id, alias, "name", now)
        self.queue_projection("subject", subject_id)
        return self.get_subject(subject_id)

    def list_subjects(
        self, *, subject_type: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if subject_type:
            if subject_type not in SUBJECT_TYPES:
                raise ValueError("Unsupported subject type")
            clauses.append("s.subject_type=?")
            args.append(subject_type)
        if status:
            if status not in SUBJECT_STATUSES:
                raise ValueError("Unsupported subject status")
            clauses.append("s.status=?")
            args.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT s.*,"
                "(SELECT COUNT(*) FROM work_items wi WHERE wi.subject_id=s.id "
                "AND wi.status IN ('suggested','active')) AS work_item_count,"
                "(SELECT COUNT(*) FROM subject_links sl WHERE sl.subject_id=s.id) AS link_count "
                f"FROM subjects s{where} ORDER BY s.status,s.updated_at DESC",
                args,
            ).fetchall()
            return [self._subject_dict(conn, row) for row in rows]

    def get_subject(self, subject_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM subjects WHERE id=?", (subject_id,)).fetchone()
            if not row:
                raise KeyError(subject_id)
            result = self._subject_dict(conn, row)
            result["relations"] = [
                dict(item)
                for item in conn.execute(
                    "SELECT sr.*,s.name AS target_name FROM subject_relations sr "
                    "JOIN subjects s ON s.id=sr.target_subject_id "
                    "WHERE sr.source_subject_id=? AND sr.status='active'",
                    (subject_id,),
                )
            ]
            result["links"] = [
                dict(item)
                for item in conn.execute(
                    "SELECT * FROM subject_links WHERE subject_id=? ORDER BY object_type,updated_at DESC",
                    (subject_id,),
                )
            ]
            return result

    def update_subject(self, subject_id: str, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {"name", "description", "status"}
        unknown = set(values) - allowed - {"aliases", "workspace_aliases"}
        if unknown:
            raise ValueError(f"Unsupported subject fields: {', '.join(sorted(unknown))}")
        now = utc_now()
        with self.db.transaction(immediate=True) as conn:
            old = conn.execute("SELECT * FROM subjects WHERE id=?", (subject_id,)).fetchone()
            if not old:
                raise KeyError(subject_id)
            name = str(values.get("name", old["name"])).strip()
            status = str(values.get("status", old["status"]))
            if not name or status not in SUBJECT_STATUSES:
                raise ValueError("Invalid subject name or status")
            conn.execute(
                "UPDATE subjects SET name=?,description=?,status=?,updated_at=? WHERE id=?",
                (name, str(values.get("description", old["description"])).strip(), status, now, subject_id),
            )
            self._insert_alias(conn, subject_id, name, "name", now)
            if "aliases" in values:
                conn.execute("DELETE FROM subject_aliases WHERE subject_id=? AND alias_type='name'", (subject_id,))
                self._insert_alias(conn, subject_id, name, "name", now)
                for alias in values.get("aliases") or []:
                    self._insert_alias(conn, subject_id, str(alias), "name", now)
            if "workspace_aliases" in values:
                conn.execute("DELETE FROM subject_aliases WHERE subject_id=? AND alias_type='workspace'", (subject_id,))
                for alias in values.get("workspace_aliases") or []:
                    self._insert_alias(conn, subject_id, str(alias), "workspace", now)
        self.queue_projection("subject", subject_id)
        return self.get_subject(subject_id)

    def merge_subjects(self, canonical_id: str, source_id: str) -> dict[str, Any]:
        if canonical_id == source_id:
            raise ValueError("Cannot merge a subject into itself")
        now = utc_now()
        with self.db.transaction(immediate=True) as conn:
            canonical = conn.execute("SELECT * FROM subjects WHERE id=?", (canonical_id,)).fetchone()
            source = conn.execute("SELECT * FROM subjects WHERE id=?", (source_id,)).fetchone()
            if not canonical or not source:
                raise KeyError(source_id if not source else canonical_id)
            if canonical["subject_type"] != source["subject_type"]:
                raise ValueError("Only subjects of the same type can be merged")
            for alias in conn.execute("SELECT alias,alias_type FROM subject_aliases WHERE subject_id=?", (source_id,)):
                self._insert_alias(conn, canonical_id, alias["alias"], alias["alias_type"], now)
            for item in conn.execute("SELECT * FROM work_items WHERE subject_id=?", (source_id,)).fetchall():
                duplicate = None
                if item["status"] in {"suggested", "active"}:
                    duplicate = conn.execute(
                        "SELECT id FROM work_items WHERE subject_id=? AND content_hash=? "
                        "AND status IN ('suggested','active') LIMIT 1",
                        (canonical_id, item["content_hash"]),
                    ).fetchone()
                if duplicate:
                    conn.execute(
                        "INSERT INTO work_item_revisions(work_item_id,snapshot_json,changed_at,change_reason) "
                        "VALUES(?,?,?,'subject_merge_duplicate')",
                        (item["id"], json.dumps(dict(item), ensure_ascii=False), now),
                    )
                    conn.execute(
                        "UPDATE work_items SET subject_id=?,status='archived',updated_at=? WHERE id=?",
                        (canonical_id, now, item["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE work_items SET subject_id=?,updated_at=? WHERE id=?",
                        (canonical_id, now, item["id"]),
                    )
            scopes = [
                row[0] for row in conn.execute(
                    "SELECT DISTINCT scope FROM summary_versions WHERE subject_id=?", (source_id,)
                )
            ]
            for scope in scopes:
                if conn.execute(
                    "SELECT 1 FROM summary_versions WHERE subject_id=? AND scope=? AND status='current'",
                    (canonical_id, scope),
                ).fetchone():
                    conn.execute(
                        "UPDATE summary_versions SET status='superseded' WHERE subject_id=? "
                        "AND scope=? AND status='current'", (source_id, scope),
                    )
            conn.execute("UPDATE summary_versions SET subject_id=? WHERE subject_id=?", (canonical_id, source_id))
            conn.execute("UPDATE session_subject_affinity SET subject_id=?,updated_at=? WHERE subject_id=?", (canonical_id, now, source_id))
            conn.execute("UPDATE candidates SET subject_id=? WHERE subject_id=?", (canonical_id, source_id))
            conn.execute("UPDATE raw_turns SET subject_id=? WHERE subject_id=?", (canonical_id, source_id))
            conn.execute("UPDATE memory_review_items SET subject_id=? WHERE subject_id=?", (canonical_id, source_id))
            relations = conn.execute(
                "SELECT * FROM subject_relations WHERE source_subject_id=? OR target_subject_id=?",
                (source_id, source_id),
            ).fetchall()
            for relation in relations:
                relation_source = canonical_id if relation["source_subject_id"] == source_id else relation["source_subject_id"]
                relation_target = canonical_id if relation["target_subject_id"] == source_id else relation["target_subject_id"]
                if relation_source != relation_target:
                    conn.execute(
                        "INSERT INTO subject_relations(id,source_subject_id,relation_type,target_subject_id,"
                        "confidence,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(source_subject_id,relation_type,target_subject_id) DO UPDATE SET "
                        "confidence=max(confidence,excluded.confidence),status='active',updated_at=excluded.updated_at",
                        (str(uuid.uuid4()), relation_source, relation["relation_type"], relation_target,
                         relation["confidence"], relation["status"], relation["created_at"], now),
                    )
                conn.execute("DELETE FROM subject_relations WHERE id=?", (relation["id"],))
            links = conn.execute("SELECT * FROM subject_links WHERE subject_id=?", (source_id,)).fetchall()
            for link in links:
                conn.execute(
                    "INSERT INTO subject_links(id,subject_id,object_type,object_id,confidence,"
                    "assignment_status,method,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(subject_id,object_type,object_id) DO UPDATE SET "
                    "confidence=max(confidence,excluded.confidence),updated_at=excluded.updated_at",
                    (str(uuid.uuid4()), canonical_id, link["object_type"], link["object_id"], link["confidence"],
                     link["assignment_status"], "merge", link["created_at"], now),
                )
            conn.execute("DELETE FROM subject_links WHERE subject_id=?", (source_id,))
            for job in conn.execute(
                "SELECT * FROM projection_jobs WHERE target_id=?", (source_id,)
            ).fetchall():
                conn.execute(
                    "INSERT OR IGNORE INTO projection_jobs(id,projection_type,target_id,revision,status,attempts,"
                    "error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), job["projection_type"], canonical_id, job["revision"], job["status"],
                     job["attempts"], job["error"], job["created_at"], now),
                )
            conn.execute("DELETE FROM projection_jobs WHERE target_id=?", (source_id,))
            conn.execute("DELETE FROM subjects WHERE id=?", (source_id,))
            conn.execute("UPDATE subjects SET updated_at=? WHERE id=?", (now, canonical_id))
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"Subject merge left foreign-key violations: {len(violations)}")
            conn.execute(
                "INSERT INTO audit_events(action,record_id,created_at) VALUES('subject-merge',?,?)",
                (f"{source_id}->{canonical_id}", now),
            )
        self.queue_projection("all", "")
        return self.get_subject(canonical_id)

    def split_subject(
        self,
        subject_id: str,
        *,
        name: str,
        object_ids: list[str] | None = None,
        work_item_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        source = self.get_subject(subject_id)
        created = self.create_subject(
            name,
            subject_type=str(source["subject_type"]),
            description=f"Split from {source['name']}",
        )
        target_id = str(created["id"])
        now = utc_now()
        with self.db.transaction(immediate=True) as conn:
            for object_id in object_ids or []:
                links = conn.execute(
                    "SELECT * FROM subject_links WHERE subject_id=? AND object_id=?",
                    (subject_id, object_id),
                ).fetchall()
                for link in links:
                    conn.execute(
                        "INSERT OR IGNORE INTO subject_links(id,subject_id,object_type,object_id,confidence,"
                        "assignment_status,method,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), target_id, link["object_type"], link["object_id"],
                         link["confidence"], "confirmed", "manual_split", now, now),
                    )
                conn.execute(
                    "DELETE FROM subject_links WHERE subject_id=? AND object_id=?",
                    (subject_id, object_id),
                )
            for item_id in work_item_ids or []:
                conn.execute(
                    "UPDATE work_items SET subject_id=?,updated_at=? WHERE id=? AND subject_id=?",
                    (target_id, now, item_id, subject_id),
                )
                conn.execute(
                    "DELETE FROM subject_links WHERE subject_id=? AND object_type='work_item' AND object_id=?",
                    (subject_id, item_id),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO subject_links(id,subject_id,object_type,object_id,confidence,"
                    "assignment_status,method,created_at,updated_at) VALUES(?,?, 'work_item',?,1,'confirmed','manual_split',?,?)",
                    (str(uuid.uuid4()), target_id, item_id, now, now),
                )
        self.queue_projection("all", "")
        return {"source": self.get_subject(subject_id), "created": self.get_subject(target_id)}

    def link_subject(
        self,
        subject_id: str,
        object_type: str,
        object_id: str,
        *,
        confidence: float = 1.0,
        assignment_status: str = "confirmed",
        method: str = "manual",
    ) -> dict[str, Any]:
        if assignment_status not in {"confirmed", "automatic", "suggested"}:
            raise ValueError("Unsupported assignment status")
        object_tables = {
            "memory": "memories",
            "candidate": "candidates",
            "raw_turn": "raw_turns",
            "work_item": "work_items",
            "summary": "summary_versions",
        }
        if object_type not in object_tables or not object_id.strip():
            raise ValueError("Unsupported or empty linked object")
        now = utc_now()
        with self.db.transaction(immediate=True) as conn:
            if not conn.execute("SELECT 1 FROM subjects WHERE id=?", (subject_id,)).fetchone():
                raise KeyError(subject_id)
            if not conn.execute(
                f"SELECT 1 FROM {object_tables[object_type]} WHERE id=?", (object_id,)
            ).fetchone():
                raise KeyError(object_id)
            conn.execute(
                "INSERT INTO subject_links(id,subject_id,object_type,object_id,confidence,"
                "assignment_status,method,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(subject_id,object_type,object_id) DO UPDATE SET "
                "confidence=excluded.confidence,assignment_status=excluded.assignment_status,"
                "method=excluded.method,updated_at=excluded.updated_at",
                (str(uuid.uuid4()), subject_id, object_type, object_id,
                 max(0.0, min(1.0, confidence)), assignment_status, method, now, now),
            )
            if object_type in {"memory", "candidate", "raw_turn"}:
                table = {"memory": "memories", "candidate": "candidates", "raw_turn": "raw_turns"}[object_type]
                if object_type != "memory":
                    conn.execute(f"UPDATE {table} SET subject_id=? WHERE id=?", (subject_id, object_id))
        self.queue_projection("subject", subject_id)
        return {"subject_id": subject_id, "object_type": object_type, "object_id": object_id}

    def unlink_subject(self, subject_id: str, object_type: str, object_id: str) -> dict[str, Any]:
        with self.db.transaction(immediate=True) as conn:
            deleted = conn.execute(
                "DELETE FROM subject_links WHERE subject_id=? AND object_type=? AND object_id=?",
                (subject_id, object_type, object_id),
            ).rowcount
            if object_type in {"candidate", "raw_turn"}:
                table = {"candidate": "candidates", "raw_turn": "raw_turns"}[object_type]
                conn.execute(
                    f"UPDATE {table} SET subject_id=NULL WHERE id=? AND subject_id=?",
                    (object_id, subject_id),
                )
        self.queue_projection("subject", subject_id)
        return {"ok": bool(deleted)}

    def add_relation(
        self, source_id: str, target_id: str, relation_type: str, *, confidence: float = 1.0
    ) -> dict[str, Any]:
        if source_id == target_id or not relation_type.strip():
            raise ValueError("A relation needs two different subjects and a type")
        relation_id = str(uuid.uuid4())
        now = utc_now()
        with self.db.transaction(immediate=True) as conn:
            count = conn.execute(
                "SELECT count(*) FROM subjects WHERE id IN (?,?)", (source_id, target_id)
            ).fetchone()[0]
            if count != 2:
                raise KeyError(target_id)
            conn.execute(
                "INSERT INTO subject_relations(id,source_subject_id,target_subject_id,relation_type,"
                "confidence,status,created_at,updated_at) VALUES(?,?,?,?,?,'active',?,?) "
                "ON CONFLICT(source_subject_id,target_subject_id,relation_type) DO UPDATE SET "
                "confidence=excluded.confidence,status='active',updated_at=excluded.updated_at",
                (relation_id, source_id, target_id, relation_type.strip(),
                 max(0.0, min(1.0, confidence)), now, now),
            )
            row = conn.execute(
                "SELECT * FROM subject_relations WHERE source_subject_id=? AND target_subject_id=? "
                "AND relation_type=?", (source_id, target_id, relation_type.strip())
            ).fetchone()
        return dict(row)

    def set_session_project(
        self,
        session_id: str,
        subject_id: str,
        *,
        confirmed: bool = True,
        confidence: float = 1.0,
        method: str = "manual",
        workspace: str | None = None,
    ) -> dict[str, Any]:
        if not session_id.strip():
            raise ValueError("session_id is required")
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT 1 FROM subjects WHERE id=? AND subject_type='project'", (subject_id,)
            ).fetchone()
            if not row:
                raise KeyError(subject_id)
            conn.execute(
                "INSERT INTO session_subject_affinity(session_id,subject_id,confidence,confirmed,method,workspace,updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET subject_id=excluded.subject_id,"
                "confidence=excluded.confidence,confirmed=excluded.confirmed,method=excluded.method,"
                "workspace=excluded.workspace,updated_at=excluded.updated_at",
                (session_id, subject_id, confidence, int(confirmed), method, workspace, utc_now()),
            )
        return {"session_id": session_id, "project_id": subject_id, "confirmed": confirmed}

    def identify_project(
        self,
        query: str,
        *,
        explicit_project_id: str | None = None,
        session_id: str = "",
        workspace: str | None = None,
    ) -> dict[str, Any]:
        with self.db.connect() as conn:
            if explicit_project_id:
                row = conn.execute(
                    "SELECT * FROM subjects WHERE id=? AND subject_type='project' AND status='active'",
                    (explicit_project_id,),
                ).fetchone()
                if row:
                    return {"project": dict(row), "confidence": 1.0, "reason": "explicit_project_id", "ambiguous": False}
                raise KeyError(explicit_project_id)
            if session_id:
                row = conn.execute(
                    "SELECT s.*,a.confidence,a.confirmed FROM session_subject_affinity a "
                    "JOIN subjects s ON s.id=a.subject_id WHERE a.session_id=? "
                    "AND s.status='active'",
                    (session_id,),
                ).fetchone()
                if row and bool(row["confirmed"]):
                    project = {key: row[key] for key in row.keys() if key not in {"confidence", "confirmed"}}
                    return {"project": project, "confidence": max(0.97, float(row["confidence"])), "reason": "confirmed_session", "ambiguous": False}
            if workspace:
                normalized = _normal(workspace)
                rows = conn.execute(
                    "SELECT s.* FROM subject_aliases a JOIN subjects s ON s.id=a.subject_id "
                    "WHERE a.alias_type='workspace' AND a.alias_normalized=? "
                    "AND s.subject_type='project' AND s.status='active' ORDER BY s.id",
                    (normalized,),
                ).fetchall()
                if len(rows) == 1:
                    return {"project": dict(rows[0]), "confidence": 0.98, "reason": "workspace_alias", "ambiguous": False}
                if len(rows) > 1:
                    return {"project": None, "confidence": 0.98, "reason": "ambiguous_workspace_alias", "ambiguous": True}
            projects = conn.execute(
                "SELECT s.*,a.alias,a.alias_normalized FROM subjects s "
                "JOIN subject_aliases a ON a.subject_id=s.id "
                "WHERE s.subject_type='project' AND s.status='active' AND a.alias_type='name'"
            ).fetchall()
        normalized_query = _normal(query)
        matches: dict[str, tuple[float, dict[str, Any]]] = {}
        exact_matches: dict[str, dict[str, Any]] = {}
        contained_matches: dict[str, dict[str, Any]] = {}
        for row in projects:
            alias = str(row["alias_normalized"])
            project = {key: row[key] for key in row.keys() if key not in {"alias", "alias_normalized"}}
            if alias and alias == normalized_query:
                score = 0.99
                exact_matches[str(row["id"])] = project
            elif _alias_in_query(alias, normalized_query):
                score = 0.95 if alias == _normal(str(row["name"])) else 0.93
                contained_matches[str(row["id"])] = project
            elif alias:
                # Fuzzy matches never cross the automatic-assignment threshold.
                # They exist only to create a user-visible organization suggestion.
                compact_query = normalized_query.replace(" ", "")
                compact_alias = alias.replace(" ", "")
                windows = (
                    [compact_query[index:index + len(compact_alias)]
                     for index in range(max(1, len(compact_query) - len(compact_alias) + 1))]
                    if compact_alias and len(compact_query) >= len(compact_alias)
                    else [compact_query]
                )
                minimum = 2 if re.search(r"[\u3400-\u9fff\uf900-\ufaff]", alias) else 3
                similarity = max(
                    (SequenceMatcher(None, compact_alias, window).ratio() for window in windows),
                    default=0.0,
                ) if len(compact_alias) >= minimum else 0.0
                score = 0.70 + min(0.19, (similarity - 0.70) * 0.64) if similarity >= 0.70 else 0.0
            else:
                score = 0.0
            if score:
                previous = matches.get(str(row["id"]))
                if not previous or score > previous[0]:
                    matches[str(row["id"])] = (score, project)
        if len(exact_matches) == 1:
            return {"project": next(iter(exact_matches.values())), "confidence": 0.99,
                    "reason": "exact_query_alias", "ambiguous": False}
        if len(exact_matches) > 1 or len(contained_matches) > 1:
            return {"project": None, "confidence": 0.95, "reason": "ambiguous_aliases", "ambiguous": True}
        if len(contained_matches) == 1:
            return {"project": next(iter(contained_matches.values())), "confidence": 0.95,
                    "reason": "query_alias", "ambiguous": False}
        ranked = sorted(matches.values(), key=lambda item: item[0], reverse=True)
        if not ranked:
            return {"project": None, "confidence": 0.0, "reason": "no_project_match", "ambiguous": False}
        lead = ranked[0][0] - (ranked[1][0] if len(ranked) > 1 else 0.0)
        ambiguous = len(ranked) > 1 and lead < 0.15
        return {
            "project": None if ambiguous else ranked[0][1],
            "confidence": ranked[0][0],
            "reason": "ambiguous_aliases" if ambiguous else "query_alias",
            "ambiguous": ambiguous,
        }

    # Working memory ------------------------------------------------------------------
    def create_work_item(
        self,
        content: str,
        *,
        item_type: str,
        confidence: float,
        subject_id: str | None = None,
        raw_turn_id: str | None = None,
        evidence_quote: str | None = None,
        admission_decision_id: str | None = None,
        confirmed: bool = False,
        assignment_confirmed: bool = True,
        source: str = "dream_observe",
    ) -> dict[str, Any]:
        content = content.strip()
        if not content:
            raise ValueError("Work item content is empty")
        if item_type not in WORK_ITEM_TYPES:
            item_type = "proposal"
        confidence = max(0.0, min(1.0, confidence))
        confirmed = bool(confirmed and evidence_quote and raw_turn_id)
        status = (
            "active"
            if (
                confirmed
                and assignment_confirmed
                and subject_id
                and confidence >= 0.90
                and item_type in {"decision", "current_state", "open_question"}
            )
            else "suggested"
        )
        days = WORK_ITEM_TTL_DAYS[item_type]
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat(timespec="seconds")
        expires_at = (now_dt + timedelta(days=days)).isoformat(timespec="seconds") if days else None
        digest = content_hash(content)
        with self.db.transaction(immediate=True) as conn:
            if subject_id and not conn.execute("SELECT 1 FROM subjects WHERE id=?", (subject_id,)).fetchone():
                subject_id = None
                status = "suggested"
            existing = conn.execute(
                "SELECT * FROM work_items WHERE content_hash=? AND coalesce(subject_id,'')=coalesce(?, '') "
                "AND status IN ('suggested','active') LIMIT 1",
                (digest, subject_id),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE work_items SET confidence=max(confidence,?),confirmed=max(confirmed,?),"
                    "status=CASE WHEN status='suggested' AND ?='active' THEN 'active' ELSE status END,"
                    "raw_turn_id=coalesce(?,raw_turn_id),evidence_quote=coalesce(?,evidence_quote),"
                    "admission_decision_id=coalesce(?,admission_decision_id),expires_at=?,updated_at=? WHERE id=?",
                    (confidence, int(confirmed), status, raw_turn_id, evidence_quote,
                     admission_decision_id, expires_at, now, existing["id"]),
                )
                item_id = str(existing["id"])
            else:
                item_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO work_items(id,item_type,content,content_hash,status,confirmed,confidence,"
                    "subject_id,raw_turn_id,admission_decision_id,evidence_quote,source,expires_at,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (item_id, item_type, content, digest, status, int(confirmed), confidence, subject_id,
                     raw_turn_id, admission_decision_id, evidence_quote, source, expires_at, now, now),
                )
            if subject_id:
                conn.execute(
                    "INSERT INTO subject_links(id,subject_id,object_type,object_id,confidence,assignment_status,method,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(subject_id,object_type,object_id) DO NOTHING",
                    (str(uuid.uuid4()), subject_id, "work_item", item_id, confidence,
                     "automatic" if assignment_confirmed and status == "active" else "suggested",
                     source, now, now),
                )
        self.queue_projection("subject" if subject_id else "profile", subject_id or "")
        return self.get_work_item(item_id)

    def list_work_items(
        self,
        *,
        status: str | None = None,
        subject_id: str | None = None,
        item_type: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if status:
            if status not in WORK_ITEM_STATUSES:
                raise ValueError("Unsupported work item status")
            clauses.append("wi.status=?")
            args.append(status)
        if subject_id:
            clauses.append("wi.subject_id=?")
            args.append(subject_id)
        if item_type:
            if item_type not in WORK_ITEM_TYPES:
                raise ValueError("Unsupported work item type")
            clauses.append("wi.item_type=?")
            args.append(item_type)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        args.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT wi.*,s.name AS subject_name FROM work_items wi "
                f"LEFT JOIN subjects s ON s.id=wi.subject_id{where} "
                "ORDER BY CASE wi.status WHEN 'suggested' THEN 0 WHEN 'active' THEN 1 ELSE 2 END,wi.updated_at DESC LIMIT ?",
                args,
            ).fetchall()
        return [dict(row) | {"confirmed": bool(row["confirmed"])} for row in rows]

    def get_work_item(self, item_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT wi.*,s.name AS subject_name FROM work_items wi "
                "LEFT JOIN subjects s ON s.id=wi.subject_id WHERE wi.id=?", (item_id,)
            ).fetchone()
            if not row:
                raise KeyError(item_id)
            result = dict(row)
            result["confirmed"] = bool(row["confirmed"])
            result["revisions"] = [dict(item) for item in conn.execute(
                "SELECT * FROM work_item_revisions WHERE work_item_id=? ORDER BY changed_at DESC", (item_id,)
            )]
            return result

    def update_work_item(self, item_id: str, values: dict[str, Any], *, reason: str = "manual_edit") -> dict[str, Any]:
        if not isinstance(values, dict):
            raise ValueError("Work item update must be an object")
        now = utc_now()
        with self.db.transaction(immediate=True) as conn:
            old = conn.execute("SELECT * FROM work_items WHERE id=?", (item_id,)).fetchone()
            if not old:
                raise KeyError(item_id)
            conn.execute(
                "INSERT INTO work_item_revisions(work_item_id,snapshot_json,changed_at,change_reason) VALUES(?,?,?,?)",
                (item_id, json.dumps(dict(old), ensure_ascii=False), now, reason),
            )
            content = str(values.get("content", old["content"])).strip()
            item_type = str(values.get("item_type", old["item_type"]))
            status = str(values.get("status", old["status"]))
            subject_id = values.get("subject_id", old["subject_id"])
            confirmed = bool(values.get("confirmed", old["confirmed"]))
            if not content or item_type not in WORK_ITEM_TYPES or status not in WORK_ITEM_STATUSES:
                raise ValueError("Invalid work item update")
            if confirmed and not old["evidence_quote"] and reason not in {"manual_confirm", "manual_restore"}:
                raise ValueError("Automatic confirmation requires user evidence")
            transitions = {
                "suggested": {"active", "archived", "expired"},
                "active": {"resolved", "archived", "expired"},
                "resolved": {"active"},
                "archived": {"active"},
                "expired": {"active"},
            }
            if status != old["status"] and status not in transitions.get(str(old["status"]), set()):
                raise ValueError(f"Unsupported work item transition: {old['status']} -> {status}")
            if subject_id:
                subject = conn.execute(
                    "SELECT subject_type,status FROM subjects WHERE id=?", (subject_id,)
                ).fetchone()
                if not subject or subject["subject_type"] != "project" or subject["status"] == "archived":
                    raise KeyError(subject_id)
            if status == "active" and reason in {"manual_confirm", "manual_restore"}:
                confirmed = True
            resolved_at = now if status == "resolved" else (None if status == "active" else old["resolved_at"])
            expires_at = old["expires_at"]
            if item_type != old["item_type"] or reason in {"manual_confirm", "manual_restore"}:
                days = WORK_ITEM_TTL_DAYS[item_type]
                expires_at = (
                    (datetime.now(UTC) + timedelta(days=days)).isoformat(timespec="seconds")
                    if days else None
                )
            conn.execute(
                "UPDATE work_items SET content=?,content_hash=?,item_type=?,status=?,confirmed=?,"
                "subject_id=?,resolved_at=?,expires_at=?,updated_at=? WHERE id=?",
                (content, content_hash(content), item_type, status, int(confirmed), subject_id,
                 resolved_at, expires_at, now, item_id),
            )
            conn.execute(
                "DELETE FROM subject_links WHERE object_type='work_item' AND object_id=? "
                "AND subject_id<>coalesce(?, '')",
                (item_id, subject_id),
            )
            if subject_id:
                conn.execute(
                    "INSERT INTO subject_links(id,subject_id,object_type,object_id,confidence,"
                    "assignment_status,method,created_at,updated_at) VALUES(?,?, 'work_item',?,?,?,? ,?,?) "
                    "ON CONFLICT(subject_id,object_type,object_id) DO UPDATE SET "
                    "assignment_status=excluded.assignment_status,method=excluded.method,updated_at=excluded.updated_at",
                    (str(uuid.uuid4()), subject_id, item_id, float(old["confidence"]),
                     "confirmed" if confirmed else "suggested", reason, old["created_at"], now),
                )
        self.queue_projection("subject" if subject_id else "profile", str(subject_id or ""))
        return self.get_work_item(item_id)

    def action_work_item(self, item_id: str, action: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        body = body or {}
        mapping = {
            "confirm": {"status": "active", "confirmed": True},
            "resolve": {"status": "resolved"},
            "archive": {"status": "archived"},
            "restore": {"status": "active", "confirmed": True},
        }
        if action not in mapping:
            raise ValueError("Unsupported work item action")
        values = mapping[action] | {key: body[key] for key in ("content", "subject_id", "item_type") if key in body}
        return self.update_work_item(item_id, values, reason=f"manual_{action}")

    def expire_due_work_items(self) -> int:
        now = utc_now()
        with self.db.transaction(immediate=True) as conn:
            rows = conn.execute(
                "SELECT * FROM work_items WHERE status IN ('suggested','active') "
                "AND expires_at IS NOT NULL AND expires_at<=?", (now,)
            ).fetchall()
            for row in rows:
                conn.execute(
                    "INSERT INTO work_item_revisions(work_item_id,snapshot_json,changed_at,change_reason) VALUES(?,?,?,'automatic_expiry')",
                    (row["id"], json.dumps(dict(row), ensure_ascii=False), now),
                )
                conn.execute("UPDATE work_items SET status='expired',updated_at=? WHERE id=?", (now, row["id"]))
        if rows:
            self.queue_projection("all", "")
        return len(rows)

    # Summaries -----------------------------------------------------------------------
    def list_summaries(self, scope: str, subject_id: str | None = None) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM summary_versions WHERE scope=? AND coalesce(subject_id,'')=coalesce(?, '') "
                "ORDER BY created_at DESC", (scope, subject_id)
            ).fetchall()
        return [dict(row) | {"source_ids": json.loads(row["source_ids_json"])} for row in rows]

    def current_summary(self, scope: str, subject_id: str | None = None) -> dict[str, Any] | None:
        rows = self.list_summaries(scope, subject_id)
        current = next((item for item in rows if item["status"] == "current"), None)
        if not current:
            return None
        sources = self._summary_sources(scope, subject_id)
        revision, newest = self._source_revision(sources)
        cited = set(current["source_ids"])
        known = {str(source["id"]) for source in sources}
        current["stale"] = bool(
            current.get("source_revision") != revision or not cited.issubset(known)
        )
        current["stale_reason"] = (
            "summary_sources_changed" if current["stale"] else None
        )
        current["current_source_revision"] = revision
        current["current_newest_source_updated_at"] = newest
        return current

    def regenerate_summary(self, scope: str, subject_id: str | None = None) -> dict[str, Any]:
        if scope not in {"profile", "project", "topic"}:
            raise ValueError("Unsupported summary scope")
        sources = self._summary_sources(scope, subject_id)
        if not sources:
            raise ValueError("No grounded sources are available for this summary")
        client = self.client_factory() if self.client_factory else None
        if not client or not client.configured:
            raise LlmError("LLM is not configured")
        result = client.chat_json(
            system=SUMMARY_SYSTEM,
            user=json.dumps({"scope": scope, "subject_id": subject_id, "sources": sources}, ensure_ascii=False),
        )
        if not isinstance(result.parsed, dict):
            raise LlmError("Summary completion was not an object")
        statements = result.parsed.get("statements")
        summary = str(result.parsed.get("summary", "")).strip()
        known = {source["id"] for source in sources}
        if not summary or not isinstance(statements, list) or not statements:
            raise LlmError("Summary completion is missing grounded statements")
        cited: set[str] = set()
        for statement in statements:
            if not isinstance(statement, dict) or not str(statement.get("text", "")).strip():
                raise LlmError("Summary statement is invalid")
            ids = statement.get("source_ids")
            if not isinstance(ids, list) or not ids or not all(str(item) in known for item in ids):
                raise LlmError("Summary statement referenced an unknown or empty source set")
            cited.update(str(item) for item in ids)
        previous = self.current_summary(scope, subject_id)
        if previous:
            still_valid = set(previous["source_ids"]).intersection(known)
            if still_valid:
                loss = len(still_valid - cited) / len(still_valid)
                if loss > 0.20:
                    raise LlmError("Summary rewrite rejected by the 20% source-loss guard")
            if previous["mode"] == "manual_override":
                raise ValueError("Summary has a manual override; resume automatic mode first")
        source_revision, newest_source = self._source_revision(sources)
        version_id = self._store_summary(
            scope, subject_id, summary, sorted(cited),
            str(result.parsed.get("change_reason", "Automatic grounded refresh")), "automatic",
            source_revision=source_revision, newest_source_updated_at=newest_source,
        )
        self.queue_projection("subject" if subject_id else "profile", subject_id or "")
        return next(item for item in self.list_summaries(scope, subject_id) if item["id"] == version_id)

    def override_summary(self, scope: str, subject_id: str | None, content: str) -> dict[str, Any]:
        content = content.strip()
        if not content:
            raise ValueError("Summary content is empty")
        previous = self.current_summary(scope, subject_id)
        source_ids = previous["source_ids"] if previous else []
        sources = self._summary_sources(scope, subject_id)
        source_revision, newest_source = self._source_revision(sources)
        version_id = self._store_summary(
            scope, subject_id, content, source_ids, "Manual override", "manual_override",
            source_revision=source_revision, newest_source_updated_at=newest_source,
        )
        self.queue_projection("subject" if subject_id else "profile", subject_id or "")
        return next(item for item in self.list_summaries(scope, subject_id) if item["id"] == version_id)

    def rollback_summary(self, scope: str, subject_id: str | None, version_id: str) -> dict[str, Any]:
        versions = self.list_summaries(scope, subject_id)
        target = next((item for item in versions if item["id"] == version_id), None)
        if not target:
            raise KeyError(version_id)
        sources = self._summary_sources(scope, subject_id)
        source_revision, newest_source = self._source_revision(sources)
        new_id = self._store_summary(
            scope, subject_id, target["content"], target["source_ids"],
            f"Rollback to {version_id}", "manual_override",
            source_revision=source_revision, newest_source_updated_at=newest_source,
        )
        self.queue_projection("subject" if subject_id else "profile", subject_id or "")
        return next(item for item in self.list_summaries(scope, subject_id) if item["id"] == new_id)

    def resume_automatic_summary(self, scope: str, subject_id: str | None) -> dict[str, Any]:
        current = self.current_summary(scope, subject_id)
        if not current:
            return self.regenerate_summary(scope, subject_id)
        with self.db.transaction(immediate=True) as conn:
            conn.execute("UPDATE summary_versions SET mode='automatic' WHERE id=?", (current["id"],))
        return self.regenerate_summary(scope, subject_id)

    def refresh_stale_summaries(self, *, limit: int = 10) -> dict[str, int]:
        if not self.client_factory or not self.client_factory().configured:
            return {"refreshed": 0, "blocked": 0}
        with self.db.connect() as conn:
            subjects = conn.execute(
                "SELECT * FROM (SELECT s.id,s.subject_type,"
                "max(coalesce((SELECT max(w.updated_at) FROM work_items w WHERE w.subject_id=s.id),''),"
                "coalesce((SELECT max(m.updated_at) FROM subject_links sl JOIN memories m "
                "ON m.id=sl.object_id WHERE sl.subject_id=s.id AND sl.object_type='memory'),'')) AS source_updated,"
                "(SELECT max(created_at) FROM summary_versions sv WHERE sv.subject_id=s.id "
                "AND sv.status='current') AS summary_updated FROM subjects s WHERE s.status='active') "
                "WHERE source_updated>coalesce(summary_updated,'') LIMIT ?", (limit,)
            ).fetchall()
        refreshed = blocked = 0
        for subject in subjects:
            try:
                scope = "project" if subject["subject_type"] == "project" else "topic"
                self.regenerate_summary(scope, subject["id"])
                refreshed += 1
            except Exception:
                blocked += 1
        return {"refreshed": refreshed, "blocked": blocked}

    # Projection and storage ----------------------------------------------------------
    def queue_projection(self, projection_type: str, target_id: str) -> str:
        now = utc_now()
        revision = content_hash(f"{projection_type}:{target_id}:{now}:{uuid.uuid4()}")
        job_id = str(uuid.uuid4())
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO projection_jobs(id,projection_type,target_id,revision,status,created_at,updated_at) "
                "VALUES(?,?,?,?, 'pending',?,?)",
                (job_id, projection_type, target_id, revision, now, now),
            )
        return job_id

    def rebuild_projections(self) -> dict[str, Any]:
        secure_directory(self.vault)
        secure_directory(self.vault / "projects")
        secure_directory(self.vault / "topics")
        secure_directory(self.indexes)
        files: dict[str, str] = {}
        profile = self._render_profile()
        files["profile.md"] = profile
        for subject in self.list_subjects():
            if subject["subject_type"] not in {"project", "topic"}:
                continue
            folder = "projects" if subject["subject_type"] == "project" else "topics"
            files[f"{folder}/{subject['slug']}.md"] = self._render_subject(subject)
        manifest_files: dict[str, str] = {}
        try:
            for relative, content in files.items():
                path = self.vault / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                self._atomic_text(path, content)
                manifest_files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "generated_at": utc_now(),
                "source": "memory.db",
                "files": manifest_files,
            }
            self._atomic_text(self.vault / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            self._atomic_text(
                self.indexes / "manifest.json",
                json.dumps({"generated_at": utc_now(), "rebuildable": True}, ensure_ascii=False, indent=2) + "\n",
            )
            with self.db.transaction(immediate=True) as conn:
                conn.execute("UPDATE projection_jobs SET status='completed',error=NULL,updated_at=? WHERE status='pending'", (utc_now(),))
            return {"ok": True, "files": len(files) + 2, "pending": 0}
        except Exception as error:
            with self.db.transaction(immediate=True) as conn:
                conn.execute(
                    "UPDATE projection_jobs SET status='failed',attempts=attempts+1,error=?,updated_at=? WHERE status='pending'",
                    (str(error), utc_now()),
                )
            return {"ok": False, "files": len(manifest_files), "error": str(error), "pending": self.pending_projection_count()}

    def storage_health(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            pending = int(conn.execute("SELECT count(*) FROM projection_jobs WHERE status IN ('pending','failed')").fetchone()[0])
        errors: list[str] = []
        manifest_path = self.vault / "manifest.json"
        checked = 0
        if not manifest_path.is_file():
            errors.append("vault manifest is missing")
        else:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                for relative, expected in manifest.get("files", {}).items():
                    checked += 1
                    path = self.vault / relative
                    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                        errors.append(f"projection mismatch: {relative}")
            except (OSError, json.JSONDecodeError, TypeError) as error:
                errors.append(f"invalid vault manifest: {error}")
        permissions = [
            permission_report(self.root, directory=True),
            permission_report(self.db.path),
            permission_report(self.vault, directory=True),
            permission_report(self.indexes, directory=True),
            permission_report(self.root / "backups", directory=True),
        ]
        return {
            "ok": integrity == "ok" and not errors and pending == 0,
            "database_integrity": integrity,
            "projection_pending": pending,
            "vault_files_checked": checked,
            "errors": errors,
            "permissions": permissions,
            "vault_path": str(self.vault),
            "indexes_path": str(self.indexes),
        }

    def pending_projection_count(self) -> int:
        with self.db.connect() as conn:
            return int(conn.execute("SELECT count(*) FROM projection_jobs WHERE status IN ('pending','failed')").fetchone()[0])

    # Internal helpers ----------------------------------------------------------------
    @staticmethod
    def _insert_alias(conn: Any, subject_id: str, alias: str, alias_type: str, now: str) -> None:
        alias = alias.strip()
        if not alias:
            return
        conn.execute(
            "INSERT OR IGNORE INTO subject_aliases(subject_id,alias,alias_normalized,alias_type,created_at) VALUES(?,?,?,?,?)",
            (subject_id, alias, _normal(alias), alias_type, now),
        )

    @staticmethod
    def _subject_dict(conn: Any, row: Any) -> dict[str, Any]:
        result = dict(row)
        aliases = conn.execute(
            "SELECT alias,alias_type FROM subject_aliases WHERE subject_id=? ORDER BY alias_type,alias",
            (row["id"],),
        ).fetchall()
        result["aliases"] = [item["alias"] for item in aliases if item["alias_type"] == "name"]
        result["workspace_aliases"] = [item["alias"] for item in aliases if item["alias_type"] == "workspace"]
        return result

    def _summary_sources(self, scope: str, subject_id: str | None) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            predicate, predicate_args = eligible_memory_predicate("m")
            now = utc_now()
            if subject_id:
                work = conn.execute(
                    "SELECT id,item_type AS kind,content,status,confirmed,updated_at FROM work_items "
                    "WHERE subject_id=? AND status='active' AND confirmed=1 AND item_type<>'proposal' "
                    "AND (expires_at IS NULL OR datetime(expires_at)>datetime(?)) "
                    "ORDER BY updated_at DESC LIMIT 60",
                    (subject_id, now),
                ).fetchall()
                memories = conn.execute(
                    "SELECT m.id,m.kind,m.content,m.temporal_status AS status,1 AS confirmed,m.updated_at "
                    "FROM subject_links sl JOIN memories m ON m.id=sl.object_id "
                    "WHERE sl.subject_id=? AND sl.object_type='memory' "
                    "AND sl.assignment_status IN ('confirmed','automatic') "
                    f"AND {predicate} ORDER BY m.updated_at DESC LIMIT 60",
                    [subject_id, *predicate_args],
                ).fetchall()
            else:
                work = []
                memories = conn.execute(
                    "SELECT m.id,m.kind,m.content,m.temporal_status AS status,1 AS confirmed,m.updated_at "
                    f"FROM memories m WHERE {predicate} "
                    "AND NOT EXISTS(SELECT 1 FROM subject_links sl WHERE sl.object_type='memory' AND sl.object_id=m.id) "
                    "ORDER BY m.updated_at DESC LIMIT 80",
                    predicate_args,
                ).fetchall()
        return [dict(row) | {"source_type": "work_item"} for row in work] + [
            dict(row) | {"source_type": "memory"} for row in memories
        ]

    def _store_summary(
        self, scope: str, subject_id: str | None, content: str, source_ids: list[str], reason: str, mode: str,
        *, source_revision: str = "", newest_source_updated_at: str | None = None,
    ) -> str:
        version_id = str(uuid.uuid4())
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE summary_versions SET status='superseded' WHERE scope=? "
                "AND coalesce(subject_id,'')=coalesce(?, '') AND status='current'", (scope, subject_id)
            )
            conn.execute(
                "INSERT INTO summary_versions(id,scope,subject_id,content,source_ids_json,change_reason,mode,status,"
                "source_revision,newest_source_updated_at,created_at) VALUES(?,?,?,?,?,?,?,'current',?,?,?)",
                (version_id, scope, subject_id, content, json.dumps(source_ids), reason, mode,
                 source_revision, newest_source_updated_at, utc_now()),
            )
        return version_id

    @staticmethod
    def _source_revision(sources: list[dict[str, Any]]) -> tuple[str, str | None]:
        ordered = sorted(
            (str(item["id"]), str(item.get("updated_at", "")), content_hash(str(item["content"])))
            for item in sources
        )
        newest = max((item[1] for item in ordered), default=None)
        return content_hash(json.dumps(ordered, ensure_ascii=False)), newest

    def _render_profile(self) -> str:
        summary = self.current_summary("profile")
        memories = self._summary_sources("profile", None)
        lines = ["# B1ack Memory Profile", "", "> Generated from memory.db. Edit through the WebUI.", ""]
        if summary and not summary.get("stale"):
            lines.extend(["## Summary", "", self._safe_markdown(summary["content"]), ""])
        if memories:
            lines.extend(["## Durable memories", ""])
            lines.extend(f"- {self._safe_markdown(item['content'])} <!-- b1ack:id={item['id']} -->" for item in memories)
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _render_subject(self, subject: dict[str, Any]) -> str:
        scope = "project" if subject["subject_type"] == "project" else "topic"
        summary = self.current_summary(scope, subject["id"])
        work = self.list_work_items(subject_id=subject["id"], limit=500)
        with self.db.connect() as conn:
            valid_sql, valid_params = eligible_memory_predicate("m")
            memories = conn.execute(
                "SELECT m.* FROM subject_links sl JOIN memories m ON m.id=sl.object_id "
                f"WHERE sl.subject_id=? AND sl.object_type='memory' AND {valid_sql} ORDER BY m.updated_at DESC",
                (subject["id"], *valid_params),
            ).fetchall()
        lines = [f"# {subject['name']}", "", f"> {subject['subject_type']} · {subject['status']} · generated from memory.db", ""]
        if subject.get("description"):
            lines.extend([self._safe_markdown(subject["description"]), ""])
        if summary and not summary.get("stale"):
            lines.extend(["## Summary", "", self._safe_markdown(summary["content"]), ""])
        for item_type, label in (("current_state", "Current state"), ("decision", "Decisions"),
                                 ("open_question", "Open questions"), ("proposal", "Proposals"),
                                 ("milestone", "Milestones")):
            items = [item for item in work if item["item_type"] == item_type and item["status"] in {"active", "suggested"}]
            if items:
                lines.extend([f"## {label}", ""])
                lines.extend(f"- {self._safe_markdown(item['content'])} <!-- b1ack:work={item['id']} -->" for item in items)
                lines.append("")
        if memories:
            lines.extend(["## Durable memories", ""])
            lines.extend(f"- {self._safe_markdown(item['content'])} <!-- b1ack:id={item['id']} -->" for item in memories)
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _safe_markdown(value: Any) -> str:
        text = "".join(char for char in str(value) if char in "\n\t" or ord(char) >= 32)
        return text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")

    @staticmethod
    def _atomic_text(path: Path, content: str) -> None:
        secure_directory(path.parent)
        temp = path.with_name(f".{path.name}.tmp")
        temp.write_text(content, encoding="utf-8")
        secure_file(temp)
        os.replace(temp, path)
        secure_file(path)
