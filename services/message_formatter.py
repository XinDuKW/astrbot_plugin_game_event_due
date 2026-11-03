"""活动推送消息的构建。

本模块不依赖 astrbot，纯字符串与数据结构处理，便于单独测试与复用。

消息按「片段」构建，而不是直接拼成字符串：``MessagePart`` 要么是一段文本，
要么是一张封面图。这样发送层既可以把图片作为真正的图片组件发出，也可以在
图片发送失败时退化成纯文本（把图片片段渲染成链接），而格式化逻辑只写一份。

本模块**不决定**哪些封面真的作为图片发送——那取决于图片体积，由
``cover_service`` + ``delivery_service`` 按总体积预算决定。这里只负责把
带封面的活动渲染成「文本 + 图片片段」。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..sources.base import Activity, Game, format_datetime, format_remaining
from .reminder_service import ReminderPlan, reason_label

MAX_ACTIVE_IN_OVERVIEW = 15
MAX_UPCOMING_IN_OVERVIEW = 8
MAX_DESCRIPTION_LENGTH = 180

# 封面压缩与体积预算的默认值。它们属于「消息格式设置」，所以定义在这里；
# cover_service 只是复用它们作为构造参数默认值，这样 message_formatter 不必
# 反向依赖 cover_service（后者需要 astrbot 运行时）。
DEFAULT_COVER_MAX_WIDTH = 720
DEFAULT_COVER_MAX_TOTAL_MB = 5

PART_TEXT = "text"
PART_IMAGE = "image"

# 一条消息里同时推送多个活动时，插在相邻活动之间的视觉分隔线
DEFAULT_ACTIVITY_SEPARATOR = "━━━━━━━━━━━━"

COVER_MODE_OFF = "off"
COVER_MODE_LINK = "link"
COVER_MODE_IMAGE = "image"
COVER_MODES = (COVER_MODE_OFF, COVER_MODE_LINK, COVER_MODE_IMAGE)
DEFAULT_COVER_MODE = COVER_MODE_IMAGE

# ---- 剩余时间视觉标记 ----

# 配置里可以直接写 emoji，也可以写颜色名，这里做一层映射
URGENCY_MARKER_COLORS = {
    "红": "🟥",
    "red": "🟥",
    "橙": "🟧",
    "orange": "🟧",
    "黄": "🟨",
    "yellow": "🟨",
    "绿": "🟩",
    "green": "🟩",
    "蓝": "🟦",
    "blue": "🟦",
    "紫": "🟪",
    "purple": "🟪",
    "黑": "⬛",
    "black": "⬛",
    "白": "⬜",
    "white": "⬜",
}

DEFAULT_URGENCY_RULES = "1:🟥,3:🟧,7:🟩,*:🟦"


@dataclass(frozen=True, slots=True)
class UrgencyLevel:
    """一个剩余时间档位对应的视觉标记。

    ``max_days`` 是该档位的上界（天）；``0`` 表示兜底档，命中所有更宽松的剩余时间。
    """

    marker: str
    max_days: float = 0.0

    @property
    def is_fallback(self) -> bool:
        return self.max_days <= 0


DEFAULT_URGENCY_LEVELS: tuple[UrgencyLevel, ...] = (
    UrgencyLevel("🟥", 1),
    UrgencyLevel("🟧", 3),
    UrgencyLevel("🟩", 7),
    UrgencyLevel("🟦", 0),
)


def parse_urgency_rules(value: object) -> tuple[UrgencyLevel, ...]:
    """把 ``"1:🟥,3:🟧,7:🟩,*:🟦"`` 解析成按紧急度升序的档位表。

    标记可以写成 emoji，也可以写颜色名（``1:红``）；天数写成 ``*`` 或 ``0``
    表示兜底档。无法识别的项会被跳过；整体解析不出内容时回退到
    ``DEFAULT_URGENCY_LEVELS``。
    """
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return DEFAULT_URGENCY_LEVELS
        items: list[object] = [
            item for item in text.replace("，", ",").split(",") if item.strip()
        ]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return DEFAULT_URGENCY_LEVELS

    levels: list[UrgencyLevel] = []
    for item in items:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            days_raw, marker_raw = item[0], item[1]
        elif isinstance(item, str):
            normalized = item.replace("：", ":")
            if ":" not in normalized:
                continue
            days_raw, marker_raw = normalized.split(":", 1)
        else:
            continue

        marker_text = str(marker_raw).strip()
        marker = (
            URGENCY_MARKER_COLORS.get(marker_text)
            or URGENCY_MARKER_COLORS.get(marker_text.casefold())
            or marker_text
        )
        if not marker:
            continue

        days_text = str(days_raw).strip()
        if days_text in ("*", "0", "其他", "其它", "else", "default"):
            days = 0.0
        else:
            try:
                days = float(days_text)
            except (TypeError, ValueError):
                continue
        levels.append(UrgencyLevel(marker, max(days, 0.0)))

    if not levels:
        return DEFAULT_URGENCY_LEVELS
    levels.sort(key=lambda level: (level.is_fallback, level.max_days))
    return tuple(levels)


def urgency_level_for(
    remaining: timedelta | None,
    rules: Sequence[UrgencyLevel],
) -> UrgencyLevel | None:
    """返回剩余时间命中的最紧急档位；已结束或无法计算时返回 ``None``。"""
    if remaining is None or remaining <= timedelta(0):
        return None
    for level in rules:
        if level.is_fallback:
            continue
        if remaining <= timedelta(days=level.max_days):
            return level
    for level in rules:
        if level.is_fallback:
            return level
    return None


def activity_urgency_level(
    activity: Activity,
    now: datetime,
    rules: Sequence[UrgencyLevel],
) -> UrgencyLevel | None:
    """活动当前的剩余时间档位。"""
    return urgency_level_for(activity.remaining(now), rules)


@dataclass(frozen=True, slots=True)
class MessagePart:
    """消息的一个片段：文本或图片。"""

    kind: str
    value: str

    @property
    def is_image(self) -> bool:
        return self.kind == PART_IMAGE


@dataclass(frozen=True, slots=True)
class MessageSettings:
    """推送消息的内容开关。"""

    include_description: bool = False
    cover_mode: str = DEFAULT_COVER_MODE
    include_tags: bool = True
    include_extra_times: bool = True
    single_message_per_game: bool = True
    use_forward_node: bool = False
    cover_compress: bool = True
    cover_max_width: int = DEFAULT_COVER_MAX_WIDTH
    cover_max_total_mb: int = DEFAULT_COVER_MAX_TOTAL_MB
    activity_separator: str = DEFAULT_ACTIVITY_SEPARATOR
    # 剩余时间标记（🟥 / 🟧 / 🟩 / 🟦）
    urgency_marker: bool = True
    urgency_rules: tuple[UrgencyLevel, ...] = DEFAULT_URGENCY_LEVELS
    # 把剩余时间写进活动标题，消息顶部就能看到
    headline_remaining: bool = True
    # 消息标题里附上各档位数量，例如「🟥1 🟧2」
    tier_summary: bool = True
    # 把最紧急的活动排在前面
    sort_by_urgency: bool = True
    # 游戏显示名覆盖：(代号/原名/内置别名 -> 想显示的名字)
    game_aliases: tuple[tuple[str, str], ...] = ()
    # 图片模式下是否仍附「详情：链接」；默认不附，有图就别再贴链接
    image_mode_detail_link: bool = False

    @property
    def cover_max_total_bytes(self) -> int:
        """单条消息封面总体积预算；``0`` 表示不限制。"""
        try:
            megabytes = int(self.cover_max_total_mb)
        except (TypeError, ValueError):
            return DEFAULT_COVER_MAX_TOTAL_MB * 1024 * 1024
        if megabytes <= 0:
            return 0
        return megabytes * 1024 * 1024


def normalize_cover_mode(value: object) -> str:
    """把配置值规范化为封面模式，并兼容旧版布尔开关。"""
    if isinstance(value, bool):
        return COVER_MODE_IMAGE if value else COVER_MODE_OFF
    text = str(value or "").strip().lower()
    legacy = {
        "true": COVER_MODE_IMAGE,
        "false": COVER_MODE_OFF,
        "on": COVER_MODE_IMAGE,
        "图片": COVER_MODE_IMAGE,
        "链接": COVER_MODE_LINK,
        "关闭": COVER_MODE_OFF,
    }
    text = legacy.get(text, text)
    return text if text in COVER_MODES else DEFAULT_COVER_MODE


def normalize_game_aliases(value: object) -> tuple[tuple[str, str], ...]:
    """解析「代号=显示名」的游戏别名，例如 sr=星铁 / arknights:舟游。

    左边可以写游戏代号、完整名称或内置别名，右边是想显示的名字。
    换行、逗号、分号都能分隔；解析不出来的项直接跳过。
    """
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        text = str(value or "")
        normalized = (
            text.replace("\r", "\n")
            .replace("，", ",")
            .replace("；", ",")
            .replace(";", ",")
        )
        items = []
        for line in normalized.split("\n"):
            items.extend(line.split(","))

    pairs: list[tuple[str, str]] = []
    for item in items:
        raw = str(item).strip()
        if not raw:
            continue
        key = ""
        name = ""
        for separator in ("=", "：", ":", "＝"):
            if separator in raw:
                key, _, name = raw.partition(separator)
                break
        key = key.strip().casefold()
        name = name.strip()
        if not key or not name:
            continue
        if all(key != existing for existing, _ in pairs):
            pairs.append((key, name))
    return tuple(pairs)


def display_game_name(game: Game, settings: MessageSettings) -> str:
    """游戏在消息里的显示名：配置了别名就用别名，否则用原名。

    匹配不区分大小写，代号、完整名、内置别名（崩铁 / 星铁 / 舟 等）都能命中。
    """
    if not settings.game_aliases:
        return game.name
    candidates = {game.game_id.casefold(), game.name.casefold()}
    candidates.update(str(alias).casefold() for alias in game.aliases)
    for key, alias in settings.game_aliases:
        if key in candidates:
            return alias
    return game.name


def truncate(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)]}…"


def format_moment(moment: datetime, now: datetime) -> str:
    """格式化时间；跨年时补上年份，避免出现「开始 07-17 / 结束 03-10」的歧义。"""
    return format_datetime(moment, with_year=moment.year != now.year)


def _remaining_suffix(activity: Activity, now: datetime) -> str:
    remaining = activity.remaining(now)
    if remaining is None or remaining <= timedelta(0):
        return ""
    return f"（剩余 {format_remaining(remaining)}）"


def build_activity_block(
    activity: Activity,
    reasons: Sequence[str],
    now: datetime,
    settings: MessageSettings,
    *,
    include_remaining: bool = True,
) -> list[MessagePart]:
    """构建单个活动的消息片段。

    封面按 ``settings.cover_mode`` 处理：``link`` 时写成文本行，``image`` 时作为
    独立的图片片段返回，``off`` 时完全不附带。
    """
    marker = ""
    if settings.urgency_marker:
        level = activity_urgency_level(activity, now, settings.urgency_rules)
        if level is not None:
            marker = f"{level.marker} "

    labels = " · ".join(reason_label(reason) for reason in reasons)
    prefix = f"【{labels}】" if labels else ""
    headline_suffix = ""
    if settings.headline_remaining and include_remaining:
        headline_suffix = _remaining_suffix(activity, now)
    headline = f"{marker}{prefix}{activity.name}{headline_suffix}"

    lines = [headline]
    if activity.start_time is not None:
        lines.append(f"开始：{format_moment(activity.start_time, now)}")
    if activity.end_time is not None:
        suffix = ""
        if include_remaining and not settings.headline_remaining:
            suffix = _remaining_suffix(activity, now)
        lines.append(f"结束：{format_moment(activity.end_time, now)}{suffix}")

    if settings.include_extra_times:
        for label, moment in activity.extra_times:
            lines.append(f"{label}：{format_moment(moment, now)}")

    if settings.include_tags and activity.tags:
        lines.append(f"标签：{'、'.join(activity.tags)}")

    if settings.include_description and activity.description:
        lines.append(f"说明：{truncate(activity.description, MAX_DESCRIPTION_LENGTH)}")

    cover = str(activity.cover or "").strip()
    if cover and settings.cover_mode == COVER_MODE_LINK:
        lines.append(f"封面：{cover}")
    if activity.url:
        hide_detail_link = (
            cover
            and settings.cover_mode == COVER_MODE_IMAGE
            and not settings.image_mode_detail_link
        )
        if not hide_detail_link:
            lines.append(f"详情：{activity.url}")

    parts = [MessagePart(PART_TEXT, "\n".join(lines))]
    if cover and settings.cover_mode == COVER_MODE_IMAGE:
        parts.append(MessagePart(PART_IMAGE, cover))
    return parts


def _assemble(
    header: str,
    segments: Sequence[str | Sequence[MessagePart]],
    separator: str = "",
) -> list[MessagePart]:
    """把头部、文本行与活动块按顺序合并为一条消息。

    ``segments`` 里每个元素要么是一行独立文本（分段标题、来源说明），要么是一个
    活动块（文本 + 可选封面图）。所有片段之间统一用一个空行分隔。

    ``separator`` 非空时会插在**相邻活动块之间**。紧跟分段标题的那个活动块不加分隔线，
    否则标题会和它自己的第一批活动被割开。

    这里会为每个带封面的活动保留一个图片片段；具体哪些封面真的发得出去，由发送层
    按体积预算决定（见 ``cover_service.select_within_budget``）。相邻的文本片段会
    合并成一段，避免产生大量零碎的消息组件。
    """
    parts: list[MessagePart] = []
    buffer = header
    blocks_seen = 0
    after_heading = False

    for segment in segments:
        if isinstance(segment, str):
            buffer += f"\n\n{segment}"
            after_heading = True
            continue

        prefix = "\n\n"
        if blocks_seen and separator and not after_heading:
            prefix = f"\n\n{separator}\n\n"
        blocks_seen += 1
        after_heading = False

        for part in segment:
            if part.is_image:
                if buffer:
                    parts.append(MessagePart(PART_TEXT, buffer))
                    buffer = ""
                parts.append(part)
            else:
                buffer += f"{prefix}{part.value}"
                prefix = "\n\n"

    if buffer:
        parts.append(MessagePart(PART_TEXT, buffer))
    return parts


def parts_to_text(parts: Sequence[MessagePart]) -> str:
    """把片段渲染为纯文本，图片片段渲染为封面链接。

    文本片段本身可能以空行开头（图片之后的续接文本），此时不再额外补分隔符，
    否则会出现多出一个空行。
    """
    text = ""
    for part in parts:
        chunk = f"封面：{part.value}" if part.is_image else part.value
        if not chunk:
            continue
        if text and not chunk.startswith("\n"):
            text += "\n"
        text += chunk
    return text


def sort_plans_by_urgency(
    plans: Sequence[ReminderPlan],
    now: datetime,
) -> list[ReminderPlan]:
    """按剩余时间升序排列，让最紧急的活动排在最前面。"""

    def sort_key(plan: ReminderPlan) -> tuple[int, float]:
        # 只按剩余时间排序：稳定排序保证同一档位内维持原有顺序
        remaining = plan.activity.remaining(now)
        if remaining is None:
            return (1, 0.0)
        return (0, remaining.total_seconds())

    return sorted(plans, key=sort_key)


def summarize_markers(
    activities: Sequence[Activity],
    now: datetime,
    settings: MessageSettings,
) -> str:
    """统计各剩余时间档位的活动数量，例如 ``🟥1 🟧2``。"""
    if not (settings.urgency_marker and settings.tier_summary):
        return ""

    counts: dict[str, int] = {}
    order: list[str] = []
    for activity in activities:
        level = activity_urgency_level(activity, now, settings.urgency_rules)
        if level is None:
            continue
        if level.marker not in counts:
            counts[level.marker] = 0
            order.append(level.marker)
        counts[level.marker] += 1

    if not order:
        return ""
    return " ".join(f"{marker}{counts[marker]}" for marker in order)


def build_reminder_parts(
    game: Game,
    plans: Sequence[ReminderPlan],
    now: datetime,
    settings: MessageSettings,
) -> list[MessagePart]:
    """把一个游戏本轮的全部提醒合并为一条消息。

    开启 ``sort_by_urgency`` 时最紧急的活动排在最前，标题里附上各档位数量，
    这样一眼就能看出这波提醒里有没有火烧眉毛的。
    """
    ordered = (
        sort_plans_by_urgency(plans, now)
        if settings.sort_by_urgency
        else list(plans)
    )
    blocks = [
        build_activity_block(plan.activity, plan.reasons, now, settings)
        for plan in ordered
    ]
    header = f"🎮 {display_game_name(game, settings)} · 活动提醒"
    summary = summarize_markers([plan.activity for plan in ordered], now, settings)
    if summary:
        header = f"{header}  {summary}"
    return _assemble(header, blocks, settings.activity_separator)


def build_single_reminder_parts(
    game: Game,
    plan: ReminderPlan,
    now: datetime,
    settings: MessageSettings,
) -> list[MessagePart]:
    """只包含一个活动的提醒消息。"""
    return _assemble(
        f"🎮 {display_game_name(game, settings)} · 活动提醒",
        [build_activity_block(plan.activity, plan.reasons, now, settings)],
        settings.activity_separator,
    )


def build_overview_parts(
    game: Game,
    activities: Sequence[Activity],
    now: datetime,
    settings: MessageSettings,
    *,
    source_name: str = "",
) -> list[MessagePart]:
    """构建「当前活动一览」，用于订阅确认与手动查询。"""
    active = sorted(
        (item for item in activities if item.is_active(now)),
        key=lambda item: item.end_time or item.sort_key()[0],
    )
    upcoming = sorted(
        (item for item in activities if item.is_upcoming(now)),
        key=lambda item: item.start_time or item.sort_key()[0],
    )

    segments: list[str | list[MessagePart]] = []

    if not active and not upcoming:
        segments.append("当前没有进行中或即将开始的活动。")
    else:
        if active:
            segments.append(f"▎进行中（{len(active)}）")
            for item in active[:MAX_ACTIVE_IN_OVERVIEW]:
                segments.append(build_activity_block(item, (), now, settings))
            if len(active) > MAX_ACTIVE_IN_OVERVIEW:
                segments.append(
                    f"……另有 {len(active) - MAX_ACTIVE_IN_OVERVIEW} 个进行中的活动"
                )

        if upcoming:
            segments.append(f"▎即将开始（{len(upcoming)}）")
            for item in upcoming[:MAX_UPCOMING_IN_OVERVIEW]:
                segments.append(
                    build_activity_block(
                        item,
                        (),
                        now,
                        settings,
                        include_remaining=False,
                    )
                )
            if len(upcoming) > MAX_UPCOMING_IN_OVERVIEW:
                segments.append(
                    f"……另有 {len(upcoming) - MAX_UPCOMING_IN_OVERVIEW} 个即将开始的活动"
                )

    if source_name:
        segments.append(f"（数据来源：{source_name}）")

    return _assemble(
        f"🎮 {display_game_name(game, settings)} · 当前活动",
        segments,
        settings.activity_separator,
    )


# ---- 纯文本便捷封装（WebUI 预览、测试与降级输出使用） ----


def build_reminder_message(
    game: Game,
    plans: Sequence[ReminderPlan],
    now: datetime,
    settings: MessageSettings,
) -> str:
    return parts_to_text(build_reminder_parts(game, plans, now, settings))


def build_single_reminder_message(
    game: Game,
    plan: ReminderPlan,
    now: datetime,
    settings: MessageSettings,
) -> str:
    return parts_to_text(build_single_reminder_parts(game, plan, now, settings))


def build_overview_message(
    game: Game,
    activities: Sequence[Activity],
    now: datetime,
    settings: MessageSettings,
    *,
    source_name: str = "",
) -> str:
    return parts_to_text(
        build_overview_parts(game, activities, now, settings, source_name=source_name)
    )


def build_urgent_dm_parts(
    activities: Sequence[Activity],
    now: datetime,
    settings: MessageSettings,
    *,
    ack_hint: str = "",
) -> list[MessagePart]:
    """构建紧急私聊提醒的消息片段。

    每个活动一个块（标题带剩余时间标记），结尾附一句「怎么停止提醒」的说明。
    """
    blocks: list[str | list[MessagePart]] = [
        build_activity_block(activity, (), now, settings) for activity in activities
    ]
    if ack_hint:
        blocks.append(ack_hint)

    if len(activities) > 1:
        header = f"🚨 {len(activities)} 个活动马上就要结束了！"
    else:
        header = "🚨 活动马上就要结束了！"
    return _assemble(header, blocks, settings.activity_separator)


def build_urgent_dm_message(
    activities: Sequence[Activity],
    now: datetime,
    settings: MessageSettings,
    *,
    ack_hint: str = "",
) -> str:
    return parts_to_text(
        build_urgent_dm_parts(activities, now, settings, ack_hint=ack_hint)
    )
