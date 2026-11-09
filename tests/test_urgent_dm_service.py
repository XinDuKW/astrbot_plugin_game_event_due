"""紧急私聊提醒的决策逻辑。"""

from __future__ import annotations

from datetime import datetime, timedelta

from astrbot_plugin_game_event_due.services.urgent_dm_service import (
    DEFAULT_ACK_KEYWORDS,
    DEFAULT_QUIET_HOURS,
    UrgentDmSettings,
    UrgentTargetState,
    describe_target_settings,
    in_quiet_hours,
    is_ack_text,
    mark_acked,
    mark_sent,
    mentions_activity,
    normalize_ack_keywords,
    normalize_games,
    normalize_targets,
    parse_quiet_hours,
    parse_target_settings,
    should_notify_now,
    target_settings,
    urgent_activities,
)
from astrbot_plugin_game_event_due.sources.base import Activity, GAME_TIMEZONE

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=GAME_TIMEZONE)


def make_activity(
    name: str = "活动",
    *,
    end: timedelta = timedelta(hours=6),
    activity_id: str = "a1",
    game_id: str = "sr",
) -> Activity:
    return Activity(
        game_id=game_id,
        activity_id=activity_id,
        name=name,
        start_time=NOW - timedelta(days=1),
        end_time=NOW + end,
    )


def settings(**kwargs) -> UrgentDmSettings:
    base = {"enabled": True}
    base.update(kwargs)
    return UrgentDmSettings(**base)


# ---- 配置解析 ----


def test_normalize_targets_splits_and_dedupes():
    raw = "aiocqhttp:FriendMessage:1, aiocqhttp:FriendMessage:2\n"
    raw += "aiocqhttp:FriendMessage:1"
    assert normalize_targets(raw) == (
        "aiocqhttp:FriendMessage:1",
        "aiocqhttp:FriendMessage:2",
    )
    assert normalize_targets(["a", " a ", "", "b"]) == ("a", "b")
    assert normalize_targets(None) == ()


def test_normalize_games_lowercases():
    assert normalize_games("sr, genshin 、SR") == ("sr", "genshin")
    assert normalize_games("") == ()


def test_normalize_ack_keywords_falls_back_to_defaults():
    assert normalize_ack_keywords("收到，明白") == ("收到", "明白")
    assert normalize_ack_keywords("") == DEFAULT_ACK_KEYWORDS


def test_parse_quiet_hours_variants():
    assert parse_quiet_hours("23-8") == (23, 8)
    assert parse_quiet_hours("23:00-08:00") == (23, 8)
    assert parse_quiet_hours("0-7") == (0, 7)
    assert parse_quiet_hours("关闭") is None
    assert parse_quiet_hours("") is None
    assert parse_quiet_hours(None) == DEFAULT_QUIET_HOURS
    assert parse_quiet_hours("乱写的") == DEFAULT_QUIET_HOURS
    assert parse_quiet_hours("25-8") == DEFAULT_QUIET_HOURS


def test_in_quiet_hours_handles_wrap_around():
    quiet = (23, 8)
    assert in_quiet_hours(datetime(2026, 9, 10, 23, 30, tzinfo=GAME_TIMEZONE), quiet)
    assert in_quiet_hours(datetime(2026, 9, 10, 3, 0, tzinfo=GAME_TIMEZONE), quiet)
    assert not in_quiet_hours(NOW, quiet)
    assert not in_quiet_hours(NOW, None)
    assert in_quiet_hours(
        datetime(2026, 9, 10, 2, 0, tzinfo=GAME_TIMEZONE), (0, 7)
    )


def test_is_ack_text():
    assert is_ack_text("我已知晓", DEFAULT_ACK_KEYWORDS)
    assert is_ack_text("  我已知晓啦  ", DEFAULT_ACK_KEYWORDS)
    assert not is_ack_text("今天天气不错", DEFAULT_ACK_KEYWORDS)
    assert not is_ack_text("", DEFAULT_ACK_KEYWORDS)


def test_mentions_activity():
    activity = make_activity("方寸大冒险")
    assert mentions_activity("我已知晓 方寸大冒险", activity)
    assert not mentions_activity("我已知晓", activity)


