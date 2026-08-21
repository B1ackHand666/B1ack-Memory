from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from collections.abc import Callable
from typing import Any

from .db import MemoryDatabase, eligible_memory_predicate, utc_now
from .models import SearchHit

_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]+")
_WORD = re.compile(r"[\w-]+", re.UNICODE)


def search_tokens(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens: list[str] = []
    for match in _CJK.finditer(normalized):
        value = match.group(0)
        if len(value) == 1:
            tokens.append(value)
        else:
            tokens.extend(value[index : index + 2] for index in range(len(value) - 1))
    without_cjk = _CJK.sub(" ", normalized)
    tokens.extend(token for token in _WORD.findall(without_cjk) if token.strip("-_"))
    return list(dict.fromkeys(tokens))


def normalized_search_text(text: str) -> str:
    return " ".join(search_tokens(text))


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return -1.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return -1.0
    return dot / (left_norm * right_norm)


class RetrievalEngine:
    def __init__(self, db: MemoryDatabase):
        self.db = db

    def rebuild_index(self) -> dict[str, int]:
        memory_count = 0
        candidate_count = 0
        workspace_count = 0
        recent_count = 0
        daily_count = 0
        with self.db.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM search_fts")
            predicate, predicate_args = eligible_memory_predicate("m")
            memories = conn.execute(
                f"SELECT m.id,m.content FROM memories m WHERE {predicate}",
                predicate_args,
            ).fetchall()
            for row in memories:
                conn.execute(
                    "INSERT INTO search_fts(record_id,source,pool,content,search_text) VALUES(?,?,?,?,?)",
                    (row["id"], "memory", "recall", row["content"], normalized_search_text(row["content"])),
                )
            memory_count = len(memories)
            candidates = conn.execute(
                "SELECT id,content,admission_state FROM candidates "
                "WHERE status='pending' AND admission_state<>'legacy_history'"
            ).fetchall()
            for row in candidates:
                conn.execute(
                    "INSERT INTO search_fts(record_id,source,pool,content,search_text) VALUES(?,?,?,?,?)",
                    (row["id"], "candidate", "explicit_search" if row["admission_state"] == "admitted" else "integration_only",
                     row["content"], normalized_search_text(row["content"])),
                )
            candidate_count = len(candidates)
            # Recent material is searchable only on an explicit query.  It is
            # intentionally kept out of the `recall` pool used for prompt
            # injection, so UI/search access cannot leak into prefetch.
            for row in conn.execute(
                "SELECT id,content FROM recent_signals WHERE status='active'"
            ):
                conn.execute(
                    "INSERT INTO search_fts(record_id,source,pool,content,search_text) VALUES(?,?,?,?,?)",
                    (row["id"], "recent_signal", "explicit_search", row["content"], normalized_search_text(row["content"])),
                )
                recent_count += 1
            for row in conn.execute(
                "SELECT id,content FROM daily_memories WHERE status='active'"
            ):
                conn.execute(
                    "INSERT INTO search_fts(record_id,source,pool,content,search_text) VALUES(?,?,?,?,?)",
                    (row["id"], "daily_memory", "explicit_search", row["content"], normalized_search_text(row["content"])),
                )
                daily_count += 1
            for row in conn.execute(
                "SELECT id,content FROM work_items WHERE status IN ('suggested','active')"
            ):
                conn.execute(
                    "INSERT INTO search_fts(record_id,source,pool,content,search_text) VALUES(?,?,?,?,?)",
                    (row["id"], "work_item", "explicit_search", row["content"], normalized_search_text(row["content"])),
                )
                workspace_count += 1
            for row in conn.execute("SELECT id,name,description FROM subjects WHERE status<>'archived'"):
                aliases = " ".join(
                    item[0] for item in conn.execute(
                        "SELECT alias FROM subject_aliases WHERE subject_id=?", (row["id"],)
                    )
                )
                content = " ".join(part for part in (row["name"], row["description"], aliases) if part)
                conn.execute(
                    "INSERT INTO search_fts(record_id,source,pool,content,search_text) VALUES(?,?,?,?,?)",
                    (row["id"], "subject", "explicit_search", content, normalized_search_text(content)),
                )
                workspace_count += 1
            for row in conn.execute("SELECT id,content FROM summary_versions WHERE status='current'"):
                conn.execute(
                    "INSERT INTO search_fts(record_id,source,pool,content,search_text) VALUES(?,?,?,?,?)",
                    (row["id"], "summary", "explicit_search", row["content"], normalized_search_text(row["content"])),
                )
                workspace_count += 1
        return {
            "memories": memory_count, "candidates": candidate_count, "workspace": workspace_count,
            "recent_signals": recent_count, "daily_memories": daily_count,
        }

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        include_candidates: bool = True,
        include_workspace: bool = True,
        injected: bool = False,
        query_vector: list[float] | None = None,
        project_id: str | None = None,
        project_confidence: float | None = None,
        project_reason: str | None = None,
    ) -> list[SearchHit]:
        # Injection is a long-term-memory-only boundary.  Keep this guard here so
        # alternate callers cannot accidentally re-enable unverified candidates.
        if injected:
            include_candidates = False
            include_workspace = False
        tokens = search_tokens(query)
        if not tokens:
            return []
        match = " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens[:20])
        keyword_rows: list[sqlite3.Row]
        with self.db.connect() as conn:
            try:
                pool = "recall" if injected else "integration_only"
                pool_operator = "=" if injected else "<>"
                keyword_rows = conn.execute(
                    "SELECT record_id,source,content,bm25(search_fts) AS score "
                    f"FROM search_fts WHERE search_fts MATCH ? AND pool{pool_operator}? "
                    "ORDER BY score LIMIT ?",
                    (match, pool, max(limit * 4, 20)),
                ).fetchall()
            except sqlite3.OperationalError:
                keyword_rows = []

            vector_rows: list[tuple[str, str, str, float]] = []
            if query_vector:
                for row in conn.execute(
                    "SELECT e.record_id,e.source,e.vector_json,f.content,f.pool "
                    "FROM embeddings e JOIN search_fts f "
                    "ON f.record_id=e.record_id AND f.source=e.source"
                ):
                    if (injected and row["pool"] != "recall") or (
                        not injected and row["pool"] == "integration_only"
                    ):
                        continue
                    vector = json.loads(row["vector_json"])
                    score = cosine_similarity(query_vector, vector)
                    if score >= 0:
                        vector_rows.append((row["record_id"], row["source"], row["content"], score))

        keyword_rank = {
            (row["record_id"], row["source"]): index + 1 for index, row in enumerate(keyword_rows)
        }
        vector_rows.sort(key=lambda item: item[3], reverse=True)
        vector_rank = {(row[0], row[1]): index + 1 for index, row in enumerate(vector_rows)}
        content_map = {
            (row["record_id"], row["source"]): row["content"] for row in keyword_rows
        }
        content_map.update({(row[0], row[1]): row[2] for row in vector_rows})
        keys = set(keyword_rank) | set(vector_rank)
        ranked: list[tuple[tuple[str, str], float]] = []
        for key in keys:
            if key[1] == "candidate" and not include_candidates:
                continue
            if key[1] in {"work_item", "subject", "summary"} and not include_workspace:
                continue
            score = 0.0
            if key in keyword_rank:
                score += 0.55 / (60 + keyword_rank[key])
            if key in vector_rank:
                score += 0.45 / (60 + vector_rank[key])
            ranked.append((key, score))
        ranked.sort(key=lambda item: item[1], reverse=True)

        hits: list[SearchHit] = []
        for (record_id, source), score in ranked:
            project_ids, scope_state = self._project_scope(record_id, source)
            hit_project_id = project_ids[0] if project_ids else None
            if injected and source == "memory":
                if scope_state == "pending":
                    continue
                if scope_state == "scoped" and (not project_id or project_id not in project_ids):
                    continue
            if (
                project_id
                and source in {"work_item", "summary", "subject"}
                and hit_project_id != project_id
            ):
                continue
            kind = self._kind_for(record_id, source)
            hits.append(
                SearchHit(
                    id=record_id,
                    content=content_map[(record_id, source)],
                    kind=kind,
                    source=source,
                    final_score=score,
                    keyword_rank=keyword_rank.get((record_id, source)),
                    vector_rank=vector_rank.get((record_id, source)),
                    unverified=source in {"candidate", "work_item"},
                    project_id=hit_project_id,
                    project_ids=project_ids,
                    scope_state=scope_state,
                    temporal_status=self._temporal_status_for(record_id, source),
                )
            )
            if len(hits) >= limit:
                break
        self._record_recall(
            query,
            hits,
            injected=injected,
            project_id=project_id,
            project_confidence=project_confidence,
            project_reason=project_reason,
        )
        if not injected:
            self.db.reinforce_recent_search_hits(
                [hit.id for hit in hits if hit.source == "recent_signal"],
                retention_days=int(self.db.get_settings()["retention"].get("recent_signal_days", 14)),
            )
        return hits

    def related_records(
        self,
        content: str,
        *,
        include_candidates: bool = True,
        exclude: set[tuple[str, str]] | None = None,
        limit: int = 12,
        query_vector: list[float] | None = None,
    ) -> list[SearchHit]:
        """Return a bounded lexical shortlist from the complete FTS pool.

        This lookup deliberately records no recall event. It is an integration
        correctness check, not evidence that a candidate was useful to a user.
        """
        tokens = search_tokens(content)
        if not tokens:
            return []
        excluded = exclude or set()
        match = " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens[:40])
        with self.db.connect() as conn:
            try:
                rows = conn.execute(
                    "SELECT record_id,source,content,bm25(search_fts) AS rank_score "
                    "FROM search_fts WHERE search_fts MATCH ? ORDER BY rank_score LIMIT 200",
                    (match,),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            vector_scores: dict[tuple[str, str], float] = {}
            vector_content: dict[tuple[str, str], str] = {}
            if query_vector:
                for vector_row in conn.execute(
                    "SELECT e.record_id,e.source,e.vector_json,f.content FROM embeddings e "
                    "JOIN search_fts f ON f.record_id=e.record_id AND f.source=e.source"
                ):
                    score = cosine_similarity(query_vector, json.loads(vector_row["vector_json"]))
                    if score >= 0.55:
                        key = (str(vector_row["record_id"]), str(vector_row["source"]))
                        vector_scores[key] = score
                        vector_content[key] = str(vector_row["content"])

        query_tokens = set(tokens)
        ranked_map: dict[tuple[str, str], tuple[float, str]] = {}
        for row in rows:
            source = str(row["source"])
            record_id = str(row["record_id"])
            if (record_id, source) in excluded:
                continue
            if source == "candidate" and not include_candidates:
                continue
            if source not in {"memory", "candidate"}:
                continue
            target_tokens = set(search_tokens(str(row["content"])))
            shared = query_tokens.intersection(target_tokens)
            if not shared:
                continue
            containment = len(shared) / max(1, min(len(query_tokens), len(target_tokens)))
            jaccard = len(shared) / max(1, len(query_tokens.union(target_tokens)))
            lexical_score = containment * 0.7 + jaccard * 0.3
            # A single generic CJK bigram is insufficient for long statements.
            if lexical_score < 0.18 or (len(shared) == 1 and min(len(query_tokens), len(target_tokens)) > 4):
                continue
            ranked_map[(record_id, source)] = (lexical_score, str(row["content"]))
        for key, vector_score in vector_scores.items():
            if key in excluded or (key[1] == "candidate" and not include_candidates):
                continue
            if key[1] not in {"memory", "candidate"}:
                continue
            previous = ranked_map.get(key, (0.0, vector_content[key]))
            ranked_map[key] = (max(previous[0], vector_score), previous[1])
        ranked = sorted(
            [(score, key, value) for key, (score, value) in ranked_map.items()],
            key=lambda item: item[0],
            reverse=True,
        )
        return [
            SearchHit(
                id=key[0],
                content=value,
                kind=self._kind_for(key[0], key[1]),
                source=key[1],
                final_score=score,
                unverified=key[1] == "candidate",
            )
            for score, key, value in ranked[:limit]
        ]

    def rebuild_embeddings(
        self,
        embed: Callable[[list[str]], list[list[float]]],
        *,
        fingerprint: str,
        batch_size: int = 32,
    ) -> dict[str, int]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT record_id,source,content FROM search_fts").fetchall()
        written = 0
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            vectors = embed([row["content"] for row in batch])
            if len(vectors) != len(batch):
                raise ValueError("Embedding response length mismatch")
            with self.db.transaction(immediate=True) as conn:
                for row, vector in zip(batch, vectors, strict=True):
                    conn.execute(
                        "INSERT INTO embeddings(record_id,source,model_fingerprint,vector_json,updated_at) "
                        "VALUES(?,?,?,?,?) ON CONFLICT(record_id,source) DO UPDATE SET "
                        "model_fingerprint=excluded.model_fingerprint,vector_json=excluded.vector_json,"
                        "updated_at=excluded.updated_at",
                        (
                            row["record_id"],
                            row["source"],
                            fingerprint,
                            json.dumps(vector),
                            utc_now(),
                        ),
                    )
                    written += 1
        return {"embedded": written}

    def _kind_for(self, record_id: str, source: str) -> str:
        mapping = {
            "memory": ("memories", "kind"),
            "candidate": ("candidates", "kind"),
            "work_item": ("work_items", "item_type"),
            "subject": ("subjects", "subject_type"),
            "summary": ("summary_versions", "scope"),
            "recent_signal": ("recent_signals", "kind"),
            "daily_memory": ("daily_memories", "scope_key"),
        }
        table, column = mapping.get(source, ("memories", "kind"))
        with self.db.connect() as conn:
            row = conn.execute(f"SELECT {column} AS kind FROM {table} WHERE id=?", (record_id,)).fetchone()
        return row["kind"] if row else "fact"

    def _project_scope(self, record_id: str, source: str) -> tuple[list[str], str]:
        with self.db.connect() as conn:
            if source == "summary":
                row = conn.execute(
                    "SELECT sv.subject_id FROM summary_versions sv JOIN subjects s ON s.id=sv.subject_id "
                    "WHERE sv.id=? AND s.subject_type='project'", (record_id,)
                ).fetchone()
                ids = [str(row["subject_id"])] if row and row["subject_id"] else []
                return ids, "scoped" if ids else "global"
            if source == "subject":
                row = conn.execute("SELECT subject_type FROM subjects WHERE id=?", (record_id,)).fetchone()
                ids = [record_id] if row and row["subject_type"] == "project" else []
                return ids, "scoped" if ids else "global"
            rows = conn.execute(
                "SELECT sl.subject_id,sl.assignment_status FROM subject_links sl "
                "JOIN subjects s ON s.id=sl.subject_id "
                "WHERE sl.object_type=? AND sl.object_id=? AND s.subject_type='project' "
                "ORDER BY sl.confidence DESC,sl.subject_id",
                (source, record_id),
            ).fetchall()
            confirmed = [
                str(row["subject_id"]) for row in rows
                if row["assignment_status"] in {"confirmed", "automatic"}
            ]
            if confirmed:
                return list(dict.fromkeys(confirmed)), "scoped"
            if rows:
                return [], "pending"
            if source == "work_item":
                row = conn.execute(
                    "SELECT wi.subject_id FROM work_items wi JOIN subjects s ON s.id=wi.subject_id "
                    "WHERE wi.id=? AND s.subject_type='project'", (record_id,)
                ).fetchone()
                if row and row["subject_id"]:
                    return [], "pending"
            return [], "global"

    def _temporal_status_for(self, record_id: str, source: str) -> str | None:
        if source != "memory":
            return None
        with self.db.connect() as conn:
            row = conn.execute("SELECT temporal_status FROM memories WHERE id=?", (record_id,)).fetchone()
        return str(row["temporal_status"]) if row else None

    def _record_recall(
        self,
        query: str,
        hits: list[SearchHit],
        *,
        injected: bool,
        project_id: str | None = None,
        project_confidence: float | None = None,
        project_reason: str | None = None,
    ) -> None:
        query_hash = hashlib.sha256(" ".join(search_tokens(query)).encode("utf-8")).hexdigest()
        now = utc_now()
        with self.db.transaction(immediate=True) as conn:
            for hit in hits:
                conn.execute(
                    """INSERT INTO recall_events(
                        record_id,source,query_text,query_hash,keyword_rank,vector_rank,
                        final_score,injected,created_at,project_id,project_confidence,project_reason
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        hit.id,
                        hit.source,
                        query[:1000],
                        query_hash,
                        hit.keyword_rank,
                        hit.vector_rank,
                        hit.final_score,
                        int(injected),
                        now,
                        project_id,
                        project_confidence,
                        project_reason,
                    ),
                )
