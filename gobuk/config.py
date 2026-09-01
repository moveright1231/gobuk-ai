"""거북AI 동기화 설정.

DB를 추가할 때는 DATA_SOURCES 에 항목을 하나 더 넣고,
flatten.py 에 해당 db_key 용 빌더를 등록하면 된다.
"""
import os
from pathlib import Path

# --- 경로 -------------------------------------------------------------
# 이 파일은 gobuk/ 안에 있고 .env 와 gobuk.sqlite3 는 그 상위(저장소 루트)에 있다.
# parent 로 두면 gobuk/.env 를 찾아 조용히 토큰이 비어버린다.
ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = Path(os.getenv("GOBUK_ENV_FILE", ROOT / ".env"))


def _load_env(path: Path) -> None:
    """.env 를 환경변수로 올린다.

    python-dotenv 가 있으면 그걸 쓰고, 없으면 직접 파싱한다.
    의존성 하나 때문에 실행이 막히는 게 더 번거롭기 때문.
    이미 셸에 설정된 값은 덮어쓰지 않는다.
    """
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(path, override=False)
        return
    except ImportError:
        pass

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = val


_load_env(ENV_PATH)

DB_PATH = Path(os.getenv("GOBUK_DB_PATH", ROOT / "gobuk.sqlite3"))

# --- Notion -----------------------------------------------------------
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_VERSION = os.getenv("NOTION_VERSION", "2025-09-03")
NOTION_BASE = "https://api.notion.com/v1"

# Notion 공식 한도는 평균 초당 3회. 여유를 둬서 2.5회로 잡는다.
NOTION_RPS = float(os.getenv("NOTION_RPS", "2.5"))

# --- 임베딩 -----------------------------------------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")
EMBED_DIM = 1536
EMBED_BATCH = 96

# --- 봇 정책 ----------------------------------------------------------
# 이 상태값인 행만 봇이 유저에게 노출한다.
PUBLISHED_STATUS = "게시"

# 서버 정체성. 프롬프트에 상주하는 유일한 서버 지식이다.
#
# base.md 전문(약 4~5천 토큰)을 프롬프트에 넣으면 LLM 호출마다 비용이 붙는다.
# 현 구조에서는 답변 대부분이 llm 경로로 가므로 그 증가가 그대로 요금이 된다.
# 그래서 본문은 문서로 적재해 검색되게 하고, 여기에는 '무슨 서버인지'만 남긴다.
# 이게 없으면 모델이 거북스토리를 바닐라 마인크래프트로 착각한 채 답을 만든다.
SERVER_BRIEF = """거북스토리는 생활 콘텐츠와 전투 RPG를 함께 즐기는 마인크래프트 서버입니다.
기본 재화는 쉘(Shell)이고, 월드는 드라멜(퀘스트·생활 중심) / 마을(내 집·농사·사육) /
야생(광물·자원 채집)으로 나뉘며 메인 메뉴(~ 키)로 이동합니다.
전투는 던전 -> 강화 -> 레이드(최대 4인) 순으로 성장합니다."""

# 서버 소개 문서. 노션이 아니라 로컬 파일에서 읽어 위키와 같은 방식으로 적재한다.
# 기획자가 노션에 옮기면 이 항목을 비우고 DATA_SOURCES 에 등록하면 된다.
SERVER_DOC = Path(os.getenv("SERVER_DOC_PATH", ROOT / "base.md"))