# ---- 提醒决策 ----


def test_disabled_settings_never_notify():
    activity = make_activity()
    assert not should_notify_now(
        activity, UrgentTargetState(), NOW, UrgentDmSettings(enabled=False)
    )
    assert urgent_activities([activity], NOW, UrgentDmSettings(enabled=False)) == []


def test_notify_only_within_trigger_window():
    assert should_notify_now(make_activity(), UrgentTargetState(), NOW, settings())
    far = make_activity(end=timedelta(days=4))
    assert not should_notify_now(far, UrgentTargetState(), NOW, settings())
    assert should_notify_now(
        far, UrgentTargetState(), NOW, settings(trigger_days=5)
    )


def test_ended_activity_is_not_notified():
    ended = make_activity(end=timedelta(hours=-1))
    assert not should_notify_now(ended, UrgentTargetState(), NOW, settings())


def test_acked_activity_is_not_notified():
    activity = make_activity()
    state = mark_acked(UrgentTargetState(), NOW)
    assert not should_notify_now(activity, state, NOW, settings())


def test_repeat_interval_throttles():
    activity = make_activity()
    state = UrgentTargetState(last_sent_at=(NOW - timedelta(minutes=10)).isoformat())
    assert not should_notify_now(activity, state, NOW, settings(repeat_minutes=60))
    assert should_notify_now(activity, state, NOW, settings(repeat_minutes=5))


def test_daily_limit_blocks_and_resets_next_day():
    activity = make_activity(end=timedelta(days=3))
    today = NOW.date().isoformat()
    state = UrgentTargetState(
        daily_date=today,
        daily_count=3,
        last_sent_at=(NOW - timedelta(hours=5)).isoformat(),
    )
    window = {"trigger_days": 5}
    assert not should_notify_now(
        activity, state, NOW, settings(daily_limit=3, **window)
    )
    assert should_notify_now(
        activity, state, NOW, settings(daily_limit=4, **window)
    )

    tomorrow = NOW + timedelta(days=1)
    assert should_notify_now(
        activity, state, tomorrow, settings(daily_limit=3, **window)
    )


def test_quiet_hours_block_notifications():
    activity = make_activity(end=timedelta(days=2))
    late = datetime(2026, 9, 10, 23, 30, tzinfo=GAME_TIMEZONE)
    window = {"trigger_days": 3}
    assert not should_notify_now(
        activity, UrgentTargetState(), late, settings(**window)
    )
    assert should_notify_now(
        activity, UrgentTargetState(), late, settings(quiet_hours=None, **window)
    )


def test_urgent_activities_sorted_by_remaining_time():
    soon = make_activity("急", activity_id="a", end=timedelta(hours=2))
    later = make_activity("缓", activity_id="b", end=timedelta(hours=20))
    picked = urgent_activities([later, soon], NOW, settings())
    assert [item.name for item in picked] == ["急", "缓"]


def test_urgent_activities_respects_states():
    activity = make_activity()
    states = {activity.uid: {"acked": True}}
    assert urgent_activities([activity], NOW, settings(), states) == []


def test_mark_sent_accumulates_and_rolls_over_day():
    state = UrgentTargetState()
    mark_sent(state, NOW)
    assert state.daily_count == 1 and state.sent_total == 1
    assert state.last_sent_at.startswith("2026-09-10")

    mark_sent(state, NOW + timedelta(days=1))
    assert state.daily_count == 1 and state.sent_total == 2


def test_state_roundtrip():
    state = UrgentTargetState(
        last_sent_at=NOW.isoformat(),
        sent_total=2,
        daily_date=NOW.date().isoformat(),
        daily_count=1,
        acked=True,
        ack_at=NOW.isoformat(),
    )
    payload = state.to_dict()
    restored = UrgentTargetState.from_dict(payload)
    assert restored.to_dict() == payload
    assert UrgentTargetState.from_dict("垃圾数据").to_dict() == {}
    assert UrgentTargetState.from_dict({}).to_dict() == {}


# ---- 目标与状态的持久化 ----

from astrbot_plugin_game_event_due.services.subscription_service import (  # noqa: E402
    SubscriptionService,
)


