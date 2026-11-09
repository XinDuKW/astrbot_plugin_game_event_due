"""管理页「紧急私聊提醒」面板的读写接口。

这一层做三件事：把 KV 里的目标整理成前端能直接渲染的形状、校验前端提交的
改动（UMO 格式、游戏 id、节奏取值范围），以及把保存后的最新快照回给前端。
校验漏掉任何一条，都会让插件把提醒发到不存在的人身上，或者把别人的节奏冲掉。
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

try:  # pragma: no cover - 取决于是否装了 AstrBot
    from astrbot.api.web import error_response, json_response, request  # noqa: F401
except Exception:  # pragma: no cover
    import astrbot.api as _astrbot_api

    _web = types.ModuleType("astrbot.api.web")

    def _stub_error(message, *, status_code=400):
        return {"error": message, "status": status_code}

    def _stub_ok(payload):
        return payload

    def _stub_request(*args, **kwargs):
        return None

    _web.error_response = _stub_error
    _web.json_response = _stub_ok
    _web.request = types.SimpleNamespace(json=_stub_request)
    sys.modules["astrbot.api.web"] = _web
    _astrbot_api.web = _web

from astrbot_plugin_game_event_due.activity_webui import ActivityWebUIController
from astrbot_plugin_game_event_due.services.urgent_dm_service import UrgentDmSettings
from astrbot_plugin_game_event_due.sources import Game

FRIEND = "aiocqhttp:FriendMessage:10001"


class FakeSubscriptions:
    """只保留读写快照的最小替身，真实的 KV 往返在别处测。"""

    def __init__(self, data=None) -> None:
        self.data = json.loads(json.dumps(data or {}))
        self.saves = 0

    async def get_urgent_dm(self):
        return json.loads(json.dumps(self.data))

    async def save_urgent_dm(self, data):
        self.saves += 1
        self.data = json.loads(json.dumps(data))


class FakeRegistry:
    def __init__(self) -> None:
        self.games = {
            "sr": Game("sr", "崩坏：星穹铁道", "sra", short_name="铁"),
            "ww": Game("ww", "鸣潮", "sra", short_name="潮"),
        }

    def ordered(self):
        return list(self.games.values())

    def get(self, game_id):
        return self.games.get(game_id)


class FakePlugin:
    def __init__(self, urgent=None, data=None) -> None:
        self.urgent_dm = urgent or UrgentDmSettings(
            enabled=True,
            trigger_days=3,
            repeat_minutes=120,
            daily_limit=3,
            quiet_hours=(23, 8),
        )
        self.subscriptions = FakeSubscriptions(data)
        self.registry = FakeRegistry()


class FakeContext:
    def __init__(self) -> None:
        self.routes: list[tuple[str, list[str]]] = []

    def register_web_api(self, path, handler, methods, description) -> None:
        self.routes.append((path, list(methods)))


def make_controller(urgent=None, data=None):
    plugin = FakePlugin(urgent, data)
    context = FakeContext()
    return ActivityWebUIController(plugin, context), context


def _patch_payload(monkeypatch, payload):
    from astrbot_plugin_game_event_due import activity_webui as module

    async def fake_json(default=None):
        return payload

    monkeypatch.setattr(module, "request", types.SimpleNamespace(json=fake_json))


def _target_entry(**overrides):
    entry = {
        "enabled": True,
        "games": [],
        "note": "",
        "activities": {
            "sr:1": {
                "name": "虚妄的星穹",
                "end": "2026-11-05 04:00",
                "sent_total": 2,
                "last_sent_at": "2026-11-04 20:00",
            },
            "ww:1": {
                "name": "无音消除",
                "end": "2026-11-06 04:00",
                "sent_total": 1,
                "acked": True,
            },
        },
    }
    entry.update(overrides)
    return entry


def test_routes_expose_urgent_endpoints():
    _, context = make_controller()
    paths = {path.rsplit("/", 1)[-1]: methods for path, methods in context.routes}
    assert paths["urgent-targets"] == ["GET"]
    assert paths["urgent-save"] == ["POST"]


def test_urgent_overview_lists_defaults_limits_and_games():
    controller, _ = make_controller(data={FRIEND: _target_entry()})

    payload = asyncio.run(controller.urgent_targets())

    assert payload["enabled"] is True
    assert payload["defaults"]["trigger_days"] == 3
    assert payload["defaults"]["repeat_minutes"] == 120
    assert payload["limits"]["daily_limit"] == [0, 100]
    assert [game["id"] for game in payload["games"]] == ["sr", "ww"]
    assert payload["targets"][0]["umo"] == FRIEND
    assert payload["targets"][0]["session_name"] == "私聊 10001"
    # 两个活动按结束时间排；已确认的那个标记出来，方便前端显示「已确认」
    activities = payload["targets"][0]["activities"]
    assert [item["name"] for item in activities] == ["虚妄的星穹", "无音消除"]
    assert activities[1]["acked"] is True
    # 前端「解除」按钮要用 uid 定位单条，接口里必须带上
    assert [item["uid"] for item in activities] == ["sr:1", "ww:1"]


def test_urgent_overview_describes_effective_pacing():
    controller, _ = make_controller(
        data={FRIEND: _target_entry(settings={"trigger_days": 7, "repeat_minutes": 30})}
    )

    payload = asyncio.run(controller.urgent_targets())

    row = payload["targets"][0]
    assert row["settings"] == {"trigger_days": 7, "repeat_minutes": 30}
    assert row["effective"]["trigger_days"] == 7
    assert row["effective"]["repeat_minutes"] == 30
    assert "7 天内" in row["describe"]


def test_urgent_save_adds_new_target(monkeypatch):
    controller, _ = make_controller()
    _patch_payload(monkeypatch, {"umo": FRIEND, "enabled": True, "games": ["sr"]})

    payload = asyncio.run(controller.urgent_save())

    assert payload["targets"][0]["umo"] == FRIEND
    assert payload["targets"][0]["games"] == ["sr"]
    assert controller.plugin.subscriptions.saves == 1


def test_urgent_save_stores_per_target_pacing(monkeypatch):
    controller, _ = make_controller(data={FRIEND: _target_entry()})
    _patch_payload(
        monkeypatch,
        {
            "umo": FRIEND,
            "settings": {"trigger_days": 5, "repeat_minutes": 60, "daily_limit": 2},
        },
    )

    payload = asyncio.run(controller.urgent_save())

    saved = controller.plugin.subscriptions.data[FRIEND]["settings"]
    assert saved == {"trigger_days": 5, "repeat_minutes": 60, "daily_limit": 2}
    assert payload["targets"][0]["effective"]["trigger_days"] == 5


def test_urgent_save_clears_pacing_when_value_is_null(monkeypatch):
    controller, _ = make_controller(
        data={FRIEND: _target_entry(settings={"trigger_days": 7})}
    )
    _patch_payload(monkeypatch, {"umo": FRIEND, "settings": {"trigger_days": None}})

    asyncio.run(controller.urgent_save())

    assert "settings" not in controller.plugin.subscriptions.data[FRIEND]


def test_urgent_save_clamps_pacing_into_range(monkeypatch):
    controller, _ = make_controller(data={FRIEND: _target_entry()})
    _patch_payload(monkeypatch, {"umo": FRIEND, "settings": {"repeat_minutes": 1}})

    asyncio.run(controller.urgent_save())

    saved = controller.plugin.subscriptions.data[FRIEND]["settings"]
    assert saved["repeat_minutes"] == 5


def test_urgent_save_rejects_bad_umo(monkeypatch):
    controller, _ = make_controller()
    _patch_payload(monkeypatch, {"umo": "10001", "enabled": True})

    result = asyncio.run(controller.urgent_save())

    assert result["status"] == 400
    assert controller.plugin.subscriptions.saves == 0


def test_urgent_save_rejects_unknown_game(monkeypatch):
    controller, _ = make_controller()
    _patch_payload(monkeypatch, {"umo": FRIEND, "games": ["nope"]})

    result = asyncio.run(controller.urgent_save())

    assert result["status"] == 400
    assert "nope" in result["error"]


def test_urgent_save_rejects_non_integer_pacing(monkeypatch):
    controller, _ = make_controller(data={FRIEND: _target_entry()})
    _patch_payload(monkeypatch, {"umo": FRIEND, "settings": {"trigger_days": "两天"}})

    result = asyncio.run(controller.urgent_save())

    assert result["status"] == 400


def test_urgent_save_requires_something_to_change(monkeypatch):
    controller, _ = make_controller(data={FRIEND: _target_entry()})
    _patch_payload(monkeypatch, {"umo": FRIEND})

    result = asyncio.run(controller.urgent_save())

    assert result["status"] == 400


def test_urgent_save_removes_target(monkeypatch):
    controller, _ = make_controller(data={FRIEND: _target_entry()})
    _patch_payload(monkeypatch, {"umo": FRIEND, "remove": True})

    payload = asyncio.run(controller.urgent_save())

    assert payload["targets"] == []
    assert FRIEND not in controller.plugin.subscriptions.data


def test_urgent_save_remove_reports_missing_target(monkeypatch):
    controller, _ = make_controller()
    _patch_payload(monkeypatch, {"umo": FRIEND, "remove": True})

    result = asyncio.run(controller.urgent_save())

    assert result["status"] == 404


def test_urgent_save_resets_acknowledged_activity(monkeypatch):
    controller, _ = make_controller(data={FRIEND: _target_entry()})
    _patch_payload(monkeypatch, {"umo": FRIEND, "reset_ack": True, "activity_id": "ww:1"})

    payload = asyncio.run(controller.urgent_save())

    activities = payload["targets"][0]["activities"]
    assert [item["acked"] for item in activities] == [False, False]


def test_urgent_save_reset_ack_can_target_a_single_activity(monkeypatch):
    entry = _target_entry()
    entry["activities"]["sr:1"]["acked"] = True
    controller, _ = make_controller(data={FRIEND: entry})
    _patch_payload(
        monkeypatch,
        {"umo": FRIEND, "reset_ack": True, "activity_id": "ww:1"},
    )

    payload = asyncio.run(controller.urgent_save())

    by_uid = {item["uid"]: item["acked"] for item in payload["targets"][0]["activities"]}
    assert by_uid == {"sr:1": True, "ww:1": False}


def test_urgent_save_reset_ack_reports_nothing_to_clear(monkeypatch):
    controller, _ = make_controller(
        data={FRIEND: {"enabled": True, "games": [], "activities": {"sr:1": {"sent_total": 1}}}}
    )
    _patch_payload(monkeypatch, {"umo": FRIEND, "reset_ack": True})

    result = asyncio.run(controller.urgent_save())

    assert result["status"] == 404


def test_urgent_save_keeps_games_and_note_when_not_submitted(monkeypatch):
    controller, _ = make_controller(
        data={FRIEND: _target_entry(games=["ww"], note="只看鸣潮")}
    )
    _patch_payload(monkeypatch, {"umo": FRIEND, "enabled": False})

    asyncio.run(controller.urgent_save())

    entry = controller.plugin.subscriptions.data[FRIEND]
    assert entry["games"] == ["ww"]
    assert entry["note"] == "只看鸣潮"
    assert entry["enabled"] is False


@pytest.mark.parametrize("umo", ["", "aiocqhttp:FriendMessage", "x:y:z:w", "aiocqhttp:BadType:1"])
def test_urgent_save_rejects_malformed_umo(monkeypatch, umo):
    controller, _ = make_controller()
    _patch_payload(monkeypatch, {"umo": umo, "enabled": True})

    result = asyncio.run(controller.urgent_save())

    assert result["status"] == 400