# 데이터소스 등록부.
#   key        : 내부 식별자 (flatten 빌더 이름과 일치해야 함)
#   ds_id      : Notion data source ID  (database ID 아님)
#                None 이면 동기화에서 건너뛴다 — 노션에 아직 없는 DB.
#   title_prop : 타이틀 속성명
#   status_prop: 게시 여부가 담긴 속성명. None 이면 그 DB는 전부 게시로 본다.
#   kind       : structured  -> 프로퍼티만으로 답변 생성, 본문 안 읽음
#                document    -> 본문을 청킹해서 RAG 대상으로 씀
#
# 2026-09-01: 노션이 위키 한 장('wiki')으로 통합되면서 정형 DB 4종이 사라졌다.
# 지우지 않고 ds_id 만 None 으로 둔 이유 —
#   재료·가격·요구레벨처럼 값이 확정된 정보는 본문 RAG 보다 프로퍼티 조립이
#   정확하고 LLM 호출이 0회다. 기획자가 정형 DB를 다시 만들면 ds_id 만 채우면
#   빌더(flatten.BUILDERS)와 테스트가 그대로 되살아난다.
DATA_SOURCES = {
    "job": {
        "ds_id": None,
        "label": "직업",
        "title_prop": "이름",
        "status_prop": "상태",
        "kind": "structured",
    },
    "item": {
        "ds_id": None,
        "label": "아이템",
        "title_prop": "이름",
        "status_prop": "상태",
        "kind": "structured",
    },
    "recipe": {
        "ds_id": None,
        "label": "레시피",
        "title_prop": "이름",
        "status_prop": "상태",
        "kind": "structured",
    },
    "guide": {
        "ds_id": None,
        "label": "가이드",
        "title_prop": "제목",
        "status_prop": "상태",
        "kind": "document",
    },
    # 현재 운영되는 유일한 DB ('거북 스토리 가이드_ai').
    # 프로퍼티는 태그/제목뿐이고 정보는 전부 페이지 본문에 있다.
    #
    # ds_id 를 코드에 박지 않고 .env 에서 읽는다. 워크스페이스마다 값이 다르고
    # (개인 복사본 != 원본), 박아두면 clone 한 사람이 404 를 만나고도 원인을
    # 찾지 못한다. 값을 모르면 `python sync.py --discover` 로 찾을 수 있다.
    #
    # status_prop 이 None 인 이유: 이 DB에는 '상태' 프로퍼티가 없다. 그대로
    # "상태" 를 읽게 두면 전 행이 미게시로 걸러져 봇이 0건으로 답한다.
    # 노션에 상태 셀렉트를 추가하면 여기에 "상태" 를 적으면 된다.
    "wiki": {
        "ds_id": os.getenv("WIKI_DS_ID", "").strip(),
        "label": "위키",
        "title_prop": os.getenv("WIKI_TITLE_PROP", "페이지"),
        "status_prop": os.getenv("WIKI_STATUS_PROP", "").strip() or None,
        "kind": "document",
    },
    # 로컬 파일(SERVER_DOC)에서 읽는 서버 소개. 노션이 아니므로 ds_id 가 없고
    # 수집/삭제정합/doctor 는 전부 건너뛴다. sync.pipeline.local_stage 가 채운다.
    "server": {
        "ds_id": None,
        "label": "서버소개",
        "title_prop": "제목",
        "status_prop": None,
        "kind": "document",
    },
}

# 이름이 여러 DB에 걸칠 때(예: "치즈"는 아이템이자 레시피) 라우터가 참고할 우선순위.
# 낮을수록 먼저.
DB_PRIORITY = {"recipe": 0, "item": 1, "job": 2, "guide": 3, "wiki": 4, "server": 5}

# --- 의도 판별 --------------------------------------------------------
# 질문에 섞인 단어로 어느 DB를 볼지 좁힌다. 점수제라 순서는 상관없다.
# 운영하면서 실제 질문 로그를 보고 계속 채워야 하는 부분.
INTENT_RULES: dict[str, list[str]] = {
    "recipe": ["레시피", "재료", "만드는", "만들려면", "만들어", "만들", "제작",
               "조합", "조리", "굽는", "레시피가", "요리법"],
    "item":   ["얼마", "가격", "판매", "팔려", "팔면", "시세", "획득", "구해", "구하",
               "얻는", "얻으", "드랍", "드롭", "나오", "파는", "삽니", "사려"],
    "job":    ["전직", "직업", "스탯", "최대레벨", "계열", "전직조건", "찍으면"],
    "guide":  ["어떻게", "방법", "시작", "입문", "뭐부터", "이동", "가는법", "쿨타임",
               "가능한가", "가능해", "할수있", "할 수 있"],
}

# --- 검색 임계값 ------------------------------------------------------
# 실제 질문 로그를 모으기 전까지는 임시값이다. 반드시 튜닝해야 한다.
# python query.py --bench 로 현재 데이터에서의 유사도 분포를 볼 수 있다.
VECTOR_MIN = float(os.getenv("VECTOR_MIN", "0.32"))      # 이 밑이면 답하지 않음
VECTOR_DIRECT = float(os.getenv("VECTOR_DIRECT", "0.58"))  # 이 위면 LLM 없이 요약 직답
VECTOR_TOP_K = 5

