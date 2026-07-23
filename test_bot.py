"""
bot.py 테스트
------------
표준 라이브러리 unittest만 사용한다 (추가 의존성 없음).

    python -m unittest -v

MeTube와 텔레그램은 가짜 객체로 대체한다. 특히 FakeMeTube는 실제 MeTube의
"대기열 중복을 URL만으로 판정한다"는 동작을 그대로 흉내 내므로,
'둘 다' 회귀(이슈 #1)를 실제 서버 없이 검증할 수 있다.
"""

import asyncio
import os
import types
import unittest
from unittest.mock import patch

os.environ.setdefault("BOT_TOKEN", "123:dummy")
os.environ.setdefault("METUBE_URL", "http://metube.test:8081")

import bot  # noqa: E402

URL = "https://youtu.be/abc123"
OTHER_URL = "https://youtu.be/xyz789"


# ─────────────────────────────── 가짜 객체 ───────────────────────────────


class FakeMeTube:
    """MeTube 흉내.

    실제 MeTube(app/ytdl.py)는 대기열 중복을 URL만으로 판정하며 format은 키에 넣지 않는다.
    중복이어도 HTTP 200 + status:"ok" 로 응답하고 msg에만 "Already in queue"를 담는다.
    완료(done)된 항목은 중복 검사 대상이 아니다.
    """

    def __init__(self, fail_status=None, raises=None, bad_json=False):
        self.queue = {}  # url -> fmt (대기/진행 중)
        self.accepted = []  # 실제로 등록된 (url, fmt)
        self.calls = 0
        self.fail_status = fail_status
        self.raises = raises
        self.bad_json = bad_json

    def finish(self, url):
        """다운로드 완료 → queue에서 done으로 이동."""
        self.queue.pop(url, None)

    def post(self, endpoint, json=None, timeout=None):
        self.calls += 1
        if self.raises:
            raise self.raises

        assert endpoint.endswith("/add"), endpoint
        url, fmt = json["url"], json["format"]

        resp = types.SimpleNamespace()
        if self.fail_status:
            resp.status_code, resp.ok = self.fail_status, False
            body = {"status": "error", "msg": "boom"}
        elif url in self.queue:
            resp.status_code, resp.ok = 200, True
            body = {"status": "ok", "msg": f"Already in queue: {url}"}
        else:
            self.queue[url] = fmt
            self.accepted.append((url, fmt))
            resp.status_code, resp.ok = 200, True
            body = {"status": "ok"}

        resp.text = str(body)
        if self.bad_json:
            def raise_value_error():
                raise ValueError("not json")
            resp.json = raise_value_error
        else:
            resp.json = lambda: body
        return resp


class FakeBot:
    def __init__(self):
        self.texts = []

    async def edit_message_text(self, text, chat_id=None, message_id=None):
        self.texts.append(text)

    @property
    def last(self):
        return self.texts[-1] if self.texts else ""


class FakeApplication:
    def __init__(self):
        self.tasks = []

    def create_task(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task


class FakeContext:
    def __init__(self):
        self.bot = FakeBot()
        self.application = FakeApplication()

    async def drain(self):
        """예약된 백그라운드 작업이 끝날 때까지 기다린다."""
        if self.tasks:
            await asyncio.wait_for(asyncio.gather(*self.tasks), timeout=10)

    @property
    def tasks(self):
        return self.application.tasks


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))


class FakeUpdate:
    def __init__(self, text, chat_id=1):
        self.message = FakeMessage(text)
        self.effective_chat = types.SimpleNamespace(id=chat_id)


class FakeQuery:
    def __init__(self, data, message_id=10, chat_id=1):
        self.data = data
        self.message = types.SimpleNamespace(chat_id=chat_id, message_id=message_id)
        self.answered = False
        self.edits = []

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, text):
        self.edits.append(text)


class FakeCallbackUpdate:
    def __init__(self, query, chat_id=1):
        self.callback_query = query
        self.effective_chat = types.SimpleNamespace(id=chat_id)


class BotTestCase(unittest.IsolatedAsyncioTestCase):
    """MeTube를 가짜로 바꾸고 전역 상태를 초기화한다."""

    def setUp(self):
        self.metube = FakeMeTube()
        patcher = patch.object(bot.requests, "post", side_effect=self._post)
        patcher.start()
        self.addCleanup(patcher.stop)
        bot.pending.clear()
        self.addCleanup(bot.pending.clear)

    def _post(self, *args, **kwargs):
        return self.metube.post(*args, **kwargs)

    def fast_followup(self, deadline=5):
        """후속 등록 재시도를 짧은 간격으로 돌린다."""
        for name, value in (
            ("FOLLOWUP_FIRST_DELAY", 0.01),
            ("FOLLOWUP_MAX_DELAY", 0.01),
            ("FOLLOWUP_BACKOFF", 1.0),
            ("FOLLOWUP_DEADLINE", deadline),
        ):
            p = patch.object(bot, name, value)
            p.start()
            self.addCleanup(p.stop)


