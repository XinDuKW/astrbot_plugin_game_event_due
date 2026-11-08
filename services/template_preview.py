"""管理页「消息模板编辑器」的预览模型。

页面要按 QQ 真实的排版把消息画出来，而不是拿一段纯文本再猜结构。这里把
「真正会发出去的那条消息」拆成带类型的段落：标题、分段标题、活动块、分隔线。
前端只负责套样式，不需要知道消息是怎么拼出来的，所以改了文案之后预览也跟着变，
不会出现「页面显示一套、实际发出去另一套」。

渲染本身仍然在 ``message_formatter``，本模块只负责把结果整理成给页面的结构。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from ..sources.base import Activity, Game
from .message_formatter import (
    DEFAULT_FORWARD_NODE_NAME,
    MessageSettings,
    UrgencyLevel,
    build_activity_block,
    build_overview_title,
    build_urgent_dm_body_parts,
    build_urgent_dm_groups,
    build_urgent_dm_headline,
    build_urgent_dm_summary,
    build_urgent_dm_title,
    display_game_name,
    parts_to_text,
    section_heading,
    short_game_name,
)


# 转发卡片顶部那一行
FORWARD_CARD_TITLE = "群聊的聊天记录"

# 消息发送者的显示名（页面预览里头像旁那行字）
DEFAULT_SENDER_NAME = "游戏活动日历"


def format_game_pairs(pairs: Sequence[tuple[str, str]]) -> str:
    """把别名/简称配置还原成「代号=显示名」的多行文本，供页面编辑。"""
    return "\n".join(
        f"{key}={value}" for key, value in pairs if str(key).strip()
    )


def format_urgency_rules(levels: Sequence[UrgencyLevel]) -> str:
    """把档位表还原成 1:🟥,3:🟧,7:🟩,*:🟦 这种写法。"""
    parts: list[str] = []
    for level in levels:
        days = "*" if level.is_fallback else f"{level.max_days:g}"
        parts.append(f"{days}:{level.marker}")
    return ",".join(parts)


def _preview_activity(
    activity: Activity,
    now: datetime,
    settings: MessageSettings,
    *,
    include_remaining: bool = True,
) -> dict[str, Any]:
    """单个活动块的预览模型：文本行 + 可选封面。"""
    parts = build_activity_block(
        activity, (), now, settings, include_remaining=include_remaining
    )
    lines: list[str] = []
    image = ""
    for part in parts:
        if part.is_image:
            image = part.value
        else:
            lines.extend(part.value.split("\n"))
    return {"kind": "activity", "lines": lines, "image": image}


def build_template_preview(
    game: Game,
    activities: Sequence[Activity],
    now: datetime,
    settings: MessageSettings,
    *,
    source_name: str = "",
    sender_name: str = DEFAULT_SENDER_NAME,
    limit: int = 4,
) -> dict[str, Any]:
    """构建页面预览模型，同时给出「普通消息」和「合并转发」两种形态。

    sections 里已经按发送规则插好了分隔线与分段标题，前端按顺序画即可；
    nodes 是合并转发里每个活动各自一个节点的样子（第一个节点是标题）。
    """
    active = sorted(
        (item for item in activities if item.is_active(now)),
        key=lambda item: item.end_time or item.sort_key()[0],
    )
    upcoming = sorted(
        (item for item in activities if item.is_upcoming(now)),
        key=lambda item: item.start_time or item.sort_key()[0],
    )

    sections: list[dict[str, Any]] = []
    blocks_seen = 0
    after_heading = False

    def add_heading(label: str, items: Sequence[Activity]) -> None:
        nonlocal after_heading
        sections.append(
            {
                "kind": "heading",
                "text": section_heading(label, items, now, settings),
            }
        )
        after_heading = True

    def add_activity(activity: Activity, *, include_remaining: bool) -> None:
        nonlocal blocks_seen, after_heading
        # 与 _assemble 一致：分隔线只出现在相邻活动块之间
        if blocks_seen and settings.activity_separator and not after_heading:
            sections.append(
                {"kind": "separator", "text": settings.activity_separator}
            )
        blocks_seen += 1
        after_heading = False
        sections.append(
            _preview_activity(
                activity, now, settings, include_remaining=include_remaining
            )
        )

    if not active and not upcoming:
        sections.append(
            {"kind": "note", "text": "当前没有进行中或即将开始的活动。"}
        )
    else:
        if active:
            add_heading("进行中", active)
            for item in active[:limit]:
                add_activity(item, include_remaining=True)
            if len(active) > limit:
                sections.append(
                    {
                        "kind": "note",
                        "text": f"……另有 {len(active) - limit} 个进行中的活动",
                    }
                )
        if upcoming:
            add_heading("即将开始", upcoming)
            for item in upcoming[:limit]:
                add_activity(item, include_remaining=False)
            if len(upcoming) > limit:
                sections.append(
                    {
                        "kind": "note",
                        "text": (
                            f"……另有 {len(upcoming) - limit} 个即将开始的活动"
                        ),
                    }
                )

    if source_name:
        sections.append({"kind": "note", "text": f"（数据来源：{source_name}）"})

    node_name = short_game_name(game, settings)
    nodes: list[dict[str, Any]] = []
    for item in active[:limit]:
        block = _preview_activity(item, now, settings, include_remaining=True)
        nodes.append({"name": node_name, "lines": block["lines"], "image": block["image"]})
    for item in upcoming[:limit]:
        block = _preview_activity(item, now, settings, include_remaining=False)
        nodes.append({"name": node_name, "lines": block["lines"], "image": block["image"]})

    return {
        "game_id": game.game_id,
        "game_name": display_game_name(game, settings),
        "short_name": node_name,
        "count": len(activities),
        "sender": {"name": sender_name, "avatar": "logo.png"},
        "plain": {
            "header": f"🎮 {display_game_name(game, settings)} · 当前活动",
            "sections": sections,
        },
        "forward": {
            "card_title": FORWARD_CARD_TITLE,
            "title_node": {
                "name": node_name,
                "lines": [build_overview_title(game, activities, now, settings)],
            },
            "nodes": nodes,
            "footer": f"查看 {len(nodes) + 1} 条转发消息",
        },
    }

# ---- 紧急提醒的「多游戏同时到期」模拟 ----

# 示例活动剩余多少小时：覆盖红（≤1 天）/ 橙（≤3 天）/ 绿（≤7 天）/ 蓝（更久）四档
URGENT_SAMPLE_HOURS = (5, 30, 60, 110, 200, 400)
URGENT_SAMPLE_NAMES = (
    "「示例」限时挑战",
    "「示例」签到活动",
    "「示例」角色试玩",
    "「示例」双倍掉落",
    "「示例」联动关卡",
    "「示例」版本预热",
)
URGENT_ACK_HINT = "（回一句「/我已知晓」就不会再催你了）"


def simulate_urgent_activities(
    games: Sequence[Game],
    now: datetime,
    count: int,
    *,
    per_game: int = 2,
) -> list[Activity]:
    """造一批「马上就要结束」的示例活动，不发起任何网络请求。

    这是给页面看的假数据：剩余时间刻意错开四个档位，好让人一眼看出颜色
    对不上该怎么办。真正的紧急提醒仍然只由轮询层按真实活动决定。
    """
    picked = [game for game in games if game][: max(1, count)]
    activities: list[Activity] = []
    slot = 0
    for game in picked:
        for index in range(max(1, per_game)):
            hours = URGENT_SAMPLE_HOURS[slot % len(URGENT_SAMPLE_HOURS)]
            activities.append(
                Activity(
                    game_id=game.game_id,
                    activity_id=f"urgent-sample-{game.game_id}-{index}",
                    name=URGENT_SAMPLE_NAMES[slot % len(URGENT_SAMPLE_NAMES)],
                    start_time=now - timedelta(days=3),
                    end_time=now + timedelta(hours=hours),
                    description="示例活动，只用来展示紧急提醒的排版效果。",
                )
            )
            slot += 1
    return activities


def build_urgent_preview(
    games: Sequence[Game],
    now: datetime,
    settings: MessageSettings,
    *,
    count: int = 3,
    ack_hint: str = URGENT_ACK_HINT,
) -> dict[str, Any]:
    """模拟「N 个游戏同时到期」时的那条私聊提醒，供模板编辑器预览。"""
    count = max(2, min(6, int(count)))
    per_game = 2 if count <= 3 else 1
    activities = simulate_urgent_activities(
        games, now, count, per_game=per_game
    )
    lookup = {game.game_id: game for game in games if game}
    groups = build_urgent_dm_groups(activities, now, settings, games=lookup)

    node_name = settings.forward_node_name or DEFAULT_FORWARD_NODE_NAME
    if not groups:
        return {
            "count": count,
            "headline": build_urgent_dm_headline(groups),
            "summary": "",
            "ack_hint": ack_hint,
            "nodes": [],
            "games": [],
            "plain_lines": [],
        }

    headline = build_urgent_dm_headline(groups)
    summary = f"🚨 {build_urgent_dm_summary(groups)}"
    title = build_urgent_dm_title(groups, settings)

    nodes: list[dict[str, Any]] = [{"name": node_name, "lines": [headline]}]
    if summary:
        nodes.append({"name": node_name, "lines": [summary]})
    for group in groups:
        for activity in group.activities:
            block = _preview_activity(activity, now, settings)
            nodes.append(
                {
                    "name": group.short,
                    "lines": block["lines"],
                    "image": block["image"],
                }
            )

    plain_lines = parts_to_text(
        build_urgent_dm_body_parts(
            groups,
            now,
            settings,
            headline=title,
            ack_hint=ack_hint,
        )
    ).split("\n")

    return {
        "count": count,
        "headline": headline,
        "summary": summary,
        "ack_hint": ack_hint,
        "nodes": nodes,
        "games": [
            {
                "game_id": group.game_id,
                "name": group.name,
                "short": group.short,
                "count": len(group.activities),
                "blocks": [
                    _preview_activity(activity, now, settings)
                    for activity in group.activities
                ],
            }
            for group in groups
        ],
        "plain_lines": plain_lines,
    }
