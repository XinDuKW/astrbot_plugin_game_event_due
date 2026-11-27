"""摘要提醒里的「看详情」按钮：一行多按钮 + 点击后按游戏展开。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from astrbot.api.message_components import ActionRow

from astrbot_plugin_game_event_due.services.delivery_service import (
    ACK_BUTTON_ID,
    DETAIL_ALL,
    DETAIL_ALL_LABEL,
    DETAIL_BUTTON_ID,
    DETAIL_MAX_BUTTONS,
    DETAIL_ROWS_MAX,
    DETAIL_LABEL_MAX,
    ActivityDeliveryService,
)
from astrbot_plugin_game_event_due.services.message_formatter import (
    PART_TEXT,
    MessagePart,
    MessageSettings,
    STYLE_PLAIN,
    build_urgent_dm_groups,
    build_urgent_dm_summary_parts,
)
from astrbot_plugin_game_event_due.services.polling_service import (
    ActivityPollingService,
    CycleReport,
    PollingSettings,
)
from astrbot_plugin_game_event_due.services.reminder_service import ReminderPolicy
from astrbot_plugin_game_event_due.services.subscription_service import (
    SubscriptionService,
)
from astrbot_plugin_game_event_due.services.urgent_dm_service import (
    DEFAULT_QUIET_HOURS,
    UrgentDmSettings,
    normalize_detail_target,
)
from astrbot_plugin_game_event_due.sources.base import (
    Activity,
    GAME_TIMEZONE,
    Game,
)

QQ_UMO = "qq_official:FriendMessage:openid-abc"
ONEBOT_UMO = "aiocqhttp:FriendMessage:123456"
NOW = datetime(2026, 5, 1, 12, 0, tzinfo=GAME_TIMEZONE)
GAMES = {
    "sr": Game("sr", "崩坏：星穹铁道", "sra", short_name="铁"),
    "ww": Game("ww", "鸣潮", "sra", short_name="潮"),
}


def activity(game_id: str, name: str, hours: float) -> Activity:
    return Activity(
        game_id=game_id,
        activity_id=f"{game_id}-{name}",
        name=name,
        start_time=NOW - timedelta(days=2),
        end_time=NOW + timedelta(hours=hours),
    )


class FakeContext:
    """记录发送出去的会话与消息链。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def send_message(self, umo: str, chain: object) -> None:
        self.sent.append((umo, chain))


def test_detail_row_keeps_order_ids_and_data():
    entries = [
        ("铁", {"detail": "sr"}),
        ("潮", {"detail": "ww"}),
        (DETAIL_ALL_LABEL, {"detail": DETAIL_ALL}),
    ]
    row = ActivityDeliveryService.build_detail_row(entries)
    assert isinstance(row, ActionRow)
    # QQ 适配器（_parse_keyboard）要求同一份 keyboard 里按钮 id 唯一，重复会直接
    # 抛 ValueError 把整条消息打回去，所以这里是「前缀 + 序号」，靠回调数据分流。
    ids = [button.id for button in row.buttons]
    assert len(set(ids)) == 3
    assert all(item.startswith(DETAIL_BUTTON_ID) for item in ids)
    assert [button.label for button in row.buttons] == ["铁", "潮", DETAIL_ALL_LABEL]
    assert [button.action.data for button in row.buttons] == [item[1] for item in entries]


def test_detail_row_truncates_labels_to_qq_limit():
    row = ActivityDeliveryService.build_detail_row([("很长的游戏名字" * 3, {"detail": "sr"})])
    assert len(row.buttons[0].label) == DETAIL_LABEL_MAX


def test_detail_row_is_none_without_entries():
    assert ActivityDeliveryService.build_detail_row([]) is None


def test_send_parts_puts_detail_row_before_ack_row():
    context = FakeContext()
    service = ActivityDeliveryService(context, MessageSettings(), resolver=None)
    row = ActivityDeliveryService.build_detail_row([("铁", {"detail": "sr"})])
    ok = asyncio.run(
        service.send_parts(
            QQ_UMO,
            [MessagePart(PART_TEXT, "摘要")],
            MessageSettings(use_forward_node=False),
            ack_button=True,
            extra_rows=[row],
        )
    )
    assert ok is True
    chain = list(context.sent[0][1].chain)
    rows = [item for item in chain if isinstance(item, ActionRow)]
    assert len(rows) == 2
    assert rows[0] is row
    assert [button.id for button in rows[1].buttons] == [ACK_BUTTON_ID]


