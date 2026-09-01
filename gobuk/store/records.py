"""레코드 · 별칭 · 동기화 커서 · 변경 로그.

A단계가 쓰는 표들이다. raw_props 를 영구 보관하는 게 핵심 — 답변 포맷을 바꿀 때
Notion 을 다시 긁지 않고 --reflatten 한 번으로 전부 재생성할 수 있다.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from gobuk.sync.flatten import Record, normalize

DDL = """
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
"""


class RecordsMixin:
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

    # -- 변경 로그 (캐시 무효화 신호) -------------------------------------
    def pending_changes(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM change_log WHERE consumed=0 ORDER BY id"
        ))

    # -- 별칭 정확매칭 ---------------------------------------------------
    def lookup_alias(self, term: str) -> list[sqlite3.Row]:
        """정확매칭. 메모리뱅크 오답을 막는 고유명사 검증에 쓴다."""
        return list(self.conn.execute(
            "SELECT a.*, r.title, r.db_key AS rdb FROM aliases a "
            "JOIN records r ON r.page_id=a.page_id "
            "WHERE a.alias_norm=? ORDER BY a.is_primary DESC",
            (normalize(term),),
        ))