def test_upsert_and_remove_urgent_target():
    data: dict[str, dict] = {}
    entry = SubscriptionService.upsert_urgent_target(data, "umo-1")
    assert entry["enabled"] is True
    assert data["umo-1"]["activities"] == {}

    SubscriptionService.upsert_urgent_target(
        data, "umo-1", enabled=False, games=["sr"]
    )
    assert data["umo-1"]["enabled"] is False
    assert data["umo-1"]["games"] == ["sr"]

    assert SubscriptionService.remove_urgent_target(data, "umo-1") is True
    assert SubscriptionService.remove_urgent_target(data, "umo-1") is False


def test_pausing_target_keeps_settings_and_activity_records():
    """「停止」只置 enabled=False，节奏设置与活动记录都要留着。"""
    data: dict[str, dict] = {}
    activity = make_activity("方寸大冒险")
    SubscriptionService.upsert_urgent_target(data, "umo-1")
    SubscriptionService.record_urgent_state(
        data, "umo-1", activity, mark_sent(UrgentTargetState(), NOW)
    )
    SubscriptionService.update_urgent_settings(data, "umo-1", {"trigger_days": 5})

    SubscriptionService.upsert_urgent_target(data, "umo-1", enabled=False)

    entry = data["umo-1"]
    assert entry["enabled"] is False
    assert entry["settings"]["trigger_days"] == 5
    assert activity.uid in entry["activities"]


def test_record_and_ack_urgent_state():
    activity = make_activity("方寸大冒险")
    data: dict[str, dict] = {}
    state = mark_sent(UrgentTargetState(), NOW)
    SubscriptionService.record_urgent_state(data, "umo-1", activity, state)

    bucket = data["umo-1"]["activities"]
    assert bucket[activity.uid]["name"] == "方寸大冒险"
    assert bucket[activity.uid]["end"]

    acked = SubscriptionService.ack_urgent_activities(data, "umo-1", NOW)
    assert acked == ["方寸大冒险"]
    assert bucket[activity.uid]["acked"] is True
    assert SubscriptionService.ack_urgent_activities(data, "umo-1", NOW) == []


def test_ack_can_target_a_single_activity():
    data: dict[str, dict] = {}
    first = make_activity("方寸大冒险", activity_id="a", end=timedelta(hours=6))
    second = make_activity("深渊回响", activity_id="b", end=timedelta(hours=8))
    for activity in (first, second):
        SubscriptionService.record_urgent_state(
            data, "umo-1", activity, mark_sent(UrgentTargetState(), NOW)
        )

    acked = SubscriptionService.ack_urgent_activities(data, "umo-1", NOW, ("方寸",))
    assert acked == ["方寸大冒险"]
    bucket = data["umo-1"]["activities"]
    assert bucket[first.uid]["acked"] is True
    assert not bucket[second.uid].get("acked")


def test_ack_without_name_only_stops_the_latest_round():
    """一句「我已知晓」只停最近一轮被催的活动，历史条目保持原样。"""
    data: dict[str, dict] = {}
    earlier = make_activity("老活动", activity_id="a", end=timedelta(hours=40))
    latest = make_activity("本轮活动", activity_id="b", end=timedelta(hours=6))
    SubscriptionService.record_urgent_state(
        data,
        "umo-1",
        earlier,
        mark_sent(UrgentTargetState(), NOW - timedelta(days=1)),
    )
    SubscriptionService.record_urgent_state(
        data, "umo-1", latest, mark_sent(UrgentTargetState(), NOW)
    )

    acked = SubscriptionService.ack_latest_batch(data, "umo-1", NOW)
    assert acked == ["本轮活动"]
    bucket = data["umo-1"]["activities"]
    assert bucket[latest.uid]["acked"] is True
    assert not bucket[earlier.uid].get("acked")


def test_ack_without_name_stops_every_activity_of_the_same_round():
    data: dict[str, dict] = {}
    first = make_activity("甲活动", activity_id="a", end=timedelta(hours=6))
    second = make_activity("乙活动", activity_id="b", end=timedelta(hours=8))
    for activity in (first, second):
        SubscriptionService.record_urgent_state(
            data, "umo-1", activity, mark_sent(UrgentTargetState(), NOW)
        )

    acked = SubscriptionService.ack_latest_batch(data, "umo-1", NOW)
    assert sorted(acked) == sorted(["甲活动", "乙活动"])
    assert SubscriptionService.ack_latest_batch(data, "umo-1", NOW) == []


