#!/usr/bin/env python3
"""Notion 토큰 없이 파이프라인 로직만 검증한다.

실제로 만든 샘플 데이터와 같은 값을 주입해서
평탄화 -> 저장 -> 별칭 정확매칭 -> 참조 전파 -> 게시취소 처리를 확인한다.
임베딩은 부르지 않는다.
"""
from __future__ import annotations

import os
import tempfile

# 별도 테스트 러너를 쓰지 않는다. 두 파일 모두 그냥 실행하면 되는 스크립트이므로
# 저장소 루트를 직접 sys.path 에 올린다. (python tests/test_sync.py)
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from gobuk.sync import flatten
from gobuk.sync.flatten import Resolver, chunk_body
from gobuk.store import Store

JOB, ITEM, RECIPE, GUIDE = "job", "item", "recipe", "guide"

P = {  # page_id 축약
    "요리사": "job-cook", "농부": "job-farm",
    "밀": "it-wheat", "토마토": "it-tomato", "치즈": "it-cheese",
    "소금": "it-salt", "생크림": "it-cream", "마늘": "it-garlic",
    "토마토스파게티": "it-tspa", "크림스파게티": "it-cspa",
    "r_토스파": "rc-tspa", "r_크스파": "rc-cspa", "r_치즈": "rc-cheese",
    "g_요리사": "gd-cook",
}


def page(pid: str) -> dict:
    return {"id": pid, "url": f"https://notion.so/{pid}", "last_edited_time": "2026-07-25T00:00:00Z"}


