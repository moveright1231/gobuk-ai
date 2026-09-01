"""거북AI — 마인크래프트 서버 '거북스토리' 디스코드 질문응답 봇.

  notion/  Notion REST 래퍼
  sync/    A단계. Notion -> 로컬 SQLite 파이프라인
  store/   SQLite 저장소 (관심사별 믹스인)
  engine/  B단계. 의도 판별 / 라우팅 / 임베딩
  bot.py   디스코드 클라이언트

실행은 저장소 루트의 sync.py / query.py / bot.py 진입점을 쓴다.
"""
