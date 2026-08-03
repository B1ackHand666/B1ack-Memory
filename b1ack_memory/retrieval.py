from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from collections.abc import Callable
from typing import Any

from .db import MemoryDatabase, utc_now
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
        with self.db.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM search_fts")
            memories = conn.execute(
                "SELECT id,content FROM memories WHERE status='active' "
                "AND (valid_until IS NULL OR valid_until > ?)",
                (utc_now(),),
            ).fetchall()
            for row in memories:
                conn.execute(
                    "INSERT INTO search_fts(record_id,source,content,search_text) VALUES(?,?,?,?)",
                    (row["id"], "memory", row["content"], normalized_search_text(row["content"])),
                )
            memory_count = len(memories)
            candidates = conn.execute(
                "SELECT id,content FROM candidates WHERE status='pending'"
            ).fetchall()
            for row in candidates:
                conn.execute(
                    "INSERT INTO search_fts(record_id,source,content,search_text) VALUES(?,?,?,?)",
                    (row["id"], "candidate", row["content"], normalized_search_text(row["content"])),
                )
            candidate_count = len(candidates)
        return {"memories": memory_count, "candidates": candidate_count}

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        include_candidates: bool = True,
        injected: bool = False,
        query_vector: list[float] | None = None,
    ) -> list[SearchHit]:
        tokens = search_tokens(query)
        if not tokens:
            return []
        match = " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens[:20])
        keyword_rows: list[sqlite3.Row]
        with self.db.connect() as conn:
            try:
                keyword_rows = conn.execute(
                    "SELECT record_id,source,content,bm25(search_fts) AS score "
                    "FROM search_fts WHERE search_fts MATCH ? ORDER BY score LIMIT ?",
                    (match, max(limit * 4, 20)),
                ).fetchall()
            except sqlite3.OperationalError:
                keyword_rows = []

            vector_rows: list[tuple[str, str, str, float]] = []
            if query_vector:
                for row in conn.execute(
                    "SELECT e.record_id,e.source,e.vector_json,f.content "
                    "FROM embeddings e JOIN search_fts f "
                    "ON f.record_id=e.record_id AND f.source=e.source"
                ):
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
            score = 0.0
            if key in keyword_rank:
                score += 0.55 / (60 + keyword_rank[key])
            if key in vector_rank:
                score += 0.45 / (60 + vector_rank[key])
            ranked.append((key, score))
        ranked.sort(key=lambda item: item[1], reverse=True)

        hits: list[SearchHit] = []
        for (record_id, source), score in ranked[:limit]:
            kind = self._kind_for(record_id, source)
            hits.append(
                SearchHit(
                    id=record_id,
                    content=content_map[(record_id, source)],
                    kind=kind,
                    source=source,  # type: ignore[arg-type]
                    final_score=score,
                    keyword_rank=keyword_rank.get((record_id, source)),
                    vector_rank=vector_rank.get((record_id, source)),
                    unverified=source == "candidate",
                )
            )
        self._record_recall(query, hits, injected=injected)
        return hits

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
        table = "memories" if source == "memory" else "candidates"
        with self.db.connect() as conn:
            row = conn.execute(f"SELECT kind FROM {table} WHERE id=?", (record_id,)).fetchone()
        return row["kind"] if row else "fact"

    def _record_recall(self, query: str, hits: list[SearchHit], *, injected: bool) -> None:
        query_hash = hashlib.sha256(" ".join(search_tokens(query)).encode("utf-8")).hexdigest()
        now = utc_now()
        with self.db.transaction(immediate=True) as conn:
            for hit in hits:
                conn.execute(
                    """INSERT INTO recall_events(
                        record_id,source,query_text,query_hash,keyword_rank,vector_rank,
                        final_score,injected,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)""",
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
                    ),
                )
                if hit.source == "candidate" and injected:
                    unique = conn.execute(
                        "SELECT COUNT(DISTINCT query_hash) FROM recall_events "
                        "WHERE record_id=? AND source='candidate' AND injected=1",
                        (hit.id,),
                    ).fetchone()[0]
                    count = conn.execute(
                        "SELECT COUNT(*) FROM recall_events WHERE record_id=? "
                        "AND source='candidate' AND injected=1",
                        (hit.id,),
                    ).fetchone()[0]
                    conn.execute(
                        "UPDATE candidates SET recall_count=?,unique_query_count=? WHERE id=?",
                        (count, unique, hit.id),
                    )