# ─────────────────────────────── metube_add ───────────────────────────────


class TestMetubeAdd(BotTestCase):
    def test_새_등록은_성공(self):
        self.assertEqual(bot.metube_add(URL, "mp4"), bot.AddResult(True, False, ""))
        self.assertEqual(self.metube.accepted, [(URL, "mp4")])

    def test_중복은_성공이_아니라_duplicate로_표시(self):
        """이슈 #1: 중복 응답은 HTTP 200 + status ok라 성공으로 오판하기 쉽다."""
        bot.metube_add(URL, "mp4")
        result = bot.metube_add(URL, "mp3")

        self.assertTrue(result.ok)
        self.assertTrue(result.duplicate)
        self.assertIn("Already in queue", result.msg)
        self.assertEqual(self.metube.accepted, [(URL, "mp4")], "두 번째는 등록되지 않는다")

    def test_format이_달라도_같은_URL이면_중복(self):
        bot.metube_add(URL, "mp3")
        self.assertTrue(bot.metube_add(URL, "mp4").duplicate)

    def test_다른_URL은_중복이_아님(self):
        bot.metube_add(URL, "mp4")
        self.assertFalse(bot.metube_add(OTHER_URL, "mp4").duplicate)

    def test_완료된_항목은_다시_등록_가능(self):
        bot.metube_add(URL, "mp3")
        self.metube.finish(URL)
        result = bot.metube_add(URL, "mp4")

        self.assertFalse(result.duplicate)
        self.assertEqual(self.metube.accepted, [(URL, "mp3"), (URL, "mp4")])

    def test_HTTP_오류는_실패(self):
        self.metube = FakeMeTube(fail_status=500)
        result = bot.metube_add(URL, "mp4")

        self.assertFalse(result.ok)
        self.assertEqual(result.msg, "boom")

    def test_연결_실패는_실패로_처리(self):
        self.metube = FakeMeTube(raises=OSError("Connection refused"))
        result = bot.metube_add(URL, "mp4")

        self.assertFalse(result.ok)
        self.assertIn("Connection refused", result.msg)

    def test_JSON이_아닌_응답도_200이면_성공(self):
        """content-type을 신뢰하지 않는다 — 과거 오판 버그의 원인."""
        self.metube = FakeMeTube(bad_json=True)
        self.assertTrue(bot.metube_add(URL, "mp4").ok)

    def test_요청_본문(self):
        captured = {}

        def capture(endpoint, json=None, timeout=None):
            captured.update(endpoint=endpoint, json=json, timeout=timeout)
            return self.metube.post(endpoint, json=json, timeout=timeout)

        with patch.object(bot.requests, "post", side_effect=capture):
            bot.metube_add(URL, "mp3")

        self.assertEqual(captured["endpoint"], "http://metube.test:8081/add")
        self.assertEqual(
            captured["json"],
            {"url": URL, "quality": "best", "format": "mp3", "auto_start": True},
        )
        self.assertEqual(captured["timeout"], 30)


# ─────────────────────────────── 접근 제어 ───────────────────────────────


class TestAllowed(unittest.TestCase):
    def test_목록이_비면_전체_허용(self):
        with patch.object(bot, "ALLOWED_CHAT_IDS", set()):
            self.assertTrue(bot.allowed(999))

    def test_목록에_있으면_허용(self):
        with patch.object(bot, "ALLOWED_CHAT_IDS", {1, 2}):
            self.assertTrue(bot.allowed(2))

    def test_목록에_없으면_거부(self):
        with patch.object(bot, "ALLOWED_CHAT_IDS", {1, 2}):
            self.assertFalse(bot.allowed(3))


# ─────────────────────────────── on_message ───────────────────────────────


