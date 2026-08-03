"""로컬 저장소.

벡터DB를 따로 두지 않고 SQLite 한 파일로 끝낸다. 거북스토리 규모(수천 행)에서
numpy 전수 코사인은 수 ms 안에 끝나므로 Chroma/Qdrant를 붙일 이유가 없다.
나중에 수만 행을 넘어가면 그때 chunks 테이블만 옮기면 된다.

한국어 FTS는 기본 unicode61 토크나이저가 조사 때문에 잘 안 맞아서
trigram 토크나이저를 쓴다 (SQLite 3.34+).
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import numpy as np

import config
from flatten import Record, normalize

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS records (
    page_id       TEXT PRIMARY KEY,
    db_key        TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT,
    status        TEXT,
    published     INTEGER NOT NULL DEFAULT 0,
    patch_version TEXT,
    last_edited   TEXT,
    aliases       TEXT,   -- JSON array
    facts         TEXT,   -- JSON object
    raw_props     TEXT,   -- Notion 원본 프로퍼티. 답변 포맷을 바꿔도 API 재호출 없이
                          -- 로컬에서 전부 다시 만들 수 있게 남겨둔다.
    answer_text   TEXT,
    search_text   TEXT,
    body          TEXT,
    depends_on    TEXT,   -- JSON array of page_id
    content_hash  TEXT,
    synced_at     TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_records_db  ON records(db_key, published);
CREATE INDEX IF NOT EXISTS idx_records_ttl ON records(title);

CREATE TABLE IF NOT EXISTS aliases (
    alias_norm TEXT NOT NULL,
    alias_raw  TEXT NOT NULL,
    page_id    TEXT NOT NULL,
    db_key     TEXT NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (alias_norm, page_id)
);
CREATE INDEX IF NOT EXISTS idx_alias_norm ON aliases(alias_norm);

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

CREATE TABLE IF NOT EXISTS sync_state (
    db_key    TEXT PRIMARY KEY,
    last_sync TEXT
);

-- 캐시 무효화 신호. 봇의 메모리뱅크가 이 표를 읽고 관련 캐시를 지운다.
CREATE TABLE IF NOT EXISTS change_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id TEXT NOT NULL,
    db_key  TEXT NOT NULL,
    change  TEXT NOT NULL,   -- created | updated | unpublished | deleted
    at      TEXT DEFAULT CURRENT_TIMESTAMP,
    consumed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_changelog_open ON change_log(consumed, id);

-- 메모리뱅크. 질문 임베딩 캐시.
-- entity_key 는 질문에 등장한 고유명사의 페이지 ID 집합이다. 임베딩 유사도만으로
-- 캐시를 히트시키면 '토마토스파게티 레시피'와 '크림스파게티 레시피'가 섞이므로,
-- 이 키가 정확히 같을 때만 재사용한다.
CREATE TABLE IF NOT EXISTS memory_bank (
    cache_id     TEXT PRIMARY KEY,
    question     TEXT NOT NULL,
    embedding    BLOB NOT NULL,
    answer       TEXT NOT NULL,
    route        TEXT,
    intent       TEXT,
    entity_key   TEXT NOT NULL DEFAULT '',
    source_pages TEXT NOT NULL DEFAULT '[]',
    hit_count    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    last_hit_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_cache_entity ON memory_bank(entity_key);

-- 답변하지 못한 질문. 기획자에게 넘길 '문서 작성 우선순위' 근거다.
-- 같은 질문이 또 오면 행을 늘리지 않고 asked_count 를 올린다.
-- 자주 묻는데 답이 없는 것이 곧 먼저 써야 할 문서다.
CREATE TABLE IF NOT EXISTS unanswered (
    q_norm      TEXT PRIMARY KEY,
    question    TEXT NOT NULL,
    intent      TEXT,
    entities    TEXT NOT NULL DEFAULT '[]',
    top_score   REAL,
    asked_count INTEGER NOT NULL DEFAULT 1,
    first_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    last_at     TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_unanswered_hot ON unanswered(asked_count DESC);

-- 답변 만족도. 임계값 튜닝의 유일한 객관적 근거다.
-- 미답변 로그는 '못 답한 것'만 잡는다. '틀리게 답한 것'은 여기서만 보인다.
-- 튜닝하려면 표만으로는 부족하고 그 답이 어느 경로/유사도에서 나왔는지가 필요하므로
-- 답변 시점의 route 와 similarity 를 함께 남긴다.
CREATE TABLE IF NOT EXISTS answers (
    message_id   TEXT PRIMARY KEY,   -- 디스코드 메시지 ID
    question     TEXT NOT NULL,
    answer       TEXT NOT NULL,
    route        TEXT,
    intent       TEXT,
    similarity   REAL,
    source_pages TEXT NOT NULL DEFAULT '[]',
    at           TEXT DEFAULT CURRENT_TIMESTAMP
);

-- 한 사람당 한 표. 리액션을 뗐다 붙였다 해도 집계가 부풀지 않는다.
CREATE TABLE IF NOT EXISTS votes (
    message_id TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    vote       INTEGER NOT NULL,   -- 1 = 👍 / -1 = 👎
    at         TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (message_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_votes_msg ON votes(message_id);
"""


