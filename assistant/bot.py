"""디스코드 봇 본체.

- 주인(DISCORD_OWNER_ID)의 메시지에만 반응 (DM 또는 홈 채널)
- 확인이 필요한 도구는 [실행]/[취소] 버튼
- 백그라운드: 새 메일 알림, 리마인더, 배치 결과, 야간 메일 요약
명령: !reset (대화 초기화)  !usage (토큰 사용량)  !deep 질문 (깊은 모드 강제)  !help
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("DATA_DIR", str(ROOT / "data"))  # 도구 모듈들이 import 시점에 경로를 정하므로 먼저 설정

import anthropic  # noqa: E402
import discord  # noqa: E402
import yaml  # noqa: E402
from discord.ext import tasks  # noqa: E402

from . import batch as batchmod  # noqa: E402
from . import costs  # noqa: E402
from . import coupons  # noqa: E402
from . import expenses  # noqa: E402
from . import improve  # noqa: E402
from . import library  # noqa: E402
from . import mailwatch  # noqa: E402
from . import readlater  # noqa: E402
from . import backup  # noqa: E402
from . import alerts  # noqa: E402
from . import briefing  # noqa: E402
from . import commute  # noqa: E402
from . import daum  # noqa: E402,F401  (도구 등록)
from . import cycles  # noqa: E402
from . import busan  # noqa: E402
from . import maildraft  # noqa: E402
from . import meet  # noqa: E402,F401  (도구 등록)
from . import pages  # noqa: E402
from . import talkcal  # noqa: E402
from . import projects  # noqa: E402
from . import recall  # noqa: E402
from . import gextra  # noqa: E402
from . import paysms  # noqa: E402
from . import guide  # noqa: E402
from . import heartbeat  # noqa: E402
from . import weekly  # noqa: E402
from . import renewals  # noqa: E402
from . import screenshot  # noqa: E402
from . import settings  # noqa: E402
from .agents import Orchestra  # noqa: E402
from .store import Store  # noqa: E402
from .tools import dev, google, life, mail, personal  # noqa: E402,F401  (도구 등록)
from .tools import library as library_tools  # noqa: E402,F401
from .tools.base import Context  # noqa: E402

log = logging.getLogger("bot")
DATA = Path(os.environ["DATA_DIR"])
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
TEXT_EXT = {".txt", ".md", ".csv", ".json", ".py", ".cs", ".js", ".ts", ".log", ".yaml", ".yml", ".gd", ".glsl"}

HELP = """**사용법** — 그냥 말하면 됩니다. 기능이 궁금하면 "○○ 어떻게 써?"라고 묻거나 `!help 주제` (예: `!help 쿠폰`, `!help 알림`)
예) `오늘 온 메일 중 중요한 거 있어?` · `내일 일정 알려줘` · `beast-hunter 어제 뭐 바꿨지?`
`금요일 3시에 치과 일정 넣어줘` · `30분 뒤에 빨래 알려줘` · `서면 근처 국밥집` · `S-100 최근 소식 찾아줘`
`beast-hunter 지난주 커밋 각각 한 줄로 요약해서 배치로 돌려줘`
사진·텍스트 파일을 첨부해도 됩니다. **사진만** 보내면 스크린샷 비서가 일정·할 일·기프티콘·영수증을 알아보고 버튼으로 정리해요. **링크만** 보내면 요약해서 '나중에 읽기'에 저장해요.
PDF·긴 텍스트를 올리면 **서재**에 저장되고, 이후 `S-52에서 DEPCNT 조건이 뭐야?`처럼 물으면 쪽 번호·인용과 함께 답해요.
**명령** `!reset` 대화 초기화 · `!usage` 토큰 사용량 · `!deep 질문` 깊게 생각하기 · `!memory` 기억 보기 · `!docs` 서재 목록 · `!docdel 번호` 문서 삭제 · `!docclean` 안 쓰는 업로드 파일 정리
`!renewals` 재발급 일정 · `!renewed 키 [날짜]` 재발급 기록
`!yt` 구독 채널 새 영상 · `!sheet` 지출 시트 동기화 · `!birthdays` 다가오는 생일
`!dash` 내 대시보드 링크 · `!out` 지금 나가도 돼? · `!bus` 즐겨찾는 정류소 (`!bus add 서면역`)
`!project 이름` 프로젝트 스레드 · `!cycle` 생활 주기 (`!cycle add 정수기필터 90`, `!done 이름`) · `!queue`/`!flush` 모아둔 알림
`!place 집|회사 주소` 출발지 등록 · `!origin 집|회사|장소` 오늘 출발지 · `!week` 한 주 회고
`!spend` 이번 달 지출 · `!pay` 결제 문자 자동 기록 · `!coupons` 기프티콘 지갑 · `!budget [off|on]` API 비용 · `!backup` 지금 백업
`!config` VM 전용 설정 보기 · `!set 경로 값` 설정 바꾸기 · `!unset 경로` 되돌리기 · `!restart` 재시작
`!improve` 자기 개선 분석 지금 실행 · `!update` 최신 코드 받고 재시작 · `!playtest [목표]` AI 플레이테스트"""


class ConfirmView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.result: asyncio.Future = asyncio.get_running_loop().create_future()

    async def interaction_check(self, it: discord.Interaction) -> bool:
        return it.user.id == self.owner_id

    async def _done(self, it: discord.Interaction, ok: bool):
        for c in self.children:
            c.disabled = True
        await it.response.edit_message(content=it.message.content + ("\n✅ 실행" if ok else "\n❌ 취소"), view=self)
        if not self.result.done():
            self.result.set_result(ok)

    @discord.ui.button(label="실행", style=discord.ButtonStyle.success)
    async def ok(self, it: discord.Interaction, _):
        await self._done(it, True)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, it: discord.Interaction, _):
        await self._done(it, False)

    async def on_timeout(self):
        if not self.result.done():
            self.result.set_result(False)


class ScreenshotView(discord.ui.View):
    """스크린샷 카드의 동작 버튼. 여러 개 눌러도 되고(예: 캘린더 + 메모), 닫기를 누르면 끝."""

    def __init__(self, bot: "Assistant", info: dict, channel_id: str):
        super().__init__(timeout=1800)
        self.bot, self.info, self.cid = bot, info, channel_id
        for i, (key, label) in enumerate(screenshot.actions_for(info)):
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.primary if i == 0 else discord.ButtonStyle.secondary)
            b.callback = self._make(key, b)
            self.add_item(b)
        close = discord.ui.Button(label="닫기", style=discord.ButtonStyle.danger)
        close.callback = self._close
        self.add_item(close)

    async def interaction_check(self, it: discord.Interaction) -> bool:
        return it.user.id == self.bot.owner_id

    def _make(self, key: str, button: discord.ui.Button):
        async def cb(it: discord.Interaction):
            button.disabled = True
            await it.response.edit_message(view=self)
            res = await screenshot.run_action(key, self.bot.cfg, self.bot.store, self.info, self.cid)
            await it.followup.send(res)
        return cb

    async def _close(self, it: discord.Interaction):
        for c in self.children:
            c.disabled = True
        await it.response.edit_message(view=self)
        self.stop()


def mail_view(bot: "Assistant", info: dict, channel_id: str) -> discord.ui.View:
    """메일 알림 버튼: (할 일·일정이 있으면) 캘린더/할 일 + 항상 ✉️ 답장 초안."""
    task_keys = {"kind", "title"}
    view = ScreenshotView(bot, info, channel_id) if task_keys <= set(info) and info.get("kind") in ("todo", "event") \
        else discord.ui.View(timeout=6 * 3600)
    m = info.get("_mail") or {}
    if m.get("uid"):
        b = discord.ui.Button(label="✉️ 답장 초안", style=discord.ButtonStyle.secondary)

        async def cb(it: discord.Interaction):
            if it.user.id != bot.owner_id:
                return
            b.disabled = True
            await it.response.edit_message(view=view)
            try:
                d = await maildraft.draft(bot.claude, bot.store, bot.cfg, m["uid"])
                link = discord.ui.View()
                link.add_item(discord.ui.Button(label="Gmail에서 새 메일 열기", url=d["url"]))
                await it.followup.send(maildraft.render(d)[:1950], view=link)
            except Exception as e:
                await it.followup.send(f"초안을 못 썼어요: {type(e).__name__}: {str(e)[:150]}")
        b.callback = cb
        view.add_item(b)
    return view


class CardView(discord.ui.View):
    """답변에 카드 링크가 있으면: [내 카톡으로 보내기] — 나와의 채팅으로 보내 두고 카톡에서 '전달'로 친구에게."""

    def __init__(self, bot: "Assistant", urls: list[str]):
        super().__init__(timeout=6 * 3600)
        self.bot = bot
        for i, url in enumerate(urls):
            label = "💬 내 카톡으로 보내기" + (f" ({pages.card_summary(url).splitlines()[0][2:14]})" if len(urls) > 1 else "")
            b = discord.ui.Button(label=label[:80], style=discord.ButtonStyle.primary if i == 0 else discord.ButtonStyle.secondary)
            b.callback = self._make(url, b)
            self.add_item(b)

    async def interaction_check(self, it: discord.Interaction) -> bool:
        return it.user.id == self.bot.owner_id

    def _make(self, url: str, button: discord.ui.Button):
        async def cb(it: discord.Interaction):
            button.disabled = True
            await it.response.edit_message(view=self)
            try:
                from .tools import life
                await life.kakao_send_card(pages.card_summary(url), url)
                await it.followup.send("💬 나와의 채팅으로 보냈어요. 카톡에서 그 메시지를 길게 눌러 **전달**하면 친구에게 보낼 수 있어요.")
            except Exception as e:
                await it.followup.send(f"카톡으로 못 보냈어요: {str(e)[:200]}")
        return cb


class PayView(discord.ui.View):
    """결제 문자 자동 기록 카드: 분류 바꾸기 · 기록 지우기. (중복 의심이면 [기록]/[무시])"""

    def __init__(self, bot: "Assistant", res: dict, text: str):
        super().__init__(timeout=24 * 3600)
        self.bot, self.res, self.text = bot, res, text
        self.page_id, self.shop = res.get("id", ""), res.get("shop", "")
        if res["action"] == "duplicate":
            for label, fn, style in (("기록", self._record, discord.ButtonStyle.success), ("무시", self._ignore, discord.ButtonStyle.secondary)):
                b = discord.ui.Button(label=label, style=style)
                b.callback = fn
                self.add_item(b)
        else:
            self._recorded_items()

    def _recorded_items(self):
        self.clear_items()
        sel = discord.ui.Select(placeholder="분류 바꾸기", options=[
            discord.SelectOption(label=c, emoji=expenses.ICON[c], default=(c == self.res.get("category"))) for c in expenses.CATEGORIES])
        sel.callback = self._category
        self.sel = sel
        self.add_item(sel)
        b = discord.ui.Button(label="기록 지우기", style=discord.ButtonStyle.danger)
        b.callback = self._delete
        self.add_item(b)

    async def interaction_check(self, it: discord.Interaction) -> bool:
        return it.user.id == self.bot.owner_id

    async def _finish(self, it: discord.Interaction, note: str):
        await it.edit_original_response(content=(self.text + "\n" + note)[:1900], view=None)

    async def _category(self, it: discord.Interaction):
        await it.response.defer()
        cat = self.sel.values[0]
        try:
            await paysms.set_category(self.bot.store, self.page_id, self.shop, cat)
            self.res["category"] = cat
            self.text = self.text.rsplit(" (", 1)[0] + f" ({cat})" if self.text.rstrip().endswith(")") else self.text
            self._recorded_items()
            await it.edit_original_response(content=(self.text + f"\n✏️ 분류를 {cat}(으)로 바꿨어요. 다음부터 {self.shop}은(는) {cat}(으)로 기록해요.")[:1900], view=self)
        except Exception as e:
            await it.followup.send(f"분류를 바꾸지 못했어요: {str(e)[:150]}")

    async def _delete(self, it: discord.Interaction):
        await it.response.defer()
        try:
            await paysms.delete(self.bot.store, self.page_id)
            await self._finish(it, "🗑️ 기록을 지웠어요.")
        except Exception as e:
            await it.followup.send(f"지우지 못했어요: {str(e)[:150]}")

    async def _record(self, it: discord.Interaction):
        await it.response.defer()
        try:
            out = await paysms.record(self.bot.cfg, self.bot.store, self.res["p"], self.shop, self.res["category"])
            self.page_id, self.text = out["id"], "💳 " + out["text"].removeprefix("🧾 ")
            self._recorded_items()
            await it.edit_original_response(content=self.text[:1900], view=self)
        except Exception as e:
            await it.followup.send(f"기록하지 못했어요: {str(e)[:150]}")

    async def _ignore(self, it: discord.Interaction):
        await it.response.defer()
        await self._finish(it, "무시했어요.")


class OriginView(discord.ui.View):
    """출발 알림 카드: 출발지를 바꿔 다시 계산."""

    def __init__(self, bot: "Assistant", event: dict, dest: dict | None = None):
        super().__init__(timeout=3 * 3600)
        self.bot, self.event = bot, event
        for name, icon in (("집", "🏠"), ("회사", "🏢")):
            b = discord.ui.Button(label=f"{icon} {name}에서", style=discord.ButtonStyle.secondary)
            b.callback = self._make(name)
            self.add_item(b)
        if dest:                                   # 링크 버튼: 카카오내비 실행 카드 · 카카오맵 길찾기
            try:
                links = [("🚗 카카오내비", pages.go_url(bot.cfg, dest, event.get("title", ""))), ("🚌 카카오맵 길찾기", pages.kakaomap_to(dest))]
            except Exception:
                links = [("🚌 카카오맵 길찾기", pages.kakaomap_to(dest))]
            for label, url in links:
                if len(url) <= 512:
                    self.add_item(discord.ui.Button(label=label, url=url))

    async def interaction_check(self, it: discord.Interaction) -> bool:
        return it.user.id == self.bot.owner_id

    def _make(self, name: str):
        async def cb(it: discord.Interaction):
            await it.response.defer()
            now = datetime.now(self.bot.tz)
            try:
                p = await commute.plan(self.bot.cfg, self.bot.store, self.event, now, prefer=name)
                text = commute.message(p, now) if p and not p.get("skip") else f"{name}에서는 계산할 수 없어요 ({(p or {}).get('skip', '?')})"
            except Exception as e:
                text = f"다시 계산하지 못했어요: {str(e)[:150]}"
            await it.edit_original_response(content=text[:1900], view=self)
        return cb


def split_message(text: str, size: int = 1900) -> list[str]:
    parts, buf = [], ""
    for line in text.split("\n"):
        while len(line) > size:
            parts.append(buf) if buf else None
            buf = ""
            parts.append(line[:size])
            line = line[size:]
        if len(buf) + len(line) + 1 > size:
            parts.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        parts.append(buf)
    return [p for p in parts if p.strip()] or ["(빈 응답)"]


class Assistant(discord.Client):
    def __init__(self, cfg: dict):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        super().__init__(intents=intents)
        self.cfg = cfg
        self.owner_id = int(os.environ["DISCORD_OWNER_ID"])
        self.home_id = int(os.environ.get("DISCORD_HOME_CHANNEL_ID", "0") or 0)
        self.store = Store(DATA / "assistant.db")
        library.init(self.store)
        coupons.init(self.store)
        alerts.init(self.store)
        cycles.init(self.store)
        recall.init(self.store)
        self.claude = anthropic.AsyncAnthropic(max_retries=4)
        self.orch = Orchestra(cfg, self.store, self.claude)
        self.locks: dict[str, asyncio.Lock] = {}
        self.tz = ZoneInfo(cfg["timezone"])

    # ───────── 시작 ─────────
    async def setup_hook(self):
        note = settings.sync_git_remote(self.cfg)
        if note:
            log.info(note)
        self.mail_loop.change_interval(seconds=self.cfg["mail"]["poll_seconds"])
        self.batch_loop.change_interval(seconds=self.cfg["batch"]["poll_seconds"])
        for loop in (self.mail_loop, self.reminder_loop, self.batch_loop, self.nightly_loop, self.morning_loop, self.renewal_loop, self.improve_loop, self.deploy_watch_loop, self.schedule_loop, self.commute_loop, self.heartbeat_loop, self.talkcal_loop, self.warning_loop):
            loop.start()

    async def on_ready(self):
        log.info("로그인: %s", self.user)

    async def home(self) -> discord.abc.Messageable:
        if self.home_id:
            ch = self.get_channel(self.home_id) or await self.fetch_channel(self.home_id)
            return ch
        user = self.get_user(self.owner_id) or await self.fetch_user(self.owner_id)
        return user.dm_channel or await user.create_dm()

    async def channel_by_id(self, cid: str) -> discord.abc.Messageable:
        try:
            return self.get_channel(int(cid)) or await self.fetch_channel(int(cid))
        except Exception:
            return await self.home()

    # ───────── 결제 문자 (웹후크) ─────────
    async def on_pay_sms(self, msg: discord.Message):
        text = "\n".join(x for x in [msg.content, *[(e.description or "") for e in msg.embeds]] if x).strip()
        if not text:
            return
        try:
            res = await paysms.handle(self.cfg, self.store, self.claude, text[:1000])
        except Exception as e:
            log.exception("결제 문자 처리 실패")
            res = {"action": "error", "text": f"⚠️ 결제 문자를 처리하지 못했어요: {str(e)[:150]}"}
        if res["action"] == "ignored":
            if res.get("repeat"):
                return
            try:
                await msg.add_reaction("👀")            # 받긴 했지만 결제 문자가 아니라고 본 글 (설정 확인용 표시)
            except Exception:
                pass
            return
        view = PayView(self, res, res["text"]) if res["action"] in ("recorded", "duplicate") else None
        await msg.channel.send(res["text"][:1900], view=view)

    # ───────── 메시지 처리 ─────────
    async def on_message(self, msg: discord.Message):
        if paysms.is_source(self.cfg, msg.channel.id, msg.webhook_id):
            return await self.on_pay_sms(msg)          # 결제 문자: 지출 기록만 하고 대화·명령으로는 넘기지 않는다
        if msg.author.bot or msg.author.id != self.owner_id:
            return
        is_dm = isinstance(msg.channel, discord.DMChannel)
        in_home_thread = getattr(msg.channel, "parent_id", None) == self.home_id and self.home_id
        if not is_dm and msg.channel.id != self.home_id and not in_home_thread and self.user not in msg.mentions \
                and not projects.get(self.store, str(msg.channel.id)):
            return
        text = msg.content.replace(f"<@{self.user.id}>", "").strip()
        cid = str(msg.channel.id)

        if text in ("!help", "!도움말"):
            return await msg.reply(HELP)
        if text.startswith(("!help ", "!도움말 ")):
            return await msg.reply(guide.help_text(text.split(maxsplit=1)[1]))
        if text == "!reset":
            self.store.flag_last_trace(cid, "reset", True)
            self.store.clear_history(cid)
            return await msg.reply("대화 기록을 지웠어요.")
        if text == "!usage":
            return await msg.reply(self.usage_report())
        if text == "!memory":
            return await msg.reply((await personal.memory_read(None))[:1900])
        if text == "!docs":
            docs = library.list_docs(self.store)
            lines = [f"`#{d['id']}` **{d['title']}**" + (f" · {d['pages']}쪽" if d["pages"] else "")
                     + (" · (구버전 방식)" if d["kind"] == "files" else "") + f"\n  {d['summary'][:120]}" for d in docs]
            return await msg.reply("\n".join(lines)[:1900] if lines else "서재가 비어 있어요. PDF나 텍스트 파일을 첨부해 주세요.")
        if text.startswith("!docdel"):
            try:
                ok = await library.delete_document(self.claude, self.store, int(text.split()[1]))
            except (IndexError, ValueError):
                return await msg.reply("사용법: `!docdel 번호`")
            return await msg.reply("삭제했어요." if ok else "그 번호의 문서가 없어요.")
        if text == "!docclean":
            return await self.docclean(msg)
        if text == "!config":
            return await msg.reply(settings.show_local())
        if text.startswith("!set "):
            parts = text.split(maxsplit=2)
            if len(parts) < 3:
                return await msg.reply("사용법: `!set 경로 값` 예: `!set talkcal.enabled true`, `!set briefing.time \"07:30\"`")
            base = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
            return await msg.reply(settings.set_local(parts[1], parts[2], base))
        if text.startswith("!unset "):
            return await msg.reply(settings.unset_local(text.split(maxsplit=1)[1]))
        if text == "!restart":
            await msg.reply("🔄 재시작할게요. 10초쯤 뒤 다시 켜져요.")
            await asyncio.sleep(1)
            os._exit(0)                 # systemd(또는 Docker restart 정책)가 다시 띄움
        if text == "!yt":
            await msg.reply("📺 구독 채널을 확인하고 있어요…")
            try:
                out = await gextra.youtube_digest(self.claude, self.store, self.cfg, datetime.now(self.tz))
            except Exception as e:
                out = f"유튜브를 확인하지 못했어요: {str(e)[:250]}"
            return await self.send_home(out or "지난 확인 이후 새 영상이 없어요.", channel_id=cid)
        if text == "!sheet":
            try:
                return await msg.reply(await gextra.sync_expense_sheet(self.cfg, self.store, datetime.now(self.tz).date()))
            except Exception as e:
                return await msg.reply(f"시트를 동기화하지 못했어요: {str(e)[:250]}")
        if text == "!birthdays":
            try:
                today = datetime.now(self.tz).date()
                items = gextra.upcoming(await gextra.birthdays(self.store, today, refresh=True), today, 60)
            except Exception as e:
                return await msg.reply(f"생일을 읽지 못했어요: {str(e)[:250]}")
            return await msg.reply("🎂 **60일 안의 생일·기념일**\n" + "\n".join(
                f"{b['date']:%m/%d} {b['name']} {b['kind']}{b['age']} (D-{b['left']})" for b in items) if items else "60일 안에 생일·기념일이 없어요.")
        if text == "!dash":
            try:
                url = await pages.publish_dash(self.cfg, self.store, datetime.now(self.tz), force=True)
            except Exception as e:
                return await msg.reply(f"대시보드를 올리지 못했어요: {str(e)[:250]}")
            return await msg.reply(f"📊 내 대시보드 (1~2분 뒤 반영, 폰 홈 화면에 추가해 두세요)\n<{url}>\n"
                                   "이 링크가 열쇠예요. 다른 사람에게 보내지 마세요. 새 열쇠가 필요하면 `!dash reset`")
        if text == "!dash reset":
            self.store.put("dash_key", "")
            self.store.put("dash_sig", "")
            return await msg.reply("열쇠를 지웠어요. `!dash`로 새 링크를 받으세요 (예전 링크는 더 이상 열리지 않아요).")
        if text in ("!out", "!나가"):
            try:
                return await msg.reply((await busan.check(self.cfg, self.store))[:1900])
            except Exception as e:
                return await msg.reply(f"확인하지 못했어요: {str(e)[:200]}")
        if text.startswith("!bus"):
            arg = text.removeprefix("!bus").strip()
            try:
                if arg.startswith("add "):
                    return await msg.reply(await busan.add_favorite(self.store, arg[4:]))
                if arg.startswith("del "):
                    return await msg.reply(busan.remove_favorite(self.store, arg[4:]))
                lines = await busan.bus_lines(self.store)
            except Exception as e:
                return await msg.reply(f"버스 정보를 못 가져왔어요: {str(e)[:200]}")
            return await msg.reply("\n".join(lines)[:1900] if lines else "즐겨찾는 정류소가 없어요. `!bus add 정류소이름` (예: `!bus add 서면역`)")
        if text.startswith("!project"):
            return await self.project_command(msg, text.removeprefix("!project").strip(), cid)
        if text.startswith("!cycle"):
            arg = text.removeprefix("!cycle").strip()
            today = datetime.now(self.tz).date()
            if arg.startswith("add "):
                parts = arg[4:].split()
                try:
                    name, every = parts[0], int(parts[1])
                    last = date.fromisoformat(parts[2]) if len(parts) > 2 else today
                except (IndexError, ValueError):
                    return await msg.reply("사용법: `!cycle add 이름 주기일수 [마지막날짜 YYYY-MM-DD]` 예: `!cycle add 정수기필터 90`")
                return await msg.reply(cycles.add(self.store, name, every, last))
            if arg.startswith("del "):
                return await msg.reply(cycles.delete(self.store, arg[4:]))
            lines = cycles.lines(self.store, today)
            return await msg.reply("🔁 **생활 주기**\n" + "\n".join(lines) if lines else
                                   "등록된 항목이 없어요. `!cycle add 정수기필터 90` 처럼 추가하세요.")
        if text.startswith("!done "):
            return await msg.reply(cycles.done(self.store, text[6:].strip(), datetime.now(self.tz).date()))
        if text in ("!talkcal on", "!talkcal"):
            self.store.put("talkcal_paused", False)
            on = (self.cfg.get("talkcal") or {}).get("enabled", False)
            return await msg.reply("📆 톡캘린더 연동을 다시 시도할게요 (30분마다)." if on else
                                   "톡캘린더 연동이 꺼져 있어요. `!set talkcal.enabled true` 후 `!restart` 하세요.")
        if text == "!queue":
            n = alerts.pending_count(self.store)
            return await msg.reply(f"📥 모아둔 알림 {n}개" + (" — `!flush`로 지금 받기" if n else ""))
        if text == "!flush":
            return await self.flush_queue("📥 모아둔 알림")
        if text.startswith("!place"):
            parts = text.split(maxsplit=2)
            if len(parts) < 3:
                saved = [f"{n}: {(self.store.get('place:' + n) or {}).get('name', '미등록')}" for n in commute.PLACES]
                return await msg.reply("📍 " + " · ".join(saved) + "\n등록: `!place 집 주소` / `!place 회사 주소`")
            return await msg.reply(await commute.set_place(self.store, parts[1], parts[2]))
        if text.startswith("!origin"):
            return await msg.reply(await commute.set_origin_today(self.store, datetime.now(self.tz), text.removeprefix("!origin")))
        if text == "!week":
            await msg.reply("🗓️ 한 주를 정리하고 있어요…")
            return await msg.channel.send(await self.run_weekly())
        if text == "!coupons":
            lines = coupons.lines(self.store, datetime.now(self.tz).date())
            return await msg.reply("👛 **기프티콘 지갑**\n" + "\n".join(lines) if lines else "지갑이 비어 있어요. 기프티콘 스크린샷을 보내면 저장돼요.")
        if text.startswith("!budget"):
            arg = text.removeprefix("!budget").strip()
            if arg in ("off", "on"):
                return await msg.reply(costs.set_override(self.store, self.cfg, off=(arg == "off")))
            return await msg.reply(costs.status(self.store, self.cfg))
        if text == "!pay":
            return await msg.reply(paysms.status(self.cfg, self.store, datetime.now(self.tz).date()))
        if text.startswith("!pay test"):
            body = text.removeprefix("!pay test").strip()
            return await msg.reply(paysms.preview(body) if body else "사용법: `!pay test 문자 내용` (기록하지 않고 어떻게 읽는지만 보여줘요)")
        if text == "!spend undo":
            try:
                return await msg.reply(await expenses.undo_last(self.store))
            except Exception as e:
                return await msg.reply(f"지우지 못했어요: {str(e)[:200]}")
        if text.startswith("!spend "):
            q = expenses.parse_quick(text[7:])
            if not q:
                return await msg.reply("사용법: `!spend 9000 점심 식비` (분류는 생략 가능: " + "·".join(expenses.CATEGORIES) + ")")
            try:
                return await msg.reply(await expenses.add(self.cfg, self.store, q["title"], q["amount"], q["category"]))
            except Exception as e:
                return await msg.reply(f"기록하지 못했어요: {str(e)[:200]}")
        if text == "!spend":
            today = datetime.now(self.tz).date()
            try:
                cur = expenses.summarize(await expenses.fetch(self.cfg, today.replace(day=1), today + timedelta(days=1)))
                return await msg.reply(expenses.report_text(f"{today:%m}월 지출 (오늘까지)", cur, expenses.summarize([])))
            except Exception as e:
                return await msg.reply(f"지출을 못 읽었어요: {str(e)[:200]}")
        if text == "!backup":
            await msg.reply("💾 백업 중…")
            return await msg.channel.send(await self.run_backup())
        if text == "!improve":
            if costs.save_mode(self.store, self.cfg):
                return await msg.reply("절약 모드라 자기 개선은 멈춰 있어요. (`!budget off`로 해제)")
            await msg.reply("🔧 지난 7일 기록을 분석하고 있어요 (1~3분)…")
            return await self.send_home(await self.run_improve(), channel_id=cid)
        if text == "!update":
            return await self.self_update(msg)
        if text.startswith("!playtest"):
            return await msg.reply(await self.start_playtest(text.removeprefix("!playtest").strip()))
        if text == "!renewals":
            lines = renewals.status_lines(self.cfg, self.store, datetime.now(self.tz).date())
            return await msg.reply("🔑 **재발급 일정**\n" + "\n".join(lines) if lines else "등록된 항목이 없어요 (config.yaml renewals.items).")
        if text.startswith("!renewed"):
            parts = text.split()
            if len(parts) < 2:
                return await msg.reply("사용법: `!renewed 키 [YYYY-MM-DD]` — 키는 `!renewals`에서 확인")
            return await msg.reply(renewals.mark_renewed(self.cfg, self.store, parts[1], parts[2] if len(parts) > 2 else "",
                                                         datetime.now(self.tz).date()))

        imgs = [a for a in msg.attachments if a.content_type in IMAGE_TYPES]
        if imgs and len(imgs) == len(msg.attachments) and (not text or text.startswith("!snap")):
            return await self.handle_screenshot(msg, imgs, text.removeprefix("!snap").strip(), cid)

        if not msg.attachments and readlater.is_link_only(text):
            async with msg.channel.typing():
                for u in readlater.urls_in(text):
                    try:
                        out = await readlater.save(self.claude, self.store, self.cfg, u)
                    except Exception as e:
                        out = f"저장 실패 ({u}): {type(e).__name__}: {str(e)[:200]}"
                    await msg.reply(out[:1900], mention_author=False, suppress_embeds=True)
            return

        improve.note_user_message(self.store, cid, text)
        lock = self.locks.setdefault(cid, asyncio.Lock())
        async with lock:
            await self.answer(msg, text, cid)

    async def handle_screenshot(self, msg: discord.Message, imgs: list, hint: str, cid: str):
        """사진만 온 메시지 → 스크린샷 비서 카드."""
        blocks = []
        for a in imgs[:4]:
            if a.size <= 5_000_000:
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": a.content_type,
                                                           "data": base64.b64encode(await a.read()).decode()}})
        if not blocks:
            return await msg.reply("이미지가 너무 커요 (5MB 이하).")
        async with msg.channel.typing():
            try:
                info = await screenshot.analyze(self.claude, self.store, self.cfg, blocks, hint)
            except Exception as e:
                return await msg.reply(f"스크린샷을 읽지 못했어요: {type(e).__name__}: {str(e)[:200]}")
        text = screenshot.card(info)
        if info.get("kind") == "coupon":            # 기프티콘은 원본 이미지와 함께 지갑에 자동 보관
            first = imgs[0]
            ext = (first.filename.rsplit(".", 1)[-1] if "." in first.filename else "png").lower()[:5]
            cpid, new = coupons.add(self.store, info, await first.read(), ext)
            info["_coupon_id"] = cpid
            text += f"\n👛 지갑에 저장했어요 (#{cpid})" if new else f"\n👛 이미 지갑에 있어요 (#{cpid})"
        await msg.reply(text, view=ScreenshotView(self, info, cid), mention_author=False)

    async def start_playtest(self, goal: str = "") -> str:
        pc = self.cfg.get("playtest") or {}
        if not pc.get("url"):
            return "config.yaml의 playtest.url이 비어 있어요."
        if costs.save_mode(self.store, self.cfg):
            return "절약 모드라 플레이테스트는 멈춰 있어요. (`!budget off`로 해제)"
        from .tools import dev
        inputs = {"url": pc["url"], "steps": str(pc.get("steps", 60)), "viewports": pc.get("viewports", "desktop,mobile")}
        if goal:
            inputs["goal"] = goal[:300]
        try:
            await dev.gh("POST", f"/repos/{pc['repo']}/actions/workflows/{pc['workflow']}/dispatches",
                         json={"ref": "main", "inputs": inputs})
        except Exception as e:
            return f"플레이테스트를 시작하지 못했어요: {str(e)[:300]}"
        return (f"🎮 플레이테스트 시작 (GitHub Actions, 10~20분). 끝나면 결과가 여기로 와요.\n"
                f"https://github.com/{pc['repo']}/actions/workflows/{pc['workflow']}")

    async def project_command(self, msg: discord.Message, arg: str, cid: str):
        if not arg:
            p = projects.get(self.store, cid)
            return await msg.reply(f"📁 이 대화는 **{p['name']}** ({p.get('repo') or '저장소 없음'})에 연결돼 있어요. 해제: `!project off`"
                                   if p else "연결된 프로젝트가 없어요. `!project 이름` (예: football-academy)")
        if arg == "off":
            projects.unbind(self.store, cid)
            return await msg.reply("프로젝트 연결을 해제했어요.")
        in_thread = isinstance(msg.channel, discord.Thread)
        if not in_thread and hasattr(msg, "create_thread") and not isinstance(msg.channel, discord.DMChannel):
            thread = await msg.create_thread(name=f"📁 {arg.split('/')[-1]}"[:90], auto_archive_duration=10080)
            p = projects.bind(self.store, str(thread.id), self.cfg, arg)
            await thread.send(f"📁 **{p['name']}** 스레드예요. 여기서는 {p.get('repo') or '이 프로젝트'} 기준으로 대화해요. "
                              "멘션 없이 말하면 돼요.")
            return
        p = projects.bind(self.store, cid, self.cfg, arg)
        return await msg.reply(f"📁 이 대화를 **{p['name']}**에 연결했어요" + (f" ({p['repo']})" if p.get("repo") else
                                                                     " (저장소를 못 찾았어요 — `!project 소유자/저장소`로 지정 가능)"))

    async def alert(self, text: str, level: str = "normal", info: dict | None = None):
        """모든 자동 알림의 입구: 급한 것은 바로, 나머지는 조용한 시간이면 모아둔다."""
        now = datetime.now(self.tz)
        if alerts.decide(self.cfg, level, now) == "queue":
            return alerts.enqueue(self.store, text, level, info)
        if info:
            ch = await self.home()
            return await ch.send(text[:1900], view=mail_view(self, info, str(ch.id)))
        await self.send_home(text)

    async def flush_queue(self, title: str = "📥 모아둔 알림", lead: str = "") -> None:
        items = alerts.drain(self.store)
        text, separate = alerts.digest(items, self.tz, title)
        body = "\n\n".join(x for x in (lead, text) if x)
        if body:
            await self.send_home(body)
        ch = await self.home() if separate else None
        for it in separate:
            await ch.send(it["text"][:1900], view=mail_view(self, it["info"], str(ch.id)))

    async def run_weekly(self) -> str:
        try:
            return await weekly.run(self.claude, self.store, self.cfg)
        except Exception as e:
            log.exception("주간 회고 실패")
            return f"🗓️ 주간 회고 실패: {type(e).__name__}: {str(e)[:200]}"

    async def run_backup(self) -> str:
        try:
            keep = (self.cfg.get("backup") or {}).get("keep", 8)
            return await asyncio.to_thread(backup.run, keep)
        except Exception as e:
            log.warning("백업 실패: %s", e)
            return f"💾 백업 실패: {type(e).__name__}: {str(e)[:300]}"

    async def run_improve(self) -> str:
        try:
            return await improve.run(self.claude, self.store, self.cfg)
        except Exception as e:
            log.exception("자기 개선 실패")
            return f"🔧 자기 개선 실패: {type(e).__name__}: {str(e)[:300]}"

    async def self_update(self, msg: discord.Message):
        """GitHub에서 최신 코드를 받고 재시작 (systemd가 Restart=always로 다시 띄움)."""
        view = ConfirmView(self.owner_id)
        await msg.reply("⬇️ GitHub main을 받아 의존성을 설치하고 재시작할까요?", view=view)
        try:
            ok = await asyncio.wait_for(view.result, timeout=300)
        except asyncio.TimeoutError:
            ok = False
        if not ok:
            return await msg.channel.send("취소했어요.")

        async def sh(*cmd):
            p = await asyncio.create_subprocess_exec(*cmd, cwd=str(ROOT), stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.STDOUT)
            out, _ = await p.communicate()
            return p.returncode, out.decode(errors="replace").strip()

        code, out = await sh("git", "pull", "--ff-only")
        if code != 0:
            return await msg.channel.send(f"git pull 실패:\n```\n{out[-1500:]}\n```")
        if "Already up to date" in out or "이미 업데이트" in out:
            return await msg.channel.send("이미 최신이에요.")
        code, pip_out = await sh(sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt")   # venv·Docker 어디서든 지금 실행 중인 파이썬에
        if code != 0:
            return await msg.channel.send(f"의존성 설치 실패 (재시작 안 함):\n```\n{pip_out[-1500:]}\n```")
        _, log_out = await sh("git", "log", "--oneline", "-5")
        await msg.channel.send(f"✅ 받았어요. 10초 뒤 다시 켜져요.\n```\n{log_out}\n```")
        await asyncio.sleep(1)
        os._exit(0)   # systemd가 재시작

    async def docclean(self, msg: discord.Message):
        """Files API에 남은, 서재가 안 쓰는 파일을 보여주고 버튼 확인 후 삭제."""
        files = await library.orphan_files(self.claude, self.store)
        if not files:
            return await msg.reply("정리할 파일이 없어요. (Files API에 남은 파일이 모두 서재에서 쓰는 중)")
        total = sum(getattr(f, "size_bytes", 0) or 0 for f in files)
        names = "\n".join(f"- {f.filename[:60]} ({(getattr(f, 'size_bytes', 0) or 0) / 1e6:.1f}MB)" for f in files[:15])
        more = f"\n…외 {len(files) - 15}개" if len(files) > 15 else ""
        view = ConfirmView(self.owner_id)
        await msg.reply(f"🧹 서재가 안 쓰는 업로드 파일 {len(files)}개 ({total / 1e6:.1f}MB):\n{names}{more}\n"
                        "같은 API 키를 쓰는 다른 프로그램의 파일이 섞여 있으면 취소하세요. 삭제할까요?", view=view)
        try:
            ok = await asyncio.wait_for(view.result, timeout=300)
        except asyncio.TimeoutError:
            ok = False
        if not ok:
            return await msg.channel.send("취소했어요.")
        n = await library.delete_files(self.claude, self.store, files)
        await msg.channel.send(f"{n}개 삭제했어요.")

    async def answer(self, msg: discord.Message, text: str, cid: str):
        content = await self.build_content(msg, text)
        status: list[discord.Message] = []

        async def notify(line: str):
            try:
                if status:
                    await status[0].edit(content=line)
                else:
                    status.append(await msg.channel.send(line))
            except discord.HTTPException:
                pass

        async def confirm(desc: str) -> bool:
            view = ConfirmView(self.owner_id)
            await msg.channel.send(f"**확인 필요**\n{desc}", view=view)
            return await view.result

        async def send_file(path: Path, caption: str = ""):
            await msg.channel.send(caption[:1900], file=discord.File(path))

        ctx = Context(cfg=self.cfg, store=self.store, channel_id=cid, confirm=confirm, notify=notify,
                      extras={"client": self.claude, "send_file": send_file})
        async with msg.channel.typing():
            try:
                reply, mode = await self.orch.handle(ctx, content, text)
            except anthropic.APIStatusError as e:
                reply, mode = f"Claude API 오류 ({e.status_code}): {str(e)[:300]}", "error"
            except Exception as e:
                log.exception("처리 실패")
                reply, mode = f"처리 중 오류가 났어요: {type(e).__name__}: {e}", "error"
        if status:
            try:
                await status[0].delete()
            except discord.HTTPException:
                pass
        parts = split_message(reply)
        cards = pages.card_links(self.cfg, reply)
        extra = {"view": CardView(self, cards)} if cards else {}     # 버튼은 마지막 조각에 붙인다
        await msg.reply(parts[0] + (" 🧠" if mode == "deep" else ""), mention_author=False, **(extra if len(parts) == 1 else {}))
        for i, p in enumerate(parts[1:], 2):
            await msg.channel.send(p, **(extra if i == len(parts) else {}))

    async def build_content(self, msg: discord.Message, text: str) -> list[dict]:
        blocks: list[dict] = []
        for a in msg.attachments:
            if a.content_type in IMAGE_TYPES and a.size <= 5_000_000:
                data = base64.b64encode(await a.read()).decode()
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": a.content_type, "data": data}})
            elif self._to_library(a):
                await msg.channel.send(f"📚 `{a.filename}` 서재에 저장 중…")
                try:
                    d = await library.add_document(self.claude, self.store, self.cfg, a.filename, await a.read(),
                                                   a.content_type or "")
                    note = (f"📚 서재 #{d['id']} **{d['title']}**" + (f" ({d['pages']}쪽)" if d["pages"] else "")
                            + (f"\n{d['summary'][:300]}" if d["summary"] else ""))
                    await msg.channel.send(note)
                    blocks.append({"type": "text", "text": f"[사용자가 문서를 올려 서재 #{d['id']} '{d['title']}'로 저장됨. "
                                                           f"내용 질문이면 library_ask(doc_ids=[{d['id']}])를 쓸 것]"})
                except Exception as e:
                    log.exception("서재 저장 실패")
                    blocks.append({"type": "text", "text": f"[첨부 {a.filename} 서재 저장 실패: {e}]"})
            elif Path(a.filename).suffix.lower() in TEXT_EXT and a.size <= 200_000:
                body = (await a.read()).decode("utf-8", "replace")
                blocks.append({"type": "text", "text": f"[첨부 파일: {a.filename}]\n{body}"})
            else:
                blocks.append({"type": "text", "text": f"[첨부 파일 {a.filename}은 형식이나 크기 때문에 읽지 못함]"})
        blocks.append({"type": "text", "text": text or "(첨부만 보냄)"})
        return blocks

    def _to_library(self, a: discord.Attachment) -> bool:
        """PDF는 항상, 텍스트는 크면(또는 '서재'라고 말하면) 서재로."""
        ext = Path(a.filename).suffix.lower()
        if ext == ".pdf" or a.content_type == "application/pdf":
            return True
        return ext in TEXT_EXT and a.size > self.cfg["library"]["text_inline_max"]

    def usage_report(self) -> str:
        since = datetime.now(self.tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        rows = self.store.usage_since(since)
        if not rows:
            return "오늘 사용량이 아직 없어요.\n" + costs.status(self.store, self.cfg)
        lines = ["**오늘 토큰 사용량** (입력 / 캐시읽기 / 캐시쓰기 / 출력 · 호출수)"]
        tot_in = tot_read = 0
        for model, role, b, i, cr, cw, o, n in rows:
            lines.append(f"`{model.replace('claude-', '')}` {role}{' (배치 50%)' if b else ''}: {i:,} / {cr:,} / {cw:,} / {o:,} · {n}")
            tot_in += i + cr + cw
            tot_read += cr
        if tot_in:
            lines.append(f"캐시 적중률: {tot_read / tot_in:.0%}")
        status = costs.status(self.store, self.cfg)
        body = "\n".join(lines)
        return (body[:1900 - len(status) - 1] + "\n" + status)

    # ───────── 백그라운드 ─────────
    async def send_home(self, text: str, file: Path | None = None, channel_id: str | None = None):
        ch = await (self.channel_by_id(channel_id) if channel_id else self.home())
        parts = split_message(text)
        for i, p in enumerate(parts):
            await ch.send(p, file=discord.File(file) if (file and i == len(parts) - 1 and file.exists()) else None)

    async def send_alert(self, text: str, info: dict | None = None, level: str = "normal"):
        await self.alert(text, level, info)

    @tasks.loop(seconds=120)
    async def mail_loop(self):
        try:
            await mailwatch.check(self.claude, self.store, self.cfg, self.send_alert)
        except Exception as e:
            log.warning("메일 확인 실패: %s", e)

    @tasks.loop(seconds=20)
    async def reminder_loop(self):
        now = time.time()
        for rid, due, text, cid, repeat in self.store.due_reminders(now):
            await self.send_home(f"⏰ **리마인더**: {text}", channel_id=cid)
            nxt = None
            if repeat:
                d = datetime.fromtimestamp(due, self.tz)
                step = {"daily": 1, "weekly": 7, "weekdays": 1}[repeat]
                d += timedelta(days=step)
                while repeat == "weekdays" and d.weekday() >= 5:
                    d += timedelta(days=1)
                nxt = d.timestamp()
            self.store.finish_reminder(rid, nxt)

    @tasks.loop(seconds=120)
    async def batch_loop(self):
        try:
            async def send(cid, text, path):
                await self.send_home(text, path, channel_id=cid)
            await batchmod.poll(self.claude, self.store, self.cfg, send)
        except Exception as e:
            log.warning("배치 확인 실패: %s", e)

    @tasks.loop(seconds=60)
    async def nightly_loop(self):
        now = datetime.now(self.tz)
        if now.strftime("%H:%M") < self.cfg["mail"]["nightly_digest"]:
            return
        if self.store.get("digest_date") == now.date().isoformat():
            return
        self.store.put("digest_date", now.date().isoformat())
        try:
            home = await self.home()
            bid = await batchmod.nightly_mail_digest(self.claude, self.store, self.cfg, str(home.id))
            if bid:
                log.info("야간 메일 요약 배치 제출: %s", bid)
        except Exception as e:
            log.warning("야간 요약 실패: %s", e)

    @tasks.loop(seconds=60)
    async def morning_loop(self):
        """통합 아침 브리핑 + 밤사이 모아둔 알림. config briefing.time (비우면 끔, 그래도 모아둔 알림은 그 시각에 보냄)."""
        bc = self.cfg.get("briefing") or {}
        at = bc.get("time") or (self.cfg.get("todo") or {}).get("morning_brief", "") or "07:47"
        now = datetime.now(self.tz)
        if now.strftime("%H:%M") < at or self.store.get("brief_date") == now.date().isoformat():
            return
        self.store.put("brief_date", now.date().isoformat())
        try:
            items = alerts.drain(self.store)
            queued, separate = alerts.digest(items, self.tz, "📥 밤사이 알림")
            if bc.get("enabled", True):
                await self.send_home(await briefing.build(self.cfg, self.store, now, queued))
            elif queued:
                await self.send_home(queued)
            if separate:
                ch = await self.home()
                for it in separate:
                    await ch.send(it["text"][:1900], view=mail_view(self, it["info"], str(ch.id)))
            for m in cycles.due_alerts(self.store, now.date()):
                await self.alert(m, "normal")
        except Exception as e:
            log.warning("아침 브리핑 실패: %s", e)

    @tasks.loop(seconds=60)
    async def renewal_loop(self):
        """하루 한 번: 토큰 상태 점검 + 재발급 일정 알림."""
        at = (self.cfg.get("renewals") or {}).get("check_time", "09:07")
        now = datetime.now(self.tz)
        if now.strftime("%H:%M") < at or self.store.get("renew_check_date") == now.date().isoformat():
            return
        self.store.put("renew_check_date", now.date().isoformat())
        try:
            problems = await renewals.health_check(self.cfg, self.store)
            if problems:
                await self.alert("🔑 **토큰 점검 실패**\n" + "\n".join(f"- {p}" for p in problems), "normal")
            for m in renewals.alerts(self.cfg, self.store, now.date()):
                await self.alert(m, "normal")
        except Exception as e:
            log.warning("재발급 점검 실패: %s", e)

    @tasks.loop(seconds=60)
    async def improve_loop(self):
        """매주 정해진 요일·시각에 자기 개선 분석 → PR."""
        sc = self.cfg.get("self_improve") or {}
        if not sc.get("enabled", True):
            return
        day, at = (sc.get("weekly") or "Sun 22:37").split()
        now = datetime.now(self.tz)
        if now.strftime("%a") != day or now.strftime("%H:%M") < at:
            return
        if self.store.get("improve_date") == now.date().isoformat():
            return
        self.store.put("improve_date", now.date().isoformat())
        if costs.save_mode(self.store, self.cfg):
            return
        await self.alert(await self.run_improve(), "normal")

    @tasks.loop(minutes=30)
    async def deploy_watch_loop(self):
        """playtest.auto가 켜져 있으면 게임 페이지가 바뀔 때(새 배포) 플레이테스트 자동 실행."""
        pc = self.cfg.get("playtest") or {}
        if not pc.get("auto") or not pc.get("url"):
            return
        try:
            import hashlib
            import httpx
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
                html = (await c.get(pc["url"])).text
            h = hashlib.sha256(html.encode()).hexdigest()
        except Exception as e:
            log.warning("배포 확인 실패: %s", e)
            return
        old = self.store.get("playtest_hash")
        self.store.put("playtest_hash", h)
        if old and old != h:
            await self.alert("🆕 게임 페이지가 바뀌었어요 (새 배포로 보임). " + await self.start_playtest(), "normal")

    def _due(self, key: str, spec: str, now: datetime) -> bool:
        """spec: 'Mon 08:47'(매주) · '1 08:52'(매월 그날) · '08:47'(매일). 해당 시각이 지났고 오늘 아직 안 했으면 True."""
        parts = spec.split()
        at = parts[-1]
        if len(parts) == 2:
            when = parts[0]
            if when.isdigit() and now.day != int(when):
                return False
            if not when.isdigit() and now.strftime("%a") != when:
                return False
        if now.strftime("%H:%M") < at or self.store.get(key) == now.date().isoformat():
            return False
        self.store.put(key, now.date().isoformat())
        return True

    @tasks.loop(seconds=60)
    async def schedule_loop(self):
        """정해진 시각 작업: 지출 리포트(주간·월간), 나중에 읽기 주간 목록, 예산 점검, 백업."""
        now = datetime.now(self.tz)
        ec, rc, bc = self.cfg.get("expenses") or {}, self.cfg.get("readlater") or {}, self.cfg.get("backup") or {}
        jobs = []
        if ec.get("weekly") and self._due("sched_exp_week", ec["weekly"], now):
            jobs.append(("주간 지출", expenses.weekly_report(self.cfg, now.date())))
        if ec.get("monthly") and self._due("sched_exp_month", ec["monthly"], now):
            jobs.append(("월간 지출", expenses.monthly_report(self.cfg, now.date())))
        if rc.get("weekly") and self._due("sched_read_week", rc["weekly"], now):
            jobs.append(("나중에 읽기", readlater.weekly_digest(self.cfg)))
        wr = self.cfg.get("weekly_review") or {}
        if wr.get("weekly") and self._due("sched_weekly_review", wr["weekly"], now):
            jobs.append(("주간 회고", self.run_weekly()))
        gb, yc, sc = self.cfg.get("birthdays") or {}, self.cfg.get("youtube") or {}, self.cfg.get("expense_sheet") or {}
        if gb.get("enabled", True) and gb.get("time") and self._due("sched_bday", gb["time"], now):
            jobs.append(("생일 확인", gextra.today_alert(self.store, now.date())))
        if yc.get("enabled", True) and yc.get("time") and self._due("sched_youtube", yc["time"], now):
            jobs.append(("유튜브 새 영상", gextra.youtube_digest(self.claude, self.store, self.cfg, now)))
        if sc.get("enabled", True) and sc.get("time") and self._due("sched_sheet", sc["time"], now):
            jobs.append(("지출 시트", gextra.sync_expense_sheet(self.cfg, self.store, now.date())))
        if bc.get("weekly") and self._due("sched_backup", bc["weekly"], now):
            jobs.append(("백업", self.run_backup()))
        for slot in (self.cfg.get("alerts") or {}).get("digest", ["12:30", "18:30"]):
            if self._due(f"sched_digest_{slot}", slot, now) and not alerts.is_quiet(self.cfg, now):
                await self.flush_queue()
        if self.store.get("budget_hour") != now.strftime("%Y%m%d%H"):   # 매시간 한 번 예산 점검
            self.store.put("budget_hour", now.strftime("%Y%m%d%H"))
            for m in costs.check(self.store, self.cfg, now):
                await self.alert(m, "normal")
            if pages.dash_key(self.store) and (self.cfg.get("pages") or {}).get("dash_hourly", True) and 7 <= now.hour <= 23:
                try:
                    await pages.publish_dash(self.cfg, self.store, now)
                except Exception as e:
                    log.info("대시보드 갱신 실패: %s", e)
        for name, coro in jobs:
            try:
                text = await coro
                if text:
                    ok = (name == "백업" and text.startswith("💾 백업 완료")) or name == "지출 시트"
                    await self.alert(text, "low" if ok else "normal")
            except Exception as e:
                log.warning("%s 실패: %s", name, e)
                await self.alert(f"⚠️ {name} 실패: {type(e).__name__}: {str(e)[:200]}", "normal")

    @tasks.loop(minutes=5)
    async def commute_loop(self):
        """장소가 있는 일정의 출발 알림."""
        cc = self.cfg.get("commute") or {}
        if not cc.get("enabled", True):
            return
        now = datetime.now(self.tz)
        try:
            events = await talkcal.all_events(self.cfg, self.store, now, now + timedelta(hours=cc.get("lookahead_hours", 4)))
            for a in await commute.due_alerts(self.cfg, self.store, events, now):
                ch = await self.home()
                await ch.send(a["text"][:1900], **({"view": OriginView(self, a["event"], a.get("dest"))} if a.get("event") else {}))
        except Exception as e:
            log.warning("출발 알림 실패: %s", e)

    @tasks.loop(minutes=30)
    async def talkcal_loop(self):
        """구글 캘린더 → 카카오 톡캘린더 (앞으로 2주)."""
        tc = self.cfg.get("talkcal") or {}
        if not tc.get("enabled", False) or self.store.get("talkcal_paused"):
            return
        now = datetime.now(self.tz)
        try:
            from .tools import google
            if not google.TOKEN.exists():
                return
            events = await asyncio.to_thread(google.list_events, now, now + timedelta(days=tc.get("days", 14)))
            r = await talkcal.sync(self.cfg, self.store, events, now)
            if r["created"] or r["deleted"]:
                log.info("톡캘린더: +%d -%d", r["created"], r["deleted"])
        except Exception as e:
            log.warning("톡캘린더 동기화 실패: %s", e)
            if any(x in str(e) for x in ("403", "insufficient", "scope", "-402")):
                self.store.put("talkcal_paused", True)
                await self.alert("📆 톡캘린더 연동을 멈췄어요: 카카오 권한(talk_calendar)이 없어요.\n"
                                 "동의항목을 켜고 VM에서 `deploy/kakao_auth.py`를 실행한 뒤 `!talkcal on` 하세요.", "normal")

    @tasks.loop(minutes=10)
    async def warning_loop(self):
        """부산 기상 특보가 새로 뜨거나 풀리면 바로 알림."""
        bc = self.cfg.get("busan") or {}
        if not bc.get("warnings", True) or not os.environ.get("DATA_GO_KR_KEY"):
            return
        try:
            m = await busan.new_warnings(self.store, bc.get("region", "부산"))
            if m:
                await self.alert(m, "urgent")
        except Exception as e:
            log.info("특보 확인 실패: %s", e)

    @tasks.loop(minutes=5)
    async def heartbeat_loop(self):
        await heartbeat.ping()

    @mail_loop.before_loop
    @talkcal_loop.before_loop
    @warning_loop.before_loop
    @commute_loop.before_loop
    @heartbeat_loop.before_loop
    @schedule_loop.before_loop
    @deploy_watch_loop.before_loop
    @improve_loop.before_loop
    @renewal_loop.before_loop
    @morning_loop.before_loop
    @reminder_loop.before_loop
    @batch_loop.before_loop
    @nightly_loop.before_loop
    async def _wait_ready(self):
        await self.wait_until_ready()


def main():
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "httpcore", "googleapiclient.discovery"):  # 요청 URL(캘린더 비공개 주소 등)이 로그에 남지 않게
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = settings.load(ROOT)          # config.yaml + config.local.yaml(VM 전용)
    Assistant(cfg).run(os.environ["DISCORD_TOKEN"], log_handler=None)


if __name__ == "__main__":
    main()
