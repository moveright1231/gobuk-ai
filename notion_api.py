"""Notion REST 래퍼.

2025-09-03 버전부터 행 조회가 /v1/databases/{id}/query 에서
/v1/data_sources/{id}/query 로 옮겨갔다. 이 모듈은 새 엔드포인트만 쓴다.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Iterator

import requests

import config


class RateLimiter:
    """단순 토큰버킷. Notion 평균 3 rps 한도를 넘지 않게 잡아준다."""

    def __init__(self, rps: float):
        self._min_interval = 1.0 / rps
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            gap = time.monotonic() - self._last
            if gap < self._min_interval:
                time.sleep(self._min_interval - gap)
            self._last = time.monotonic()


class NotionClient:
    def __init__(self, token: str | None = None):
        self.token = token or config.require_notion_token()
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": config.NOTION_VERSION,
            "Content-Type": "application/json",
        })
        self.limiter = RateLimiter(config.NOTION_RPS)

    # -- 저수준 ---------------------------------------------------------
    def _request(self, method: str, path: str, **kw) -> dict[str, Any]:
        url = f"{config.NOTION_BASE}{path}"
        for attempt in range(6):
            self.limiter.wait()
            resp = self.session.request(method, url, timeout=30, **kw)

            if resp.status_code == 429:
                # Notion 이 알려주는 대기시간을 그대로 존중한다.
                delay = float(resp.headers.get("Retry-After", 2 ** attempt))
                time.sleep(delay)
                continue
            if resp.status_code >= 500:
                time.sleep(min(2 ** attempt, 30))
                continue
            if not resp.ok:
                raise RuntimeError(
                    f"Notion {method} {path} -> {resp.status_code}: {resp.text[:400]}"
                )
            return resp.json()
        raise RuntimeError(f"Notion {method} {path} 재시도 초과")

    def _paginate(self, method: str, path: str, body: dict | None = None) -> Iterator[dict]:
        cursor = None
        while True:
            payload = dict(body or {})
            if cursor:
                payload["start_cursor"] = cursor
            data = (
                self._request(method, path, json=payload)
                if method == "POST"
                else self._request(method, path, params=payload)
            )
            yield from data.get("results", [])
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")

    # -- 행 조회 --------------------------------------------------------
    def query_rows(self, ds_id: str, since: str | None = None) -> Iterator[dict]:
        """데이터소스의 행을 가져온다.

        since 를 주면 그 시각 이후 수정된 행만 받는다(증분).
        상태(게시/검수중)로는 서버에서 거르지 않는다. '게시 -> 검수중' 으로
        내려간 행을 로컬에서 지우려면 그 행도 받아봐야 하기 때문이다.
        """
        body: dict[str, Any] = {"page_size": 100}
        if since:
            body["filter"] = {
                "timestamp": "last_edited_time",
                "last_edited_time": {"after": since},
            }
        yield from self._paginate("POST", f"/data_sources/{ds_id}/query", body)

    def list_row_ids(self, ds_id: str) -> set[str]:
        """전체 행 ID만 훑는다. 원격에서 지워진 행을 찾아내는 용도."""
        return {row["id"] for row in self._paginate(
            "POST", f"/data_sources/{ds_id}/query", {"page_size": 100}
        )}

    # -- 본문 -----------------------------------------------------------
    def block_children(self, block_id: str) -> Iterator[dict]:
        yield from self._paginate("GET", f"/blocks/{block_id}/children", {"page_size": 100})

    def page_markdown(self, page_id: str, depth: int = 0, max_depth: int = 3) -> str:
        """페이지 본문을 마크다운 비슷한 평문으로 편다.

        토글 안에 핵심 정보를 숨기지 말라고 기획 가이드에 적어뒀지만,
        실수로 넣는 경우가 있어서 중첩 블록도 max_depth 까지는 따라 들어간다.
        """
        if depth > max_depth:
            return ""
        out: list[str] = []
        for block in self.block_children(page_id):
            btype = block.get("type", "")
            body = block.get(btype, {}) or {}
            text = rich_text_to_plain(body.get("rich_text", []))

            if btype == "heading_1":
                out.append(f"\n# {text}")
            elif btype == "heading_2":
                out.append(f"\n## {text}")
            elif btype == "heading_3":
                out.append(f"\n### {text}")
            elif btype in ("bulleted_list_item", "to_do"):
                out.append(f"- {text}")
            elif btype == "numbered_list_item":
                out.append(f"1. {text}")
            elif btype == "quote":
                out.append(f"> {text}")
            elif btype == "code":
                out.append(f"```\n{text}\n```")
            elif btype in ("paragraph", "callout", "toggle"):
                if text:
                    out.append(text)
            elif btype == "table":
                # 본문 표는 파싱 품질이 나쁘다. 있으면 경고만 남기고 건너뛴다.
                out.append("[표 블록 감지 — DB로 옮기는 걸 권장]")
            elif btype == "image":
                out.append("[이미지 — 텍스트 설명 없음]")

            if block.get("has_children") and btype != "table":
                nested = self.page_markdown(block["id"], depth + 1, max_depth)
                if nested:
                    out.append(nested)

        return "\n".join(p for p in out if p is not None).strip()


# -- 프로퍼티 추출 -------------------------------------------------------
def rich_text_to_plain(rt: list[dict]) -> str:
    return "".join(seg.get("plain_text", "") for seg in rt or []).strip()


def extract_prop(prop: dict) -> Any:
    """Notion 프로퍼티 하나를 파이썬 값으로 바꾼다."""
    if not prop:
        return None
    ptype = prop.get("type")

    if ptype == "title":
        return rich_text_to_plain(prop["title"])
    if ptype == "rich_text":
        return rich_text_to_plain(prop["rich_text"])
    if ptype == "number":
        return prop["number"]
    if ptype == "checkbox":
        return bool(prop["checkbox"])
    if ptype in ("select", "status"):
        val = prop[ptype]
        return val["name"] if val else None
    if ptype == "multi_select":
        return [o["name"] for o in prop["multi_select"]]
    if ptype == "relation":
        # 관계가 25개를 넘으면 has_more 로 잘린다. 재료 슬롯은 1개씩이라
        # 현 스키마에서는 걸릴 일이 없지만, 넘칠 경우 경고를 남긴다.
        ids = [r["id"] for r in prop["relation"]]
        if prop.get("has_more"):
            ids.append("__TRUNCATED__")
        return ids
    if ptype in ("last_edited_time", "created_time"):
        return prop[ptype]
    if ptype == "date":
        val = prop["date"]
        return val.get("start") if val else None
    if ptype in ("url", "email", "phone_number"):
        return prop[ptype]
    if ptype == "unique_id":
        val = prop["unique_id"]
        prefix = val.get("prefix") or ""
        return f"{prefix}{val.get('number')}"
    if ptype == "formula":
        val = prop["formula"]
        return val.get(val.get("type"))
    if ptype == "rollup":
        val = prop["rollup"]
        if val.get("type") == "array":
            return [extract_prop(x) for x in val["array"]]
        return val.get(val.get("type"))
    if ptype == "files":
        return [f.get("name") for f in prop["files"]]
    return None


def extract_all(page: dict) -> dict[str, Any]:
    return {name: extract_prop(prop) for name, prop in page.get("properties", {}).items()}
