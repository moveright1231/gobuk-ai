"""운영 지표 — 답변 못 한 질문, 답변 만족도.

미답변 로그는 '못 답한 것'만 잡는다. '틀리게 답한 것'은 만족도에서만 보인다.
둘 다 임계값 튜닝과 문서 작성 우선순위의 근거다.
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


class FeedbackMixin:
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
