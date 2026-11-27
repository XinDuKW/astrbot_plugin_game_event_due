"""合并转发链的节点结构：标题拆成两行、每个活动一个节点。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import astrbot.api.message_components as Comp

from astrbot_plugin_game_event_due.services.delivery_service import (
    ActivityDeliveryService,
)
from astrbot_plugin_game_event_due.services.message_formatter import (
    MessagePart,
    MessageSettings,
    build_urgent_dm_groups,
    build_urgent_dm_title,
)
from astrbot_plugin_game_event_due.sources.base import GAME_TIMEZONE, Activity
from astrbot_plugin_game_event_due.sources.sra import SRA_GAMES

NOW = datetime(2026, 11, 8, 10, 0, tzinfo=GAME_TIMEZONE)
SETTINGS = MessageSettings()
GAMES = {game.game_id: game for game in SRA_GAMES}
UMO = "aiocqhttp:FriendMessage:123456"
ACK_HINT = "（回一句「/我已知晓」就不会再催你了）"


class FakeMeta:
    """只带 id 与 name 的平台元信息，够 platform_type 反查用。"""

    def __init__(self, platform_id: str, name: str) -> None:
        self.id = platform_id
        self.name = name


class FakePlatform:
    def __init__(self, platform_id: str, name: str) -> None:
        self._meta = FakeMeta(platform_id, name)

    def meta(self) -> FakeMeta:
        return self._meta


class FakePlatformManager:
    def __init__(self, *platforms: FakePlatform) -> None:
        self._insts = list(platforms)

    def get_insts(self) -> list[FakePlatform]:
        return self._insts


class FakeContext:
    """记录发送出去的会话与消息链。

    传了平台实例才有 platform_manager，用来验证「按平台 id 反查适配器类型」；
    不传时故意保持缺少该属性，让调用方走「按 UMO 前缀判断」的退回分支。
    """

    def __init__(self, *platforms: tuple[str, str]) -> None:
        self.sent: list[tuple[str, object]] = []
        self.platform_manager = (
            FakePlatformManager(*(FakePlatform(*item) for item in platforms))
            if platforms
            else None
        )

    async def send_message(self, umo: str, chain: object) -> None:
        self.sent.append((umo, chain))


def make_activity(
    game_id: str,
    name: str,
    *,
    hours: int,
    activity_id: str = "",
) -> Activity:
    return Activity(
        game_id=game_id,
        activity_id=activity_id or name,
        name=name,
        start_time=NOW - timedelta(days=3),
        end_time=NOW + timedelta(hours=hours),
    )


def urgent_groups():
    """鸣潮（5 小时后结束）与绝区零（9 小时后结束）。"""
    activities = [
        make_activity("ww", "无音消除", hours=5),
        make_activity("zzz", "式的破晓", hours=9),
    ]
    return build_urgent_dm_groups(activities, NOW, SETTINGS, games=GAMES)


def send(groups, *, ack_hint: str = "") -> FakeContext:
    context = FakeContext()
    service = ActivityDeliveryService(context, SETTINGS, resolver=None)
    delivered = asyncio.run(
        service.send_urgent_forward(
            UMO,
            groups,
            NOW,
            SETTINGS,
            title=build_urgent_dm_title(groups, SETTINGS),
            ack_hint=ack_hint,
        )
    )
    assert delivered is True
    return context


def nodes_of(context: FakeContext) -> list:
    _, chain = context.sent[0]
    forward = chain.chain[0]
    return getattr(forward, "nodes", None) or getattr(forward, "content", None)


def test_urgent_forward_splits_headline_and_summary_into_two_nodes():
    nodes = nodes_of(send(urgent_groups(), ack_hint=ACK_HINT))

    # 两个标题节点 + 两个活动节点
    assert len(nodes) == 4
    assert nodes[0].content[0].text == "🚨 2 个活动马上就要结束了！"
    assert nodes[1].content[0].text == f"🚨 潮:1 | 绝:1\n\n{ACK_HINT}"
    # 活动节点用游戏简称做名字，点开才看得出是哪个游戏
    assert [node.name for node in nodes[2:]] == ["潮", "绝"]
    assert nodes[0].name == SETTINGS.forward_node_name


def test_urgent_forward_without_ack_hint_keeps_summary_alone():
    nodes = nodes_of(send(urgent_groups()))
    assert nodes[1].content[0].text == "🚨 潮:1 | 绝:1"


def test_forward_chain_named_needs_at_least_two_nodes():
    single = ActivityDeliveryService.build_forward_chain_named(
        [("活动提醒", [Comp.Plain("只有一行")])]
    )
    assert single is None

    pair = ActivityDeliveryService.build_forward_chain_named(
        [
            ("活动提醒", [Comp.Plain("标题")]),
            ("潮", [Comp.Plain("活动")]),
        ]
    )
    assert pair is not None
    assert len(pair) == 1


def test_supports_forward_only_on_aiocqhttp():
    service = ActivityDeliveryService(FakeContext(), SETTINGS, resolver=None)
    assert service.supports_forward(UMO) is True
    assert service.supports_forward("aiocqhttp:GroupMessage:1")
    # QQ 官方机器人会忽略 Node/Nodes 且不报错，整条消息会被静默丢弃
    assert service.supports_forward("qq_official:FriendMessage:x") is False
    assert service.supports_forward("telegram:FriendMessage:1") is False
    assert service.supports_forward("") is False


def test_urgent_forward_on_qq_official_falls_back_to_plain_message():
    context = FakeContext()
    service = ActivityDeliveryService(context, SETTINGS, resolver=None)
    groups = urgent_groups()
    delivered = asyncio.run(
        service.send_urgent_forward(
            "qq_official:FriendMessage:abc",
            groups,
            NOW,
            SETTINGS,
            title=build_urgent_dm_title(groups, SETTINGS),
            ack_hint=ACK_HINT,
        )
    )
    # 返回 False 让调用方回退普通消息；这里什么都不该发
    assert delivered is False
    assert context.sent == []


def test_send_parts_on_qq_official_never_builds_forward_nodes():
    context = FakeContext()
    service = ActivityDeliveryService(context, SETTINGS, resolver=None)
    settings = MessageSettings(use_forward_node=True)
    ok = asyncio.run(
        service.send_parts(
            "qq_official:GroupMessage:g1",
            [MessagePart("text", "普通消息")],
            settings,
        )
    )
    assert ok is True
    _, chain = context.sent[0]
    assert all(isinstance(item, Comp.Plain) for item in chain.chain)


def test_supports_forward_looks_up_renamed_platform():
    """平台 id 叫 Ponderer、适配器还是 aiocqhttp 时，照样要认成支持转发。

    1.5.0 的回归就发生在这里：那时只拿 UMO 前缀和 aiocqhttp 硬比，平台改过名
    就一律判成不支持，合并转发被静默降级成普通消息。这条用例专门盯住反查路径，
    前缀比较本身在下面的用例里单独覆盖。
    """
    context = FakeContext(("Ponderer", "aiocqhttp"), ("Fairy", "qq_official"))
    service = ActivityDeliveryService(context, SETTINGS, resolver=None)
    assert service.platform_type("Ponderer:GroupMessage:1") == "aiocqhttp"
    assert service.supports_forward("Ponderer:GroupMessage:1") is True
    assert service.platform_type("Fairy:FriendMessage:x") == "qq_official"
    assert service.supports_forward("Fairy:FriendMessage:x") is False
    assert service.supports_markdown("Fairy:FriendMessage:x") is True
    # 适配器不在实例列表里（比如刚被删掉）时，退回按前缀原样判断
    assert service.supports_forward("telegram:FriendMessage:1") is False


def test_urgent_forward_without_groups_sends_nothing():
    context = FakeContext()
    service = ActivityDeliveryService(context, SETTINGS, resolver=None)
    delivered = asyncio.run(
        service.send_urgent_forward(
            UMO, [], NOW, SETTINGS, title="🚨 活动马上就要结束了！"
        )
    )
    assert delivered is True
    assert context.sent == []