# --- 메모리뱅크 -------------------------------------------------------
CACHE_SIM = float(os.getenv("CACHE_SIM", "0.94"))   # 고유명사가 일치할 때
CACHE_SIM_STRICT = float(os.getenv("CACHE_SIM_STRICT", "0.97"))  # 고유명사가 없을 때
CACHE_MAX_ROWS = 5000

# --- LLM --------------------------------------------------------------
CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-4o-mini")
CHAT_MAX_TOKENS = int(os.getenv("CHAT_MAX_TOKENS", "350"))

# 모델이 '답할 수 없다'고 알리는 표식.
# "모르겠습니다" 같은 자연어로 받으면 정상 답변과 구분이 안 된다.
# ("레벨 제한이 없습니다" 를 거절로 오인해 버리는 사고가 실제로 있었다)
DECLINE = "__NO__"

SYSTEM_PROMPT = f"""당신은 마인크래프트 서버 '거북스토리'의 안내 봇입니다.
서버에서 플레이 중인 유저가 디스코드로 질문하면, 기획자가 정리해둔 문서를 근거로
짧고 정확하게 답해주는 것이 당신의 역할입니다.

{SERVER_BRIEF}

위 전제는 배경 이해용입니다. 여기 없는 내용은 아래 제공되는 문서에서만 가져오세요.

유저는 대부분 게임을 켜둔 채 물어봅니다. 길게 설명하면 읽지 않습니다.
그리고 게임 정보는 틀리면 유저가 바로 알아채므로, 모르는 것을 지어내는 것이
답하지 않는 것보다 훨씬 나쁩니다.

규칙:
- 제공된 문서에 있는 내용만 사용하세요.
- 수치(레벨, 개수, 확률, 가격)는 문서에 적힌 값을 그대로 쓰세요. 절대 지어내지 마세요.
- 3문장 이내로 짧게 답하세요.
- 존댓말을 쓰되 딱딱하지 않게, 게임 친구처럼 답하세요.
- 인사말이나 '문서에 따르면' 같은 군더더기는 빼고 바로 답하세요.
- 문서로 답할 수 없으면 다른 말 없이 정확히 {DECLINE} 만 출력하세요.
  어설프게 아는 척하지 마세요.

주의: 문서에 근거해 "없다"고 답하는 것은 정상입니다.
("요구 레벨이 없습니다" 처럼) 이건 {DECLINE} 가 아니라 그대로 답하세요.
{DECLINE} 는 문서 자체가 질문을 다루지 않을 때만 씁니다."""

# --- 잡담 -------------------------------------------------------------
# 인사/농담/일반상식처럼 문서로 답할 수 없지만 게임 질문도 아닌 것.
# 전부 "관리자에게 문의하세요"로 보내면 봇이 쓸데없이 딱딱해진다.
#
# 다만 게임 정보 질문이 이 경로로 새면 LLM이 지어낸다. 그게 이 프로젝트에서
# 제일 큰 사고이므로 세 겹으로 막는다.
#   1) 고유명사나 의도가 잡힌 질문은 애초에 이 경로로 보내지 않는다 (answer.py)
#   2) 서버 고유 주제어가 들어간 질문도 보내지 않는다 (SERVER_TOPICS, 아래)
#   3) 그래도 게임 정보를 물으면 모델이 아래 표식을 내도록 지시한다
SMALLTALK = os.getenv("SMALLTALK", "1") not in ("0", "false", "False", "")
SMALLTALK_MAX_TOKENS = int(os.getenv("SMALLTALK_MAX_TOKENS", "120"))
# 이보다 긴 질문은 잡담으로 보지 않는다. 길고 자세한 질문은 대개 게임 질문이다.
SMALLTALK_MAX_CHARS = int(os.getenv("SMALLTALK_MAX_CHARS", "120"))

