"""A단계 파이프라인 — Notion 에서 로컬 SQLite 로.

단계를 나눈 이유가 각각 있다.

  fetch      관계를 풀려면 참조 대상의 제목이 필요한데 증분에서는 그게 이번
             배치에 없을 수 있다. 그래서 수집을 먼저 다 끝낸다.
  flatten    answer_text(완성 답변)와 search_text(색인용)를 여기서 만든다.
  ripple     참조 전파. 아이템 이름이 바뀌면 그걸 쓰는 레시피도 낡는데,
             레시피 행은 수정된 적이 없어서 증분에 안 잡힌다.
  reconcile  삭제 정합. 지워진 페이지는 애초에 조회 결과에 없으므로
             전체 ID 대조만이 잡아낸다 — --full 에서만 돈다.
"""
from __future__ import annotations

import datetime as dt

from gobuk import config
from gobuk.notion.client import NotionClient, extract_all
from gobuk.store import Store
from gobuk.sync import flatten
from gobuk.sync.flatten import Resolver, chunk_body


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def fetch_stage(client: NotionClient, store: Store, full: bool) -> dict[str, dict]:
    """1단계: 원시 행을 긁어온다. 아직 평탄화하지 않는다.

    관계를 풀려면 참조 대상의 '제목'이 필요한데, 증분 동기화에서는 참조 대상이
    이번 배치에 안 들어올 수 있다. 그래서 수집을 먼저 다 끝내고 이름표를
    완성한 다음에 평탄화한다.
    """
    raw: dict[str, dict] = {}
    for db_key, spec in config.DATA_SOURCES.items():
        if not spec["ds_id"]:
            print(f"  {spec['label']:<6} 건너뜀 (노션에 없는 DB, ds_id 미등록)")
            continue
        since = None if full else store.get_cursor(db_key)
        mode = "전체" if since is None else f"증분(>{since})"
        rows = list(client.query_rows(spec["ds_id"], since))
        print(f"  {spec['label']:<6} {mode:<28} {len(rows)}건")

        for page in rows:
            props = extract_all(page)
            body = ""
            if spec["kind"] == "document" and flatten.props_published(spec, props):
                body = client.page_markdown(page["id"])
            raw[page["id"]] = {
                "db_key": db_key, "page": page, "props": props, "body": body,
            }
    return raw


def build_resolver(store: Store, raw: dict[str, dict]) -> Resolver:
    """로컬에 이미 있는 제목 + 이번에 새로 받은 제목을 합친다."""
    resolver = Resolver(store.title_map())
    for pid, item in raw.items():
        spec = config.DATA_SOURCES[item["db_key"]]
        title = item["props"].get(spec["title_prop"]) or "(제목 없음)"
        resolver.add(pid, title, item["db_key"], item["page"].get("url", ""))
    return resolver


def flatten_stage(store: Store, raw: dict[str, dict], resolver: Resolver) -> dict[str, int]:
    tally = {"created": 0, "updated": 0, "unpublished": 0, "unchanged": 0}
    for pid, item in raw.items():
        rec = flatten.flatten(
            item["page"], item["db_key"], item["props"], resolver, item["body"]
        )
        change = store.upsert(rec, raw_props=item["props"])
        tally[change] = tally.get(change, 0) + 1

        if change != "unchanged":
            spec = config.DATA_SOURCES[item["db_key"]]
            if not rec.is_published:
                store._drop_chunks(pid)
            elif spec["kind"] == "document":
                store.replace_chunks(pid, item["db_key"], chunk_body(rec))
            else:
                store.replace_chunks(pid, item["db_key"], [
                    {"heading": None, "text": rec.search_text}
                ])
    store.commit()
    return tally


def ripple_stage(store: Store, changed_ids: set[str], resolver: Resolver) -> int:
    """이름이 바뀐 페이지를 참조하던 레코드를 다시 만든다.

    '토마토'를 '방울토마토'로 고치면 그걸 재료로 쓰는 레시피의 답변 문구가
    낡는다. 레시피 행은 수정된 적이 없어서 증분 쿼리에 안 잡히므로
    로컬에 저장해둔 원본으로 다시 만든다. API 호출은 발생하지 않는다.
    """
    targets = store.dependents_of(changed_ids)
    if not targets:
        return 0
    stale = store.load_raw(targets)
    touched = 0
    for pid, item in stale.items():
        if not item["props"]:
            continue
        rec = flatten.flatten(
            item["page"], item["db_key"], item["props"], resolver, item["body"]
        )
        if store.upsert(rec, raw_props=item["props"]) != "unchanged":
            touched += 1
            spec = config.DATA_SOURCES[item["db_key"]]
            if rec.is_published:
                chunks = (chunk_body(rec) if spec["kind"] == "document"
                          else [{"heading": None, "text": rec.search_text}])
                store.replace_chunks(pid, item["db_key"], chunks)
    store.commit()
    return touched


def reconcile_stage(client: NotionClient, store: Store) -> int:
    """원격에서 지워진 행을 로컬에서도 정리한다."""
    total = 0
    for db_key, spec in config.DATA_SOURCES.items():
        if not spec["ds_id"]:
            # 원격을 물어볼 수 없으니 대조도 못 한다. 여기서 remote 를 빈
            # 집합으로 넘기면 그 DB의 로컬 행이 통째로 지워진다.
            continue
        remote = client.list_row_ids(spec["ds_id"])
        gone = store.delete_missing(db_key, remote)
        if gone:
            print(f"  {spec['label']}: 원격에서 사라진 {len(gone)}건 삭제")
        total += len(gone)
    return total


def reflatten_all(store: Store) -> None:
    """API를 전혀 부르지 않고 저장된 원본만으로 전부 다시 만든다.

    flatten.py 의 답변 문구를 손봤을 때 쓴다.
    """
    ids = store.all_page_ids()
    raw = store.load_raw(ids)
    resolver = Resolver(store.title_map())
    tally = flatten_stage(store, raw, resolver)
    print(f"  재생성 완료: {tally}")



def local_stage(store: Store) -> str:
    """로컬 서버 소개 문서(config.SERVER_DOC)를 적재한다.

    노션에 없는 정보라 위키만으로는 "이 서버 뭐하는 서버야" 류에 답할 수 없었다.
    파일을 그냥 두면 아무 코드도 읽지 않으므로 위키와 같은 경로로 넣어준다.

    내용이 그대로면 content_hash 가 같아 unchanged 로 끝나고 임베딩도 다시 하지
    않는다. API 호출은 전혀 없다.
    """
    path = config.SERVER_DOC
    if not path.is_file():
        return "없음"

    body = path.read_text(encoding="utf-8")
    title = next(
        (ln.lstrip("# ").strip() for ln in body.splitlines() if ln.startswith("# ")),
        path.stem,
    )
    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
    page = {
        "id": f"local:{path.stem}",
        "url": "",
        "last_edited_time": mtime.isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    props = {"제목": title}
    rec = flatten.flatten(page, "server", props, Resolver(store.title_map()), body)
    change = store.upsert(rec, raw_props=props)
    if change != "unchanged":
        store.replace_chunks(rec.page_id, "server", chunk_body(rec))
    store.commit()
    return change
