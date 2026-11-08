"""紧急私聊提醒的决策逻辑。

本模块是纯逻辑：不依赖 astrbot，也不做任何 IO。目标如何被发现、消息怎么发出去、
状态存在哪里，都由调用方（轮询层与主类）负责；这里只回答两件事：

* 某个活动现在需不需要给某个私聊目标**再**提醒一次；
* 用户发来的那句话是不是「确认知晓」。

节流与安全阀（静默时段、重复间隔、每日上限、确认后停止）是这类「疯狗式提醒」
能长期用下去的前提：QQ 对高频私聊非常敏感，无节制轰炸很容易被风控。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from ..sources.base import Activity, parse_datetime

DEFAULT_TRIGGER_DAYS = 1
DEFAULT_REPEAT_MINUTES = 120
DEFAULT_DAILY_LIMIT = 3
DEFAULT_QUIET_HOURS = (23, 8)
DEFAULT_ACK_KEYWORDS = ("我已知晓", "已知晓", "我知道了", "知道了")

MAX_TARGETS = 32
MAX_GAMES = 32

QUIET_OFF_TOKENS = ("", "-", "关闭", "off", "none", "无", "关")


@dataclass(frozen=True, slots=True)
class UrgentDmSettings:
    """紧急私聊提醒的全局配置。"""

    enabled: bool = False
    targets: tuple[str, ...] = ()
    trigger_days: int = DEFAULT_TRIGGER_DAYS
    repeat_minutes: int = DEFAULT_REPEAT_MINUTES
    daily_limit: int = DEFAULT_DAILY_LIMIT
    quiet_hours: tuple[int, int] | None = DEFAULT_QUIET_HOURS
    ack_keywords: tuple[str, ...] = DEFAULT_ACK_KEYWORDS
    games: tuple[str, ...] = ()

    @property
    def trigger_delta(self) -> timedelta:
        return timedelta(days=max(1, int(self.trigger_days)))


@dataclass(slots=True)
class UrgentTargetState:
    """某个私聊目标对某个活动已经提醒到什么程度。"""

    last_sent_at: str = ""
    sent_total: int = 0
    daily_date: str = ""
    daily_count: int = 0
    acked: bool = False
    ack_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.last_sent_at:
            payload["last_sent_at"] = self.last_sent_at
        if self.sent_total:
            payload["sent_total"] = self.sent_total
        if self.daily_date:
            payload["daily_date"] = self.daily_date
        if self.daily_count:
            payload["daily_count"] = self.daily_count
        if self.acked:
            payload["acked"] = True
        if self.ack_at:
            payload["ack_at"] = self.ack_at
        return payload

    @classmethod
    def from_dict(cls, raw: Any) -> UrgentTargetState:
        if not isinstance(raw, cls):
            raw = raw if isinstance(raw, dict) else {}
        else:
            return raw
        try:
            sent_total = int(raw.get("sent_total") or 0)
        except (TypeError, ValueError):
            sent_total = 0
        try:
            daily_count = int(raw.get("daily_count") or 0)
        except (TypeError, ValueError):
            daily_count = 0
        return cls(
            last_sent_at=str(raw.get("last_sent_at") or ""),
            sent_total=max(0, sent_total),
            daily_date=str(raw.get("daily_date") or ""),
            daily_count=max(0, daily_count),
            acked=bool(raw.get("acked")),
            ack_at=str(raw.get("ack_at") or ""),
        )


def _split_items(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        normalized = (
            value.replace("，", ",")
            .replace("、", ",")
            .replace(";", ",")
            .replace("；", ",")
            .replace("\r", "\n")
        )
        parts: list[str] = []
        for chunk in normalized.split("\n"):
            parts.extend(chunk.split(","))
        return parts
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return [str(value)]


def normalize_targets(value: Any) -> tuple[str, ...]:
    """把配置里的私聊目标（UMO 列表）规范化：去空、去重、保序。"""
    result: list[str] = []
    for item in _split_items(value):
        text = item.strip()
        if not text or text in result:
            continue
        result.append(text)
    return tuple(result[:MAX_TARGETS])


def normalize_games(value: Any) -> tuple[str, ...]:
    """限定参与私聊提醒的游戏代号。"""
    result: list[str] = []
    for item in _split_items(value):
        text = item.strip().casefold()
        if not text or text in result:
            continue
        result.append(text)
    return tuple(result[:MAX_GAMES])


def normalize_ack_keywords(value: Any) -> tuple[str, ...]:
    """确认知晓的关键词；留空时回退到默认词表。"""
    result: list[str] = []
    for item in _split_items(value):
        text = item.strip()
        if not text or text in result:
            continue
        result.append(text)
    return tuple(result) or DEFAULT_ACK_KEYWORDS


def parse_quiet_hours(value: Any) -> tuple[int, int] | None:
    """解析静默时段，支持 23-8 / 23:00-08:00 / 关闭。"""
    if value is None:
        return DEFAULT_QUIET_HOURS
    if isinstance(value, (list, tuple)) and len(value) == 2:
        items = [str(value[0]), str(value[1])]
    else:
        text = str(value).strip()
        if text.casefold() in QUIET_OFF_TOKENS:
            return None
        normalized = text.replace("：", ":").replace("~", "-").replace("—", "-")
        items = [part for part in normalized.replace(" ", "").split("-") if part]
        if len(items) != 2:
            return DEFAULT_QUIET_HOURS
    try:
        start = int(items[0].split(":")[0])
        end = int(items[1].split(":")[0])
    except (IndexError, ValueError):
        return DEFAULT_QUIET_HOURS
    if not (0 <= start <= 23 and 0 <= end <= 24) or start == end:
        return DEFAULT_QUIET_HOURS
    return (start, end)


def in_quiet_hours(now: datetime, quiet: tuple[int, int] | None) -> bool:
    """当前时刻是否落在静默时段内（支持跨零点，例如 23-8）。"""
    if quiet is None:
        return False
    start, end = quiet
    if start == end:
        return False
    hour = now.hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def is_ack_text(text: str, keywords: Sequence[str]) -> bool:
    """用户发来的内容是否表示「我已知晓」。"""
    normalized = " ".join(str(text or "").split()).casefold()
    if not normalized:
        return False
    return any(
        keyword.casefold() in normalized
        for keyword in keywords or DEFAULT_ACK_KEYWORDS
        if keyword
    )


def mentions_activity(text: str, activity: Activity) -> bool:
    """确认语句里是否点名了该活动（用于「只停这一个」）。"""
    normalized = " ".join(str(text or "").split()).casefold()
    name = str(activity.name or "").strip().casefold()
    return bool(name) and name in normalized


def should_notify_now(
    activity: Activity,
    state: UrgentTargetState | Mapping[str, Any] | None,
    now: datetime,
    settings: UrgentDmSettings,
) -> bool:
    """判断该活动此刻是否应当给这个目标再发一条私聊提醒。"""
    if not settings.enabled:
        return False

    current = UrgentTargetState.from_dict(state)
    if current.acked:
        return False
    if activity.is_ended(now):
        return False

    remaining = activity.remaining(now)
    if remaining is None or remaining <= timedelta(0):
        return False
    if remaining > settings.trigger_delta:
        return False

    if in_quiet_hours(now, settings.quiet_hours):
        return False

    limit = max(0, int(settings.daily_limit))
    today = now.date().isoformat()
    used_today = current.daily_count if current.daily_date == today else 0
    if limit and used_today >= limit:
        return False

    last_sent = parse_datetime(current.last_sent_at)
    interval = timedelta(minutes=max(1, int(settings.repeat_minutes)))
    if last_sent is not None and now - last_sent < interval:
        return False
    return True


def urgent_activities(
    activities: Sequence[Activity],
    now: datetime,
    settings: UrgentDmSettings,
    states: Mapping[str, Any] | None = None,
) -> list[Activity]:
    """挑出此刻需要私聊提醒的活动，按剩余时间升序（最紧急在前）。"""
    if not settings.enabled:
        return []
    lookup = states or {}
    picked = [
        activity
        for activity in activities
        if should_notify_now(activity, lookup.get(activity.uid), now, settings)
    ]

    def sort_key(activity: Activity) -> float:
        remaining = activity.remaining(now)
        return remaining.total_seconds() if remaining else 0.0

    picked.sort(key=sort_key)
    return picked


def mark_sent(state: UrgentTargetState, now: datetime) -> UrgentTargetState:
    """记录一次成功发送：当日计数（跨天自动重置）与累计次数。"""
    today = now.date().isoformat()
    if state.daily_date != today:
        state.daily_date = today
        state.daily_count = 0
    state.daily_count += 1
    state.sent_total += 1
    state.last_sent_at = now.isoformat()
    return state


def mark_acked(state: UrgentTargetState, now: datetime) -> UrgentTargetState:
    """记录用户已确认知晓，此后不再提醒。"""
    state.acked = True
    state.ack_at = now.isoformat()
    return state


# ---- 每个目标自己的提醒节奏 ----

SETTING_TRIGGER = "trigger_days"
SETTING_REPEAT = "repeat_minutes"
SETTING_LIMIT = "daily_limit"
TARGET_SETTING_KEYS = (SETTING_TRIGGER, SETTING_REPEAT, SETTING_LIMIT)

MIN_TRIGGER_DAYS = 1
MAX_TRIGGER_DAYS = 365
MIN_REPEAT_MINUTES = 5
MAX_REPEAT_MINUTES = 1440
MIN_DAILY_LIMIT = 0
MAX_DAILY_LIMIT = 100

DEFAULT_SETTING_TOKENS = ("默认", "跟随", "重置", "清除", "reset", "default")

_DAYS_PATTERN = re.compile(r"^(\d+)\s*(?:天|日|d|day)$")
_HOURS_PATTERN = re.compile(r"^(\d+)\s*(?:小时|时|h|hour)$")
_MINUTES_PATTERN = re.compile(r"^(\d+)\s*(?:分钟|分|m|min)$")
_TIMES_PATTERN = re.compile(r"^(\d+)\s*(?:次|条|回)$")
_PLAIN_PATTERN = re.compile(r"^(\d+)$")


def clamp_setting(key: str, value: Any) -> int | None:
    """把一个覆盖值夹到合法区间；不认识的键或非数字返回 None。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if key == SETTING_TRIGGER:
        return min(MAX_TRIGGER_DAYS, max(MIN_TRIGGER_DAYS, number))
    if key == SETTING_REPEAT:
        return min(MAX_REPEAT_MINUTES, max(MIN_REPEAT_MINUTES, number))
    if key == SETTING_LIMIT:
        return min(MAX_DAILY_LIMIT, max(MIN_DAILY_LIMIT, number))
    return None


