#!/usr/bin/env python3
"""동기화 진입점 — Notion 에서 로컬 SQLite 로 긁어온다.

  python sync.py                # 증분. 크론으로 10~30분마다 돌리면 된다.
  python sync.py --full         # 전체 대조. 삭제된 행까지 정리한다. 하루 1회 권장.
  python sync.py --reflatten    # API 안 부르고 로컬 원본만으로 답변 문구 재생성.
  python sync.py --no-embed     # 임베딩 건너뛰기. 구조만 확인할 때.
  python sync.py --stats        # 현재 적재 상태만 출력.
  python sync.py --doctor       # 설정/권한 점검. 뭔가 안 되면 항상 여기부터.
  python sync.py --discover     # 통합이 볼 수 있는 데이터소스 ID 찾기. 설치 시 1회.

구현은 gobuk/sync/ 에 있다. 이 파일은 명령어를 유지하기 위한 진입점이다.
"""
import sys

from gobuk.sync.cli import main

if __name__ == "__main__":
    sys.exit(main())
