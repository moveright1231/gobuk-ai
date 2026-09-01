"""저장소 기반 — 연결과 스키마 조립.

벡터DB를 따로 두지 않고 SQLite 한 파일로 끝낸다. 거북스토리 규모(수천 행)에서
numpy 전수 코사인은 수 ms 안에 끝나므로 Chroma/Qdrant를 붙일 이유가 없다.
나중에 수만 행을 넘어가면 그때 chunks 테이블만 옮기면 된다.

테이블 정의(DDL)는 이 파일에 모으지 않고 각 관심사 모듈이 자기 것을 들고 있다.
예전에는 memory_bank 스키마만 store.py 에 있고 클래스는 memory_bank.py 에
있어서, 표를 고칠 때 두 파일을 왕복해야 했다.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from gobuk import config

PRAGMA = "PRAGMA journal_mode=WAL;\n"


class BaseStore:
    """연결만 든다. 표를 아는 건 믹스인들이다."""

    #: 믹스인이 각자 채운다. Store 조립 시점에 순서대로 실행된다.
    DDL: tuple[str, ...] = ()

    def __init__(self, path: Path | str = config.DB_PATH):
        self.path = str(path)
        # 봇은 이벤트 루프를 막지 않으려고 엔진을 별도 스레드에서 호출한다.
        # 그래서 생성 스레드 제한을 푼다. 동시 접근은 상위(bot.py)의 락으로 막는다.
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(PRAGMA + "\n".join(self.DDL))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()
