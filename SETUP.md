# 설치 안내 (담당자용)

거북AI 봇을 새 환경에 올리는 순서다. 위에서부터 그대로 따라가면 된다.
막히면 대부분 **3번(노션 통합 연결)** 이다.

---

## 0. 준비물

| 항목 | 발급처 | 비용 |
|---|---|---|
| 노션 내부 통합 토큰 | https://www.notion.so/my-integrations | 무료 |
| OpenAI API 키 | https://platform.openai.com/api-keys | **크레딧 충전 필요** |
| 디스코드 봇 토큰 | https://discord.com/developers/applications | 무료 |

OpenAI는 **ChatGPT Plus 구독과 API 크레딧이 별개**다. Plus를 결제해도 API는
따로 충전해야 한다. 충전이 안 돼 있으면 임베딩 단계에서 429가 나며 멈춘다.

파이썬 3.11 이상이 필요하다.

---

## 1. 클론 & 가상환경

```bash
git clone <저장소 URL> gobuk-ai
cd gobuk-ai

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

가상환경을 쓰는 이유: macOS에 Homebrew 파이썬이 깔려 있으면 전역 `pip install`이
PEP 668로 막힌다. 아래 명령어는 문서 편의상 `python`으로 적었지만 실제로는
전부 `.venv/bin/python`으로 실행한다.

---

## 2. `.env` 작성

```bash
cp .env.example .env
```

`.env`를 열고 채운다. **`WIKI_DS_ID`는 아직 비워둔다** — 3번에서 알아낸다.

```
NOTION_TOKEN=ntn_...          # 1번에서 발급한 통합 토큰
OPENAI_API_KEY=sk-...
DISCORD_TOKEN=                # 5번에서 채움
WIKI_DS_ID=                   # 3번에서 채움
```

`.env`는 `.gitignore`에 있다. **절대 커밋하지 않는다.**

---

## 3. 노션 통합 연결 — 여기가 제일 많이 막힌다

### 3-1. 통합을 가이드 DB에 연결

1. 노션에서 **가이드 위키 DB를 풀페이지로** 연다
2. 우측 상단 `···` → **연결(Connections)** → **연결 추가**
3. 1번에서 만든 통합을 선택

> 상위 페이지에만 연결하면 하위 DB로 상속되지 않는 경우가 있다.
> **DB 자체를 풀페이지로 열어서** 연결해야 확실하다.

연결을 빼먹으면 **에러 없이 조회 결과가 0건**으로 나온다. 토큰도 유효하고
명령어도 성공하는데 봇만 "모르겠다"고 답하는, 원인 찾기 제일 어려운 상태다.

### 3-2. 데이터소스 ID 알아내기

```bash
.venv/bin/python sync.py --discover
```

`.env`에 붙여넣을 줄이 그대로 출력된다.

```
.env 에 붙여넣을 값:

  # 거북 스토리 가이드_ai
  WIKI_DS_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

이 값을 `.env`의 `WIKI_DS_ID`에 넣는다.

> **노션 URL에 보이는 ID를 그대로 쓰면 안 된다.** URL에 있는 건 데이터베이스
> ID인데, 2025-09-03 API부터 조회는 데이터소스 ID로 한다. 둘은 다른 값이고
> 잘못 넣으면 404가 난다. 그래서 `--discover`가 있다.

`--discover`가 **0건**으로 나오면 3-1을 안 한 것이다.

### 3-3. 스키마 확인

코드는 위키 DB에 다음을 가정한다.

| 프로퍼티 | 타입 | 용도 |
|---|---|---|
| `페이지` | title | 문서 제목. 별칭 색인에 오른다 |
| `태그` | multi_select | 분류 |

제목 프로퍼티 이름이 원본에서 다르면(`이름`, `Name` 등) `.env`에서 바꾼다.

```
WIKI_TITLE_PROP=이름
```

**중요**: 정보는 프로퍼티가 아니라 **페이지 본문**에 있어야 한다. 본문을
H2(`##`) 단위로 잘라 검색하므로, 절 제목을 달아두면 정확도가 올라간다.

---

## 4. 첫 동기화

```bash
.venv/bin/python sync.py --doctor    # 반드시 먼저. 위키가 OK 로 떠야 한다
.venv/bin/python sync.py --full      # 전체 수집 + 임베딩
.venv/bin/python sync.py --stats     # 건수 확인
```

`--doctor` 출력에서 `위키  OK  21건 (게시 21건)` 처럼 나오면 정상이다.
정형 DB 4종이 `ds_id 미등록 (건너뜀)`으로 나오는 건 **정상**이다 —
노션에 없는 DB이고 나중에 추가할 여지를 남겨둔 것이다.

로컬 DB(`gobuk.sqlite3`)는 저장소에 없다. 위 명령으로 직접 만든다.
표가 많은 페이지는 표마다 API 호출이 한 번 더 나가서 2~3분 걸릴 수 있다.

### 봇 없이 먼저 검증

```bash
.venv/bin/python query.py "던전 3은 몇레벨"
.venv/bin/python query.py -i          # 대화형
.venv/bin/python query.py --bench     # 표준 질문 일괄 실행
```

