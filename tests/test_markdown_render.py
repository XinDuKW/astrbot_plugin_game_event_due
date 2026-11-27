"""Markdown 排版：按平台选渲染、活动块结构、与合并转发互斥。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from astrbot_plugin_game_event_due.services.delivery_service import (
    MARKDOWN_PLATFORMS,
    ActivityDeliveryService,
)
from astrbot_plugin_game_event_due.services.message_formatter import (
    DEFAULT_MARKDOWN_COVER_LIMIT,
    MARKDOWN_COVER_LIMIT_MAX,
    MARKDOWN_MODE_AUTO,
    MARKDOWN_MODE_OFF,
    MARKDOWN_MODE_ON,
    STYLE_MARKDOWN,
    STYLE_PLAIN,
    MessageSettings,
    build_activity_block,
    build_overview_parts,
    build_reminder_parts,
    normalize_markdown_cover_limit,
    normalize_markdown_mode,
    parts_to_text,
)
from astrbot_plugin_game_event_due.services.reminder_service import ReminderPlan
from astrbot_plugin_game_event_due.sources.base import GAME_TIMEZONE, Activity
from astrbot_plugin_game_event_due.sources.sra import SRA_GAMES

NOW = datetime(2026, 11, 8, 10, 0, tzinfo=GAME_TIMEZONE)
GAMES = {game.game_id: game for game in SRA_GAMES}
GAME = GAMES["ys"]


def make_activity(hours: int = 5, **kwargs) -> Activity:
    payload = {
        "game_id": "ys",
        "activity_id": "markdown-demo",
        "name": "「示例」限时挑战",
        "start_time": NOW - timedelta(days=3),
        "end_time": NOW + timedelta(hours=hours),
        "url": "https://example.com/a b",
        "cover": "https://example.com/cover.png",
    }
    payload.update(kwargs)
    return Activity(**payload)


def plan(activity: Activity, reasons: tuple[str, ...] = ("new",)) -> ReminderPlan:
    return ReminderPlan(
        activity=activity,
        reasons=reasons,
        tier="",
        remaining=activity.end_time - NOW,
    )


class FakeContext:
    """记录发送出去的会话与消息链。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def send_message(self, umo: str, chain: object) -> None:
        self.sent.append((umo, chain))


def test_normalize_markdown_mode_accepts_common_values() -> None:
    assert normalize_markdown_mode("auto") == MARKDOWN_MODE_AUTO
    assert normalize_markdown_mode(" ON ") == MARKDOWN_MODE_ON
    assert normalize_markdown_mode("off") == MARKDOWN_MODE_OFF
    assert normalize_markdown_mode(True) == MARKDOWN_MODE_ON
    assert normalize_markdown_mode(False) == MARKDOWN_MODE_OFF
    assert normalize_markdown_mode("开启") == MARKDOWN_MODE_ON
    assert normalize_markdown_mode("关闭") == MARKDOWN_MODE_OFF
    assert normalize_markdown_mode(None) == MARKDOWN_MODE_AUTO
    assert normalize_markdown_mode("随便写的") == MARKDOWN_MODE_AUTO


def test_markdown_activity_block_has_no_image_part() -> None:
    activity = make_activity()
    parts = build_activity_block(
        activity, ("new",), NOW, MessageSettings(), style=STYLE_MARKDOWN
    )
    assert all(part.kind == "text" for part in parts)
    text = parts_to_text(parts)
    assert text.splitlines()[0].startswith("**")
    assert "🟥" in text
    assert "（剩余 5小时" in text
    assert "[详情](https://example.com/a%20b)" in text
    assert "[封面图](https://example.com/cover.png)" in text
    assert "开始：" in text


def test_markdown_reminder_parts_use_markdown_separator() -> None:
    settings = MessageSettings()
    plans = [plan(make_activity(5)), plan(make_activity(40, activity_id="b"))]
    text = parts_to_text(
        build_reminder_parts(
            GAME, plans, NOW, settings, style=STYLE_MARKDOWN
        )
    )
    assert text.startswith("## 🎮")
    assert "***" in text
    assert "━━━━━" not in text


def test_markdown_overview_uses_markdown_headings() -> None:
    settings = MessageSettings()
    text = parts_to_text(
        build_overview_parts(
            GAME, [make_activity(5)], NOW, settings, style=STYLE_MARKDOWN
        )
    )
    assert text.startswith("## 🎮 原神 · 当前活动")
    assert "### ▎进行中" in text


def test_plain_style_still_emits_image_parts() -> None:
    parts = build_activity_block(
        make_activity(), ("new",), NOW, MessageSettings(), style=STYLE_PLAIN
    )
    assert any(part.is_image for part in parts)