def test_send_parts_drops_detail_rows_on_platforms_without_buttons():
    context = FakeContext()
    service = ActivityDeliveryService(context, MessageSettings(), resolver=None)
    row = ActivityDeliveryService.build_detail_row([("铁", {"detail": "sr"})])
    asyncio.run(
        service.send_parts(
            ONEBOT_UMO,
            [MessagePart(PART_TEXT, "摘要")],
            MessageSettings(use_forward_node=False),
            ack_button=True,
            extra_rows=[row],
        )
    )
    chain = list(context.sent[0][1].chain)
    assert [item for item in chain if isinstance(item, ActionRow)] == []


def test_latest_batch_returns_only_the_latest_unacked_round():
    umo = QQ_UMO
    data = {
        umo: {
            "activities": {
                "sr|old": {"last_sent_at": "2026-05-01T10:00:00+08:00"},
                "sr|a": {"last_sent_at": "2026-05-01T12:00:00+08:00"},
                "ww|b": {"last_sent_at": "2026-05-01T12:00:00+08:00"},
                "ww|acked": {
                    "last_sent_at": "2026-05-01T12:00:00+08:00",
                    "acked": True,
                },
                "sr|never": {},
            }
        }
    }
    batch = SubscriptionService.latest_batch(data, umo)
    assert set(batch) == {"sr|a", "ww|b"}
    assert SubscriptionService.latest_batch(data, "nobody:FriendMessage:1") == {}


def test_summary_parts_are_short_and_point_at_the_most_urgent():
    activities = [
        activity("sr", "无音消除", 17),
        activity("sr", "限时挑战", 5),
        activity("ww", "签到奖励", 20),
    ]
    parts = build_urgent_dm_summary_parts(
        activities,
        NOW,
        MessageSettings(),
        games=GAMES,
        ack_hint="（回一句「/我已知晓」就不会再催你了）",
    )
    text = parts[0].value
    lines = text.splitlines()
    assert lines[0] == "🚨 3 个活动马上就要结束了！"
    assert lines[1] == "🚨 铁:2 | 潮:1"
    assert lines[2].startswith("最急：限时挑战（剩 ")
    assert lines[3].startswith("（")
    # 摘要里不该出现具体活动列表：详细内容交给按钮
    assert "签到奖励" not in text


def test_summary_parts_use_markdown_heading():
    activities = [activity("sr", "无音消除", 17), activity("ww", "签到", 20)]
    parts = build_urgent_dm_summary_parts(
        activities, NOW, MessageSettings(), games=GAMES, style="markdown"
    )
    assert parts[0].value.startswith("## 🚨 2 个活动马上就要结束了！")


def test_summary_marker_matches_group_order():
    activities = [
        activity("ww", "签到奖励", 20),
        activity("sr", "无音消除", 17),
    ]
    groups = build_urgent_dm_groups(activities, NOW, MessageSettings(), games=GAMES)
    assert [group.game_id for group in groups] == ["sr", "ww"]
    assert [group.short for group in groups] == ["铁", "潮"]


def test_normalize_detail_target_defaults_to_private():
    # 默认私聊：群里点一下就把一堆活动刷进群，太容易炸群
    assert normalize_detail_target("private") == "private"
    assert normalize_detail_target("Private") == "private"
    assert normalize_detail_target("auto") == "auto"
    assert normalize_detail_target("") == "private"
    assert normalize_detail_target("乱写") == "private"
    assert UrgentDmSettings().detail_button is True
    assert UrgentDmSettings().detail_min == 3
    assert UrgentDmSettings().detail_target == "private"
    assert DEFAULT_QUIET_HOURS == (23, 8)

# ---- 轮询层：什么时候发摘要、什么时候发详细 ----


class FakeRegistry:
    def __init__(self, games: dict[str, Game]) -> None:
        self._games = games

    @property
    def games(self) -> dict[str, Game]:
        return dict(self._games)

    def get(self, game_id: str) -> Game | None:
        return self._games.get(game_id)


class FakeSubscriptions:
    def record_urgent_state(self, data, umo, activity, state) -> None:
        SubscriptionService.record_urgent_state(data, umo, activity, state)


