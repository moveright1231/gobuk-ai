#!/usr/bin/env python3
"""라우터/메모리뱅크 검증. OpenAI 키 없이 돈다.

임베딩은 결정적 가짜 벡터로 대체한다. 문자 단위 해시 기반이라
비슷한 문장은 실제로 높은 코사인 유사도가 나오므로, 캐시 오염
시나리오를 그대로 재현할 수 있다.
"""
from __future__ import annotations

import os
import tempfile

import numpy as np

# 별도 테스트 러너를 쓰지 않는다. 두 파일 모두 그냥 실행하면 되는 스크립트이므로
# 저장소 루트를 직접 sys.path 에 올린다. (python tests/test_sync.py)
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from gobuk import config
from gobuk.engine.answer import Engine
from test_sync import P, load, make_raw, check
from gobuk.store import Store


class FakeEmbedder:
    """문자 trigram 해시 임베딩. 비슷한 문장 = 높은 유사도."""

    def encode(self, texts: list[str]) -> np.ndarray:
        out = []
        for t in texts:
            v = np.zeros(config.EMBED_DIM, dtype=np.float32)
            s = t.replace(" ", "")
            for n in (2, 3):
                for i in range(max(len(s) - n + 1, 0)):
                    v[hash(s[i:i + n]) % config.EMBED_DIM] += 1.0
            norm = np.linalg.norm(v)
            out.append(v / norm if norm else v)
        return np.vstack(out)


def build() -> Engine:
    tmp = tempfile.mkdtemp()
    store = Store(os.path.join(tmp, "router.sqlite3"))
    load(store, make_raw())
    emb = FakeEmbedder()
    rows = store.chunks_needing_embedding()
    store.save_embeddings(list(zip([r["chunk_id"] for r in rows],
                                   emb.encode([r["text"] for r in rows]))))
    return Engine(store, embedder=emb, use_llm=False)