def parse_target_settings(tokens: Sequence[Any]) -> dict[str, int] | None:
    """把命令参数解析成某个目标的提醒节奏。

    支持的单位：3天 / 2小时 / 120分钟 / 3次；裸数字按「天数」处理。
    返回 None 表示清空覆盖（恢复全局默认），返回空字典表示没有可识别的参数。
    """
    items = [str(token).strip() for token in tokens]
    items = [item for item in items if item]
    if not items:
        return {}
    if any(item.casefold() in DEFAULT_SETTING_TOKENS for item in items):
        return None

    parsed: dict[str, int] = {}
    for item in items:
        folded = item.casefold()
        matched = _DAYS_PATTERN.match(folded)
        if matched:
            parsed[SETTING_TRIGGER] = clamp_setting(SETTING_TRIGGER, matched.group(1))
            continue
        matched = _HOURS_PATTERN.match(folded)
        if matched:
            minutes = int(matched.group(1)) * 60
            parsed[SETTING_REPEAT] = clamp_setting(SETTING_REPEAT, minutes)
            continue
        matched = _MINUTES_PATTERN.match(folded)
        if matched:
            parsed[SETTING_REPEAT] = clamp_setting(
                SETTING_REPEAT, matched.group(1)
            )
            continue
        matched = _TIMES_PATTERN.match(folded)
        if matched:
            parsed[SETTING_LIMIT] = clamp_setting(SETTING_LIMIT, matched.group(1))
            continue
        matched = _PLAIN_PATTERN.match(folded)
        if matched:
            parsed[SETTING_TRIGGER] = clamp_setting(
                SETTING_TRIGGER, matched.group(1)
            )
    return parsed