def test_ack_without_name_ignores_never_sent_entries():
    data: dict[str, dict] = {}
    fresh = make_activity("还没催过", activity_id="c", end=timedelta(hours=5))
    SubscriptionService.record_urgent_state(
        data, "umo-1", fresh, UrgentTargetState()
    )

    assert SubscriptionService.ack_latest_batch(data, "umo-1", NOW) == []
    assert not data["umo-1"]["activities"][fresh.uid].get("acked")


def test_normalize_urgent_dm_drops_garbage():
    raw = {
        "targets": {
            "umo-1": {
                "enabled": True,
                "games": ["sr", ""],
                "note": "备注",
                "activities": {
                    "u1": {"name": "活动", "end": NOW.isoformat(), "acked": True},
                    "u2": "垃圾数据",
                    "u3": {},
                },
            },
            "": {"enabled": True},
            "umo-2": "垃圾数据",
        }
    }
    data = SubscriptionService._normalize_urgent_dm(raw)
    assert set(data) == {"umo-1"}
    entry = data["umo-1"]
    assert entry["games"] == ["sr"]
    assert entry["activities"]["u1"]["name"] == "活动"
    assert entry["activities"]["u1"]["acked"] is True
    assert "u2" not in entry["activities"]
    assert "u3" not in entry["activities"]


def test_prune_urgent_dm_keeps_targets():
    data: dict[str, dict] = {}
    old = make_activity("老活动", activity_id="old", end=timedelta(days=-40))
    fresh = make_activity("新活动", activity_id="new", end=timedelta(days=2))
    for activity in (old, fresh):
        SubscriptionService.record_urgent_state(
            data, "umo-1", activity, mark_sent(UrgentTargetState(), NOW)
        )

    removed = SubscriptionService.prune_urgent_dm(data, NOW, 30)
    assert removed == 1
    assert set(data["umo-1"]["activities"]) == {fresh.uid}
    assert "umo-1" in data


# ---- 每个目标自己的节奏 ----


def test_parse_target_settings_accepts_units():
    assert parse_target_settings(["3天", "2小时", "3次"]) == {
        "trigger_days": 3,
        "repeat_minutes": 120,
        "daily_limit": 3,
    }
    assert parse_target_settings(["7d"]) == {"trigger_days": 7}
    assert parse_target_settings(["5日"]) == {"trigger_days": 5}
    assert parse_target_settings(["90分钟"]) == {"repeat_minutes": 90}
    assert parse_target_settings(["5h"]) == {"repeat_minutes": 300}
    assert parse_target_settings(["2次"]) == {"daily_limit": 2}


def test_parse_target_settings_treats_plain_number_as_days():
    assert parse_target_settings(["3"]) == {"trigger_days": 3}


def test_parse_target_settings_default_clears_overrides():
    assert parse_target_settings(["默认"]) is None
    assert parse_target_settings(["跟随", "3天"]) is None
    assert parse_target_settings([]) == {}
    assert parse_target_settings(["看不懂的东西"]) == {}


def test_parse_target_settings_clamps_to_sane_range():
    assert parse_target_settings(["0天"]) == {"trigger_days": 1}
    assert parse_target_settings(["9999天"]) == {"trigger_days": 365}
    assert parse_target_settings(["1分钟"]) == {"repeat_minutes": 5}
    assert parse_target_settings(["0分钟"]) == {"repeat_minutes": 5}
    assert parse_target_settings(["9999次"]) == {"daily_limit": 100}


def test_target_settings_only_overrides_configured_keys():
    base = settings(trigger_days=1, repeat_minutes=120, daily_limit=3)
    merged = target_settings(base, {"settings": {"trigger_days": 7}})
    assert merged.trigger_days == 7
    assert merged.repeat_minutes == base.repeat_minutes
    assert merged.daily_limit == base.daily_limit


