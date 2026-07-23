"""
MeTube 텔레그램 봇
-----------------
텔레그램으로 유튜브(또는 yt-dlp가 지원하는 사이트) 링크를 보내면
"영상(최고화질) / MP3(최고음질)" 버튼을 보여주고,
선택한 형식으로 시놀로지의 MeTube에 다운로드를 요청한다.

환경 변수:
  BOT_TOKEN         : BotFather에서 발급받은 봇 토큰 (필수)
  METUBE_URL        : MeTube 주소 (예: http://192.168.219.253:8081)
  ALLOWED_CHAT_IDS  : 허용할 텔레그램 챗 ID (쉼표로 여러 개, 비우면 아무나 사용 가능 - 비추천)
"""

import logging
import os
import re
import uuid

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
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


def allowed(chat_id: int) -> bool:
    return not ALLOWED_CHAT_IDS or chat_id in ALLOWED_CHAT_IDS


def metube_add(url: str, fmt: str) -> tuple[bool, str]:
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
        log.info("metube response %s: %s", r.status_code, r.text[:200])
        # HTTP 200이고 명시적 error가 아니면 성공으로 간주
        if r.ok and data.get("status") != "error":
            return True, ""
        return False, data.get("msg") or f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


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
                    InlineKeyboardButton("🎬 영상 (최고화질)", callback_data=f"mp4:{token}"),
                    InlineKeyboardButton("🎵 MP3 (최고음질)", callback_data=f"mp3:{token}"),
                ],
                [
                    InlineKeyboardButton("🎬+🎵 둘 다", callback_data=f"both:{token}"),
                ],
            ]
        )
        await update.message.reply_text(
            f"어떤 형식으로 받을까요?\n{url}", reply_markup=keyboard
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

    labels = {
        "mp4": "🎬 영상 (최고화질)",
        "mp3": "🎵 MP3 (최고음질)",
        "both": "🎬 영상 + 🎵 MP3",
    }
    label = labels.get(fmt, fmt)
    formats = ["mp4", "mp3"] if fmt == "both" else [fmt]

    errors = []
    for f in formats:
        ok, err = metube_add(url, f)
        if not ok:
            errors.append(f"{f}: {err}")

    if not errors:
        await query.edit_message_text(
            f"✅ MeTube에 요청했어요! [{label}]\n{url}\n\n"
            "다운로드가 끝나면 NAS 저장 폴더에서 확인할 수 있어요."
        )
        log.info("queued %s as %s", url, fmt)
    else:
        await query.edit_message_text(
            f"❌ 요청 실패: {'; '.join(errors)}\n{url}\n\n"
            "MeTube가 실행 중인지, METUBE_URL 설정이 맞는지 확인해 주세요."
        )
        log.warning("failed to queue %s: %s", url, errors)


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_button))
    log.info("bot started. MeTube: %s / allowed: %s", METUBE_URL, ALLOWED_CHAT_IDS or "everyone")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