def test_message_style_follows_platform_and_mode() -> None:
    service = ActivityDeliveryService(FakeContext(), MessageSettings())
    auto = MessageSettings()
    assert service.message_style("qq_official:FriendMessage:abc", auto) == STYLE_MARKDOWN
    assert service.message_style("aiocqhttp:GroupMessage:1", auto) == STYLE_PLAIN
    forced = replace(auto, markdown_mode=MARKDOWN_MODE_ON)
    assert service.message_style("aiocqhttp:GroupMessage:1", forced) == STYLE_MARKDOWN
    off = replace(auto, markdown_mode=MARKDOWN_MODE_OFF)
    assert service.message_style("qq_official:FriendMessage:abc", off) == STYLE_PLAIN


def test_markdown_disables_forwarding() -> None:
    service = ActivityDeliveryService(FakeContext(), MessageSettings())
    auto = MessageSettings()
    assert service._can_forward("aiocqhttp:GroupMessage:1", auto) is True
    assert service._can_forward("qq_official:GroupMessage:abc", auto) is False
    forced = replace(auto, markdown_mode=MARKDOWN_MODE_ON)
    assert service._can_forward("aiocqhttp:GroupMessage:1", forced) is False
    off = replace(auto, markdown_mode=MARKDOWN_MODE_OFF)
    assert service._can_forward("aiocqhttp:GroupMessage:1", off) is True


def test_markdown_platforms_are_qq_official_only() -> None:
    assert set(MARKDOWN_PLATFORMS) == {"qq_official", "qq_official_webhook"}


def test_markdown_embeds_public_cover_as_image() -> None:
    text = parts_to_text(
        build_activity_block(
            make_activity(), ("new",), NOW, MessageSettings(), style=STYLE_MARKDOWN
        )
    )
    assert "![封面图](https://example.com/cover.png)" in text


def test_markdown_cover_falls_back_to_link_for_local_path() -> None:
    text = parts_to_text(
        build_activity_block(
            make_activity(cover="logo.png"),
            ("new",),
            NOW,
            MessageSettings(),
            style=STYLE_MARKDOWN,
        )
    )
    assert "![封面图]" not in text
    assert "[封面图](logo.png)" in text


def test_markdown_link_cover_mode_keeps_plain_link() -> None:
    settings = MessageSettings(cover_mode="link")
    text = parts_to_text(
        build_activity_block(make_activity(), ("new",), NOW, settings, style=STYLE_MARKDOWN)
    )
    assert "![封面图]" not in text
    assert "[封面图](https://example.com/cover.png)" in text


def test_markdown_cover_limit_embeds_only_the_first_cover() -> None:
    # 一条消息塞太多 ![]() 可能被平台截断，默认只嵌第一张
    activities = [
        make_activity(activity_id="a1", cover="https://example.com/1.png"),
        make_activity(activity_id="a2", cover="https://example.com/2.png"),
        make_activity(activity_id="a3", cover="https://example.com/3.png"),
    ]
    parts = build_reminder_parts(
        GAME,
        [plan(item) for item in activities],
        NOW,
        MessageSettings(),
        style=STYLE_MARKDOWN,
    )
    text = parts_to_text(parts)
    assert "![封面图](https://example.com/1.png)" in text
    assert "![封面图](https://example.com/2.png)" not in text
    assert "[封面图](https://example.com/2.png)" in text
    assert "![封面图](https://example.com/3.png)" not in text


def test_markdown_cover_limit_zero_uses_links_only() -> None:
    settings = MessageSettings(markdown_cover_limit=0)
    text = parts_to_text(
        build_activity_block(
            make_activity(), ("new",), NOW, settings, style=STYLE_MARKDOWN
        )
    )
    assert "![封面图]" not in text
    assert "[封面图](https://example.com/cover.png)" in text


def test_markdown_cover_limit_can_be_raised() -> None:
    activities = [
        make_activity(activity_id="a1", cover="https://example.com/1.png"),
        make_activity(activity_id="a2", cover="https://example.com/2.png"),
        make_activity(activity_id="a3", cover="https://example.com/3.png"),
    ]
    settings = MessageSettings(markdown_cover_limit=2)
    text = parts_to_text(
        build_reminder_parts(
            GAME,
            [plan(item) for item in activities],
            NOW,
            settings,
            style=STYLE_MARKDOWN,
        )
    )
    assert "![封面图](https://example.com/1.png)" in text
    assert "![封面图](https://example.com/2.png)" in text
    assert "![封面图](https://example.com/3.png)" not in text
    assert "[封面图](https://example.com/3.png)" in text


def test_normalize_markdown_cover_limit_clamps() -> None:
    assert normalize_markdown_cover_limit(3) == 3
    assert normalize_markdown_cover_limit("2") == 2
    assert normalize_markdown_cover_limit(-5) == 0
    assert normalize_markdown_cover_limit(999) == MARKDOWN_COVER_LIMIT_MAX
    assert normalize_markdown_cover_limit("乱写") == DEFAULT_MARKDOWN_COVER_LIMIT
    assert DEFAULT_MARKDOWN_COVER_LIMIT == 1