def make_raw(tomato_name: str = "토마토") -> dict:
    return {
        P["요리사"]: {"db_key": JOB, "page": page(P["요리사"]), "body": "", "props": {
            "이름": "요리사", "계열": "생활", "별칭": "쿡, 쉐프, 요리, 요섹남",
            "전직조건": "레벨 10 달성 후 거북마을 식당 NPC '한식이'에게 밀 20개 전달",
            "최대레벨": 100, "한줄설명": "재료를 조합해 버프 음식을 만드는 생활직업입니다.",
            "상태": "게시", "패치버전": "v1.2"}},
        P["농부"]: {"db_key": JOB, "page": page(P["농부"]), "body": "", "props": {
            "이름": "농부", "계열": "생활", "별칭": "파머, 농사꾼",
            "전직조건": "레벨 5 달성 후 거북마을 밭 NPC '흙손'에게 대화",
            "최대레벨": 100, "한줄설명": "작물을 심고 수확하는 생활직업입니다.",
            "상태": "게시", "패치버전": "v1.2"}},

        P["밀"]: {"db_key": ITEM, "page": page(P["밀"]), "body": "", "props": {
            "이름": "밀", "별칭": "밀씩, wheat", "분류": "재료", "판매가": 5,
            "획득처": "농부 레벨 1 이상 밭 경작", "관련직업": [P["농부"]],
            "한줄설명": "가장 기본이 되는 공용 요리 재료입니다.", "상태": "게시", "패치버전": "v1.2"}},
        P["토마토"]: {"db_key": ITEM, "page": page(P["토마토"]), "body": "", "props": {
            "이름": tomato_name, "별칭": "토마토, tomato", "분류": "재료", "판매가": 12,
            "획득처": "농부 레벨 3 이상 밭 경작", "관련직업": [P["농부"]],
            "한줄설명": "붉은 계열 요리의 핵심 재료입니다.", "상태": "게시", "패치버전": "v1.2"}},
        P["치즈"]: {"db_key": ITEM, "page": page(P["치즈"]), "body": "", "props": {
            "이름": "치즈", "별칭": "치즈, cheese", "분류": "재료", "판매가": 40,
            "획득처": "요리사 레벨 6 이상 조리대에서 가공", "관련직업": [P["요리사"]],
            "한줄설명": "대부분의 중급 요리에 들어가는 가공 재료입니다.", "상태": "게시",
            "패치버전": "v1.2"}},
        P["소금"]: {"db_key": ITEM, "page": page(P["소금"]), "body": "", "props": {
            "이름": "소금", "별칭": "소금, salt", "분류": "재료", "판매가": 3,
            "획득처": "호수마을 잡화상에서 구매", "한줄설명": "기본 조미료입니다.",
            "상태": "게시", "패치버전": "v1.2"}},
        P["생크림"]: {"db_key": ITEM, "page": page(P["생크림"]), "body": "", "props": {
            "이름": "생크림", "별칭": "크림, cream", "분류": "재료", "판매가": 45,
            "획득처": "요리사 레벨 8 이상 조리대에서 우유 3개로 가공",
            "관련직업": [P["요리사"]], "한줄설명": "크림 계열 요리의 핵심 재료입니다.",
            "상태": "게시", "패치버전": "v1.2"}},
        P["토마토스파게티"]: {"db_key": ITEM, "page": page(P["토마토스파게티"]), "body": "", "props": {
            "이름": "토마토 스파게티", "별칭": "토스파, 토마토파스타, 끝장면", "분류": "요리",
            "판매가": 120, "획득처": "요리사 레벨 12 이상 조리대에서 제작",
            "관련직업": [P["요리사"]], "한줄설명": "공격력 버프를 주는 중급 요리입니다.",
            "상태": "게시", "패치버전": "v1.2"}},
        P["크림스파게티"]: {"db_key": ITEM, "page": page(P["크림스파게티"]), "body": "", "props": {
            "이름": "크림 스파게티", "별칭": "크스파, 크림파스타, 백스파", "분류": "요리",
            "판매가": 150, "획득처": "요리사 레벨 14 이상 조리대에서 제작",
            "관련직업": [P["요리사"]], "한줄설명": "방어력 버프를 주는 중급 요리입니다.",
            "상태": "게시", "패치버전": "v1.2"}},

        P["r_토스파"]: {"db_key": RECIPE, "page": page(P["r_토스파"]), "body": "", "props": {
            "이름": "토마토 스파게티", "별칭": "토스파, 토마토파스타, 끝장면",
            "직업": [P["요리사"]], "요구레벨": 12, "제작장소": "조리대",
            "재료1": [P["밀"]], "수량1": 3, "재료2": [P["토마토"]], "수량2": 4,
            "재료3": [P["치즈"]], "수량3": 1, "재료4": [P["소금"]], "수량4": 1,
            "결과아이템": [P["토마토스파게티"]], "결과수량": 2, "획득경험치": 250,
            "효과": "공격력 +8%, 15분 지속", "상태": "게시", "패치버전": "v1.2"}},
        P["r_크스파"]: {"db_key": RECIPE, "page": page(P["r_크스파"]), "body": "", "props": {
            "이름": "크림 스파게티", "별칭": "크스파, 크림파스타, 백스파",
            "직업": [P["요리사"]], "요구레벨": 14, "제작장소": "조리대",
            "재료1": [P["밀"]], "수량1": 3, "재료2": [P["생크림"]], "수량2": 2,
            "재료3": [P["치즈"]], "수량3": 1, "재료4": [P["소금"]], "수량4": 1,
            "결과아이템": [P["크림스파게티"]], "결과수량": 2, "획득경험치": 320,
            "효과": "방어력 +10%, 15분 지속", "상태": "게시", "패치버전": "v1.2"}},
        P["r_치즈"]: {"db_key": RECIPE, "page": page(P["r_치즈"]), "body": "", "props": {
            "이름": "치즈", "별칭": "치즈 만들기", "직업": [P["요리사"]], "요구레벨": 6,
            "제작장소": "조리대", "재료1": [P["소금"]], "수량1": 1,
            "결과아이템": [P["치즈"]], "결과수량": 1, "획득경험치": 40,
            "효과": "중간 가공 재료", "상태": "게시", "패치버전": "v1.2"}},

        P["g_요리사"]: {"db_key": GUIDE, "page": page(P["g_요리사"]),
                        "body": "## 전직 조건\n\n레벨 10을 달성해야 합니다.\n\n"
                                "## 초반 레벨업 루트\n\n1. 레벨 5까지는 마늘빵을 반복 제작합니다.\n"
                                "2. 레벨 6부터 치즈 가공이 열립니다.",
                        "props": {
                            "제목": "요리사 시작하는 법", "카테고리": "생활",
                            "요약": "레벨 10을 찍고 거북마을 식당의 '한식이'에게 밀 20개를 "
                                    "가져가면 전직됩니다.",
                            "태그": ["요리", "전직"], "관련직업": [P["요리사"]],
                            "상태": "게시", "패치버전": "v1.2"}},
    }


def load(store: Store, raw: dict) -> None:
    resolver = Resolver(store.title_map())
    for pid, item in raw.items():
        spec_title = {"job": "이름", "item": "이름", "recipe": "이름", "guide": "제목"}[item["db_key"]]
        resolver.add(pid, item["props"].get(spec_title, ""), item["db_key"],
                     item["page"]["url"])
    for pid, item in raw.items():
        rec = flatten.flatten(item["page"], item["db_key"], item["props"], resolver,
                              item["body"])
        store.upsert(rec, raw_props=item["props"])
        if rec.is_published:
            chunks = (chunk_body(rec) if item["db_key"] == GUIDE
                      else [{"heading": None, "text": rec.search_text}])
            store.replace_chunks(pid, item["db_key"], chunks)
        else:
            store._drop_chunks(pid)
    store.commit()


def check(label: str, cond: bool, detail: str = "") -> bool:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    return cond