class TestOnMessage(BotTestCase):
    async def test_URL마다_버튼_메시지(self):
        update = FakeUpdate(f"이것 좀 {URL} 그리고 {OTHER_URL}")
        await bot.on_message(update, FakeContext())

        self.assertEqual(len(update.message.replies), 2)
        self.assertEqual(len(bot.pending), 2, "URL마다 토큰이 발급된다")
        self.assertEqual(set(bot.pending.values()), {URL, OTHER_URL})

    async def test_버튼은_세_종류(self):
        update = FakeUpdate(URL)
        await bot.on_message(update, FakeContext())

        _, markup = update.message.replies[0]
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertEqual([d.split(":")[0] for d in data], ["mp4", "mp3", "both"])

    async def test_callback_data는_64바이트_이하(self):
        """텔레그램 제한. URL을 직접 넣지 않고 토큰을 쓰는 이유."""
        update = FakeUpdate("https://example.com/" + "x" * 300)
        await bot.on_message(update, FakeContext())

        _, markup = update.message.replies[0]
        for row in markup.inline_keyboard:
            for button in row:
                self.assertLessEqual(len(button.callback_data.encode()), 64)

    async def test_URL이_없으면_안내(self):
        update = FakeUpdate("안녕하세요")
        await bot.on_message(update, FakeContext())

        self.assertIn("링크를 찾지 못했어요", update.message.replies[0][0])
        self.assertEqual(bot.pending, {})

    async def test_허용되지_않은_사용자는_거부(self):
        update = FakeUpdate(URL, chat_id=999)
        with patch.object(bot, "ALLOWED_CHAT_IDS", {1}):
            await bot.on_message(update, FakeContext())

        self.assertIn("허용되지 않은", update.message.replies[0][0])
        self.assertEqual(bot.pending, {}, "거부된 요청은 토큰을 만들지 않는다")


# ─────────────────────────────── 단일 형식 ───────────────────────────────


class TestHandleSingle(BotTestCase):
    async def test_성공(self):
        ctx = FakeContext()
        await bot.handle_single(ctx, 1, 10, URL, "mp4")

        self.assertTrue(ctx.bot.last.startswith("✅"))
        self.assertIn(URL, ctx.bot.last)
        self.assertEqual(self.metube.accepted, [(URL, "mp4")])

    async def test_중복이면_성공으로_표시하지_않음(self):
        ctx = FakeContext()
        await bot.handle_single(ctx, 1, 10, URL, "mp4")
        await bot.handle_single(ctx, 1, 11, URL, "mp3")

        self.assertTrue(ctx.bot.last.startswith("ℹ️"), ctx.bot.last)
        self.assertIn("이미 대기열에", ctx.bot.last)
        self.assertEqual(self.metube.accepted, [(URL, "mp4")])

    async def test_실패는_오류_표시(self):
        self.metube = FakeMeTube(fail_status=500)
        ctx = FakeContext()
        await bot.handle_single(ctx, 1, 10, URL, "mp4")

        self.assertTrue(ctx.bot.last.startswith("❌"), ctx.bot.last)
        self.assertIn("boom", ctx.bot.last)

    async def test_응답은_간결하게(self):
        """이슈 #2: 매번 반복되는 안내 문구 없이 2줄."""
        ctx = FakeContext()
        await bot.handle_single(ctx, 1, 10, URL, "mp3")

        self.assertEqual(len(ctx.bot.last.splitlines()), 2, ctx.bot.last)


# ─────────────────────────────── 둘 다 ───────────────────────────────


