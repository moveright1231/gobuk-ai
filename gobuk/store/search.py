"""청크 · 임베딩 · 전문검색.

한국어 FTS는 기본 unicode61 토크나이저가 조사 때문에 잘 안 맞아서
trigram 토크나이저를 쓴다 (SQLite 3.34+).
"""
from __future__ import annotations

import sqlite3
from typing import Any

import numpy as np

from gobuk import config

DDL = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id  TEXT PRIMARY KEY,
    page_id   TEXT NOT NULL,
    db_key    TEXT NOT NULL,
    ord       INTEGER NOT NULL,
    heading   TEXT,
    text      TEXT NOT NULL,
    embedding BLOB,
    model     TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_page ON chunks(page_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
    USING fts5(text, chunk_id UNINDEXED, tokenize='trigram');
"""


class SearchMixin:
    # -- 청크 ------------------------------------------------------------
    def _drop_chunks(self, page_id: str) -> None:
        ids = [r["chunk_id"] for r in self.conn.execute(
            "SELECT chunk_id FROM chunks WHERE page_id=?", (page_id,))]
        self.conn.execute("DELETE FROM chunks WHERE page_id=?", (page_id,))
        for cid in ids:
            self.conn.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (cid,))

    def replace_chunks(self, page_id: str, db_key: str, chunks: list[dict]) -> None:
        self._drop_chunks(page_id)
        for i, ch in enumerate(chunks):
            cid = f"{page_id}:{i}"
            self.conn.execute(
                "INSERT INTO chunks(chunk_id,page_id,db_key,ord,heading,text)"
                " VALUES(?,?,?,?,?,?)",
                (cid, page_id, db_key, i, ch.get("heading"), ch["text"]),
            )
            self.conn.execute(
                "INSERT INTO chunks_fts(text, chunk_id) VALUES(?,?)", (ch["text"], cid)
            )

    def chunks_needing_embedding(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT c.chunk_id, c.text FROM chunks c "
            "JOIN records r ON r.page_id = c.page_id "
            "WHERE r.published = 1 AND (c.embedding IS NULL OR c.model != ?)",
            (config.EMBED_MODEL,),
        ))

    def save_embeddings(self, pairs: list[tuple[str, np.ndarray]]) -> None:
        self.conn.executemany(
            "UPDATE chunks SET embedding=?, model=? WHERE chunk_id=?",
            [(v.astype(np.float32).tobytes(), config.EMBED_MODEL, cid) for cid, v in pairs],
        )
        self.conn.commit()

    # -- 검색 ------------------------------------------------------------
    def load_vectors(self) -> tuple[list[str], np.ndarray]:
        rows = list(self.conn.execute(
            "SELECT c.chunk_id, c.embedding FROM chunks c "
            "JOIN records r ON r.page_id=c.page_id "
            "WHERE r.published=1 AND c.embedding IS NOT NULL"
        ))
        if not rows:
            return [], np.zeros((0, config.EMBED_DIM), dtype=np.float32)
        ids = [r["chunk_id"] for r in rows]
        mat = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        return ids, mat / np.clip(norms, 1e-9, None)

    def chunk_context(self, chunk_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not chunk_ids:
            return {}
        marks = ",".join("?" * len(chunk_ids))
        rows = self.conn.execute(
            f"""SELECT c.chunk_id, c.text, c.heading, r.page_id, r.title, r.url,
                       r.db_key, r.answer_text
                FROM chunks c JOIN records r ON r.page_id=c.page_id
                WHERE c.chunk_id IN ({marks})""",
            chunk_ids,
        )
        return {r["chunk_id"]: dict(r) for r in rows}
    def fts_search(self, query: str, limit: int = 20) -> list[tuple[str, float]]:
        safe = query.replace('"', " ").strip()
        if len(safe) < 3:
            return []
        try:
            rows = self.conn.execute(
                "SELECT chunk_id, bm25(chunks_fts) AS score FROM chunks_fts "
                "WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?",
                (f'"{safe}"', limit),
            )
            return [(r["chunk_id"], -r["score"]) for r in rows]
        except sqlite3.OperationalError:
            return []
