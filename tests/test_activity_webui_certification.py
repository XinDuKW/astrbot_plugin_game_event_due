"""管理页「认证类型」下拉接口。

QQ 平台接口不返回机器人是个人认证还是企业认证，只能让用户在标题栏自己选；
这个值只影响「主动私聊发不出去」时给哪条提示，存进 `urgent_dm.certification`。
"""

from __future__ import annotations

import asyncio
import sys
import types

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
from astrbot_plugin_game_event_due.services.urgent_dm_service import (
    CERTIFICATION_ENTERPRISE,
    CERTIFICATION_PERSONAL,
    UrgentDmSettings,
)


class FakeSubscriptions:
    async def get_urgent_dm(self):
        return {}

    async def get_all(self):
        return {}


class FakeRegistry:
    def ordered(self):
        return []


class FakeConfig(dict):
    """真 AstrBot 的配置对象带 save_config_async，这里补一个记录调用的。"""

    def __init__(self, data=None) -> None:
        super().__init__(data or {})
        self.saves = 0

    async def save_config_async(self):
        self.saves += 1
        return True


class FakePlugin:
    def __init__(self, config=None) -> None:
        self.config = FakeConfig(
            config
            if config is not None
            else {"urgent_dm": {"enable": True, "certification": "unknown"}}
        )
        self.urgent_dm = UrgentDmSettings(enabled=True)
        self.subscriptions = FakeSubscriptions()
        self.registry = FakeRegistry()


class FakeContext:
    def __init__(self) -> None:
        self.routes: list[str] = []

    def register_web_api(self, path, handler, methods, description) -> None:
        self.routes.append(path)


def make_controller(config=None):
    plugin = FakePlugin(config)
    return ActivityWebUIController(plugin, FakeContext()), plugin


def _patch_payload(monkeypatch, payload):
    from astrbot_plugin_game_event_due import activity_webui as module

    async def fake_json(default=None):
        return payload

    monkeypatch.setattr(module, "request", types.SimpleNamespace(json=fake_json))


def test_certification_route_is_registered():
    controller, _ = make_controller()
    assert any(path.endswith("/certification-save") for path in controller.context.routes)


def test_save_certification_writes_config_and_settings(monkeypatch):
    controller, plugin = make_controller()
    _patch_payload(monkeypatch, {"certification": "个人认证"})

    payload = asyncio.run(controller.certification_save())

    assert payload["certification"] == CERTIFICATION_PERSONAL
    assert plugin.config["urgent_dm"]["certification"] == CERTIFICATION_PERSONAL
    # 其它键不能被顺手清掉
    assert plugin.config["urgent_dm"]["enable"] is True
    # 运行中的配置立刻生效，不用重启
    assert plugin.urgent_dm.certification == CERTIFICATION_PERSONAL
    assert plugin.urgent_dm.enabled is True
    assert plugin.config.saves == 1


def test_save_certification_accepts_english_value(monkeypatch):
    controller, plugin = make_controller()
    _patch_payload(monkeypatch, {"certification": "enterprise"})

    payload = asyncio.run(controller.certification_save())

    assert payload["certification"] == CERTIFICATION_ENTERPRISE
    assert plugin.urgent_dm.certification == CERTIFICATION_ENTERPRISE


def test_save_certification_rejects_unknown_value(monkeypatch):
    controller, plugin = make_controller()
    _patch_payload(monkeypatch, {"certification": "小米"})

    payload = asyncio.run(controller.certification_save())

    assert payload["status"] == 400
    assert plugin.config["urgent_dm"]["certification"] == "unknown"
    assert plugin.urgent_dm.certification == "unknown"


def test_save_certification_creates_missing_block(monkeypatch):
    controller, plugin = make_controller(config={})
    _patch_payload(monkeypatch, {"certification": "personal"})

    asyncio.run(controller.certification_save())

    assert plugin.config["urgent_dm"] == {"certification": CERTIFICATION_PERSONAL}