def main() -> int:
    tmp = tempfile.mkdtemp()
    store = Store(os.path.join(tmp, "test.sqlite3"))
    load(store, make_raw())

    from gobuk.engine.answer import Engine
    eng = Engine(store, use_llm=False)

    def ask(q):
        return eng.ask(q, use_cache=False)

    ok = True

    print("\n1. 재료 슬롯 평탄화")
    row = store.conn.execute(
        "SELECT answer_text FROM records WHERE page_id=?", (P["r_토스파"],)).fetchone()
    print("     " + row["answer_text"].replace("\n", "\n     "))
    ok &= check("재료가 사람이 읽는 문장으로 펴짐",
                "밀 3개, 토마토 4개, 치즈 1개, 소금 1개" in row["answer_text"])

    print("\n2. 별칭 정확매칭")
    for q, expect in [("토스파 레시피 알려줘", "토마토 스파게티"),
                      ("크스파는 재료가 뭐야?", "크림 스파게티"),
                      ("끝장면 어떻게 만들어", "토마토 스파게티")]:
        res = ask(q)
        hit = res.sources[0]["title"] if res.sources else None
        ok &= check(f"{q!r} -> {expect}", hit == expect, f"실제 {hit}")

    print("\n3. 유사 질문 구분 (캐시 오답 시나리오)")
    a = ask("요리사 토마토스파게티 레시피")
    b = ask("요리사 크림스파게티 레시피")
    ok &= check("두 질문이 서로 다른 문서로 감",
                a.sources[0]["title"] != b.sources[0]["title"],
                f"{a.sources[0]['title']} vs {b.sources[0]['title']}")

    print("\n4. 이름 충돌 감지 ('치즈' = 아이템이자 레시피)")
    res = ask("치즈")
    ok &= check("우선순위대로 레시피를 먼저 집음",
                res.sources[0]["db"] == "recipe")
    ok &= check("레시피와 아이템을 함께 답변함", res.ambiguous,
                f"근거 {len(res.sources)}건")

    print("\n5. 참조 전파 (아이템 이름 변경 -> 레시피 문구 갱신)")
    load(store, make_raw(tomato_name="방울토마토"))
    changed = {P["토마토"]}
    resolver = Resolver(store.title_map())
    deps = store.dependents_of(changed)
    ok &= check("토마토를 쓰는 레시피가 의존 대상으로 잡힘", P["r_토스파"] in deps,
                f"{len(deps)}건")
    stale = store.load_raw(deps)
    for pid, item in stale.items():
        rec = flatten.flatten(item["page"], item["db_key"], item["props"], resolver,
                              item["body"])
        store.upsert(rec, raw_props=item["props"])
    store.commit()
    row = store.conn.execute(
        "SELECT answer_text FROM records WHERE page_id=?", (P["r_토스파"],)).fetchone()
    ok &= check("레시피 재료명이 새 이름으로 갱신됨", "방울토마토 4개" in row["answer_text"])

    print("\n6. 게시취소 처리")
    raw = make_raw()
    raw[P["r_크스파"]]["props"]["상태"] = "검수중"
    load(store, raw)
    pub = store.conn.execute(
        "SELECT published FROM records WHERE page_id=?", (P["r_크스파"],)).fetchone()
    ok &= check("published 플래그가 0으로 내려감", pub["published"] == 0)
    alias_hits = store.lookup_alias("크스파")
    ok &= check("해당 레시피가 별칭 색인에서 빠짐",
                P["r_크스파"] not in {h["page_id"] for h in alias_hits})
    ok &= check("같은 이름의 게시중 아이템은 색인에 남음",
                P["크림스파게티"] in {h["page_id"] for h in alias_hits},
                f"{len(alias_hits)}건 잔존")
    chunk_n = store.conn.execute(
        "SELECT COUNT(*) c FROM chunks WHERE page_id=?", (P["r_크스파"],)).fetchone()["c"]
    ok &= check("검색 청크에서도 제거됨", chunk_n == 0)

    print("\n7. 가이드 본문 청킹")
    rec_row = store.conn.execute(
        "SELECT * FROM chunks WHERE page_id=? ORDER BY ord", (P["g_요리사"],)).fetchall()
    heads = [r["heading"] for r in rec_row]
    ok &= check("요약이 0번 청크", heads[:1] == ["요약"], str(heads))
    ok &= check("H2 단위로 분리됨", "초반 레벨업 루트" in heads, str(heads))

    print("\n8. 캐시 무효화 신호")
    pend = store.pending_changes()
    kinds = {}
    for r in pend:
        kinds[r["change"]] = kinds.get(r["change"], 0) + 1
    ok &= check("change_log에 변경 이력이 쌓임", kinds.get("unpublished", 0) >= 1, str(kinds))

    print("\n" + ("전체 통과" if ok else "실패 항목 있음"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
