"""질문 해석.

두 가지를 뽑아낸다.

  intent : 어느 DB를 봐야 하는가 ("치즈 얼마야" -> item, "치즈 레시피" -> recipe)
  entities: 질문에 등장한 고유명사 (별칭 사전 정확매칭)

entities 가 메모리뱅크 오답을 막는 핵심이다. "토마토스파게티 레시피"와
"크림스파게티 레시피"는 임베딩상 0.96 이상으로 붙지만, 고유명사가 다르므로
캐시가 섞이지 않는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import config
from flatten import normalize

# 조사/어미. 긴 것부터 떼어낸다.
PARTICLES = ("으로는", "에서는", "이라고", "으로", "에서", "이랑", "하고", "까지",
             "부터", "에게", "한테", "이나", "라도", "은", "는", "이", "가", "을",
             "를", "의", "에", "도", "만", "랑", "와", "과", "로")

# 고유명사 후보에서 제외할 일반 단어.
STOPWORDS = {
    "레시피", "재료", "만드는", "만들려면", "만들어", "어떻게", "어디서", "뭐야",
    "뭔가요", "알려줘", "알려주세요", "구해", "구하", "얻어", "되나요", "인가요",
    "방법", "좀", "제작", "조합", "가격", "얼마", "판매", "전직", "직업", "조건",
    "그리고", "근데", "혹시", "질문", "궁금", "이거", "저거", "그거", "지금",
}


@dataclass
class Parsed:
    query: str
    intent: str | None = None
    intent_scores: dict[str, int] = field(default_factory=dict)
    entities: list[dict] = field(default_factory=list)

    @property
    def entity_key(self) -> str:
        """캐시 검증용 키. 매칭된 페이지 ID를 정렬해서 이어붙인다.

        표기가 달라도('토스파' vs '토마토 스파게티') 같은 페이지면 같은 키가 되고,
        비슷하게 생긴 다른 대상이면 반드시 달라진다.
        """
        return "|".join(sorted({e["page_id"] for e in self.entities}))

    @property
    def entity_names(self) -> list[str]:
        seen, out = set(), []
        for e in self.entities:
            if e["title"] not in seen:
                seen.add(e["title"])
                out.append(e["title"])
        return out


def detect_intent(query: str) -> tuple[str | None, dict[str, int]]:
    """어느 DB를 봐야 하는지 점수로 판단한다.

    동점이거나 신호가 없으면 None. 이 경우 필터를 걸지 않고
    후보를 전부 살펴본 뒤 답할 수 있는 만큼 답한다.
    """
    text = query.replace(" ", "")
    scores: dict[str, int] = {}
    for db_key, words in config.INTENT_RULES.items():
        hit = sum(1 for w in words if w.replace(" ", "") in text)
        if hit:
            scores[db_key] = hit
    if not scores:
        return None, {}
    top = max(scores.values())
    winners = [k for k, v in scores.items() if v == top]
    return (winners[0] if len(winners) == 1 else None), scores


def candidate_terms(query: str) -> list[str]:
    """고유명사 후보를 뽑는다. 긴 조합부터 시도한다.

    '토마토 스파게티'처럼 띄어쓰기된 이름을 잡으려면 인접 단어를 붙여봐야 한다.
    """
    cleaned = re.sub(r"[?!.,~/]", " ", query)
    words = [w for w in cleaned.split() if w]

    stripped: list[str] = []
    for w in words:
        for p in sorted(PARTICLES, key=len, reverse=True):
            if len(w) > len(p) + 1 and w.endswith(p):
                w = w[: -len(p)]
                break
        stripped.append(w)

    terms: list[str] = []
    for size in (3, 2, 1):
        for i in range(len(stripped) - size + 1):
            span = stripped[i:i + size]
            if size == 1 and span[0] in STOPWORDS:
                continue
            terms.append("".join(span))
            if size > 1:
                terms.append(" ".join(span))

    seen, out = set(), []
    for t in terms:
        k = normalize(t)
        if k and k not in seen:
            seen.add(k)
            out.append(t)
    return out


def extract_entities(store, query: str, intent: str | None = None) -> list[dict]:
    """별칭 사전 정확매칭.

    같은 이름이 여러 DB에 있으면(예: '치즈') 전부 돌려준다.
    거르는 건 상위 라우터의 몫이다.
    """
    hits: list[dict] = []
    seen: set[str] = set()
    for term in candidate_terms(query):
        for row in store.lookup_alias(term):
            if row["page_id"] in seen:
                continue
            seen.add(row["page_id"])
            hits.append({
                "page_id": row["page_id"],
                "db_key": row["rdb"],
                "title": row["title"],
                "matched": row["alias_raw"],
                "primary": bool(row["is_primary"]),
                "term_len": len(term),
            })

    def rank(h: dict) -> tuple:
        return (
            0 if (intent and h["db_key"] == intent) else 1,  # 의도에 맞는 DB 먼저
            config.DB_PRIORITY.get(h["db_key"], 9),
            -h["term_len"],       # 긴 이름이 더 구체적이다
            0 if h["primary"] else 1,
        )

    hits.sort(key=rank)
    return hits


def parse(store, query: str) -> Parsed:
    intent, scores = detect_intent(query)
    entities = extract_entities(store, query, intent)
    return Parsed(query=query, intent=intent, intent_scores=scores, entities=entities)
