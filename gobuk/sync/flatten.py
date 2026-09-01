"""원시 Notion 행 -> 봇이 바로 쓸 수 있는 레코드.

여기서 만드는 값 중 핵심은 두 개다.

  answer_text : 정형 질문에 LLM 없이 그대로 내보낼 완성된 답변
  search_text : 임베딩/FTS 대상이 되는 검색용 문장

레시피의 '재료1~5 + 수량1~5' 같은 슬롯 구조는 사람이 읽을 수 없으므로
이 단계에서 "밀 3개, 토마토 4개" 형태로 편다.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from gobuk import config


@dataclass
class Record:
    page_id: str
    db_key: str
    title: str
    url: str
    status: str | None
    patch_version: str | None
    last_edited: str
    aliases: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    answer_text: str = ""
    search_text: str = ""
    body: str = ""
    # 이 레코드가 값을 빌려온 다른 페이지들. 그 페이지가 바뀌면 여기도 다시 만들어야 한다.
    depends_on: list[str] = field(default_factory=list)

    @property
    def is_published(self) -> bool:
        return self.status == config.PUBLISHED_STATUS

    def content_hash(self) -> str:
        blob = json.dumps(
            {
                "t": self.title,
                "a": self.aliases,
                "s": self.status,
                "f": self.facts,
                "ans": self.answer_text,
                "b": self.body,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


class Resolver:
    """페이지 ID -> 제목. 관계 프로퍼티를 사람이 읽는 이름으로 바꾼다."""

    def __init__(self, table: dict[str, dict[str, str]] | None = None):
        self.table = table or {}

    def add(self, page_id: str, title: str, db_key: str, url: str = "") -> None:
        self.table[page_id] = {"title": title, "db_key": db_key, "url": url}

    def name(self, page_id: str) -> str:
        return self.table.get(page_id, {}).get("title") or "(알 수 없음)"

    def first(self, ids: Any) -> str | None:
        if not ids or not isinstance(ids, list):
            return None
        real = [i for i in ids if i != "__TRUNCATED__"]
        return real[0] if real else None


def split_aliases(raw: str | None) -> list[str]:
    """별칭 필드를 쪼갠다. 쉼표든 슬래시든 줄바꿈이든 다 받아준다."""
    if not raw:
        return []
    parts = re.split(r"[,/\n·|]+", raw)
    seen, out = set(), []
    for p in parts:
        p = p.strip()
        key = normalize(p)
        if p and key and key not in seen:
            seen.add(key)
            out.append(p)
    return out


def normalize(text: str | None) -> str:
    """별칭 정확매칭용 정규화. 공백/문장부호 제거 후 소문자."""
    if not text:
        return ""
    return re.sub(r"[\s\-_·.]+", "", text).lower()


def _lvl(v: Any) -> str:
    return f"레벨 {int(v)}" if isinstance(v, (int, float)) else "레벨 제한 없음"


def _num(v: Any) -> str:
    return str(int(v)) if isinstance(v, (int, float)) and float(v).is_integer() else str(v)


# --------------------------------------------------------------------------
# DB별 빌더
# --------------------------------------------------------------------------
def build_job(p: dict, rec: Record, rs: Resolver) -> None:
    rec.aliases = split_aliases(p.get("별칭"))
    rec.facts = {
        "계열": p.get("계열"),
        "전직조건": p.get("전직조건"),
        "최대레벨": p.get("최대레벨"),
        "설명": p.get("한줄설명"),
    }
    bits = [f"**{rec.title}**"]
    if p.get("계열"):
        bits.append(f"({p['계열']}직업)")
    head = " ".join(bits)
    lines = [head]
    if p.get("한줄설명"):
        lines.append(p["한줄설명"])
    if p.get("전직조건"):
        lines.append(f"전직 조건: {p['전직조건']}")
    if p.get("최대레벨"):
        lines.append(f"최대 레벨: {_num(p['최대레벨'])}")
    rec.answer_text = "\n".join(lines)
    rec.search_text = " ".join(filter(None, [
        rec.title, " ".join(rec.aliases), p.get("계열") or "",
        p.get("한줄설명") or "", p.get("전직조건") or "",
        "전직 방법 어떻게 되나요 조건",
    ]))


def build_item(p: dict, rec: Record, rs: Resolver) -> None:
    rec.aliases = split_aliases(p.get("별칭"))
    jobs = [rs.name(i) for i in (p.get("관련직업") or []) if i != "__TRUNCATED__"]
    rec.depends_on += [i for i in (p.get("관련직업") or []) if i != "__TRUNCATED__"]
    rec.facts = {
        "분류": p.get("분류"),
        "획득처": p.get("획득처"),
        "판매가": p.get("판매가"),
        "관련직업": jobs,
        "설명": p.get("한줄설명"),
    }
    lines = [f"**{rec.title}**" + (f" ({p['분류']})" if p.get("분류") else "")]
    if p.get("한줄설명"):
        lines.append(p["한줄설명"])
    if p.get("획득처"):
        lines.append(f"획득: {p['획득처']}")
    if p.get("판매가") is not None:
        lines.append(f"판매가: {_num(p['판매가'])}골드")
    rec.answer_text = "\n".join(lines)
    rec.search_text = " ".join(filter(None, [
        rec.title, " ".join(rec.aliases), p.get("분류") or "",
        p.get("획득처") or "", p.get("한줄설명") or "", " ".join(jobs),
        "어디서 구해 얻는 방법 획득처 가격",
    ]))


def build_recipe(p: dict, rec: Record, rs: Resolver) -> None:
    rec.aliases = split_aliases(p.get("별칭"))

    materials: list[dict] = []
    for slot in range(1, 6):
        mid = rs.first(p.get(f"재료{slot}"))
        if not mid:
            continue
        qty = p.get(f"수량{slot}")
        materials.append({
            "page_id": mid,
            "name": rs.name(mid),
            "qty": int(qty) if isinstance(qty, (int, float)) else None,
        })
        rec.depends_on.append(mid)

    job_id = rs.first(p.get("직업"))
    job = rs.name(job_id) if job_id else None
    if job_id:
        rec.depends_on.append(job_id)

    out_id = rs.first(p.get("결과아이템"))
    out_name = rs.name(out_id) if out_id else rec.title
    if out_id:
        rec.depends_on.append(out_id)

    rec.facts = {
        "직업": job,
        "요구레벨": p.get("요구레벨"),
        "제작장소": p.get("제작장소"),
        "재료": materials,
        "결과": {"이름": out_name, "수량": p.get("결과수량")},
        "획득경험치": p.get("획득경험치"),
        "효과": p.get("효과"),
    }

    mat_str = ", ".join(
        f"{m['name']} {m['qty']}개" if m["qty"] else m["name"] for m in materials
    ) or "재료 정보 없음"

    cond = " ".join(filter(None, [
        job, _lvl(p.get("요구레벨")) if p.get("요구레벨") else None,
    ]))
    place = f"{p['제작장소']}에서 제작" if p.get("제작장소") else "제작"

    lines = [f"**{rec.title}** — {cond} / {place}".replace("  ", " ")]
    lines.append(f"재료: {mat_str}")
    if p.get("결과수량"):
        lines.append(f"결과: {out_name} {_num(p['결과수량'])}개")
    if p.get("효과"):
        lines.append(f"효과: {p['효과']}")
    if p.get("획득경험치"):
        lines.append(f"제작 경험치: {_num(p['획득경험치'])}")
    rec.answer_text = "\n".join(lines)

    rec.search_text = " ".join(filter(None, [
        rec.title, " ".join(rec.aliases), job or "", p.get("제작장소") or "",
        mat_str, p.get("효과") or "",
        "레시피 재료 만드는 법 제작 조합법",
    ]))


def lead_paragraph(body: str, max_chars: int = 300) -> str:
    """본문 도입부를 답변용 한 덩어리로 뽑는다.

    위키 DB는 조립할 프로퍼티가 없어서 answer_text 를 만들 재료가 제목뿐이다.
    제목만 넣으면 exact 경로가 "보스 가이드" 라는 답을 내보내게 되므로,
    첫 실제 문단을 대신 쓴다.

    머리글(#) 줄과 표(|)는 걷어낸다. 표를 도입부로 끌어오면 디스코드에
    파이프 범벅인 한 줄이 나간다. 표의 내용은 chunk_body 쪽에서 살린다.

    길이 제한은 문장 경계에서 끊는다. 글자 수로 자르면 "메테오 강" 처럼
    낱말 중간에서 끊긴 답이 디스코드로 그대로 나간다.
    """
    out: list[str] = []
    for line in (body or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith(("|", "[표 블록")):
            if out:
                break          # 문단이 끝났다. 다음 절까지 끌고 오지 않는다.
            continue           # 아직 도입부 전이다. 계속 넘긴다.
        out.append(s)
        if sum(len(x) + 1 for x in out) >= max_chars:
            break
    return _cut_at_sentence(" ".join(out), max_chars)


def _cut_at_sentence(text: str, max_chars: int) -> str:
    """max_chars 안쪽의 마지막 문장 끝에서 자른다.

    문장 끝을 못 찾으면(한 문장이 통째로 긴 경우) 낱말 경계로 물러서고,
    그마저 없으면 그냥 자른 뒤 말줄임표를 붙여 잘렸음을 알린다.
    """
    text = text.strip()
    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    end = max(window.rfind(m) for m in (".", "!", "?"))
    if end >= max_chars // 3:
        return window[:end + 1].strip()
    space = window.rfind(" ")
    if space >= max_chars // 3:
        return window[:space].strip() + "…"
    return window.strip() + "…"


def build_wiki(p: dict, rec: Record, rs: Resolver) -> None:
    """단일 위키 DB. 정보가 프로퍼티에 없고 전부 페이지 본문에 있다.

    그래서 이 DB에서는 exact 경로의 이점이 작다 — 실제 답변은 chunk_body 가
    H2 단위로 자른 본문 청크에서 나온다. answer_text 는 본문 도입부로
    채워서, 제목이 정확매칭됐을 때 최소한 말이 되는 답이 나가게만 한다.
    """
    tags = p.get("태그") or []
    rec.facts = {"태그": tags}
    rec.answer_text = lead_paragraph(rec.body) or rec.title
    rec.search_text = " ".join(filter(None, [rec.title, " ".join(tags)]))


def build_server(p: dict, rec: Record, rs: Resolver) -> None:
    """서버 소개 문서(base.md). 노션이 아니라 로컬 파일에서 온다.

    프로퍼티가 없으므로 위키와 같은 방식으로 본문에만 의존한다. 실제 답변은
    chunk_body 가 절 단위로 자른 청크에서 나온다.
    """
    rec.facts = {"출처": "로컬 서버 소개 문서"}
    rec.answer_text = lead_paragraph(rec.body) or rec.title
    rec.search_text = rec.title


def build_guide(p: dict, rec: Record, rs: Resolver) -> None:
    tags = p.get("태그") or []
    jobs = [rs.name(i) for i in (p.get("관련직업") or []) if i != "__TRUNCATED__"]
    rec.depends_on += [i for i in (p.get("관련직업") or []) if i != "__TRUNCATED__"]
    summary = p.get("요약") or ""
    rec.facts = {"카테고리": p.get("카테고리"), "태그": tags, "관련직업": jobs, "요약": summary}
    rec.answer_text = summary or rec.title
    rec.search_text = " ".join(filter(None, [
        rec.title, summary, p.get("카테고리") or "", " ".join(tags), " ".join(jobs),
    ]))


BUILDERS: dict[str, Callable[[dict, Record, Resolver], None]] = {
    "job": build_job,
    "item": build_item,
    "recipe": build_recipe,
    "guide": build_guide,
    "wiki": build_wiki,
    "server": build_server,
}


def props_published(spec: dict, props: dict) -> bool:
    """이 행을 유저에게 노출할 것인가.

    status_prop 이 None 인 DB는 상태 개념이 없으므로 전부 게시로 본다.
    없는 프로퍼티를 읽으면 None 이 나와서 전 행이 조용히 미게시로 걸러진다 —
    수집은 되는데 봇만 0건으로 답하는, 원인 찾기 제일 어려운 실패다.

    본문을 긁을지 판단하는 sync.collect_stage 와 여기서 같은 규칙을 써야 한다.
    """
    prop = spec.get("status_prop")
    if not prop:
        return True
    return props.get(prop) == config.PUBLISHED_STATUS


def flatten(page: dict, db_key: str, props: dict, resolver: Resolver, body: str = "") -> Record:
    spec = config.DATA_SOURCES[db_key]
    rec = Record(
        page_id=page["id"],
        db_key=db_key,
        title=props.get(spec["title_prop"]) or "(제목 없음)",
        url=page.get("url", ""),
        # 상태 개념이 없는 DB는 PUBLISHED_STATUS 를 그대로 박아둔다.
        # 이러면 Record.is_published 부터 answer.py 까지 손댈 곳이 없다.
        status=(props.get(spec["status_prop"]) if spec.get("status_prop")
                else config.PUBLISHED_STATUS),
        patch_version=props.get("패치버전"),
        last_edited=page.get("last_edited_time", ""),
        body=body,
    )
    BUILDERS[db_key](props, rec, resolver)
    rec.depends_on = sorted(set(rec.depends_on) - {rec.page_id})
    if body:
        rec.search_text = f"{rec.search_text}\n{body[:2000]}"
    return rec


# --------------------------------------------------------------------------
# 본문 청킹
# --------------------------------------------------------------------------
def chunk_body(rec: Record, max_chars: int = 700) -> list[dict]:
    """가이드 본문을 H2/H3 단위로 자른다.

    요약 필드는 항상 0번 청크로 넣는다. 봇이 이걸 최우선으로 인용하게 하려는 것.
    """
    chunks: list[dict] = []
    summary = (rec.facts or {}).get("요약")
    if summary:
        chunks.append({"heading": "요약", "text": f"{rec.title}\n{summary}"})

    if not rec.body:
        return chunks

    current_head = rec.title
    buf: list[str] = []

    def flush() -> None:
        text = "\n".join(buf).strip()
        if text:
            chunks.append({"heading": current_head, "text": f"[{rec.title} / {current_head}]\n{text}"})
        buf.clear()

    for line in rec.body.splitlines():
        if line.startswith(("# ", "## ", "### ")):
            flush()
            current_head = line.lstrip("# ").strip()
            continue
        buf.append(line)
        # 표 중간에서 끊으면 뒤쪽 조각이 헤더 없는 파이프 나열이 되어
        # 임베딩도 답변도 망가진다. 표는 끝까지 한 청크에 담는다.
        # ("채집" 문서의 80행짜리 표가 700자 단위로 쪼개져 있었다)
        if sum(len(x) for x in buf) >= max_chars and not line.lstrip().startswith("|"):
            flush()
    flush()
    return chunks
