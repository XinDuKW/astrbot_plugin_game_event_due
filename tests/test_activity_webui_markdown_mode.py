"""管理页「排版方式」下拉的即时保存接口。

切「排版 / 预览」里的排版下拉不该等「保存模板」才生效，否则关掉编辑器又退回上一次的值。
这个接口只写 `message_format.markdown_mode` 一个键，其它模板键原样保留。
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
            else {"message_format": {"markdown_mode": "auto", "cover_mode": "image"}}
        )
        self.subscriptions = FakeSubscriptions()
        self.registry = FakeRegistry()
        self.reloads = 0

    def reload_message_settings(self) -> None:
        self.reloads += 1


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


def test_markdown_mode_route_is_registered():
    controller, _ = make_controller()
    assert any(
        path.endswith("/markdown-mode-save") for path in controller.context.routes
    )


def test_save_markdown_mode_writes_config_and_reloads(monkeypatch):
    controller, plugin = make_controller()
    _patch_payload(monkeypatch, {"markdown_mode": "on"})

    payload = asyncio.run(controller.markdown_mode_save())

    assert payload["markdown_mode"] == "on"
    assert plugin.config["message_format"]["markdown_mode"] == "on"
    # 其它模板键不能顺手清掉
    assert plugin.config["message_format"]["cover_mode"] == "image"
    assert plugin.config.saves == 1
    assert plugin.reloads == 1


def test_save_markdown_mode_normalizes_case_and_spaces(monkeypatch):
    controller, plugin = make_controller()
    _patch_payload(monkeypatch, {"markdown_mode": " OFF "})

    payload = asyncio.run(controller.markdown_mode_save())

    assert payload["markdown_mode"] == "off"
    assert plugin.config["message_format"]["markdown_mode"] == "off"


def test_save_markdown_mode_rejects_unknown_value(monkeypatch):
    controller, plugin = make_controller()
    _patch_payload(monkeypatch, {"markdown_mode": "html"})

    payload = asyncio.run(controller.markdown_mode_save())

    assert payload["status"] == 400
    assert plugin.config["message_format"]["markdown_mode"] == "auto"
    assert plugin.config.saves == 0
    assert plugin.reloads == 0


def test_save_markdown_mode_creates_missing_block(monkeypatch):
    controller, plugin = make_controller(config={})
    _patch_payload(monkeypatch, {"markdown_mode": "auto"})

    asyncio.run(controller.markdown_mode_save())

    assert plugin.config["message_format"] == {"markdown_mode": "auto"}