class TestHandleBoth(BotTestCase):
    async def test_MP3를_먼저_등록(self):
        """음원이 영상보다 빨리 끝나므로 두 번째 항목의 대기가 짧아진다."""
        self.fast_followup()
        ctx = FakeContext()
        await bot.handle_both(ctx, 1, 10, URL)

        self.assertEqual(self.metube.accepted, [(URL, "mp3")])
        await asyncio.sleep(0.05)
        self.metube.finish(URL)
        await ctx.drain()

    async def test_앞선_다운로드가_끝나면_영상이_자동_등록(self):
        """이슈 #1 회귀: 둘 다를 골랐으면 결국 둘 다 등록되어야 한다."""
        self.fast_followup()
        ctx = FakeContext()
        await bot.handle_both(ctx, 1, 10, URL)

        await asyncio.sleep(0.05)
        self.assertEqual(self.metube.accepted, [(URL, "mp3")], "MP3 처리 중엔 추가 안 됨")

        self.metube.finish(URL)
        await ctx.drain()

        self.assertEqual(self.metube.accepted, [(URL, "mp3"), (URL, "mp4")])
        self.assertIn("둘 다 요청 완료", ctx.bot.last)

    async def test_첫_안내에_이어서_등록한다고_알림(self):
        self.fast_followup()
        ctx = FakeContext()
        await bot.handle_both(ctx, 1, 10, URL)

        first = ctx.bot.texts[0]
        self.assertIn("이어서", first)
        self.assertLessEqual(len(first.splitlines()), 3, first)

        self.metube.finish(URL)
        await ctx.drain()

    async def test_시한을_넘기면_안내하고_포기(self):
        self.fast_followup(deadline=0.05)
        ctx = FakeContext()
        await bot.handle_both(ctx, 1, 10, URL)
        await ctx.drain()  # MP3를 끝내지 않아 영상은 계속 중복

        self.assertTrue(ctx.bot.last.startswith("⚠️"), ctx.bot.last)
        self.assertEqual(self.metube.accepted, [(URL, "mp3")])

    async def test_첫_요청이_실패하면_후속을_예약하지_않음(self):
        self.metube = FakeMeTube(fail_status=500)
        ctx = FakeContext()
        await bot.handle_both(ctx, 1, 10, URL)

        self.assertTrue(ctx.bot.last.startswith("❌"), ctx.bot.last)
        self.assertEqual(ctx.tasks, [])

    async def test_이미_대기열에_있어도_영상은_이어서_등록(self):
        self.fast_followup()
        bot.metube_add(URL, "mp3")  # 사용자가 미리 등록해 둔 상황
        ctx = FakeContext()
        await bot.handle_both(ctx, 1, 10, URL)

        self.assertTrue(ctx.bot.texts[0].startswith("ℹ️"), ctx.bot.texts[0])
        self.assertEqual(len(ctx.tasks), 1, "그래도 영상 후속 등록은 예약된다")

        self.metube.finish(URL)
        await ctx.drain()
        self.assertEqual(self.metube.accepted, [(URL, "mp3"), (URL, "mp4")])


# ─────────────────────────────── on_button ───────────────────────────────


class TestOnButton(BotTestCase):
    async def test_토큰이_만료되면_안내(self):
        query = FakeQuery("mp4:없는토큰")
        await bot.on_button(FakeCallbackUpdate(query), FakeContext())

        self.assertIn("만료", query.edits[0])
        self.assertEqual(self.metube.calls, 0)

    async def test_토큰은_한_번만_사용_가능(self):
        bot.pending["tok"] = URL
        ctx = FakeContext()

        await bot.on_button(FakeCallbackUpdate(FakeQuery("mp4:tok")), ctx)
        self.assertNotIn("tok", bot.pending)

        query = FakeQuery("mp4:tok")
        await bot.on_button(FakeCallbackUpdate(query), ctx)
        self.assertIn("만료", query.edits[0])

    async def test_형식별로_요청(self):
        for fmt in ("mp4", "mp3"):
            with self.subTest(fmt=fmt):
                self.metube = FakeMeTube()
                bot.pending["tok"] = URL
                await bot.on_button(FakeCallbackUpdate(FakeQuery(f"{fmt}:tok")), FakeContext())
                self.assertEqual(self.metube.accepted, [(URL, fmt)])

    async def test_허용되지_않은_사용자는_거부(self):
        bot.pending["tok"] = URL
        query = FakeQuery("mp4:tok")
        with patch.object(bot, "ALLOWED_CHAT_IDS", {1}):
            await bot.on_button(FakeCallbackUpdate(query, chat_id=999), FakeContext())

        self.assertIn("허용되지 않은", query.edits[0])
        self.assertEqual(self.metube.calls, 0)

    async def test_잘못된_callback_data는_무시(self):
        query = FakeQuery("형식없음")
        await bot.on_button(FakeCallbackUpdate(query), FakeContext())

        self.assertEqual(query.edits, [])
        self.assertEqual(self.metube.calls, 0)

    async def test_로딩_표시를_해제한다(self):
        bot.pending["tok"] = URL
        query = FakeQuery("mp4:tok")
        await bot.on_button(FakeCallbackUpdate(query), FakeContext())

        self.assertTrue(query.answered)


# ─────────────────────────────── 앱 구성 ───────────────────────────────


class TestBuildApp(unittest.TestCase):
    def test_링크_미리보기가_꺼져_있다(self):
        """이슈 #2: URL이 있으면 텔레그램이 썸네일 카드를 자동으로 붙인다."""
        app = bot.build_app()
        options = app.bot.defaults.link_preview_options

        self.assertIsNotNone(options)
        self.assertTrue(options.is_disabled)

    def test_핸들러가_모두_등록된다(self):
        app = bot.build_app()
        self.assertEqual(len(app.handlers[0]), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