def test_target_settings_falls_back_to_global():
    base = settings(trigger_days=3)
    assert target_settings(base, None) is base
    assert target_settings(base, {}) is base
    assert target_settings(base, {"settings": {}}) is base
    assert target_settings(base, {"settings": {"unknown": 1}}) is base


def test_target_settings_ignores_garbage_values():
    base = settings(trigger_days=1, daily_limit=3)
    merged = target_settings(
        base, {"settings": {"trigger_days": "5", "daily_limit": "很多"}}
    )
    assert merged.trigger_days == 5
    assert merged.daily_limit == base.daily_limit


def test_should_notify_now_follows_per_target_trigger_days():
    activity = make_activity(end=timedelta(hours=60))
    strict = settings(trigger_days=1, quiet_hours=None)
    relaxed = settings(trigger_days=3, quiet_hours=None)
    assert should_notify_now(activity, None, NOW, strict) is False
    assert should_notify_now(activity, None, NOW, relaxed) is True


def test_should_notify_now_follows_per_target_daily_limit():
    activity = make_activity(end=timedelta(hours=6))
    state = UrgentTargetState(
        last_sent_at=(NOW - timedelta(hours=3)).isoformat(),
        daily_date=NOW.date().isoformat(),
        daily_count=1,
    )
    base = settings(repeat_minutes=60, daily_limit=3, quiet_hours=None)
    capped = settings(repeat_minutes=60, daily_limit=1, quiet_hours=None)
    assert should_notify_now(activity, state, NOW, base) is True
    assert should_notify_now(activity, state, NOW, capped) is False


def test_describe_target_settings_is_human_readable():
    assert (
        describe_target_settings(
            settings(trigger_days=3, repeat_minutes=120, daily_limit=3)
        )
        == "3 天内 · 每 2 小时 · 每天最多 3 次"
    )
    assert (
        describe_target_settings(
            settings(trigger_days=1, repeat_minutes=90, daily_limit=0)
        )
        == "1 天内 · 每 90 分钟 · 不限次数"
    )


# ---- 每个目标设置的读写 ----


def test_update_urgent_settings_merges_and_clears():
    data: dict[str, dict] = {}
    SubscriptionService.upsert_urgent_target(data, "umo-1", enabled=True)
    SubscriptionService.update_urgent_settings(
        data, "umo-1", {"trigger_days": 3, "repeat_minutes": 120}
    )
    assert data["umo-1"]["settings"] == {"trigger_days": 3, "repeat_minutes": 120}

    SubscriptionService.update_urgent_settings(data, "umo-1", {"daily_limit": 2})
    assert data["umo-1"]["settings"] == {
        "trigger_days": 3,
        "repeat_minutes": 120,
        "daily_limit": 2,
    }

    SubscriptionService.update_urgent_settings(data, "umo-1", {"trigger_days": None})
    assert data["umo-1"]["settings"] == {"repeat_minutes": 120, "daily_limit": 2}


def test_update_urgent_settings_drops_key_when_empty():
    data: dict[str, dict] = {}
    SubscriptionService.upsert_urgent_target(
        data, "umo-1", settings={"daily_limit": 1}
    )
    assert data["umo-1"]["settings"] == {"daily_limit": 1}
    SubscriptionService.update_urgent_settings(data, "umo-1", {"daily_limit": None})
    assert "settings" not in data["umo-1"]


def test_update_urgent_settings_ignores_unknown_target_and_keys():
    data: dict[str, dict] = {}
    assert (
        SubscriptionService.update_urgent_settings(data, "nobody", {"trigger_days": 3})
        is None
    )
    SubscriptionService.upsert_urgent_target(data, "umo-1")
    SubscriptionService.update_urgent_settings(
        data, "umo-1", {"garbage": 1, "trigger_days": 0}
    )
    assert data["umo-1"]["settings"] == {"trigger_days": 1}


def test_normalize_urgent_dm_keeps_target_settings():
    raw = {
        "targets": {
            "umo-1": {"enabled": True, "settings": {"trigger_days": "7", "junk": 1}}
        }
    }
    data = SubscriptionService._normalize_urgent_dm(raw)
    assert data["umo-1"]["settings"] == {"trigger_days": 7}
