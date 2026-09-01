"""로컬 저장소.

Store 는 관심사별 믹스인을 합친 것이다. 클래스를 쪼개지 않고 믹스인으로 둔 이유:
  - 호출부가 store.upsert / store.log_answer 처럼 한 객체에 그대로 접근한다
  - delete_missing(레코드)이 _drop_chunks(검색)를 부르는 것처럼 관심사를
    가로지르는 연산이 실제로 있고, 이걸 위임으로 풀면 배선만 늘어난다

표 정의는 각 믹스인 모듈이 자기 것을 들고 있고, DDL 을 여기서 이어 붙인다.
"""
from __future__ import annotations

from typing import Any

from gobuk.store import cache, feedback, records, search
from gobuk.store.base import BaseStore
from gobuk.store.cache import MemoryBank
from gobuk.store.feedback import FeedbackMixin
from gobuk.store.records import RecordsMixin
from gobuk.store.search import SearchMixin

__all__ = ["Store", "MemoryBank"]


class Store(RecordsMixin, SearchMixin, FeedbackMixin, BaseStore):
    DDL = (records.DDL, search.DDL, cache.DDL, feedback.DDL)

    def stats(self) -> dict[str, Any]:
        """일부러 여기 둔다 — 레코드와 청크를 함께 세므로 어느 한 관심사가 아니다."""
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
