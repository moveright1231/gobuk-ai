"""응답 엔진.

경로는 위에서부터 순서대로 시도한다. 위쪽일수록 싸고 빠르고 정확하다.

  cache    메모리뱅크 히트                     LLM 0회
  exact    고유명사 정확매칭 -> 저장된 완성 답변  LLM 0회
  direct   벡터 상위 결과가 충분히 확실함        LLM 0회
  llm      여러 문서를 엮어야 함                LLM 1회
  chat     게임 질문이 아님 (인사/농담/상식)     LLM 1회
  fallback 근거가 부족함 -> 관리자 문의 안내     LLM 0회

정형 질문(레시피/아이템/직업)은 대부분 exact 에서 끝난다. 이게 토큰 절감의
핵심이다. 메모리뱅크는 그 위에서 비정형 질문의 반복을 걷어내는 역할이다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
import requests

import config
import intent as intent_mod
from flatten import normalize
from memory_bank import MemoryBank


def is_server_topic(question: str) -> bool:
    """거북스토리 고유 주제를 묻는 질문인가.

    잡담 경로를 막는 두 번째 그물. 의도도 고유명사도 안 잡혔지만 서버 주제어가
    들어 있다면 그건 잡담이 아니라 '문서가 없는 게임 질문'이므로, LLM에 보내지
    말고 관리자 문의로 돌려야 한다. 없는 지역·일정·콘텐츠를 지어내는 걸 막는다.

    별칭 매칭과 같은 정규화를 쓴다. "거북 마을", "거북마을", "거북마을은" 이
    모두 같게 걸린다.
    """
    low = normalize(question)
    return any(w in low for w in config.SERVER_TOPICS)


@dataclass
class Reply:
    text: str
    route: str
    sources: list[dict] = field(default_factory=list)
    llm_calls: int = 0
    similarity: float | None = None
    intent: str | None = None
    entities: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    ambiguous: bool = False

    @property
    def answered(self) -> bool:
        return self.route != "fallback"


def chat(messages: list[dict], max_tokens: int | None = None,
         temperature: float = 0.2) -> str:
    """짧은 답변 하나를 받는다."""
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}",
                 "Content-Type": "application/json"},
        json={
            "model": config.CHAT_MODEL,
            "messages": messages,
            "max_tokens": max_tokens or config.CHAT_MAX_TOKENS,
            "temperature": temperature,
        },
        timeout=45,
    )
    if not resp.ok:
        raise RuntimeError(f"LLM 호출 실패 {resp.status_code}: {resp.text[:300]}")
    return resp.json()["choices"][0]["message"]["content"].strip()


class Engine:
    def __init__(self, store, embedder=None, use_llm: bool = True,
                 log_misses: bool = True):
        self.store = store
        self.bank = MemoryBank(store)
        self.use_llm = use_llm
        # 답변 못 한 질문 적재. 벤치마크처럼 같은 질문을 반복 실행하는
        # 도구에서는 꺼야 한다. 안 그러면 집계가 부풀어 우선순위가 왜곡된다.
        self.log_misses = log_misses
        self._embedder = embedder
        self._vec_cache: tuple[list[str], np.ndarray] | None = None

    @property
    def embedder(self):
        if self._embedder is None:
            from embed import Embedder
            self._embedder = Embedder()
        return self._embedder

    def _vectors(self):
        # 청크가 수천 개 수준이라 통째로 메모리에 올려도 부담이 없다.
        if self._vec_cache is None:
            self._vec_cache = self.store.load_vectors()
        return self._vec_cache

    def invalidate_vectors(self) -> None:
        self._vec_cache = None

    # ------------------------------------------------------------------
    def ask(self, question: str, use_cache: bool = True) -> Reply:
        started = time.monotonic()
        parsed = intent_mod.parse(self.store, question)

        def finish(reply: Reply) -> Reply:
            reply.elapsed_ms = int((time.monotonic() - started) * 1000)
            reply.intent = parsed.intent
            reply.entities = parsed.entity_names
            return reply

        # 1) 정확매칭 --------------------------------------------------
        # 의도가 '가이드'면 설명형 질문이므로 프로퍼티 답변보다 본문이 낫다.
        if parsed.entities and parsed.intent != "guide":
            reply = self._exact(parsed)
            if reply:
                return finish(reply)

        # 2) 메모리뱅크 ------------------------------------------------
        # 여기부터는 임베딩이 필요하다. exact 로 끝났으면 부를 일이 없다.
        try:
            qvec = self.embedder.encode([question])[0]
        except Exception as exc:
            return finish(Reply(
                text=config.ADMIN_CONTACT, route="fallback",
                sources=[{"error": str(exc)[:200]}]))

        if use_cache:
            hit = self.bank.lookup(qvec, parsed.entity_key, parsed.intent)
            if hit:
                return finish(Reply(
                    text=hit["answer"], route="cache",
                    similarity=hit["similarity"],
                    sources=[{"cached_from": hit["cached_question"]}]))

        # 3) 벡터 검색 -------------------------------------------------
        reply = self._vector(parsed, qvec)

        # 4) 잡담 ------------------------------------------------------
        # 문서로 못 답했는데 게임 질문도 아니라면 가볍게 대꾸한다.
        # 고유명사나 의도가 잡혔다면 그건 '문서가 없는 게임 질문'이므로
        # 여기로 보내면 안 된다. LLM이 없는 정보를 지어낸다.
        #
        # 둘 다 안 잡히는 게임 질문이 있다. "거북마을이 뭐야" 는 별칭에도
        # INTENT_RULES 에도 없어서 잡담으로 새고, 모델은 서버 고유명사인 줄
        # 모르니 그냥 지어낸다. 주제어 검사로 한 겹 더 막는다.
        if (reply.route == "fallback" and self.use_llm and config.SMALLTALK
                and not parsed.entities and not parsed.intent
                and not is_server_topic(question)
                and len(question) <= config.SMALLTALK_MAX_CHARS):
            small = self._smalltalk(parsed)
            if small:
                reply = small

        if reply.answered:
            # 잡담은 캐시하지 않는다. 같은 농담만 반복하는 봇이 된다.
            if use_cache and reply.route != "chat":
                self.bank.store_answer(
                    question=question, query_vec=qvec, answer=reply.text,
                    route=reply.route, intent=parsed.intent,
                    entity_key=parsed.entity_key,
                    source_pages=[s["page_id"] for s in reply.sources if s.get("page_id")],
                )
        elif self.log_misses:
            # 근거가 없어서 못 답한 질문만 쌓는다. 위쪽 임베딩 실패 경로는
            # 설비 장애지 문서 부재가 아니므로 여기까지 오지 않는다.
            self.store.log_unanswered(
                question, intent=parsed.intent,
                entities=parsed.entity_names, top_score=reply.similarity,
            )
        return finish(reply)

    # ------------------------------------------------------------------
    def _exact(self, parsed) -> Reply | None:
        """고유명사가 특정된 질문. 저장해둔 완성 답변을 그대로 낸다."""
        if parsed.intent:
            cands = [e for e in parsed.entities if e["db_key"] == parsed.intent]
            if not cands:
                return None
            cands = cands[:1]
        else:
            # 의도가 불분명하면 답할 수 있는 만큼 답한다.
            # 같은 이름이 여러 DB에 있어도 되묻지 않고 전부 보여준다.
            cands, seen = [], set()
            for e in parsed.entities:
                key = (e["title"], e["db_key"])
                if key in seen:
                    continue
                seen.add(key)
                cands.append(e)
                if len(cands) >= 3:
                    break

        blocks, sources = [], []
        for e in cands:
            row = self.store.conn.execute(
                "SELECT title,url,answer_text,db_key,status FROM records WHERE page_id=?",
                (e["page_id"],),
            ).fetchone()
            if not row or row["status"] != config.PUBLISHED_STATUS:
                continue
            label = config.DATA_SOURCES[row["db_key"]]["label"]
            prefix = f"[{label}]\n" if len(cands) > 1 else ""
            blocks.append(prefix + row["answer_text"])
            sources.append({
                "page_id": e["page_id"], "title": row["title"],
                "url": row["url"], "db": row["db_key"],
                "matched_alias": e["matched"],
            })

        if not blocks:
            return None
        return Reply(
            text="\n\n".join(blocks), route="exact", sources=sources,
            ambiguous=len(blocks) > 1,
        )

    def _smalltalk(self, parsed) -> Reply | None:
        """인사·농담·일반상식처럼 문서가 필요 없는 질문에 가볍게 답한다.

        게임 정보 질문이 이 경로로 새면 LLM이 지어낸다. 세 겹으로 막는다.
          1) 고유명사나 의도가 잡힌 질문은 호출부에서 이미 걸러진다
          2) 서버 고유 주제어가 든 질문도 호출부에서 걸러진다 (is_server_topic)
          3) 그래도 게임 정보를 물으면 모델이 표식을 내도록 지시해뒀다

        모델이 표식을 냈거나 호출이 실패하면 None. 원래대로 관리자 문의가 나간다.
        온도를 조금 높인 건 같은 인사에 매번 같은 문장이 나오지 않게 하려는 것.
        """
        try:
            text = chat(
                [{"role": "system", "content": config.SMALLTALK_PROMPT},
                 {"role": "user", "content": parsed.query}],
                max_tokens=config.SMALLTALK_MAX_TOKENS,
                temperature=0.7,
            )
        except Exception:
            return None
        if not text or config.DECLINE in text:
            return None
        return Reply(text=text, route="chat", llm_calls=1)

    def _vector(self, parsed, qvec: np.ndarray) -> Reply:
        ids, mat = self._vectors()
        if not ids:
            return Reply(text=config.ADMIN_CONTACT, route="fallback")

        q = qvec / max(float(np.linalg.norm(qvec)), 1e-9)
        sims = mat @ q
        order = np.argsort(-sims)[:config.VECTOR_TOP_K]
        picked = [(ids[i], float(sims[i])) for i in order]
        ctx = self.store.chunk_context([cid for cid, _ in picked])
        picked = [(cid, s) for cid, s in picked if cid in ctx]

        if not picked or picked[0][1] < config.VECTOR_MIN:
            return Reply(
                text=config.ADMIN_CONTACT, route="fallback",
                similarity=round(picked[0][1], 4) if picked else None,
            )

        top_id, top_score = picked[0]
        top = ctx[top_id]
        sources = [{
            "page_id": ctx[cid]["page_id"], "title": ctx[cid]["title"],
            "url": ctx[cid]["url"], "db": ctx[cid]["db_key"],
            "score": round(s, 4),
        } for cid, s in picked]

        # 충분히 확실하면 LLM을 부르지 않는다. 기획자가 쓴 요약이 이미 답이다.
        if top_score >= config.VECTOR_DIRECT or not self.use_llm:
            return Reply(
                text=top["answer_text"] or top["text"], route="direct",
                sources=sources[:2], similarity=round(top_score, 4),
            )

        docs = "\n\n---\n\n".join(
            f"[{ctx[cid]['title']}]\n{ctx[cid]['text']}" for cid, _ in picked[:3]
        )
        try:
            text = chat([
                {"role": "system", "content": config.SYSTEM_PROMPT},
                {"role": "user",
                 "content": f"참고 문서:\n{docs}\n\n질문: {parsed.query}"},
            ])
        except Exception as exc:
            return Reply(
                text=top["answer_text"] or config.ADMIN_CONTACT,
                route="direct", sources=sources[:2],
                similarity=round(top_score, 4),
            )

        # 모델이 표식을 냈으면 근거가 없다는 뜻이다.
        #
        # 예전에는 "없습니다" 같은 낱말로 거절을 판별했는데, 그러면
        # "요구 레벨이 없습니다" 처럼 문서에 근거한 정상 답변까지 버려진다.
        # 표식으로 받으면 정상 답변과 절대 헷갈리지 않는다.
        # 표식을 무시하고 자연어로 거절하는 경우만 좁게 한 번 더 본다.
        stripped = text.replace(config.DECLINE, "").strip()
        low = stripped.replace(" ", "")
        if not stripped or any(k in low for k in
                               ("모르겠", "정보가없", "찾을수없", "알수없")):
            return Reply(text=config.ADMIN_CONTACT, route="fallback",
                         similarity=round(top_score, 4))
        text = stripped

        return Reply(text=text, route="llm", sources=sources[:2],
                     llm_calls=1, similarity=round(top_score, 4))
