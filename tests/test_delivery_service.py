"""合并转发链的节点结构：标题拆成两行、每个活动一个节点。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import astrbot.api.message_components as Comp

from astrbot_plugin_game_event_due.services.delivery_service import (
    ActivityDeliveryService,
)
from astrbot_plugin_game_event_due.services.message_formatter import (
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


class FakeContext:
    """记录发送出去的会话与消息链。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

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
