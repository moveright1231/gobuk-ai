#!/usr/bin/env python3
"""거북AI 동기화 엔트리포인트.

  python sync.py                # 증분. 크론으로 10~30분마다 돌리면 된다.
  python sync.py --full         # 전체 대조. 삭제된 행까지 정리한다. 하루 1회 권장.
  python sync.py --reflatten    # API 안 부르고 로컬 원본만으로 답변 문구 재생성.
  python sync.py --no-embed     # 임베딩 건너뛰기. 구조만 확인할 때.
  python sync.py --stats        # 현재 적재 상태만 출력.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

import config
import flatten
from embed import Embedder, embed_pending
from flatten import Resolver, chunk_body
from notion_api import NotionClient, extract_all
from store import Store


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
        since = None if full else store.get_cursor(db_key)
        mode = "전체" if since is None else f"증분(>{since})"
        rows = list(client.query_rows(spec["ds_id"], since))
        print(f"  {spec['label']:<6} {mode:<28} {len(rows)}건")

        for page in rows:
            props = extract_all(page)
            body = ""
            if spec["kind"] == "document" and props.get("상태") == config.PUBLISHED_STATUS:
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


def print_stats(store: Store) -> None:
    st = store.stats()
    print("\n적재 상태")
    for db_key, spec in config.DATA_SOURCES.items():
        s = st.get(db_key, {"total": 0, "published": 0})
        held = s["total"] - s["published"]
        note = f"  (비공개 {held}건 보류)" if held else ""
        print(f"  {spec['label']:<6} 총 {s['total']:>4}건 / 게시 {s['published']:>4}건{note}")
    print(f"  청크    {st['_chunks']}개 (임베딩 완료 {st['_embedded']}개)")

    from memory_bank import MemoryBank
    cs = MemoryBank(store).stats()
    if cs["entries"]:
        print(f"  캐시    {cs['entries']}건 저장 / 재사용 {cs['hits']}회 "
              f"(적중률 {cs['hit_rate']:.0%})")

    kinds, asked = store.unanswered_count()
    if kinds:
        print(f"  미답변  {kinds}종 / 누적 {asked}회  (python query.py --unanswered)")
        for r in store.unanswered_top(3):
            print(f"          {r['asked_count']:>3}회  {r['question'][:40]}")

    pending = store.pending_changes()
    if pending:
        kinds: dict[str, int] = {}
        for row in pending:
            kinds[row["change"]] = kinds.get(row["change"], 0) + 1
        print(f"  캐시 무효화 대기: {kinds}")


def doctor() -> int:
    """설정을 단계별로 점검한다.

    특히 '통합 연결 누락'은 에러 없이 0건으로 나와서 원인을 찾기 어렵다.
    데이터소스마다 따로 확인해준다.
    """
    import requests

    print("환경 점검\n")
    print(f"  .env 경로       : {config.ENV_PATH}")
    print(f"  .env 존재       : {'예' if config.ENV_PATH.is_file() else '아니오'}")

    token = config.NOTION_TOKEN
    if not token:
        print("  NOTION_TOKEN    : 비어 있음")
        print("\n  -> .env 에 NOTION_TOKEN 을 채워주세요.")
        return 1
    if token.startswith("secret_xxx") or "xxxx" in token:
        print("  NOTION_TOKEN    : 예시값 그대로임")
        print("\n  -> .env.example 의 자리표시자를 실제 토큰으로 바꿔주세요.")
        return 1
    print(f"  NOTION_TOKEN    : 있음 ({token[:7]}…{token[-4:]}, {len(token)}자)")
    print(f"  NOTION_VERSION  : {config.NOTION_VERSION}")
    print(f"  OPENAI_API_KEY  : {'있음' if config.OPENAI_API_KEY else '없음 (--no-embed 필요)'}")
    print(f"  로컬 DB         : {config.DB_PATH}")

    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": config.NOTION_VERSION,
        "Content-Type": "application/json",
    }

    print("\n토큰 유효성")
    r = requests.get(f"{config.NOTION_BASE}/users/me", headers=headers, timeout=20)
    if not r.ok:
        print(f"  실패 {r.status_code}: {r.text[:200]}")
        print("\n  -> 토큰이 잘못됐거나 폐기됐습니다. 재발급해주세요.")
        return 1
    me = r.json()
    print(f"  통합 이름: {me.get('name') or me.get('bot', {}).get('owner', {}).get('type', '?')}")

    print("\n데이터소스 접근")
    problems = 0
    for db_key, spec in config.DATA_SOURCES.items():
        ds = spec["ds_id"]
        meta = requests.get(f"{config.NOTION_BASE}/data_sources/{ds}",
                            headers=headers, timeout=20)
        if meta.status_code == 404:
            print(f"  {spec['label']:<6} 404 — 통합이 이 DB에 연결되지 않았거나 ID가 틀림")
            problems += 1
            continue
        if not meta.ok:
            print(f"  {spec['label']:<6} {meta.status_code}: {meta.text[:120]}")
            problems += 1
            continue

        q = requests.post(f"{config.NOTION_BASE}/data_sources/{ds}/query",
                          headers=headers, json={"page_size": 100}, timeout=30)
        rows = q.json().get("results", []) if q.ok else []
        pub = sum(
            1 for row in rows
            if (row.get("properties", {}).get("상태", {}).get("select") or {}).get("name")
            == config.PUBLISHED_STATUS
        )
        flag = ""
        if not rows:
            flag = "  <- 0건. 통합 연결 또는 상태값 확인 필요"
            problems += 1
        print(f"  {spec['label']:<6} OK  {len(rows):>3}건 (게시 {pub}건){flag}")

    if problems:
        print(f"\n문제 {problems}건. 해당 DB를 풀페이지로 열고 "
              "··· > 연결 > 통합 추가 를 확인해주세요.")
        return 1

    print("\n임베딩 API")
    if not config.OPENAI_API_KEY:
        print("  키 없음 — python sync.py --no-embed 로만 실행 가능")
        print("\n이상 없음(임베딩 제외). python sync.py --full --no-embed 로 진행하세요.")
        return 0

    key = config.OPENAI_API_KEY
    print(f"  키 형태: {key[:7]}…{key[-4:]} ({len(key)}자)")
    if not key.startswith("sk-"):
        print("  주의: OpenAI 키는 보통 sk- 로 시작합니다. 값을 다시 확인해주세요.")

    # 파싱을 거치지 않고 원본 응답을 그대로 본다.
    # 진단 도구가 파싱 때문에 죽으면 아무 쓸모가 없다.
    try:
        er = requests.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": config.EMBED_MODEL, "input": "연결 확인용 문장"},
            timeout=30,
        )
    except Exception as exc:
        print(f"  요청 자체 실패: {type(exc).__name__}: {exc}")
        print("\n  네트워크/프록시/SSL 문제일 수 있습니다.")
        return 1

    print(f"  HTTP {er.status_code}")
    if er.ok:
        try:
            dim = len(er.json()["data"][0]["embedding"])
            print(f"  OK  {config.EMBED_MODEL}, 차원 {dim}")
            if dim != config.EMBED_DIM:
                print(f"  주의: config.EMBED_DIM({config.EMBED_DIM})과 다릅니다. 값을 맞춰주세요.")
        except Exception:
            print(f"  응답 형태가 예상과 다릅니다:\n    {er.text[:400]}")
            return 1
    else:
        print(f"  content-type: {er.headers.get('content-type', '?')}")
        print("  원본 응답:")
        for line in (er.text or "(본문 없음)")[:800].splitlines()[:12]:
            print(f"    {line}")
        print("\n  잔액 확인: https://platform.openai.com/settings/organization/billing")
        print("  키 관리  : https://platform.openai.com/api-keys")
        return 1

    print("\n이상 없음. python sync.py --full 로 진행하세요.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="거북AI Notion 동기화")
    ap.add_argument("--full", action="store_true", help="전체 재수집 + 삭제 정합")
    ap.add_argument("--reflatten", action="store_true", help="API 없이 로컬 재생성")
    ap.add_argument("--no-embed", action="store_true", help="임베딩 생략")
    ap.add_argument("--stats", action="store_true", help="상태만 출력")
    ap.add_argument("--doctor", action="store_true", help="설정/권한 점검")
    args = ap.parse_args()

    if args.doctor:
        return doctor()

    store = Store()

    if args.stats:
        print_stats(store)
        return 0

    if args.reflatten:
        print("로컬 재생성 중 (Notion API 호출 없음)")
        reflatten_all(store)
        if not args.no_embed:
            embed_pending(store, Embedder())
        print_stats(store)
        return 0

    started = utcnow()
    client = NotionClient()

    print(f"[1/4] 수집  {'(전체)' if args.full else '(증분)'}")
    raw = fetch_stage(client, store, args.full)

    print("[2/4] 평탄화")
    resolver = build_resolver(store, raw)
    tally = flatten_stage(store, raw, resolver)
    print(f"  신규 {tally['created']} / 갱신 {tally['updated']} / "
          f"게시취소 {tally['unpublished']} / 변화없음 {tally['unchanged']}")

    changed = {pid for pid in raw if tally}  # 이번 배치에 들어온 전부를 후보로 본다
    rippled = ripple_stage(store, changed, resolver)
    if rippled:
        print(f"  참조 전파로 {rippled}건 재생성")

    if args.full:
        print("[3/4] 삭제 정합")
        reconcile_stage(client, store)
    else:
        print("[3/4] 삭제 정합 건너뜀 (--full 에서만 수행)")

    print("[4/4] 임베딩")
    embed_failed = None
    if args.no_embed:
        print("  건너뜀")
    else:
        from embed import EmbeddingError
        try:
            embed_pending(store, Embedder())
        except (EmbeddingError, SystemExit) as exc:
            # Notion 수집은 이미 끝났다. 여기서 죽으면 그 결과를 버리는 셈이 된다.
            # 임베딩 안 된 청크는 embedding IS NULL 로 남아 다음 실행에서 다시 시도된다.
            embed_failed = str(exc)
            print(f"\n  {embed_failed}\n")

    for db_key in config.DATA_SOURCES:
        store.set_cursor(db_key, started)

    # 바뀐 페이지를 근거로 만들어진 캐시를 지운다.
    # 이걸 빼먹으면 레시피가 패치돼도 봇이 예전 답을 계속 낸다.
    from memory_bank import MemoryBank
    MemoryBank(store).purge_stale()

    print_stats(store)
    if embed_failed:
        print("\nNotion 수집은 정상 완료됐습니다. 임베딩만 실패했으니")
        print("키 문제를 해결한 뒤 python sync.py 를 다시 돌리면 남은 청크만 채웁니다.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