class Store:
    def __init__(self, path: Path | str = config.DB_PATH):
        self.path = str(path)
        # 봇은 이벤트 루프를 막지 않으려고 엔진을 별도 스레드에서 호출한다.
        # 그래서 생성 스레드 제한을 푼다. 동시 접근은 상위(bot.py)의 락으로 막는다.
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- 동기화 상태 -----------------------------------------------------
    def get_cursor(self, db_key: str) -> str | None:
        row = self.conn.execute(
            "SELECT last_sync FROM sync_state WHERE db_key=?", (db_key,)
        ).fetchone()
        return row["last_sync"] if row else None

    def set_cursor(self, db_key: str, ts: str) -> None:
        self.conn.execute(
            "INSERT INTO sync_state(db_key,last_sync) VALUES(?,?) "
            "ON CONFLICT(db_key) DO UPDATE SET last_sync=excluded.last_sync",
            (db_key, ts),
        )
        self.conn.commit()

    # -- 레코드 ----------------------------------------------------------
    def existing_hashes(self) -> dict[str, str]:
        return {
            r["page_id"]: r["content_hash"]
            for r in self.conn.execute("SELECT page_id, content_hash FROM records")
        }

    def title_map(self) -> dict[str, dict[str, str]]:
        """Resolver 복원용. 증분 동기화에서도 관계를 풀 수 있게 전체 제목을 준다."""
        return {
            r["page_id"]: {"title": r["title"], "db_key": r["db_key"], "url": r["url"] or ""}
            for r in self.conn.execute("SELECT page_id,title,db_key,url FROM records")
        }

    def upsert(self, rec: Record, raw_props: dict | None = None) -> str:
        """레코드를 저장하고 변화 종류를 돌려준다."""
        prev = self.conn.execute(
            "SELECT content_hash, published FROM records WHERE page_id=?", (rec.page_id,)
        ).fetchone()
        new_hash = rec.content_hash()

        if prev and prev["content_hash"] == new_hash:
            return "unchanged"

        self.conn.execute(
            """INSERT INTO records
               (page_id,db_key,title,url,status,published,patch_version,last_edited,
                aliases,facts,raw_props,answer_text,search_text,body,depends_on,
                content_hash,synced_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(page_id) DO UPDATE SET
                 db_key=excluded.db_key, title=excluded.title, url=excluded.url,
                 status=excluded.status, published=excluded.published,
                 patch_version=excluded.patch_version, last_edited=excluded.last_edited,
                 aliases=excluded.aliases, facts=excluded.facts,
                 raw_props=excluded.raw_props,
                 answer_text=excluded.answer_text, search_text=excluded.search_text,
                 body=excluded.body, depends_on=excluded.depends_on,
                 content_hash=excluded.content_hash, synced_at=CURRENT_TIMESTAMP""",
            (
                rec.page_id, rec.db_key, rec.title, rec.url, rec.status,
                int(rec.is_published), rec.patch_version, rec.last_edited,
                json.dumps(rec.aliases, ensure_ascii=False),
                json.dumps(rec.facts, ensure_ascii=False),
                json.dumps(raw_props or {}, ensure_ascii=False),
                rec.answer_text, rec.search_text, rec.body,
                json.dumps(rec.depends_on), new_hash,
            ),
        )
        self._reindex_aliases(rec)

        if prev is None:
            change = "created"
        elif prev["published"] and not rec.is_published:
            change = "unpublished"
        else:
            change = "updated"
        self._log(rec.page_id, rec.db_key, change)
        return change

    def _reindex_aliases(self, rec: Record) -> None:
        self.conn.execute("DELETE FROM aliases WHERE page_id=?", (rec.page_id,))
        if not rec.is_published:
            return
        rows = [(normalize(rec.title), rec.title, rec.page_id, rec.db_key, 1)]
        for a in rec.aliases:
            key = normalize(a)
            if key and key != rows[0][0]:
                rows.append((key, a, rec.page_id, rec.db_key, 0))
        self.conn.executemany(
            "INSERT OR REPLACE INTO aliases"
            "(alias_norm,alias_raw,page_id,db_key,is_primary) VALUES(?,?,?,?,?)",
            rows,
        )

    def _log(self, page_id: str, db_key: str, change: str) -> None:
        self.conn.execute(
            "INSERT INTO change_log(page_id,db_key,change) VALUES(?,?,?)",
            (page_id, db_key, change),
        )

    def delete_missing(self, db_key: str, remote_ids: set[str]) -> list[str]:
        """원격에 없는 로컬 행을 지운다.

        증분 동기화만으로는 잡히지 않는 케이스다. 삭제된 페이지는 애초에
        쿼리 결과에 안 나오므로 전체 대조를 주기적으로 돌려야 한다.
        """
        local = {
            r["page_id"] for r in
            self.conn.execute("SELECT page_id FROM records WHERE db_key=?", (db_key,))
        }
        gone = sorted(local - remote_ids)
        for pid in gone:
            self.conn.execute("DELETE FROM records WHERE page_id=?", (pid,))
            self.conn.execute("DELETE FROM aliases WHERE page_id=?", (pid,))
            self._drop_chunks(pid)
            self._log(pid, db_key, "deleted")
        self.conn.commit()
        return gone

    def load_raw(self, page_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """저장해둔 원본 프로퍼티를 되읽는다. 재평탄화에 쓴다."""
        ids = list(page_ids)
        if not ids:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(ids), 400):
            block = ids[i:i + 400]
            marks = ",".join("?" * len(block))
            for r in self.conn.execute(
                f"""SELECT page_id, db_key, url, last_edited, body, raw_props
                    FROM records WHERE page_id IN ({marks})""",
                block,
            ):
                out[r["page_id"]] = {
                    "db_key": r["db_key"],
                    "page": {
                        "id": r["page_id"],
                        "url": r["url"] or "",
                        "last_edited_time": r["last_edited"] or "",
                    },
                    "props": json.loads(r["raw_props"] or "{}"),
                    "body": r["body"] or "",
                }
        return out

    def all_page_ids(self, db_key: str | None = None) -> list[str]:
        if db_key:
            rows = self.conn.execute(
                "SELECT page_id FROM records WHERE db_key=?", (db_key,))
        else:
            rows = self.conn.execute("SELECT page_id FROM records")
        return [r["page_id"] for r in rows]

    def dependents_of(self, page_ids: Iterable[str]) -> set[str]:
        """주어진 페이지를 참조하는 레코드를 찾는다.

        아이템 '토마토'의 이름이 바뀌면 그걸 재료로 쓰는 레시피의 answer_text가
        낡는다. 그런데 레시피 행 자체는 수정된 적이 없어서 증분에 안 잡힌다.
        이 함수가 그 구멍을 메운다.
        """
        targets = set(page_ids)
        if not targets:
            return set()
        hits: set[str] = set()
        for r in self.conn.execute("SELECT page_id, depends_on FROM records"):
            deps = set(json.loads(r["depends_on"] or "[]"))
            if deps & targets:
                hits.add(r["page_id"])
        return hits - targets

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

    def lookup_alias(self, term: str) -> list[sqlite3.Row]:
        """정확매칭. 메모리뱅크 오답을 막는 고유명사 검증에 쓴다."""
        return list(self.conn.execute(
            "SELECT a.*, r.title, r.db_key AS rdb FROM aliases a "
            "JOIN records r ON r.page_id=a.page_id "
            "WHERE a.alias_norm=? ORDER BY a.is_primary DESC",
            (normalize(term),),
        ))

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

    # -- 답변 못 한 질문 --------------------------------------------------
    def log_unanswered(self, question: str, intent: str | None = None,
                       entities: list[str] | None = None,
                       top_score: float | None = None) -> None:
        """fallback 으로 빠진 질문을 쌓는다.

        중복 판정은 별칭용 normalize 에 물음표류를 더 떼어낸 값으로 한다.
        ('치즈 얼마?' 와 '치즈얼마' 는 같은 질문, '치즈 어디서' 는 다른 질문)
        """
        q = (question or "").strip()
        key = normalize(re.sub(r"[?!,~]+", "", q))
        if not key:
            return
        self.conn.execute(
            """INSERT INTO unanswered(q_norm,question,intent,entities,top_score)
               VALUES(?,?,?,?,?)
               ON CONFLICT(q_norm) DO UPDATE SET
                 asked_count = asked_count + 1,
                 last_at     = CURRENT_TIMESTAMP,
                 top_score   = excluded.top_score,
                 intent      = COALESCE(excluded.intent, unanswered.intent)""",
            (key, q, intent, json.dumps(entities or [], ensure_ascii=False), top_score),
        )
        self.conn.commit()

    def unanswered_top(self, limit: int = 30) -> list[sqlite3.Row]:
        """많이 물어본 순. 이 순서가 곧 문서 작성 우선순위다."""
        return list(self.conn.execute(
            "SELECT * FROM unanswered ORDER BY asked_count DESC, last_at DESC LIMIT ?",
            (limit,),
        ))

    def unanswered_count(self) -> tuple[int, int]:
        """(서로 다른 질문 수, 총 질문 횟수)"""
        row = self.conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(asked_count),0) t FROM unanswered"
        ).fetchone()
        return row["n"], row["t"]

    def clear_unanswered(self) -> int:
        n = self.unanswered_count()[0]
        self.conn.execute("DELETE FROM unanswered")
        self.conn.commit()
        return n

    # -- 답변 만족도 ------------------------------------------------------
    def log_answer(self, message_id: str, question: str, answer: str,
                   route: str | None, intent: str | None,
                   similarity: float | None, source_pages: list[str] | None = None) -> None:
        """봇이 내보낸 답변을 기록한다. 나중에 붙을 표와 이어붙이기 위한 것."""
        self.conn.execute(
            """INSERT INTO answers
               (message_id,question,answer,route,intent,similarity,source_pages)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(message_id) DO NOTHING""",
            (str(message_id), question.strip(), answer, route, intent, similarity,
             json.dumps(sorted(set(source_pages or [])))),
        )
        self.conn.commit()

    def vote(self, message_id: str, user_id: str, vote: int) -> bool:
        """표를 남긴다. 봇이 낸 답변이 아니면 무시하고 False."""
        mid = str(message_id)
        known = self.conn.execute(
            "SELECT 1 FROM answers WHERE message_id=?", (mid,)).fetchone()
        if not known:
            return False
        self.conn.execute(
            """INSERT INTO votes(message_id,user_id,vote) VALUES(?,?,?)
               ON CONFLICT(message_id,user_id) DO UPDATE SET
                 vote=excluded.vote, at=CURRENT_TIMESTAMP""",
            (mid, str(user_id), 1 if vote > 0 else -1),
        )
        self.conn.commit()
        return True

    def unvote(self, message_id: str, user_id: str, vote: int) -> None:
        """리액션을 뗀 경우. 그 사이 반대표로 바꿨다면 지우지 않는다."""
        self.conn.execute(
            "DELETE FROM votes WHERE message_id=? AND user_id=? AND vote=?",
            (str(message_id), str(user_id), 1 if vote > 0 else -1),
        )
        self.conn.commit()

    def feedback_by_route(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            """SELECT a.route,
                      SUM(CASE WHEN v.vote > 0 THEN 1 ELSE 0 END) good,
                      SUM(CASE WHEN v.vote < 0 THEN 1 ELSE 0 END) bad
               FROM votes v JOIN answers a ON a.message_id = v.message_id
               GROUP BY a.route ORDER BY bad DESC, good DESC"""
        ))

    def feedback_bad(self, limit: int = 20) -> list[sqlite3.Row]:
        """👎 받은 답변. 유사도가 같이 나와야 임계값을 어디로 옮길지 판단할 수 있다."""
        return list(self.conn.execute(
            """SELECT a.question, a.route, a.similarity, a.intent,
                      COUNT(*) bad, MAX(v.at) last_at
               FROM votes v JOIN answers a ON a.message_id = v.message_id
               WHERE v.vote < 0
               GROUP BY a.message_id
               ORDER BY bad DESC, last_at DESC LIMIT ?""",
            (limit,),
        ))

    def feedback_totals(self) -> tuple[int, int]:
        row = self.conn.execute(
            """SELECT SUM(CASE WHEN vote > 0 THEN 1 ELSE 0 END) g,
                      SUM(CASE WHEN vote < 0 THEN 1 ELSE 0 END) b FROM votes"""
        ).fetchone()
        return (row["g"] or 0), (row["b"] or 0)

    def pending_changes(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM change_log WHERE consumed=0 ORDER BY id"
        ))

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for r in self.conn.execute(
            "SELECT db_key, COUNT(*) n, SUM(published) pub FROM records GROUP BY db_key"
        ):
            out[r["db_key"]] = {"total": r["n"], "published": r["pub"] or 0}
        out["_chunks"] = self.conn.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"]
        out["_embedded"] = self.conn.execute(
            "SELECT COUNT(*) c FROM chunks WHERE embedding IS NOT NULL"
        ).fetchone()["c"]
        return out

    def commit(self) -> None:
        self.conn.commit()