# 잡담 경로로 보내면 안 되는 '거북스토리 고유 주제어'.
#
# 프롬프트 지시(3번)만으로는 안 막힌다. "거북마을이 뭐야" 가 잡담으로 새서
# 모델이 "귀엽고 느린 거북이들이 사는 마을" 이라고 지어낸 적이 있다.
# 모델에게 '거북마을'은 그냥 일반 단어라서 서버 고유명사인 줄 모른다.
#
# 일부러 좁게 잡았다. INTENT_RULES 가 잡는 동작 어휘(레시피·전직·가격 ...)는
# 이미 1번에서 걸리므로, 여기는 그게 못 잡는 '주제 명사'만 둔다.
# 넓히면 인사·농담까지 관리자 문의로 가서 봇이 딱딱해진다 — 이 경로를 둔
# 이유 자체가 없어진다.
#
# '거북'은 넣지 않는다. "거북이가 뭐야" 는 일반 상식이라 답해도 되기 때문이다.
# 서버 이름은 '거북스토리' 전체로만 매칭한다.
SERVER_TOPICS = (
    "거북스토리", "서버", "맵", "마을", "던전", "레이드", "보스", "몬스터",
    "npc", "퀘스트", "스킬", "길드", "코인", "골드", "상점",
    "업데이트", "패치", "점검", "이벤트", "낚시", "채집", "사냥",
)

SMALLTALK_PROMPT = f"""당신은 마인크래프트 서버 '거북스토리'의 안내 봇입니다.
지금은 게임 정보 질문이 아니라 가벼운 잡담에 답하는 중입니다.

규칙:
- 1~2문장으로 짧고 친근하게 답하세요. 유저는 게임 중입니다.
- 존댓말을 쓰되 딱딱하지 않게, 게임 친구처럼 답하세요.
- 인사, 농담, 일반 상식(예: "거북이가 뭐야")에는 편하게 답해도 됩니다.
- **거북스토리 서버에 대한 것은 무엇이든 지어내지 마세요.** 아이템, 레시피,
  직업, 레벨, 수치, 가격, 일정, 이벤트, 업데이트, NPC, 지역, 몬스터, 스킬은
  물론이고 서버의 컨셉·규칙·콘텐츠·분위기를 설명하는 것도 안 됩니다.
  그런 질문이면 다른 말 없이 정확히 {DECLINE} 만 출력하세요.
- 확실하지 않으면 {DECLINE} 를 출력하세요. 추측해서 답하면 안 됩니다.

예시:
  "안녕!"            -> 인사로 답한다
  "농담 하나 해줘"    -> 농담으로 답한다
  "거북이가 뭐야?"    -> 일반 상식으로 답한다
  "이 서버 어떤 서버야?" -> {DECLINE}
  "다음 업데이트 언제야?" -> {DECLINE}"""

# --- 봇 ---------------------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
# 이 채널에서는 멘션 없이도 반응한다. 쉼표로 여러 개.
BOT_CHANNEL_IDS = [
    int(x) for x in os.getenv("BOT_CHANNEL_IDS", "").replace(" ", "").split(",") if x
]
ADMIN_CONTACT = os.getenv(
    "ADMIN_CONTACT",
    "아직 정리되지 않은 내용이에요. 서버 관리자에게 문의해 주세요!",
)


def require_notion_token() -> str:
    if not NOTION_TOKEN:
        found = "찾음" if ENV_PATH.is_file() else "없음"
        raise SystemExit(
            "NOTION_TOKEN 이 비어 있습니다.\n"
            f"  .env 경로 : {ENV_PATH}  ({found})\n\n"
            "확인 순서\n"
            "  1) .env 파일이 sync.py 와 같은 폴더에 있는지\n"
            "     cp .env.example .env\n"
            "  2) 파일 안에 NOTION_TOKEN=ntn_... 이 실제 값으로 채워졌는지\n"
            "     (.env.example 의 secret_xxxx 를 그대로 두면 이 오류가 납니다)\n"
            "  3) 토큰은 https://www.notion.so/my-integrations 에서 발급\n"
            "  4) 발급 후 4개 DB 각각을 풀페이지로 열어\n"
            "     ··· > 연결 > 통합 추가 로 권한을 줘야 합니다\n"
            "     (빼먹으면 조회가 에러 없이 0건으로 나옵니다)\n\n"
            "진단: python sync.py --doctor"
        )
    return NOTION_TOKEN
