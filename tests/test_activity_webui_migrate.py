"""管理页「改会话号」接口。

换群、换机器人账号之后，旧会话号再也匹配不到消息，用户看到的数据就「丢了」。
这一层负责校验 UMO、把迁移交给 SubscriptionService，并把结果回给前端；
订阅、提醒进度、紧急目标必须一起搬，少搬一份就会重复推送或漏掉提醒。
"""

from __future__ import annotations

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
from astrbot_plugin_game_event_due.services.subscription_service import (
    SubscriptionService,
)

from helpers import FakeKV

OLD = "aiocqhttp:GroupMessage:111111"
NEW = "aiocqhttp:GroupMessage:222222"


class FakePlugin:
    def __init__(self, service) -> None:
        self.subscriptions = service


class FakeContext:
    def __init__(self) -> None:
        self.routes: list[tuple[str, list[str]]] = []

    def register_web_api(self, path, handler, methods, description) -> None:
        self.routes.append((path, list(methods)))


def make_controller():
    kv = FakeKV()
    service = SubscriptionService(kv.get, kv.put)
    context = FakeContext()
    return ActivityWebUIController(FakePlugin(service), context), context, service


def _patch_payload(monkeypatch, payload):
    from astrbot_plugin_game_event_due import activity_webui as module

    async def fake_json(default=None):
        return payload

    monkeypatch.setattr(module, "request", types.SimpleNamespace(json=fake_json))


async def _seed(service) -> None:
    await service.add_games(OLD, ["sr"])
    await service.save_notify_state({OLD: {"sr|a1": {"new": True}}})
    urgent = await service.get_urgent_dm()
    SubscriptionService.upsert_urgent_target(urgent, OLD, games=["sr"])
    await service.save_urgent_dm(urgent)


def test_routes_expose_migrate_endpoints():
    _, context, _ = make_controller()
    # 注册的是 /<插件名>/session/migrate，取最后一段做键
    paths = {path.rsplit("/", 1)[-1]: methods for path, methods in context.routes}
    assert paths["migrate"] == ["POST"]
    assert paths["urgent-migrate"] == ["POST"]


@pytest.mark.anyio
async def test_migrate_session_moves_everything(monkeypatch):
    controller, _, service = make_controller()
    await _seed(service)
    _patch_payload(monkeypatch, {"umo": OLD, "target_umo": NEW})

    result = await controller.migrate_session()

    assert result["migrated"] is True
    assert result["umo"] == OLD
    assert result["target_umo"] == NEW
    assert result["session"] is True
    assert result["urgent"] is True
    assert result["notify_entries"] == 1
    assert result["merged"] is False
    assert await service.get_session(OLD) is None
    assert (await service.get_session(NEW))["games"] == ["sr"]


@pytest.mark.anyio
async def test_migrate_session_rejects_bad_umo(monkeypatch):
    controller, _, _ = make_controller()
    _patch_payload(monkeypatch, {"umo": "group1", "target_umo": NEW})

    result = await controller.migrate_session()

    assert result["status"] == 400
    assert "UMO" in result["error"]


@pytest.mark.anyio
async def test_migrate_session_rejects_same_umo(monkeypatch):
    controller, _, _ = make_controller()
    _patch_payload(monkeypatch, {"umo": OLD, "target_umo": OLD})

    result = await controller.migrate_session()

    assert result["status"] == 400


@pytest.mark.anyio
async def test_migrate_session_reports_missing_source(monkeypatch):
    controller, _, _ = make_controller()
    _patch_payload(monkeypatch, {"umo": OLD, "target_umo": NEW})

    result = await controller.migrate_session()

    assert result["status"] == 404


@pytest.mark.anyio
async def test_migrate_urgent_merges_progress(monkeypatch):
    controller, _, service = make_controller()
    await _seed(service)
    urgent = await service.get_urgent_dm()
    SubscriptionService.upsert_urgent_target(urgent, NEW, games=["ww"])
    await service.save_urgent_dm(urgent)
    _patch_payload(monkeypatch, {"umo": OLD, "target_umo": NEW, "merge": True})

    result = await controller.migrate_urgent()

    assert result["migrated"] is True
    assert result["merged"] is True
    merged = (await service.get_urgent_dm())[NEW]
    assert merged["games"] == ["sr", "ww"]
    # 只搬紧急目标，订阅留在原地
    assert await service.get_session(OLD) is not None