def main() -> int:
    eng = build()
    ok = True

    print("\n1. 의도 판별로 DB가 갈리는가  ('치즈' 문제)")
    for q, want_db in [("치즈 어떻게 만들어", "recipe"),
                       ("치즈 얼마에 팔려", "item"),
                       ("치즈 어디서 구해", "item"),
                       ("치즈 레시피", "recipe")]:
        r = eng.ask(q, use_cache=False)
        got = r.sources[0].get("db") if r.sources else None
        ok &= check(f"{q!r} -> {want_db}", got == want_db, f"실제 {got} (의도={r.intent})")

    print("\n2. 의도가 없으면 최대한 다 답하는가")
    r = eng.ask("치즈", use_cache=False)
    ok &= check("레시피와 아이템을 함께 답변",
                "[레시피]" in r.text and "[아이템]" in r.text)
    ok &= check("모호 플래그가 켜짐", r.ambiguous)

    print("\n3. 정형 질문이 LLM 없이 끝나는가")
    for q in ["토스파 레시피 알려줘", "크스파 재료 뭐야", "요리사 전직 조건"]:
        r = eng.ask(q, use_cache=False)
        ok &= check(f"{q!r} 경로=exact, LLM 0회",
                    r.route == "exact" and r.llm_calls == 0,
                    f"{r.route}/{r.llm_calls}회")

    print("\n4. 캐시 오염 방지  (핵심)")
    # 가짜 임베더로는 실제 모델의 유사도 분포를 못 만든다.
    # 그래서 '유사도는 임계값을 넘는데 고유명사가 다른' 상황을 직접 만들어
    # 방어 로직만 정확히 검증한다.
    from gobuk.store import MemoryBank
    bank = MemoryBank(eng.store)
    rng = np.random.default_rng(0)
    base = rng.normal(size=config.EMBED_DIM).astype(np.float32)
    base /= np.linalg.norm(base)
    near = base + rng.normal(scale=0.004, size=config.EMBED_DIM).astype(np.float32)
    near /= np.linalg.norm(near)
    sim = float(base @ near)
    print(f"     두 질문 벡터의 유사도: {sim:.4f}  (임계값 {config.CACHE_SIM})")
    ok &= check("오염 위험 상황이 재현됨", sim >= config.CACHE_SIM)

    bank.clear()
    bank.store_answer("요리사 토마토스파게티 레시피", base, "토마토 답변",
                      "exact", "recipe", "ENTITY_TOMATO", ["p1"])

    got = bank.lookup(near, "ENTITY_CREAM", "recipe")
    ok &= check("고유명사가 다르면 캐시를 물지 않음", got is None,
                f"반환 {got and got['answer']}")

    got = bank.lookup(near, "ENTITY_TOMATO", "recipe")
    ok &= check("고유명사가 같으면 정상 재사용", got is not None and
                got["answer"] == "토마토 답변")

    got = bank.lookup(near, "ENTITY_TOMATO", "item")
    ok &= check("의도가 다르면 캐시를 물지 않음", got is None,
                "('치즈 레시피' vs '치즈 얼마' 구분")
    bank.clear()

    print("\n5. 캐시가 실제로 동작하는가")
    # 가짜 임베더는 실제 모델과 유사도 스케일이 달라 임계값을 잠시 낮춘다.
    orig_min, orig_direct = config.VECTOR_MIN, config.VECTOR_DIRECT
    config.VECTOR_MIN, config.VECTOR_DIRECT = 0.05, 0.10
    try:
        q = "요리사 초반에 뭐부터 해야 돼"
        first = eng.ask(q)
        second = eng.ask(q)
        ok &= check("1회차는 검색 경로", first.route in ("direct", "llm"),
                    f"경로 {first.route}")
        ok &= check("2회차는 캐시 히트", second.route == "cache",
                    f"{first.route} -> {second.route}")
        ok &= check("답변 내용 동일", first.text == second.text)
    finally:
        config.VECTOR_MIN, config.VECTOR_DIRECT = orig_min, orig_direct

    print("\n6. 근거 없는 질문은 관리자 안내")
    r = eng.ask("서버에 드래곤 레이드 언제 열려?", use_cache=False)
    ok &= check("fallback 경로", r.route == "fallback",
                f"{r.route} / 유사도 {r.similarity}")
    ok &= check("관리자 문의 문구", config.ADMIN_CONTACT in r.text)

    print("\n7. 미게시 항목이 새어나가지 않는가")
    r = eng.ask("새우 파스타 레시피", use_cache=False)
    ok &= check("검수중인 레시피는 답하지 않음",
                "새우 4개" not in r.text, f"경로 {r.route}")

    print("\n8. 패치 시 캐시 무효화")
    bank.clear()
    # 적재 과정에서 쌓인 created 이력을 먼저 비워 격리한다.
    bank.purge_stale(verbose=False)
    bank.store_answer("토스파 레시피", base, "옛날 답변", "exact", "recipe",
                      "E1", [P["r_토스파"]])
    bank.store_answer("앗사 전직", base, "무관한 답변", "exact", "job",
                      "E2", [P["요리사"]])
    ok &= check("캐시 2건 저장됨", bank.stats()["entries"] == 2)

    eng.store._log(P["r_토스파"], "recipe", "updated")
    eng.store.commit()
    killed = bank.purge_stale(verbose=False)
    ok &= check("바뀐 페이지를 근거로 한 캐시만 삭제", killed == 1, f"{killed}건 삭제")
    ok &= check("무관한 캐시는 남음", bank.stats()["entries"] == 1)
    ok &= check("change_log 소비 처리됨", not eng.store.pending_changes())

    print("\n9. 답변 못 한 질문이 쌓이는가")
    eng.store.clear_unanswered()
    miss = "서버에 드래곤 레이드 언제 열려?"
    eng.ask(miss, use_cache=False)
    rows = eng.store.unanswered_top()
    ok &= check("fallback 질문이 기록됨", len(rows) == 1, f"{len(rows)}건")

    eng.ask(miss, use_cache=False)
    eng.ask("서버에 드래곤 레이드 언제 열려", use_cache=False)  # 물음표만 다름
    rows = eng.store.unanswered_top()
    ok &= check("같은 질문은 행이 늘지 않고 횟수만 오름",
                len(rows) == 1 and rows[0]["asked_count"] == 3,
                f"{len(rows)}종 / {rows[0]['asked_count']}회")

    eng.ask("토스파 레시피 알려줘", use_cache=False)
    ok &= check("답변한 질문은 기록되지 않음",
                len(eng.store.unanswered_top()) == 1)

    quiet = Engine(eng.store, embedder=eng._embedder, use_llm=False, log_misses=False)
    quiet.ask("완전히 없는 내용 질문", use_cache=False)
    ok &= check("log_misses=False 면 쌓지 않음 (벤치용)",
                len(eng.store.unanswered_top()) == 1)
    eng.store.clear_unanswered()

    print("\n10. 만족도 수집")
    st = eng.store
    st.log_answer("msg1", "토스파 레시피", "답변A", "direct", "recipe", 0.61, ["p1"])
    st.log_answer("msg2", "앗사 전직", "답변B", "exact", "job", None, ["p2"])

    ok &= check("봇 답변이 아닌 메시지의 표는 무시",
                st.vote("없는메시지", "u1", 1) is False)

    st.vote("msg1", "u1", -1)
    st.vote("msg1", "u2", -1)
    st.vote("msg2", "u1", 1)
    good, bad = st.feedback_totals()
    ok &= check("표가 집계됨", (good, bad) == (1, 2), f"👍{good} 👎{bad}")

    # 같은 사람이 마음을 바꾼 경우. 행이 늘면 안 된다.
    st.vote("msg1", "u1", 1)
    good, bad = st.feedback_totals()
    ok &= check("한 사람당 한 표 (뒤집으면 교체)", (good, bad) == (2, 1),
                f"👍{good} 👎{bad}")

    # 👍 로 바꾼 뒤 옛 👎 리액션이 떨어져도 현재 표를 지우면 안 된다.
    st.unvote("msg1", "u1", -1)
    good, bad = st.feedback_totals()
    ok &= check("반대표로 바꾼 뒤 옛 리액션 제거는 무해", (good, bad) == (2, 1),
                f"👍{good} 👎{bad}")

    st.unvote("msg1", "u2", -1)
    good, bad = st.feedback_totals()
    ok &= check("리액션을 떼면 표가 빠짐", (good, bad) == (2, 0), f"👍{good} 👎{bad}")

    by_route = {r["route"]: (r["good"], r["bad"]) for r in st.feedback_by_route()}
    ok &= check("경로별로 집계됨", by_route.get("direct") == (1, 0)
                and by_route.get("exact") == (1, 0), str(by_route))

    print("\n11. 잡담 경로 (게임 질문이 새지 않는가)")
    from gobuk.engine import answer as answer_mod
    real_chat = answer_mod.chat
    talker = Engine(eng.store, embedder=eng._embedder, use_llm=True, log_misses=False)
    try:
        answer_mod.chat = lambda *a, **k: "저야 늘 좋죠! 오늘도 즐겜하세요."
        r = talker.ask("오늘 하루 어때?", use_cache=False)
        ok &= check("인사에는 가볍게 답함", r.route == "chat",
                    f"경로 {r.route} / 유사도 {r.similarity}")

        # 여기가 핵심. 의도가 잡힌 질문을 LLM에 넘기면 없는 정보를 지어낸다.
        r = talker.ask("드래곤 레이드 얼마에 팔려", use_cache=False)
        ok &= check("의도가 잡히면 잡담으로 새지 않음", r.route != "chat",
                    f"경로 {r.route} (의도={r.intent})")

        # 의도도 고유명사도 안 잡히는 게임 질문. 여기서 새면 모델이 없는
        # 지역·일정·콘텐츠를 지어낸다. 실제로 "거북마을이 뭐야" 가 잡담으로
        # 새서 "귀엽고 느린 거북이들이 사는 마을" 이라는 답이 나간 적이 있다.
        # 모델이 답해버리는 상황을 가정해야 가드를 검증할 수 있다.
        answer_mod.chat = lambda *a, **k: "귀여운 거북이들이 사는 마을이죠!"
        for q in ("거북마을이 뭐야", "던전 종류 뭐가 있지", "다음 업데이트 언제야?",
                  "이 서버에 대해 설명해줘"):
            r = talker.ask(q, use_cache=False)
            ok &= check(f"서버 주제어는 잡담으로 새지 않음: {q}",
                        r.route == "fallback" and config.ADMIN_CONTACT in r.text,
                        f"경로 {r.route} / {r.text[:40]}")

        # 반대쪽도 지켜야 한다. 주제어 목록을 넓히다가 일반 상식까지 막으면
        # 잡담 경로를 둔 이유가 없어진다. '거북'만으로는 걸리지 않아야 한다.
        answer_mod.chat = lambda *a, **k: "거북이는 등껍질이 있는 파충류예요!"
        r = talker.ask("거북이가 뭐야?", use_cache=False)
        ok &= check("일반 상식은 계속 잡담으로 답함", r.route == "chat",
                    f"경로 {r.route}")

        # 모델이 표식을 내면 원래대로 관리자 문의가 나가야 한다.
        answer_mod.chat = lambda *a, **k: config.DECLINE
        r = talker.ask("오늘 하루 어때?", use_cache=False)
        ok &= check("게임 정보를 물으면 표식 -> 관리자 문의",
                    r.route == "fallback" and config.ADMIN_CONTACT in r.text,
                    f"경로 {r.route}")

        # 잡담을 캐시하면 같은 농담만 반복하는 봇이 된다.
        answer_mod.chat = lambda *a, **k: "농담 하나!"
        eng.store.conn.execute("DELETE FROM memory_bank")
        talker.ask("농담 하나 해줘", use_cache=True)
        ok &= check("잡담은 캐시하지 않음",
                    MemoryBank(eng.store).stats()["entries"] == 0)
    finally:
        answer_mod.chat = real_chat

    print("\n12. LLM 요약의 거절 판별")
    # 예전엔 '없습니다' 라는 낱말로 거절을 판별해서, 문서에 근거한 정상 답변인
    # "요구 레벨이 없습니다" 가 통째로 버려지고 관리자 문의가 나갔다.
    orig_min, orig_direct = config.VECTOR_MIN, config.VECTOR_DIRECT
    config.VECTOR_MIN, config.VECTOR_DIRECT = 0.05, 0.99
    real_chat = answer_mod.chat
    llm_eng = Engine(eng.store, embedder=eng._embedder, use_llm=True, log_misses=False)
    try:
        answer_mod.chat = lambda *a, **k: "토마토 스파게티는 요구 레벨이 없습니다."
        r = llm_eng.ask("요리사 초반에 뭐부터 해야 돼", use_cache=False)
        ok &= check("'없습니다' 가 든 정상 답변을 버리지 않음",
                    r.route == "llm" and "없습니다" in r.text, f"경로 {r.route}")

        answer_mod.chat = lambda *a, **k: config.DECLINE
        r = llm_eng.ask("요리사 초반에 뭐부터 해야 돼", use_cache=False)
        ok &= check("표식이면 관리자 문의", r.route == "fallback", f"경로 {r.route}")

        answer_mod.chat = lambda *a, **k: "문서에 정보가 없어 잘 모르겠습니다."
        r = llm_eng.ask("요리사 초반에 뭐부터 해야 돼", use_cache=False)
        ok &= check("표식을 무시하고 말로 거절해도 잡아냄",
                    r.route == "fallback", f"경로 {r.route}")
    finally:
        answer_mod.chat = real_chat
        config.VECTOR_MIN, config.VECTOR_DIRECT = orig_min, orig_direct

    print("\n" + ("전체 통과" if ok else "실패 항목 있음"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