class StubDelivery:
    def __init__(self, *, buttons: bool = True) -> None:
        self.buttons = buttons
        self.sent: list[dict] = []
        self.detail_entries: list[list] = []
        self.forwarded = 0

    def supports_button(self, umo: str) -> bool:
        return self.buttons

    def message_style(self, umo: str, settings=None) -> str:
        return STYLE_PLAIN

    def build_detail_row(self, entries):
        self.detail_entries.append(list(entries))
        return ActivityDeliveryService.build_detail_row(list(entries))

    def build_detail_rows(self, entries, *, all_entry=None):
        entries = list(entries)
        self.detail_entries.append(entries)
        if all_entry is not None:
            entries.append(all_entry)
        return ActivityDeliveryService.build_detail_rows(
            entries[:-1] if all_entry is not None else entries,
            all_entry=all_entry,
        )

    async def send_parts(self, umo, parts, settings=None, **kwargs) -> bool:
        self.sent.append({"umo": umo, "parts": parts, "settings": settings, **kwargs})
        return True

    async def send_urgent_forward(self, *args, **kwargs) -> bool:
        self.forwarded += 1
        return True


def build_polling(
    delivery: StubDelivery,
    *,
    urgent: UrgentDmSettings | None = None,
    message_settings: MessageSettings | None = None,
) -> ActivityPollingService:
    return ActivityPollingService(
        registry=FakeRegistry(GAMES),
        subscriptions=FakeSubscriptions(),
        delivery=delivery,
        settings=PollingSettings(
            urgent_dm=urgent or UrgentDmSettings(enabled=True),
        ),
        default_policy=ReminderPolicy(),
        message_settings=message_settings or MessageSettings(),
    )


def deliver(
    delivery: StubDelivery,
    fetched: dict[str, list[Activity]],
    *,
    urgent: UrgentDmSettings | None = None,
    message_settings: MessageSettings | None = None,
    targets: dict | None = None,
    data: dict | None = None,
) -> tuple[bool, dict, CycleReport]:
    polling = build_polling(
        delivery, urgent=urgent, message_settings=message_settings
    )
    urgent_data: dict = data if data is not None else {}
    entries = targets or {QQ_UMO: {"activities": {}, "games": list(fetched)}}
    report = CycleReport()
    dirty = asyncio.run(
        polling._deliver_urgent_dm(urgent_data, entries, fetched, NOW, report)
    )
    return dirty, urgent_data, report


def test_many_activities_switch_to_summary_with_detail_row():
    fetched = {
        "sr": [activity("sr", "无音消除", 17), activity("sr", "限时挑战", 5)],
        "ww": [activity("ww", "签到奖励", 20)],
    }
    delivery = StubDelivery()
    dirty, data, report = deliver(delivery, fetched)
    assert dirty is True
    assert report.reminders == 3
    assert len(delivery.sent) == 1
    call = delivery.sent[0]
    assert call["ack_button"] is True
    assert call["settings"].use_forward_node is False
    assert len(call["extra_rows"]) == 1
    text = "".join(part.value for part in call["parts"] if part.kind == PART_TEXT)
    assert "3 个活动马上就要结束了" in text
    assert "最急" in text
    assert "签到奖励" not in text
    # 每个游戏一个按钮，最后一个固定是「全部」
    entries = delivery.detail_entries[0]
    assert [item[0] for item in entries] == ["铁", "潮", DETAIL_ALL_LABEL]
    assert [item[1] for item in entries] == [
        {"detail": "sr"},
        {"detail": "ww"},
        {"detail": DETAIL_ALL},
    ]
    # 送达后状态落盘，摘要上的按钮才能靠它认出「最近一轮」
    batch = SubscriptionService.latest_batch(data, QQ_UMO)
    assert len(batch) == 3


def test_two_activities_still_send_full_detail():
    fetched = {
        "sr": [activity("sr", "无音消除", 17)],
        "ww": [activity("ww", "签到奖励", 20)],
    }
    delivery = StubDelivery()
    deliver(delivery, fetched)
    call = delivery.sent[0]
    assert not call.get("extra_rows")
    text = "".join(part.value for part in call["parts"] if part.kind == PART_TEXT)
    assert "无音消除" in text and "签到奖励" in text


