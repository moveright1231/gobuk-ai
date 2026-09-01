#!/usr/bin/env python3
"""질의 진입점 — 봇과 똑같은 응답 경로를 CLI 에서 태운다.

  python query.py "던전 3은 몇레벨"
  python query.py -i                    # 대화형
  python query.py --bench               # 표준 질문 일괄 실행, 경로 분포 출력
  python query.py --unanswered          # 답변 못 한 질문 (= 문서 작성 우선순위)

구현은 gobuk/engine/cli.py 에 있다.
"""
import sys

from gobuk.engine.cli import main

if __name__ == "__main__":
    sys.exit(main())
