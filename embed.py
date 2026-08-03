"""임베딩 생성.

동기화 때마다 전부 다시 만들지 않는다. 내용 해시가 바뀐 페이지의 청크만
embedding=NULL 로 초기화되고, 여기서 그 청크들만 채운다.
"""
from __future__ import annotations

import time

import numpy as np
import requests

import config

# 429 로 오지만 기다린다고 풀리지 않는 것들. 재시도하면 안 된다.
FATAL_CODES = {
    "insufficient_quota",
    "invalid_api_key",
    "account_deactivated",
    "billing_hard_limit_reached",
    "model_not_found",
}

HINTS = {
    "insufficient_quota":
        "OpenAI 계정에 크레딧이 없습니다. 키는 유효하지만 잔액이 0입니다.\n"
        "  https://platform.openai.com/settings/organization/billing 에서 결제수단 등록\n"
        "  (ChatGPT Plus 구독은 API 크레딧과 별개입니다. 따로 충전해야 합니다.)",
    "invalid_api_key":
        "키가 잘못됐습니다. sk- 로 시작하는 값 전체를 붙여넣었는지 확인해주세요.",
    "model_not_found":
        f"'{config.EMBED_MODEL}' 모델에 접근할 수 없습니다.\n"
        "  프로젝트 키라면 해당 모델 권한이 켜져 있는지 확인해주세요.",
}


class EmbeddingError(RuntimeError):
    pass


class Embedder:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or config.OPENAI_API_KEY
        self.model = model or config.EMBED_MODEL
        if not self.api_key:
            raise SystemExit("OPENAI_API_KEY 가 없습니다. 임베딩을 건너뛰려면 --no-embed 를 쓰세요.")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })

    def encode(self, texts: list[str]) -> np.ndarray:
        out: list[np.ndarray] = []
        for i in range(0, len(texts), config.EMBED_BATCH):
            batch = [t[:8000] for t in texts[i:i + config.EMBED_BATCH]]
            out.append(self._call(batch))
        return np.vstack(out) if out else np.zeros((0, config.EMBED_DIM), dtype=np.float32)

    def _call(self, batch: list[str]) -> np.ndarray:
        last = "원인 미상"
        for attempt in range(5):
            try:
                resp = self.session.post(
                    "https://api.openai.com/v1/embeddings",
                    json={"model": self.model, "input": batch},
                    timeout=60,
                )
            except requests.RequestException as exc:
                last = f"네트워크 오류: {exc}"
                time.sleep(min(2 ** attempt, 20))
                continue

            if resp.ok:
                data = sorted(resp.json()["data"], key=lambda d: d["index"])
                return np.array([d["embedding"] for d in data], dtype=np.float32)

            code, message = _parse_error(resp)
            last = f"HTTP {resp.status_code} / {code or '?'} / {message}"

            # 기다려도 안 풀리는 오류는 즉시 중단한다.
            # 코드가 안 실려 오는 응답도 있어서 메시지 내용으로도 한 번 더 본다.
            lowered = message.lower()
            fatal_text = any(k in lowered for k in
                             ("quota", "billing", "insufficient", "api key", "deactivated"))
            if code in FATAL_CODES or resp.status_code in (401, 403, 404) or \
                    (resp.status_code == 429 and fatal_text):
                if code is None and fatal_text:
                    code = "insufficient_quota" if "quota" in lowered or "billing" in lowered \
                        else "invalid_api_key"
                raise EmbeddingError(_format(last, code))

            if resp.status_code == 429 or resp.status_code >= 500:
                wait = float(resp.headers.get("retry-after", min(2 ** attempt, 30)))
                time.sleep(wait)
                continue

            raise EmbeddingError(_format(last, code))

        raise EmbeddingError(_format(f"재시도 5회 초과. 마지막 응답: {last}", None))


def _parse_error(resp: requests.Response) -> tuple[str | None, str]:
    """오류 응답에서 코드와 메시지를 뽑는다.

    형태가 제각각이다. OpenAI 표준은 {"error": {"code":..., "message":...}} 이지만
    프록시나 게이트웨이를 거치면 {"error": "문자열"} 이나 HTML 로 오기도 한다.
    파싱 실패가 진짜 원인을 가리면 안 되므로 무슨 형태든 받아낸다.
    """
    raw = (resp.text or "").strip()
    try:
        payload = resp.json()
    except ValueError:
        return None, raw[:400] or "(응답 본문 없음)"

    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            code = err.get("code") or err.get("type")
            msg = err.get("message") or err.get("detail") or ""
            return (str(code) if code else None), str(msg)[:400] or raw[:400]
        if isinstance(err, str):
            return None, err[:400]
        if payload.get("message"):
            code = payload.get("code") or payload.get("type")
            return (str(code) if code else None), str(payload["message"])[:400]
        if payload.get("detail"):
            return None, str(payload["detail"])[:400]

    return None, raw[:400] or "(응답 본문 없음)"


def _format(detail: str, code: str | None) -> str:
    hint = HINTS.get(code or "")
    msg = f"임베딩 실패 — {detail}"
    if hint:
        msg += f"\n\n  {hint}"
    else:
        msg += (
            "\n\n  코드가 확인되지 않았습니다. 위 원본 응답을 보고 판단해주세요."
            "\n  자주 있는 원인: 크레딧 잔액 0, 프록시/방화벽, 사내망 SSL 검사"
        )
    msg += "\n\n  임베딩 없이 진행하려면: python sync.py --no-embed"
    msg += "\n  (정확매칭 검색은 임베딩 없이도 동작합니다)"
    return msg


def embed_pending(store, embedder: Embedder, verbose: bool = True) -> int:
    rows = store.chunks_needing_embedding()
    if not rows:
        if verbose:
            print("  임베딩: 새로 만들 청크 없음")
        return 0
    if verbose:
        print(f"  임베딩: {len(rows)}개 청크 생성 중...")
    vecs = embedder.encode([r["text"] for r in rows])
    store.save_embeddings(list(zip([r["chunk_id"] for r in rows], vecs)))
    return len(rows)