def target_settings(
    base: UrgentDmSettings,
    entry: Mapping[str, Any] | None = None,
) -> UrgentDmSettings:
    """把某个目标自己配置的节奏合到全局设置上。

    目标记录里的 settings 只认 trigger_days / repeat_minutes / daily_limit；
    没写的项跟随全局，这样全局改默认值时不会把每个人的个性化设置冲掉。
    """
    overrides = entry.get("settings") if isinstance(entry, Mapping) else None
    if not isinstance(overrides, Mapping):
        return base
    patch: dict[str, int] = {}
    for key in TARGET_SETTING_KEYS:
        number = clamp_setting(key, overrides.get(key))
        if number is not None:
            patch[key] = number
    if not patch:
        return base
    return replace(base, **patch)


def describe_target_settings(settings: UrgentDmSettings) -> str:
    """人话描述一套节奏，用在命令回显里。"""
    days = max(MIN_TRIGGER_DAYS, int(settings.trigger_days))
    repeat = max(1, int(settings.repeat_minutes))
    limit = max(0, int(settings.daily_limit))
    if repeat >= 60 and repeat % 60 == 0:
        interval = f"{repeat // 60} 小时"
    else:
        interval = f"{repeat} 分钟"
    quota = f"每天最多 {limit} 次" if limit else "不限次数"
    return f"{days} 天内 · 每 {interval} · {quota}"
