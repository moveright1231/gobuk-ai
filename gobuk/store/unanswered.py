"""답변 못 한 질문 로그.

fallback 으로 빠진 질문을 쌓는다. 같은 질문이 또 오면 행을 늘리지 않고
asked_count 만 올리므로, 많이 물어본 순서가 곧 기획자의 문서 작성 우선순위다.

읽는 법: 고유명사가 잡혔는데 답을 못 했다면 문서는 있고 내용이 빈 것이고,
안 잡혔다면 문서가 아예 없는 것이다. 전자가 훨씬 고치기 쉽다.
"""
from __future__ import annotations

import json
import re
import sqlite3

from gobuk.sync.flatten import normalize

DDL = """
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
"""


class UnansweredMixin:
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
