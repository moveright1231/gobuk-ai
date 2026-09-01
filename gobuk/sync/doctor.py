"""설치 진단 — --doctor / --discover.

이 프로젝트에서 제일 흔한 실패는 에러가 아니라 침묵이다. 통합 연결을 빼먹으면
조회가 0건으로 나오고, 프로퍼티 이름이 틀리면 제목이 전부 '(제목 없음)' 이 된다.
둘 다 명령어는 성공하고 건수도 그럴듯하게 나온다. 그래서 진단을 따로 둔다.
"""
from __future__ import annotations

import requests

from gobuk import config
from gobuk.notion.client import NotionClient, rich_text_to_plain


def discover() -> int:
    """통합에 연결된 데이터소스를 찾아 .env 에 넣을 값을 출력한다.

    새 워크스페이스에 설치할 때 첫 관문이다. ds_id 는 노션 UI 어디에도 안 보이고
    URL 에 있는 건 데이터베이스 ID 라서 그대로 쓰면 404 가 난다. search API 로
    통합이 실제로 볼 수 있는 것만 뽑아주면, 연결 누락과 ID 오기입을 한 번에 가른다.
    """
    client = NotionClient(config.require_notion_token())

    def title_of(o: dict) -> str:
        t = o.get("title")
        if isinstance(t, list):
            s = "".join(x.get("plain_text", "") for x in t)
            if s:
                return s
        for v in (o.get("properties") or {}).values():
            if isinstance(v, dict) and v.get("type") == "title":
                s = "".join(x.get("plain_text", "") for x in v.get("title", []))
                if s:
                    return s
        return o.get("name") or "(제목 없음)"

    print("통합이 접근할 수 있는 노션 객체")
    try:
        res = client._request("POST", "/search", json={"page_size": 100})
    except Exception as exc:
        print(f"  조회 실패: {str(exc)[:200]}")
        return 1

    rows = res.get("results", [])
    if not rows:
        print("\n  0건입니다. 통합이 어떤 페이지에도 연결되지 않았습니다.")
        print("  노션에서 가이드 DB를 풀페이지로 열고 ··· > 연결 > 통합 추가.")
        print("  (상위 페이지에만 걸면 상속되지 않는 경우가 있으니 DB 자체에도 해주세요)")
        return 1

    found: list[tuple[str, str]] = []
    for o in rows:
        kind, oid = o.get("object"), o.get("id")
        print(f"\n  [{kind}] {title_of(o)}\n      id: {oid}")
        if kind == "data_source":
            found.append((title_of(o), oid))
        elif kind == "database":
            for d in o.get("data_sources") or []:
                found.append((d.get("name") or title_of(o), d.get("id")))

    print("\n" + "-" * 58)
    if not found:
        print("데이터소스를 찾지 못했습니다. DB 자체를 풀페이지로 열고 연결을 추가해주세요.")
        return 1
    print(".env 에 붙여넣을 값:\n")
    seen: set[str] = set()
    for name, dsid in found:
        if dsid in seen:
            continue
        seen.add(dsid)
        print(f"  # {name}")
        print(f"  WIKI_DS_ID={dsid}")
    if len(seen) > 1:
        print("\n  여러 개가 나왔습니다. 가이드 DB 하나만 골라 넣어주세요.")
    return 0


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
        if not ds:
            print(f"  {spec['label']:<6} —    ds_id 미등록 (노션에 없는 DB, 건너뜀)")
            continue
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

        # 스키마 대조. 워크스페이스마다 프로퍼티 이름이 다를 수 있고, 틀리면
        # 에러 없이 제목이 전부 '(제목 없음)' 이 되어 별칭 색인이 죽는다.
        # 수집도 성공하고 건수도 맞게 나오므로 눈치채기 어렵다.
        schema = (meta.json().get("properties") or {})
        tprop = spec["title_prop"]
        if tprop not in schema:
            actual = [k for k, v in schema.items() if v.get("type") == "title"]
            hint = f"실제 제목 프로퍼티: {actual[0]!r}" if actual else "title 타입 없음"
            print(f"  {spec['label']:<6} 스키마 불일치 — 제목 프로퍼티 {tprop!r} 없음. {hint}")
            print(f"           .env 에 WIKI_TITLE_PROP={actual[0] if actual else '<이름>'} 를 넣어주세요")
            problems += 1
            continue
        sprop = spec.get("status_prop")
        if sprop and sprop not in schema:
            print(f"  {spec['label']:<6} 스키마 불일치 — 상태 프로퍼티 {sprop!r} 없음.")
            print("           비워두면(WIKI_STATUS_PROP=) 전부 게시로 취급합니다")
            problems += 1
            continue

        q = requests.post(f"{config.NOTION_BASE}/data_sources/{ds}/query",
                          headers=headers, json={"page_size": 100}, timeout=30)
        rows = q.json().get("results", []) if q.ok else []
        sprop = spec.get("status_prop")
        if sprop:
            pub = sum(
                1 for row in rows
                if (row.get("properties", {}).get(sprop, {}).get("select") or {}).get("name")
                == config.PUBLISHED_STATUS
            )
            # 상태 프로퍼티를 쓰기로 한 DB인데 게시가 0건이면, 수집은 되고
            # 봇만 0건으로 답한다. 404 보다 찾기 어려운 실패라 따로 알린다.
            pub_note = "" if pub else "  <- 게시 0건. 상태값 확인 필요"
        else:
            pub = len(rows)
            pub_note = "  (상태 프로퍼티 없음 -> 전부 게시로 취급)"
        flag = ""
        if not rows:
            flag = "  <- 0건. 통합 연결 확인 필요"
            problems += 1
        print(f"  {spec['label']:<6} OK  {len(rows):>3}건 (게시 {pub}건){flag}{pub_note}")

    # 처음 설치한 사람이 제일 먼저 만나는 상태다. 위 루프는 DB별로 '건너뜀'만
    # 찍고 조용히 끝나므로, 아무것도 등록되지 않았다는 사실을 따로 말해준다.
    if not any(s["ds_id"] for s in config.DATA_SOURCES.values()):
        print("\n  등록된 데이터소스가 하나도 없습니다.")
        print("  .env 의 WIKI_DS_ID 를 채워주세요. 값을 모르면:")
        print("      python sync.py --discover")
        problems += 1

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

