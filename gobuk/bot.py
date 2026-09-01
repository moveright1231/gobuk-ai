#!/usr/bin/env python3
"""거북AI 디스코드 봇.

  python bot.py

두 가지로 반응한다.
  /거북 <질문>              어디서든
  지정 채널에 그냥 쓰기      BOT_CHANNEL_IDS 에 등록한 채널

디스코드는 상호작용에 3초 안에 응답하지 않으면 실패 처리한다.
임베딩+LLM 은 그보다 오래 걸릴 수 있으므로 defer() 로 먼저 시간을 번다.
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

from gobuk import config
from gobuk.engine.answer import Engine
from gobuk.store import Store

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gobuk")

MAX_LEN = 1900  # 디스코드 본문 2000자 제한


class GobukBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.store = Store()
        self.engine = Engine(self.store)
        self._lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        await self.tree.sync()
        log.info("슬래시 커맨드 동기화 완료")

    async def on_ready(self) -> None:
        log.info("로그인: %s", self.user)
        st = self.store.stats()
        log.info("적재: 청크 %s개 (임베딩 %s개)", st["_chunks"], st["_embedded"])
        if config.BOT_CHANNEL_IDS:
            log.info("자동응답 채널: %s", config.BOT_CHANNEL_IDS)
        else:
            log.info("자동응답 채널 미설정 — /거북 명령만 동작합니다")

    async def ask(self, question: str) -> "object":
        """엔진은 동기 코드라 스레드로 넘긴다. 이벤트 루프를 막지 않기 위해서."""
        async with self._lock:
            return await asyncio.to_thread(self.engine.ask, question)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.content.strip():
            return
        mentioned = self.user in message.mentions
        in_channel = message.channel.id in config.BOT_CHANNEL_IDS
        if not (mentioned or in_channel):
            return

        question = message.content
        for m in message.mentions:
            question = question.replace(f"<@{m.id}>", "").replace(f"<@!{m.id}>", "")
        question = question.strip()
        if len(question) < 2:
            return

        async with message.channel.typing():
            try:
                reply = await self.ask(question)
            except Exception:
                log.exception("응답 실패")
                await message.reply(config.ADMIN_CONTACT, mention_author=False)
                return
        await message.reply(embed=build_embed(question, reply),
                            mention_author=False)
        log.info("[%s] %.0fms %s", reply.route, reply.elapsed_ms, question[:60])


def build_embed(question: str, reply) -> discord.Embed:
    answered = reply.answered
    # 잡담은 문서에 근거한 답이 아니므로 색을 달리해 구분한다.
    # 유저가 이걸 확정된 게임 정보로 오해하면 안 된다.
    color = 0x5C6BC0 if reply.route == "chat" else (0x4CAF50 if answered else 0x9E9E9E)
    embed = discord.Embed(description=reply.text[:MAX_LEN], color=color)
    embed.set_author(name=question[:250])

    if answered and reply.sources:
        links = []
        for s in reply.sources[:2]:
            if s.get("url") and s.get("title"):
                label = config.DATA_SOURCES.get(s.get("db", ""), {}).get("label", "")
                links.append(f"[{s['title']}{f' ({label})' if label else ''}]({s['url']})")
        if links:
            embed.add_field(name="자세히 보기", value="\n".join(links), inline=False)

    if reply.ambiguous:
        embed.set_footer(text="같은 이름이 여러 개라 전부 보여드렸어요")
    elif reply.route == "chat":
        embed.set_footer(text="가벼운 답변이에요 · 게임 정보는 /거북 으로 물어봐 주세요")
    elif reply.route == "cache":
        embed.set_footer(text="이전 답변 재사용")
    return embed


bot = GobukBot()


@bot.tree.command(name="거북", description="거북스토리에 대해 물어보세요")
@app_commands.describe(질문="예: 토스파 레시피 알려줘")
async def gobuk(interaction: discord.Interaction, 질문: str) -> None:
    # 3초 룰. 무거운 작업 전에 반드시 먼저 응답해둔다.
    await interaction.response.defer(thinking=True)
    try:
        reply = await bot.ask(질문)
    except Exception:
        log.exception("응답 실패")
        await interaction.followup.send(config.ADMIN_CONTACT)
        return
    await interaction.followup.send(embed=build_embed(질문, reply))
    log.info("[%s] %.0fms %s", reply.route, reply.elapsed_ms, 질문[:60])


def main() -> int:
    if not config.DISCORD_TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN 이 없습니다.\n"
            "  https://discord.com/developers/applications 에서 봇 생성 후\n"
            "  Bot > Reset Token 으로 발급하고 .env 에 넣어주세요.\n"
            "  Bot > Privileged Gateway Intents > MESSAGE CONTENT INTENT 도 켜야 합니다."
        )
    try:
        bot.run(config.DISCORD_TOKEN, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        raise SystemExit(
            "MESSAGE CONTENT INTENT 가 꺼져 있습니다.\n"
            "  https://discord.com/developers/applications\n"
            "    -> 해당 앱 > Bot > Privileged Gateway Intents\n"
            "    -> MESSAGE CONTENT INTENT 켜고 저장 후 다시 실행\n\n"
            "  채널에서 멘션 없이 반응하는 기능에 필요합니다.\n"
            "  /거북 명령만 쓸 거라면 위 GobukBot.__init__ 의\n"
            "  intents.message_content 를 False 로 두면 이 인텐트 없이도 뜹니다."
        )
    except discord.LoginFailure:
        raise SystemExit(
            "DISCORD_TOKEN 이 잘못됐거나 폐기됐습니다.\n"
            "  Bot > Reset Token 으로 재발급해 .env 에 넣어주세요."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