def test_summary_disabled_by_config_sends_full_detail():
    fetched = {
        "sr": [activity("sr", "无音消除", 17), activity("sr", "限时挑战", 5)],
        "ww": [activity("ww", "签到奖励", 20)],
    }
    delivery = StubDelivery()
    deliver(delivery, fetched, urgent=UrgentDmSettings(enabled=True, detail_button=False))
    call = delivery.sent[0]
    assert not call.get("extra_rows")
    text = "".join(part.value for part in call["parts"] if part.kind == PART_TEXT)
    assert "无音消除" in text


def test_onebot_without_buttons_never_gets_a_summary():
    fetched = {
        "sr": [activity("sr", "无音消除", 17), activity("sr", "限时挑战", 5)],
        "ww": [activity("ww", "签到奖励", 20)],
    }
    delivery = StubDelivery(buttons=False)
    deliver(delivery, fetched)
    call = delivery.sent[0]
    assert call["ack_button"] is False
    assert not call.get("extra_rows")
    text = "".join(part.value for part in call["parts"] if part.kind == PART_TEXT)
    assert "无音消除" in text


def test_detail_min_can_be_raised_to_keep_full_messages():
    fetched = {
        "sr": [activity("sr", "无音消除", 17), activity("sr", "限时挑战", 5)],
        "ww": [activity("ww", "签到奖励", 20)],
    }
    delivery = StubDelivery()
    deliver(delivery, fetched, urgent=UrgentDmSettings(enabled=True, detail_min=5))
    call = delivery.sent[0]
    assert not call.get("extra_rows")



def test_ack_hint_drops_slash_command_when_button_is_attached():
    # 挂着按钮时再教人回 /我已知晓 就是废话（Markdown 下还多占一行）
    summary = ActivityPollingService._urgent_ack_hint(True, summary=True)
    single = ActivityPollingService._urgent_ack_hint(True)
    assert "/我已知晓" not in summary
    assert "/我已知晓" not in single
    assert "我已知晓" in summary
    assert "我已知晓" in single
    assert "看" in summary.split("；")[0]


def test_ack_hint_keeps_slash_command_without_button():
    hint = ActivityPollingService._urgent_ack_hint(False)
    assert "/我已知晓" in hint


def test_ack_hint_is_used_by_summary_parts_without_duplicating_button_text():
    activities = [activity("sr", "无音消除", 17), activity("ww", "签到", 20)]
    parts = build_urgent_dm_summary_parts(
        activities,
        NOW,
        MessageSettings(),
        games=GAMES,
        ack_hint=ActivityPollingService._urgent_ack_hint(True, summary=True),
        style="markdown",
    )
    text = parts[0].value
    assert "/我已知晓" not in text
    assert "我已知晓" in text

def test_build_detail_rows_wraps_into_multiple_rows():
    entries = [(f"游戏{i}", {"detail": f"g{i}"}) for i in range(6)]
    rows = ActivityDeliveryService.build_detail_rows(
        entries, all_entry=(DETAIL_ALL_LABEL, {"detail": DETAIL_ALL})
    )
    assert [len(row.buttons) for row in rows] == [5, 2]
    ids = [button.id for row in rows for button in row.buttons]
    assert len(ids) == len(set(ids))
    assert rows[-1].buttons[-1].label == DETAIL_ALL_LABEL


def test_build_detail_rows_keeps_all_button_caps():
    entries = [(f"g{i}", {"detail": str(i)}) for i in range(40)]
    rows = ActivityDeliveryService.build_detail_rows(entries)
    assert len(rows) <= DETAIL_ROWS_MAX
    assert sum(len(row.buttons) for row in rows) <= DETAIL_MAX_BUTTONS
    assert ActivityDeliveryService.build_detail_rows([]) == []


def test_detail_rows_all_entry_is_primary():
    rows = ActivityDeliveryService.build_detail_rows(
        [("铁", {"detail": "sr"})],
        all_entry=(DETAIL_ALL_LABEL, {"detail": DETAIL_ALL}),
    )
    assert len(rows) == 1
    assert rows[0].buttons[-1].style == rows[0].buttons[-1].Style.PRIMARY
    assert rows[0].buttons[0].style == rows[0].buttons[0].Style.DEFAULT