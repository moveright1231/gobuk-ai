#!/usr/bin/env python3
"""응답 엔진 CLI. 봇과 완전히 같은 경로를 탄다.

  python query.py "토스파 레시피 알려줘"
  python query.py -i                    대화형
  python query.py --no-cache "..."      캐시 무시
  python query.py --no-llm "..."        LLM 없이 (임베딩만)
  python query.py --bench               표준 질문 세트 일괄 실행
  python query.py --unanswered          답변 못 한 질문 (문서 작성 우선순위)
  python query.py --feedback            👍/👎 집계 (임계값 튜닝 근거)
"""
from __future__ import annotations

import argparse
import json
import sys

import config
from answer import Engine
from store import Store

BENCH = [
    "토스파 레시피 알려줘",
    "크스파는 재료가 뭐야?",
    "치즈",
    "치즈 얼마에 팔려",
    "치즈 어떻게 만들어",
    "마늘빵 레시피",
    "요리사 전직 어떻게 해",
    "앗사 전직 조건",
    "야생맵 어떻게 가",
    "생활직업이랑 전투직업 같이 할 수 있어?",
    "새우 파스타 레시피",
    "서버에 용이 있어?",
]

ROUTE_DESC = {
    "cache": "캐시 재사용", "exact": "정확매칭", "direct": "벡터 직답",
    "llm": "LLM 요약", "chat": "잡담", "fallback": "답변 불가",
}


def render(question: str, r) -> str:
    head = f"[{ROUTE_DESC.get(r.route, r.route)}] {r.elapsed_ms}ms"
    if r.similarity is not None:
        head += f" / 유사도 {r.similarity}"
    if r.llm_calls:
        head += f" / LLM {r.llm_calls}회"
    lines = [f"질문: {question}", head, "", r.text]
    if r.intent or r.entities:
        lines.append("")
        lines.append(f"  의도={r.intent or '-'} 고유명사={r.entities or '-'}")
    if r.sources:
        for s in r.sources:
            if s.get("title"):
                extra = f" ({s['score']})" if s.get("score") else ""
                lines.append(f"  - [{s.get('db','?')}] {s['title']}{extra}")
            elif s.get("cached_from"):
                lines.append(f"  - 원본 질문: {s['cached_from']}")
    return "\n".join(lines)


def print_unanswered(store) -> None:
    """기획자에게 그대로 넘길 수 있는 형태로 출력한다."""
    rows = store.unanswered_top(30)
    if not rows:
        print("답변 못 한 질문이 없습니다.")
        return
    kinds, total = store.unanswered_count()
    print(f"\n답변 못 한 질문 {kinds}종 / 누적 {total}회  (많이 물어본 순)\n")
    for r in rows:
        ents = json.loads(r["entities"] or "[]")
        note = []
        if r["intent"]:
            note.append(f"의도={r['intent']}")
        if ents:
            # 이름은 찾았는데 답을 못 했다면 문서가 비어 있다는 뜻이다.
            note.append(f"고유명사={','.join(ents)}")
        if r["top_score"] is not None:
            note.append(f"최고유사도={r['top_score']}")
        tail = f"   ({' / '.join(note)})" if note else ""
        print(f"  {r['asked_count']:>3}회  {r['question']}{tail}")
    print(f"\n  마지막 질문 시각은 last_at 열에 있습니다. "
          f"문서를 채운 뒤에는 python query.py --clear-unanswered")


def print_feedback(store) -> None:
    """임계값을 어느 쪽으로 움직여야 하는지 보이게 출력한다."""
    good, bad = store.feedback_totals()
    if not (good + bad):
        print("아직 모인 표가 없습니다. 봇을 띄우고 답변에 👍/👎 를 받아주세요.")
        return

    print(f"\n답변 만족도  👍 {good} / 👎 {bad}  (총 {good + bad}표)\n")
    # 한글/이모지는 두 칸을 차지해서 폭 지정이 안 맞는다. 헤더는 직접 맞춘다.
    print("  경로      👍    👎  만족도")
    for r in store.feedback_by_route():
        g, b = r["good"] or 0, r["bad"] or 0
        rate = f"{g / (g + b):.0%}" if (g + b) else "-"
        print(f"  {r['route'] or '?':<10}{g:>4}{b:>6}{rate:>8}")

    rows = store.feedback_bad(15)
    if not rows:
        print("\n  👎 없음. 지금 임계값을 낮춰 더 답하게 만들 여지가 있습니다.")
        return

    print("\n  👎 받은 답변  (유사도가 임계값을 어디로 옮길지 알려준다)")
    for r in rows:
        sim = f"{r['similarity']:.4f}" if r["similarity"] is not None else "-"
        print(f"    {r['bad']:>2}표  [{r['route']}] 유사도 {sim}  {r['question'][:44]}")

    # direct 경로의 👎 는 '요약을 그대로 내보냈는데 부족했다'는 뜻이다.
    # 그 유사도들의 최댓값 위로 VECTOR_DIRECT 를 올리면 LLM 요약으로 넘어간다.
    direct_bad = [r["similarity"] for r in rows
                  if r["route"] == "direct" and r["similarity"] is not None]
    if direct_bad:
        print(f"\n  direct 경로 👎 의 유사도 범위: "
              f"{min(direct_bad):.4f} ~ {max(direct_bad):.4f}")
        print(f"  현재 VECTOR_DIRECT={config.VECTOR_DIRECT}. "
              f"{max(direct_bad):.2f} 위로 올리면 이 질문들이 LLM 요약으로 넘어갑니다.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="*")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("-i", "--interactive", action="store_true")
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--clear-cache", action="store_true")
    ap.add_argument("--unanswered", action="store_true",
                    help="답변 못 한 질문을 많이 물어본 순으로 출력")
    ap.add_argument("--clear-unanswered", action="store_true")
    ap.add_argument("--feedback", action="store_true",
                    help="👍/👎 집계 (임계값 튜닝 근거)")
    args = ap.parse_args()

    store = Store()
    # 벤치는 같은 질문을 반복해서 돌리므로 집계를 오염시키지 않게 꺼둔다.
    engine = Engine(store, use_llm=not args.no_llm, log_misses=not args.bench)

    if args.clear_cache:
        print(f"캐시 {engine.bank.clear()}건 삭제")
        return 0

    if args.clear_unanswered:
        print(f"미답변 기록 {store.clear_unanswered()}건 삭제")
        return 0

    if args.unanswered:
        print_unanswered(store)
        return 0

    if args.feedback:
        print_feedback(store)
        return 0

    if args.bench:
        stats = {}
        for q in BENCH:
            r = engine.ask(q, use_cache=not args.no_cache)
            stats[r.route] = stats.get(r.route, 0) + 1
            print(render(q, r), "\n" + "-" * 60)
        print("경로 분포:", {ROUTE_DESC.get(k, k): v for k, v in stats.items()})
        print("캐시:", engine.bank.stats())
        return 0

    if args.interactive:
        print("질문을 입력하세요. 종료는 Ctrl+D\n")
        try:
            while True:
                q = input("> ").strip()
                if q:
                    print(render(q, engine.ask(q, use_cache=not args.no_cache)), "\n")
        except (EOFError, KeyboardInterrupt):
            return 0

    if not args.query:
        ap.error("질문을 입력하거나 -i / --bench 를 쓰세요")
    q = " ".join(args.query)
    print(render(q, engine.ask(q, use_cache=not args.no_cache)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
