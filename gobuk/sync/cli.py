"""동기화 CLI. 저장소 루트의 sync.py 가 이걸 부른다."""
from __future__ import annotations

import argparse
import sys

from gobuk import config
from gobuk.engine.embed import Embedder, embed_pending
from gobuk.notion.client import NotionClient
from gobuk.store import MemoryBank, Store
from gobuk.sync.doctor import doctor, discover
from gobuk.sync.flatten import Resolver
from gobuk.sync.pipeline import (
    fetch_stage, build_resolver, flatten_stage, ripple_stage,
    reconcile_stage, reflatten_all, local_stage, utcnow,
)


def print_stats(store: Store) -> None:
    st = store.stats()
    print("\n적재 상태")
    for db_key, spec in config.DATA_SOURCES.items():
        s = st.get(db_key, {"total": 0, "published": 0})
        held = s["total"] - s["published"]
        note = f"  (비공개 {held}건 보류)" if held else ""
        print(f"  {spec['label']:<6} 총 {s['total']:>4}건 / 게시 {s['published']:>4}건{note}")
    print(f"  청크    {st['_chunks']}개 (임베딩 완료 {st['_embedded']}개)")

    from gobuk.store import MemoryBank
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



def main() -> int:
    ap = argparse.ArgumentParser(description="거북AI Notion 동기화")
    ap.add_argument("--full", action="store_true", help="전체 재수집 + 삭제 정합")
    ap.add_argument("--reflatten", action="store_true", help="API 없이 로컬 재생성")
    ap.add_argument("--no-embed", action="store_true", help="임베딩 생략")
    ap.add_argument("--stats", action="store_true", help="상태만 출력")
    ap.add_argument("--doctor", action="store_true", help="설정/권한 점검")
    ap.add_argument("--discover", action="store_true",
                    help="통합이 볼 수 있는 데이터소스 ID 찾기 (설치 시 1회)")
    args = ap.parse_args()

    if args.discover:
        return discover()

    if args.doctor:
        return doctor()

    store = Store()

    if args.stats:
        print_stats(store)
        return 0

    if args.reflatten:
        print("로컬 재생성 중 (Notion API 호출 없음)")
        reflatten_all(store)
        local = local_stage(store)
        if local not in ("unchanged", "없음"):
            print(f"  서버소개 {config.SERVER_DOC.name}: {local}")
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

    # 노션이 아니라 로컬 파일에서 오는 서버 소개. API 호출이 없어 매번 돌려도 싸다.
    local = local_stage(store)
    if local not in ("unchanged", "없음"):
        print(f"  서버소개 {config.SERVER_DOC.name}: {local}")

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
        from gobuk.engine.embed import EmbeddingError
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
    from gobuk.store import MemoryBank
    MemoryBank(store).purge_stale()

    print_stats(store)
    if embed_failed:
        print("\nNotion 수집은 정상 완료됐습니다. 임베딩만 실패했으니")
        print("키 문제를 해결한 뒤 python sync.py 를 다시 돌리면 남은 청크만 채웁니다.")
        return 2
    return 0

