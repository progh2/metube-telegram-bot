"""
MeTube 텔레그램 봇
-----------------
텔레그램으로 유튜브(또는 yt-dlp가 지원하는 사이트) 링크를 보내면
"영상(최고화질) / MP3(최고음질)" 버튼을 보여주고,
선택한 형식으로 시놀로지의 MeTube에 다운로드를 요청한다.

환경 변수:
  BOT_TOKEN         : BotFather에서 발급받은 봇 토큰 (필수)
  METUBE_URL        : MeTube 주소 (예: http://YOUR_NAS_IP:8081)
  ALLOWED_CHAT_IDS  : 허용할 텔레그램 챗 ID (쉼표로 여러 개, 비우면 아무나 사용 가능 - 비추천)
"""

import asyncio
import logging
import os
import re
import uuid
from typing import NamedTuple

import requests
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("metube-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
METUBE_URL = os.environ.get("METUBE_URL", "http://localhost:8081").rstrip("/")
ALLOWED_CHAT_IDS = {
    int(x)
    for x in os.environ.get("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",")
    if x
}

URL_RE = re.compile(r"(https?://\S+)")

# callback_data는 64바이트 제한이 있어 URL을 직접 못 넣는다.
# 짧은 ID를 발급해 메모리에 URL을 보관한다. (봇 재시작 시 대기 중이던 버튼은 무효화됨)
pending: dict[str, str] = {}

LABELS = {
    "mp4": "🎬 영상 (최고화질)",
    "mp3": "🎵 MP3 (최고음질)",
    "both": "🎬 영상 + 🎵 MP3",
}

# MeTube는 대기열 중복을 URL만으로 판정한다(format은 키에 포함되지 않는다).
# 그래서 같은 링크를 mp4/mp3로 연달아 요청하면 두 번째는 조용히 버려지고
# {"status": "ok", "msg": "Already in queue: ..."} 가 HTTP 200으로 돌아온다.
DUPLICATE_HINT = "already in queue"

# 완료된 항목(done)은 중복 검사 대상이 아니므로, 앞선 다운로드가 대기열을 떠나면
# 같은 URL을 다른 형식으로 다시 등록할 수 있다. "둘 다"는 이 성질을 이용해
# 남은 형식을 백그라운드에서 재시도한다. (재시도마다 MeTube가 메타데이터를
# 다시 긁으므로 간격을 점점 늘려 과도한 요청을 피한다)
FOLLOWUP_FIRST_DELAY = 15  # 첫 재시도까지 대기(초)
FOLLOWUP_MAX_DELAY = 300  # 재시도 간격 상한(초)
FOLLOWUP_BACKOFF = 1.5  # 간격 증가 배수
FOLLOWUP_DEADLINE = 2 * 60 * 60  # 총 재시도 시한(초)


class AddResult(NamedTuple):
    """MeTube /add 요청 결과.

    ok=True, duplicate=True는 "요청은 받아들여졌지만 이미 대기열에 있어
    새로 등록된 것은 없다"는 뜻이다. 성공으로 표시하면 안 된다.
    """

    ok: bool
    duplicate: bool
    msg: str


def allowed(chat_id: int) -> bool:
    return not ALLOWED_CHAT_IDS or chat_id in ALLOWED_CHAT_IDS


def metube_add(url: str, fmt: str) -> AddResult:
    """MeTube에 다운로드 요청. fmt: 'mp4'(영상) 또는 'mp3'(음원)."""
    try:
        r = requests.post(
            f"{METUBE_URL}/add",
            json={"url": url, "quality": "best", "format": fmt, "auto_start": True},
            timeout=30,
        )
        try:
            data = r.json()
        except ValueError:
            data = {}
        msg = str(data.get("msg") or "")
        log.info("metube response %s: %s", r.status_code, r.text[:200])
        # HTTP 200이고 명시적 error가 아니면 요청은 받아들여진 것.
        # 다만 중복이면 실제로 등록된 항목이 없으므로 따로 구분한다.
        if r.ok and data.get("status") != "error":
            return AddResult(True, DUPLICATE_HINT in msg.lower(), msg)
        return AddResult(False, False, msg or f"HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        return AddResult(False, False, str(e))


async def metube_add_async(url: str, fmt: str) -> AddResult:
    """metube_add를 별도 스레드에서 실행 (이벤트 루프를 막지 않도록)."""
    return await asyncio.to_thread(metube_add, url, fmt)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "안녕하세요! 유튜브 링크를 보내주시면 NAS로 다운로드해 드립니다.\n"
        "링크를 보낸 뒤 🎬 영상 / 🎵 MP3 버튼을 눌러 형식을 선택하세요.\n\n"
        "내 챗 ID 확인: /id"
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"이 대화의 챗 ID: {update.effective_chat.id}")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not allowed(chat_id):
        await update.message.reply_text(
            f"허용되지 않은 사용자입니다. (챗 ID: {chat_id})\n"
            "NAS 관리자가 ALLOWED_CHAT_IDS에 이 ID를 추가해야 사용할 수 있어요."
        )
        return

    urls = URL_RE.findall(update.message.text or "")
    if not urls:
        await update.message.reply_text("링크를 찾지 못했어요. http(s)로 시작하는 주소를 보내주세요.")
        return

    for url in urls:
        token = uuid.uuid4().hex[:10]
        pending[token] = url
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(LABELS["mp4"], callback_data=f"mp4:{token}"),
                    InlineKeyboardButton(LABELS["mp3"], callback_data=f"mp3:{token}"),
                ],
                [
                    InlineKeyboardButton("🎬+🎵 둘 다", callback_data=f"both:{token}"),
                ],
            ]
        )
        await update.message.reply_text(
            f"어떤 형식으로 받을까요?\n{url}", reply_markup=keyboard
        )


