"""订阅存储、状态初始化与裁剪。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from astrbot_plugin_game_event_due.services.reminder_service import (
    NEW_ACTIVITY_ON_START,
    ActivityNotifyState,
    ReminderPolicy,
)
from astrbot_plugin_game_event_due.services.subscription_service import (
    BIND_CODE_KEY,
    MAX_BIND_CODES,
    SubscriptionService,
    normalize_session,
    resolve_policy,
)
from astrbot_plugin_game_event_due.sources.base import Activity, GAME_TIMEZONE

from helpers import FakeKV

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=GAME_TIMEZONE)
UMO = "aiocqhttp:GroupMessage:123456"
POLICY = ReminderPolicy(thresholds=(7, 3, 1), notify_new=True, lookahead_days=45)


def make_service() -> tuple[SubscriptionService, FakeKV]:
    kv = FakeKV()
    service = SubscriptionService(kv.get, kv.put)
    return service, kv


def make_activity(
    activity_id: str = "a1",
    *,
    start: timedelta = timedelta(days=-1),
    end: timedelta = timedelta(days=5),
    name: str = "活动",
) -> Activity:
    return Activity(
        game_id="sr",
        activity_id=activity_id,
        name=name,
        start_time=NOW + start,
        end_time=NOW + end,
    )


# ---- 规范化 ----


def test_normalize_session_defaults():
    session = normalize_session(None)
    assert session["enabled"] is True
    assert session["games"] == []
    assert session["thresholds"] is None
    assert session["notify_new"] is None
    assert session["whitelist"] == []
    assert session["blacklist"] == []


def test_normalize_session_dedupes_games():
    session = normalize_session({"games": ["sr", "sr", "", "ys"]})
    assert session["games"] == ["sr", "ys"]


def test_normalize_session_disabled_thresholds_survive():
    session = normalize_session({"thresholds": []})
    assert session["thresholds"] == []


# ---- 订阅读写 ----


@pytest.mark.anyio
async def test_add_and_list_games():
    service, _ = make_service()
    await service.add_games(UMO, ["sr", "ys"])
    session = await service.get_session(UMO)
    assert session is not None
    assert session["games"] == ["sr", "ys"]
    assert session["enabled"] is True
    assert session["created_at"]

    await service.add_games(UMO, ["sr", "zzz"])
    session = await service.get_session(UMO)
    assert session["games"] == ["sr", "ys", "zzz"]


@pytest.mark.anyio
async def test_remove_games_clears_empty_session():
    service, _ = make_service()
    await service.add_games(UMO, ["sr"])
    assert await service.remove_games(UMO, ["sr"]) is None
    assert await service.get_session(UMO) is None


@pytest.mark.anyio
async def test_remove_session_reports_existence():
    service, _ = make_service()
    await service.add_games(UMO, ["sr"])
    assert await service.remove_session(UMO) is True
    assert await service.remove_session(UMO) is False


@pytest.mark.anyio
async def test_subscriptions_for_game_filters_disabled():
    service, _ = make_service()
    await service.add_games(UMO, ["sr"])
    await service.add_games("aiocqhttp:FriendMessage:1", ["ys"])
    assert [umo for umo, _ in await service.subscriptions_for_game("sr")] == [UMO]

    await service.update_session(UMO, {"enabled": False})
    assert await service.subscriptions_for_game("sr") == []


@pytest.mark.anyio
async def test_update_session_ignores_unknown_fields():
    service, _ = make_service()
    await service.add_games(UMO, ["sr"])
    session = await service.update_session(
        UMO, {"blacklist": ["双倍"], "unknown": "x"}
    )
    assert session is not None
    assert session["blacklist"] == ["双倍"]
    assert "unknown" not in session


@pytest.mark.anyio
async def test_update_session_missing_returns_none():
    service, _ = make_service()
    assert await service.update_session(UMO, {"enabled": True}) is None


@pytest.mark.anyio
async def test_clear_all():
    service, _ = make_service()
    await service.add_games(UMO, ["sr"])
    await service.add_games("aiocqhttp:FriendMessage:2", ["ys"])
    assert await service.clear_all() == 2
    assert await service.get_all() == {}


# ---- 策略合并 ----


def test_resolve_policy_uses_defaults_when_not_overridden():
    session = normalize_session({"games": ["sr"]})
    assert resolve_policy(session, POLICY) is POLICY


def test_resolve_policy_applies_overrides():
    session = normalize_session(
        {"thresholds": [14, 1], "notify_new": False}
    )
    policy = resolve_policy(session, POLICY)
    assert policy.thresholds == (14, 1)
    assert policy.notify_new is False
    # 未覆盖的字段保持默认
    assert policy.lookahead_days == POLICY.lookahead_days


def test_resolve_policy_disabled_countdown():
    session = normalize_session({"thresholds": []})
    policy = resolve_policy(session, POLICY)
    assert policy.thresholds == ()
    assert policy.countdown_enabled is False
    assert policy.enabled is True


# ---- 提醒状态 ----


def test_build_initial_states_marks_new_and_current_tier():
    activities = [
        make_activity("a1", end=timedelta(days=20)),
        make_activity("a2", end=timedelta(days=2)),
        make_activity("a3", end=timedelta(days=-1)),
    ]
    states = SubscriptionService.build_initial_states(activities, NOW, POLICY)

    assert states["sr|a1"]["new"] is True
    assert "tier" not in states["sr|a1"]

    assert states["sr|a2"]["new"] is True
    assert states["sr|a2"]["tier"] == "d3"

    # 已结束的活动不写入状态
    assert "sr|a3" not in states


def test_build_initial_states_preview_marks_unstarted_as_known():
    # 「可预告通知」：订阅时还没开始的活动也算已经预告过，不会开服再补一次
    states = SubscriptionService.build_initial_states(
        [make_activity("a1", start=timedelta(days=3), end=timedelta(days=20))],
        NOW,
        POLICY,
    )
    assert states["sr|a1"]["new"] is True


def test_build_initial_states_on_start_keeps_unstarted_pending():
    # 「开启当天通知」：订阅时还没开始的活动不能吞掉，等活动开了再推一次
    policy = ReminderPolicy(thresholds=(7, 3, 1), notify_new=True, lookahead_days=45,
                            new_activity_timing=NEW_ACTIVITY_ON_START)
    states = SubscriptionService.build_initial_states(
        [
            make_activity("a1", start=timedelta(days=3), end=timedelta(days=20)),
            make_activity("a2", start=timedelta(days=-1), end=timedelta(days=2)),
        ],
        NOW,
        policy,
    )
    # 只有 new=True 才会写进 KV，所以这里断言「没有 new 标记」
    assert "new" not in states["sr|a1"]
    assert "tier" not in states["sr|a1"]
    assert states["sr|a2"]["new"] is True
    assert states["sr|a2"]["tier"] == "d3"


def test_build_initial_states_with_countdown_disabled():
    policy = ReminderPolicy(thresholds=(), notify_new=True)
    states = SubscriptionService.build_initial_states(
        [make_activity("a1", end=timedelta(hours=2))], NOW, policy
    )
    assert states["sr|a1"]["new"] is True
    assert "tier" not in states["sr|a1"]


@pytest.mark.anyio
async def test_notify_state_round_trip():
    service, _ = make_service()
    data = {UMO: {"sr|a1": {"new": True, "tier": "d3", "end": "2026-09-20T12:00:00+08:00"}}}
    await service.save_notify_state(data)
    loaded = await service.get_notify_state()
    assert loaded[UMO]["sr|a1"]["tier"] == "d3"
    assert loaded[UMO]["sr|a1"]["end"].startswith("2026-09-20")


def test_record_plan_merges_state():
    data: dict = {}
    activity = make_activity("a1", end=timedelta(days=2))
    state = ActivityNotifyState(new_done=True, tier="d3")
    SubscriptionService.record_plan(data, UMO, activity, state)

    entry = data[UMO]["sr|a1"]
    assert entry["new"] is True
    assert entry["tier"] == "d3"
    assert entry["end"].startswith("2026-09-12")


def test_record_plan_drops_empty_entry():
    data = {UMO: {"sr|a1": {"new": True}}}
    SubscriptionService.record_plan(
        data, UMO, make_activity("a1"), ActivityNotifyState()
    )
    assert data[UMO] == {}


def test_record_plan_bucket_stays_addressable():
    """清空最后一个条目后，仍在轮询中持有的桶引用必须依然有效。"""
    data: dict = {}
    bucket = data.setdefault(UMO, {})
    activity = make_activity("a1")

    SubscriptionService.record_plan(data, UMO, activity, ActivityNotifyState())
    assert data[UMO] is bucket

    SubscriptionService.record_plan(
        data, UMO, activity, ActivityNotifyState(new_done=True)
    )
    assert bucket[activity.uid]["new"] is True
    assert data[UMO] is bucket


def test_prune_notify_state_removes_expired_entries():
    data = {
        UMO: {
            "sr|old": {"new": True, "end": (NOW - timedelta(days=60)).isoformat()},
            "sr|recent": {
                "new": True,
                "end": (NOW - timedelta(days=5)).isoformat(),
            },
            "sr|unknown": {"new": True},
        },
        "aiocqhttp:FriendMessage:9": {
            "sr|old": {"new": True, "end": (NOW - timedelta(days=90)).isoformat()}
        },
    }
    removed = SubscriptionService.prune_notify_state(data, NOW, retention_days=30)

    assert removed == 2
    assert set(data[UMO]) == {"sr|recent", "sr|unknown"}
    assert "aiocqhttp:FriendMessage:9" not in data


def test_drop_session_state():
    data = {UMO: {"sr|a1": {"new": True}}}
    SubscriptionService.drop_session_state(data, UMO)
    assert data == {}

# ---- 会话迁移（换群 / 换机器人账号） ----

OLD = "aiocqhttp:GroupMessage:111111"
NEW = "aiocqhttp:GroupMessage:222222"


async def _seed(service: SubscriptionService) -> None:
    await service.add_games(OLD, ["sr", "ys"])
    await service.update_session(OLD, {"thresholds": [5, 2]})
    await service.save_notify_state({OLD: {"sr|a1": {"new": True, "tier": "d3"}}})
    urgent = await service.get_urgent_dm()
    SubscriptionService.upsert_urgent_target(urgent, OLD, games=["sr"])
    await service.save_urgent_dm(urgent)


@pytest.mark.anyio
async def test_migrate_session_moves_all_three_kv():
    service, _ = make_service()
    await _seed(service)

    result = await service.migrate_session(OLD, NEW)

    assert result["moved"] is True
    assert result["session"] is True
    assert result["urgent"] is True
    assert result["notify_entries"] == 1
    assert result["merged"] is False

    assert await service.get_session(OLD) is None
    session = await service.get_session(NEW)
    assert session is not None
    assert session["games"] == ["sr", "ys"]
    assert set(session["thresholds"]) == {5, 2}

    states = await service.get_notify_state()
    assert OLD not in states
    assert states[NEW]["sr|a1"]["tier"] == "d3"

    urgent = await service.get_urgent_dm()
    assert OLD not in urgent
    assert urgent[NEW]["games"] == ["sr"]


@pytest.mark.anyio
async def test_migrate_session_merges_into_existing_target():
    service, _ = make_service()
    await _seed(service)
    await service.add_games(NEW, ["ww"])
    await service.update_session(NEW, {"thresholds": [7], "blacklist": ["签到"]})
    # save_notify_state 是整体写入，必须先读再写，否则会把旧会话的进度一起清掉
    states = await service.get_notify_state()
    states.setdefault(NEW, {})["ww|b1"] = {"new": True}
    await service.save_notify_state(states)

    result = await service.migrate_session(OLD, NEW)

    assert result["merged"] is True
    assert result["notify_entries"] == 2
    session = await service.get_session(NEW)
    assert session["games"] == ["sr", "ys", "ww"]
    # 冲突项以目标会话为准
    assert set(session["thresholds"]) == {7}
    assert session["blacklist"] == ["签到"]
    states = await service.get_notify_state()
    assert set(states[NEW]) == {"sr|a1", "ww|b1"}


@pytest.mark.anyio
async def test_migrate_session_overwrite_replaces_target():
    service, _ = make_service()
    await _seed(service)
    await service.add_games(NEW, ["ww"])
    await service.update_session(NEW, {"thresholds": [7]})

    await service.migrate_session(OLD, NEW, merge=False)

    session = await service.get_session(NEW)
    assert session["games"] == ["sr", "ys"]
    assert set(session["thresholds"]) == {5, 2}


@pytest.mark.anyio
async def test_migrate_session_without_source_changes_nothing():
    service, kv = make_service()
    await service.add_games(NEW, ["ww"])
    await service.save_notify_state({NEW: {"ww|b1": {"new": True}}})
    before = dict(kv.store)

    result = await service.migrate_session(OLD, NEW)

    assert result["moved"] is False
    assert kv.store == before


@pytest.mark.anyio
async def test_migrate_session_rejects_same_umo():
    service, _ = make_service()
    await _seed(service)
    with pytest.raises(ValueError):
        await service.migrate_session(OLD, OLD)


@pytest.mark.anyio
async def test_migrate_urgent_target_keeps_subscriptions():
    service, _ = make_service()
    await _seed(service)

    result = await service.migrate_urgent_target(OLD, NEW)

    assert result["moved"] is True
    assert await service.get_session(OLD) is not None
    urgent = await service.get_urgent_dm()
    assert OLD not in urgent
    assert NEW in urgent


@pytest.mark.anyio
async def test_migrate_urgent_target_merges_progress():
    service, _ = make_service()
    urgent = await service.get_urgent_dm()
    SubscriptionService.upsert_urgent_target(urgent, OLD, games=["sr"])
    urgent[OLD]["activities"] = {"sr|a1": {"sent_total": 3}}
    SubscriptionService.upsert_urgent_target(urgent, NEW, games=["ww"])
    urgent[NEW]["activities"] = {
        "sr|a1": {"sent_total": 9},
        "ww|b1": {"sent_total": 1},
    }
    await service.save_urgent_dm(urgent)

    await service.migrate_urgent_target(OLD, NEW)

    merged = (await service.get_urgent_dm())[NEW]
    assert merged["games"] == ["sr", "ww"]
    # 目标已有进度优先
    assert merged["activities"]["sr|a1"]["sent_total"] == 9
    assert merged["activities"]["ww|b1"]["sent_total"] == 1


def test_create_bind_code_is_six_digits_and_stored():
    data: dict = {}
    code = SubscriptionService.create_bind_code(
        data, source=UMO, sender="sender-1", now=NOW
    )
    assert len(code) == 6 and code.isdigit()
    entry = data[code]
    assert entry["source"] == UMO
    assert entry["sender"] == "sender-1"
    assert entry["used"] is False
    assert entry["created"] == NOW.isoformat()
    assert entry["expires"] == (NOW + timedelta(minutes=15)).isoformat()


def test_create_bind_code_drops_expired_and_used_entries():
    data: dict = {}
    old = SubscriptionService.create_bind_code(
        data, source=UMO, sender="a", now=NOW - timedelta(minutes=30)
    )
    used = SubscriptionService.create_bind_code(
        data, source=UMO, sender="b", now=NOW
    )
    data[used]["used"] = True

    fresh = SubscriptionService.create_bind_code(
        data, source=UMO, sender="c", now=NOW
    )

    assert old not in data
    assert used not in data
    assert fresh in data
    assert len(data) == 1


def test_create_bind_code_trims_oldest_when_full():
    data: dict = {}
    first = SubscriptionService.create_bind_code(
        data, source=UMO, sender="a", now=NOW
    )
    for index in range(MAX_BIND_CODES - 1):
        SubscriptionService.create_bind_code(
            data, source=UMO, sender=f"s{index}", now=NOW
        )
    assert len(data) == MAX_BIND_CODES

    SubscriptionService.create_bind_code(data, source=UMO, sender="x", now=NOW)

    assert len(data) == MAX_BIND_CODES
    assert first not in data


def test_consume_bind_code_marks_used_and_returns_copy():
    data: dict = {}
    code = SubscriptionService.create_bind_code(
        data, source=UMO, sender="sender-1", now=NOW
    )

    entry = SubscriptionService.consume_bind_code(data, code, NOW)

    assert entry is not None
    assert entry["source"] == UMO
    assert entry["used"] is True
    assert data[code]["used"] is True
    entry["source"] = "tampered"
    assert data[code]["source"] == UMO
    # 同一个码只认一次
    assert SubscriptionService.consume_bind_code(data, code, NOW) is None


def test_consume_bind_code_rejects_expired_and_unknown():
    data: dict = {}
    code = SubscriptionService.create_bind_code(
        data, source=UMO, sender="sender-1", now=NOW, ttl_minutes=1
    )

    assert SubscriptionService.consume_bind_code(data, "000000", NOW) is None
    assert SubscriptionService.consume_bind_code(data, "", NOW) is None
    assert (
        SubscriptionService.consume_bind_code(data, code, NOW + timedelta(minutes=2))
        is None
    )
    assert code not in data


def test_consume_bind_code_honours_custom_ttl():
    data: dict = {}
    code = SubscriptionService.create_bind_code(
        data, source=UMO, sender="sender-1", now=NOW, ttl_minutes=1
    )

    assert SubscriptionService.consume_bind_code(
        data, code, NOW + timedelta(seconds=30)
    ) is not None


@pytest.mark.anyio
async def test_bind_codes_roundtrip_through_kv():
    service, kv = make_service()
    data: dict = {}
    code = SubscriptionService.create_bind_code(
        data, source=UMO, sender="sender-1", now=NOW
    )
    await service.save_bind_codes(data)

    loaded = await service.get_bind_codes()

    assert loaded == data
    assert await service.get_bind_codes() == {
        code: {
            "source": UMO,
            "sender": "sender-1",
            "created": NOW.isoformat(),
            "expires": (NOW + timedelta(minutes=15)).isoformat(),
            "used": False,
        }
    }
    assert kv.store


@pytest.mark.anyio
async def test_bind_codes_ignore_garbage_payload():
    service, kv = make_service()
    await kv.put(BIND_CODE_KEY, "not a dict")
    assert await service.get_bind_codes() == {}
    await kv.put(BIND_CODE_KEY, {"codes": {"123456": "nope", "": {"used": True}}})
    assert await service.get_bind_codes() == {}