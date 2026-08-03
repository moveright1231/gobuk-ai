"""메모리뱅크 (시맨틱 캐시).

같은 질문이 또 오면 저장해둔 답을 그대로 낸다. LLM 호출 0회, 응답 즉시.

여기서 제일 중요한 건 '언제 히트시키지 않을 것인가'다.
임베딩 유사도만 보면 마인크래프트 도메인에서 반드시 사고가 난다.

    "요리사 토마토스파게티 레시피"  vs  "요리사 크림스파게티 레시피"   -> 0.96+
    "워리어 전직 조건"             vs  "어세신 전직 조건"            -> 0.97+

문장 구조가 같고 고유명사 한 단어만 다르기 때문이다. 그래서 유사도와 별개로
고유명사(entity_key)가 정확히 일치할 것을 필수 조건으로 건다.
"""
from __future__ import annotations

import hashlib
import json

import numpy as np

import config


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:24]


class MemoryBank:
    def __init__(self, store):
        self.store = store
        self.conn = store.conn

    # -- 조회 ------------------------------------------------------------
    def lookup(self, query_vec: np.ndarray, entity_key: str,
               intent: str | None) -> dict | None:
        """캐시에서 재사용 가능한 답을 찾는다.

        고유명사가 잡힌 질문은 그 집합이 완전히 같은 항목만 후보로 본다.
        고유명사가 없는 질문(예: "야생맵 어떻게 가")은 후보를 좁힐 수단이 없으므로
        더 높은 유사도를 요구한다.
        """
        if entity_key:
            rows = list(self.conn.execute(
                "SELECT * FROM memory_bank WHERE entity_key=?", (entity_key,)))
            threshold = config.CACHE_SIM
        else:
            rows = list(self.conn.execute(
                "SELECT * FROM memory_bank WHERE entity_key=''"))
            threshold = config.CACHE_SIM_STRICT
        if not rows:
            return None

        mat = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        mat = mat / np.clip(np.linalg.norm(mat, axis=1, keepdims=True), 1e-9, None)
        q = query_vec / max(float(np.linalg.norm(query_vec)), 1e-9)
        sims = mat @ q

        order = np.argsort(-sims)
        for idx in order:
            row = rows[int(idx)]
            score = float(sims[int(idx)])
            if score < threshold:
                break
            # 의도가 둘 다 잡혔는데 서로 다르면 다른 질문으로 본다.
            # ("치즈 레시피" 와 "치즈 얼마" 는 고유명사가 같아도 답이 달라야 한다)
            if intent and row["intent"] and intent != row["intent"]:
                continue
            self.conn.execute(
                "UPDATE memory_bank SET hit_count=hit_count+1,"
                " last_hit_at=CURRENT_TIMESTAMP WHERE cache_id=?",
                (row["cache_id"],),
            )
            self.conn.commit()
            return {
                "answer": row["answer"],
                "route": row["route"],
                "similarity": round(score, 4),
                "cached_question": row["question"],
                "source_pages": json.loads(row["source_pages"]),
            }
        return None

    # -- 저장 ------------------------------------------------------------
    def store_answer(self, question: str, query_vec: np.ndarray, answer: str,
                     route: str, intent: str | None, entity_key: str,
                     source_pages: list[str]) -> None:
        if not answer or not answer.strip():
            return
        cache_id = _hash(f"{entity_key}|{intent or ''}|{question.strip()}")
        self.conn.execute(
            """INSERT INTO memory_bank
               (cache_id,question,embedding,answer,route,intent,entity_key,source_pages)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(cache_id) DO UPDATE SET
                 answer=excluded.answer, route=excluded.route,
                 source_pages=excluded.source_pages,
                 created_at=CURRENT_TIMESTAMP""",
            (cache_id, question.strip(), query_vec.astype(np.float32).tobytes(),
             answer, route, intent, entity_key,
             json.dumps(sorted(set(source_pages)))),
        )
        self.conn.commit()
        self._trim()

    def _trim(self) -> None:
        """오래되고 안 쓰이는 항목부터 정리한다."""
        n = self.conn.execute("SELECT COUNT(*) c FROM memory_bank").fetchone()["c"]
        if n <= config.CACHE_MAX_ROWS:
            return
        self.conn.execute(
            "DELETE FROM memory_bank WHERE cache_id IN ("
            "  SELECT cache_id FROM memory_bank"
            "  ORDER BY hit_count ASC, COALESCE(last_hit_at, created_at) ASC"
            "  LIMIT ?)",
            (n - config.CACHE_MAX_ROWS,),
        )
        self.conn.commit()

    # -- 무효화 ----------------------------------------------------------
    def purge_stale(self, verbose: bool = True) -> int:
        """패치로 바뀐 페이지를 근거로 만들어진 캐시를 지운다.

        이걸 안 하면 레시피가 바뀌어도 봇이 패치 전 정보를 계속 뱉는다.
        sync.py 가 동기화를 마칠 때마다 호출한다.
        """
        pending = self.store.pending_changes()
        if not pending:
            return 0
        changed = {r["page_id"] for r in pending}
        max_id = max(r["id"] for r in pending)

        killed = 0
        for row in self.conn.execute(
                "SELECT cache_id, source_pages FROM memory_bank").fetchall():
            pages = set(json.loads(row["source_pages"] or "[]"))
            if pages & changed:
                self.conn.execute("DELETE FROM memory_bank WHERE cache_id=?",
                                  (row["cache_id"],))
                killed += 1

        self.conn.execute("UPDATE change_log SET consumed=1 WHERE id<=?", (max_id,))
        self.conn.commit()
        if verbose and killed:
            print(f"  캐시 무효화: 변경 {len(changed)}페이지 -> 캐시 {killed}건 삭제")
        return killed

    def clear(self) -> int:
        n = self.conn.execute("SELECT COUNT(*) c FROM memory_bank").fetchone()["c"]
        self.conn.execute("DELETE FROM memory_bank")
        self.conn.commit()
        return n

    def stats(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(hit_count),0) hits FROM memory_bank"
        ).fetchone()
        total = row["n"] + row["hits"]
        return {
            "entries": row["n"],
            "hits": row["hits"],
            "hit_rate": round(row["hits"] / total, 3) if total else 0.0,
        }