async def edit_text(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, text: str) -> None:
    """메시지 수정. 원본이 지워졌거나 내용이 같으면 조용히 넘어간다."""
    try:
        await context.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
    except Exception as e:  # noqa: BLE001
        log.warning("failed to edit message %s: %s", message_id, e)


async def queue_followup(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    url: str,
    fmt: str,
    done_label: str,
) -> None:
    """앞선 다운로드가 대기열을 떠나면 남은 형식을 등록한다.

    MeTube가 같은 URL을 동시에 두 번 받지 못하므로, 중복이 아니게 될 때까지
    간격을 늘려가며 재시도한다.
    """
    delay = FOLLOWUP_FIRST_DELAY
    waited = 0.0
    last_msg = ""

    while waited < FOLLOWUP_DEADLINE:
        await asyncio.sleep(delay)
        waited += delay
        delay = min(delay * FOLLOWUP_BACKOFF, FOLLOWUP_MAX_DELAY)

        result = await metube_add_async(url, fmt)
        if result.ok and not result.duplicate:
            log.info("followup queued %s as %s (after %.0fs)", url, fmt, waited)
            await edit_text(
                context,
                chat_id,
                message_id,
                f"✅ 둘 다 요청 완료 · {done_label}\n{url}",
            )
            return

        last_msg = result.msg
        if not result.ok:
            log.warning("followup attempt failed for %s (%s): %s", url, fmt, result.msg)

    log.warning("followup gave up for %s as %s: %s", url, fmt, last_msg)
    await edit_text(
        context,
        chat_id,
        message_id,
        f"⚠️ 영상을 추가하지 못했어요 · 링크를 다시 보내 주세요\n{url}",
    )


async def handle_single(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, url: str, fmt: str
) -> None:
    result = await metube_add_async(url, fmt)
    label = LABELS[fmt]

    if not result.ok:
        log.warning("failed to queue %s as %s: %s", url, fmt, result.msg)
        await edit_text(
            context, chat_id, message_id, f"❌ 요청 실패 · {result.msg}\n{url}"
        )
        return

    if result.duplicate:
        log.info("already queued %s as %s", url, fmt)
        await edit_text(
            context, chat_id, message_id, f"ℹ️ 이미 대기열에 있어요 · {label}\n{url}"
        )
        return

    log.info("queued %s as %s", url, fmt)
    await edit_text(context, chat_id, message_id, f"✅ 요청 완료 · {label}\n{url}")


async def handle_both(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, url: str
) -> None:
    """영상과 MP3를 모두 등록한다.

    MeTube가 같은 URL을 동시에 두 번 받지 못하므로 한 번에 둘 다 넣을 수 없다.
    음원이 영상보다 먼저 끝나므로 MP3를 먼저 넣고, 영상은 백그라운드에서 이어 넣는다.
    """
    first, second = "mp3", "mp4"
    result = await metube_add_async(url, first)

    if not result.ok:
        log.warning("failed to queue %s as %s: %s", url, first, result.msg)
        await edit_text(
            context, chat_id, message_id, f"❌ 요청 실패 · {result.msg}\n{url}"
        )
        return

    if result.duplicate:
        head = f"ℹ️ 이미 대기열에 있어요 · {LABELS[first]}"
    else:
        log.info("queued %s as %s", url, first)
        head = f"✅ 요청 완료 · {LABELS[first]}"

    await edit_text(
        context,
        chat_id,
        message_id,
        f"{head}\n⏳ 영상은 이어서 자동으로 등록할게요\n{url}",
    )

    context.application.create_task(
        queue_followup(context, chat_id, message_id, url, second, LABELS["both"])
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not allowed(update.effective_chat.id):
        await query.edit_message_text("허용되지 않은 사용자입니다.")
        return

    try:
        fmt, token = query.data.split(":", 1)
    except ValueError:
        return

    url = pending.pop(token, None)
    if url is None:
        await query.edit_message_text("요청이 만료되었어요. 링크를 다시 보내주세요.")
        return

    chat_id = query.message.chat_id
    message_id = query.message.message_id

    if fmt == "both":
        await handle_both(context, chat_id, message_id, url)
    elif fmt in ("mp4", "mp3"):
        await handle_single(context, chat_id, message_id, url, fmt)


def build_app() -> Application:
    # 메시지에 URL이 있으면 텔레그램이 썸네일·설명 미리보기를 자동으로 붙인다.
    # 링크마다 카드가 반복되면 대화창이 번잡해지므로 봇이 보내는 모든 메시지에서 끈다.
    # (사용자가 직접 보낸 링크의 미리보기는 봇이 제어할 수 없다)
    defaults = Defaults(link_preview_options=LinkPreviewOptions(is_disabled=True))
    app = Application.builder().token(BOT_TOKEN).defaults(defaults).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_button))
    return app


def main() -> None:
    app = build_app()
    log.info("bot started. MeTube: %s / allowed: %s", METUBE_URL, ALLOWED_CHAT_IDS or "everyone")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