디스코드를 붙이기 전에 여기서 답이 나오는지 확인하는 게 빠르다.

---

## 5. 디스코드 봇

1. https://discord.com/developers/applications → **New Application**
2. **Bot** → Reset Token → `.env`의 `DISCORD_TOKEN`에 넣기
3. **Bot → Privileged Gateway Intents → MESSAGE CONTENT INTENT 켜기**
   (이걸 안 켜면 일반 메시지에 반응하지 못한다)
4. **OAuth2 → URL Generator**
   - 스코프: `bot`, `applications.commands`
   - 권한: `Send Messages`, `Embed Links`, `Read Message History`
5. 생성된 URL로 서버에 초대

```bash
.venv/bin/python bot.py
```

반응 방식 두 가지:

- `/거북 <질문>` — 어느 채널에서든
- 지정 채널에서 그냥 말하기 — `.env`의 `BOT_CHANNEL_IDS`에 채널 ID를
  쉼표로 넣는다 (개발자 모드를 켜고 채널 우클릭 → ID 복사)

**코드를 고치면 `gobuk/bot.py`를 재시작해야 반영된다.** 파이썬은 시작할 때
모듈을 읽는다.

---

## 6. 운영 (크론)

노션을 고쳐도 **동기화를 돌리기 전까지 봇은 모른다.** 봇은 질문을 받을 때
노션 API를 호출하지 않고 로컬 스냅샷만 읽는다.

```cron
*/15 * * * *  cd /path/to/gobuk-ai && .venv/bin/python sync.py            >> sync.log 2>&1
30   4 * * *  cd /path/to/gobuk-ai && .venv/bin/python sync.py --full      >> sync.log 2>&1
```

`--full`을 **하루 한 번은 반드시** 돌려야 한다. 노션에서 삭제된 페이지는
증분 동기화로 절대 잡히지 않는다(삭제된 페이지는 애초에 조회 결과에
안 나온다). 전체 ID 대조만이 잡아낸다.

---

## 7. 운영 중에 볼 것

```bash
.venv/bin/python query.py --unanswered   # 답변 못 한 질문 (많이 물어본 순)
```

`--unanswered`가 **기획자에게 넘길 문서 작성 우선순위**다. 같은 질문은 행이
늘지 않고 횟수만 오르므로, 위에 있는 것부터 문서를 쓰면 된다.

읽는 법:
- `고유명사`가 잡혔는데 답을 못 했다 → 문서는 있는데 내용이 비었다
- 안 잡혔다 → 문서가 아예 없다 (이쪽이 대부분)

`.env`의 `VECTOR_MIN` / `VECTOR_DIRECT` 는 임시값이다. `--unanswered` 가
쌓인 뒤에 조정한다.

---

## 문제 해결

| 증상 | 원인 | 조치 |
|---|---|---|
| `--doctor`에서 `등록된 데이터소스가 하나도 없습니다` | `WIKI_DS_ID` 미설정 | `--discover` 실행 |
| `--discover`가 0건 | 통합 연결 누락 | 3-1 |
| `--doctor`에서 404 | ID 오기입 (DB ID를 넣었을 가능성) | `--discover`로 다시 확인 |
| 조회는 되는데 봇이 다 "모르겠다" | 동기화 미실행 | `sync.py --full` |
| `--stats`는 건수가 있는데 봇이 0건 | 게시 판정 | `WIKI_STATUS_PROP` 확인 |
| 임베딩에서 429로 멈춤 | OpenAI 크레딧 소진 | 충전 (재시도해도 안 풀린다) |
| `NOTION_TOKEN 이 비어 있습니다` | `.env` 위치/값 | `.env`가 `sync.py`와 같은 폴더에 있는지 |
| 노션을 고쳤는데 봇이 옛 답 | 동기화 미실행 | `sync.py` |
| 코드를 고쳤는데 봇이 옛 동작 | 봇 재시작 안 함 | `gobuk/bot.py` 재시작 |

셸에 `NOTION_TOKEN`이 이미 `export`돼 있으면 `.env`가 그걸 덮어쓰지 않는다.
값이 안 먹는 것 같으면 `env | grep NOTION`으로 확인한다.

---

## 기획자에게 전달할 문서 작성 규칙

문서 품질이 봇 품질을 그대로 결정한다. 코드로 메울 수 없는 부분이다.

- **정보는 페이지 본문에 쓴다.** 지금 구조에서는 본문이 유일한 정보원이다
- **절 제목(`##`)을 달아준다.** 본문을 절 단위로 잘라 검색하므로 정확도가 오른다
- **1행 = 1주제.** "모든 직업 정리" 같은 페이지는 검색이 뭉개진다
- **스크린샷에 정보를 담지 않는다.** 이미지 안의 텍스트는 읽지 못한다
- **수치는 표나 문장으로 명시한다.** "레벨 좀 높아야 함" ❌ / "요구 레벨 15" ✅
- **패치로 값이 바뀌면 기존 행을 수정한다.** 새로 만들면 옛 내용이 함께 검색된다
- 유저가 쓰는 말을 제목이나 본문에 넣는다 (유저는 "토스파"라고 묻는다)

본문 표는 마크다운 표로 변환해서 읽으므로 표를 써도 된다.
