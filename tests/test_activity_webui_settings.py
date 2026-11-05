"""管理页「数据源与代理」设置的校验逻辑。

这些用例覆盖 ``_draft_source_settings`` 的边界：配置写错了会导致插件下次
启动时没有任何数据源，或者代理地址非法让所有请求失败，所以必须先在
Web 接口这一层拦下来。
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

# 离线跑测试时 astrbot.api.web 不存在，补一个最小存根（真实环境不会被覆盖）
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


class FakeConfig(dict):
    """AstrBotConfig 的最小替身：支持 get / setitem / 保存。"""

    def __init__(self, data=None) -> None:
        super().__init__(data or {})
        self.saved = 0

    async def save_config_async(self) -> bool:
        self.saved += 1
        return True


class FakePlugin:
    SOURCE_CHOICES = {"arknights": ("prts", "sra"), "endfield": ("akedata", "sra")}
    SOURCE_LABELS = {
        "sra": "SRA 公共 API",
        "akedata": "AKEData（终末地配置表）",
        "prts": "PRTS（明日方舟 Wiki）",
    }

    def __init__(self) -> None:
        self.config = FakeConfig({"sources": {}, "basic": {}})
        self.reloads = 0

    async def reload_source_settings(self) -> None:
        self.reloads += 1

    def source_settings_values(self) -> dict:
        return {"sources": [], "game_sources": [], "network": {}}


class FakeContext:
    def __init__(self) -> None:
        self.routes: list[tuple[str, list[str]]] = []

    def register_web_api(self, path, handler, methods, description) -> None:
        self.routes.append((path, list(methods)))


def make_controller():
    context = FakeContext()
    controller = ActivityWebUIController(FakePlugin(), context)
    return controller, context


def test_routes_expose_settings_endpoints():
    _, context = make_controller()
    paths = {path.rsplit("/", 1)[-1]: methods for path, methods in context.routes}
    assert paths["settings"] == ["GET"]
    assert paths["settings-save"] == ["POST"]


def test_draft_settings_enables_all_sources_by_default():
    controller, _ = make_controller()
    block, basic = controller._draft_source_settings({}, {}, {})
    assert block["enable_sra"] is True
    assert block["enable_akedata"] is True
    assert block["enable_prts"] is True
    assert block["arknights_source"] == "prts"
    assert block["endfield_source"] == "akedata"
    assert basic == {"proxy": "", "direct_hosts": ""}


def test_draft_settings_rejects_disabling_every_source():
    controller, _ = make_controller()
    payload = {"enabled": {"sra": False, "akedata": False, "prts": False}}
    with pytest.raises(ValueError, match="至少要启用一个数据源"):
        controller._draft_source_settings(payload, {}, {})


def test_draft_settings_rejects_unknown_game_source():
    controller, _ = make_controller()
    payload = {"enabled": {"sra": True, "akedata": True, "prts": True},
               "game_sources": {"arknights": "akedata"}}
    with pytest.raises(ValueError, match="不支持的数据源"):
        controller._draft_source_settings(payload, {}, {})


def test_draft_settings_rejects_source_that_is_disabled():
    controller, _ = make_controller()
    payload = {
        "enabled": {"sra": True, "akedata": False, "prts": True},
        "game_sources": {"endfield": "akedata"},
    }
    with pytest.raises(ValueError, match="未启用"):
        controller._draft_source_settings(payload, {}, {})


def test_draft_settings_falls_back_when_preferred_source_is_disabled():
    controller, _ = make_controller()
    payload = {"enabled": {"sra": True, "akedata": True, "prts": False}}
    block, _ = controller._draft_source_settings(
        payload, {"arknights_source": "prts"}, {}
    )
    # 明日方舟默认走 PRTS，但 PRTS 被关掉了 → 自动退到 SRA
    assert block["arknights_source"] == "sra"
    assert block["endfield_source"] == "akedata"


def test_draft_settings_keeps_existing_choice_when_source_still_enabled():
    controller, _ = make_controller()
    payload = {"enabled": {"sra": True, "akedata": True, "prts": True}}
    block, _ = controller._draft_source_settings(
        payload, {"arknights_source": "sra"}, {}
    )
    assert block["arknights_source"] == "sra"


def test_draft_settings_rejects_bad_proxy():
    controller, _ = make_controller()
    with pytest.raises(ValueError, match="代理地址"):
        controller._draft_source_settings({"proxy": "127.0.0.1:7890"}, {}, {})


@pytest.mark.parametrize(
    "proxy",
    ["", "http://127.0.0.1:7890", "socks5h://127.0.0.1:1080", "HTTPS://proxy.lan"],
)
def test_draft_settings_accepts_valid_proxy(proxy):
    controller, _ = make_controller()
    _, basic = controller._draft_source_settings({"proxy": proxy}, {}, {})
    assert basic["proxy"] == proxy


def test_draft_settings_normalizes_direct_hosts():
    controller, _ = make_controller()
    _, basic = controller._draft_source_settings(
        {"direct_hosts": "https://Prts.wiki/x, media.prts.wiki、prts.wiki"}, {}, {}
    )
    assert basic["direct_hosts"] == "prts.wiki, media.prts.wiki"


def test_draft_settings_keeps_untouched_fields_from_config():
    controller, _ = make_controller()
    block, basic = controller._draft_source_settings(
        {"proxy": "http://proxy.lan:8080"},
        {"enable_prts": False, "enable_sra": True},
        {"proxy": "http://old.proxy:1080", "direct_hosts": "prts.wiki"},
    )
    # 没提交的开关沿用配置里的值，没提交的直连域名也沿用配置
    assert block["enable_prts"] is False
    assert block["enable_sra"] is True
    assert basic["proxy"] == "http://proxy.lan:8080"
    assert basic["direct_hosts"] == "prts.wiki"


def _patch_payload(monkeypatch, payload):
    """把 Web 请求体替换成给定内容（真实环境由 AstrBot 注入）。"""
    from astrbot_plugin_game_event_due import activity_webui as module

    async def fake_json(default=None):
        return payload

    monkeypatch.setattr(module, "request", types.SimpleNamespace(json=fake_json))


def test_settings_save_writes_config_and_hot_reloads(monkeypatch):
    controller, _ = make_controller()
    _patch_payload(
        monkeypatch,
        {
            "enabled": {"sra": True, "akedata": False, "prts": True},
            "game_sources": {"arknights": "sra"},
            "proxy": "http://127.0.0.1:7890",
            "direct_hosts": "prts.wiki, media.prts.wiki",
        },
    )

    result = asyncio.run(controller.settings_save())

    assert result["saved"] is True
    sources = controller.plugin.config["sources"]
    assert sources["enable_sra"] is True
    assert sources["enable_akedata"] is False
    assert sources["arknights_source"] == "sra"
    # 终末地只剩 SRA 可选（AKEData 被关），自动切过去
    assert sources["endfield_source"] == "sra"
    assert controller.plugin.config["basic"]["proxy"] == "http://127.0.0.1:7890"
    assert controller.plugin.config["basic"]["direct_hosts"] == "prts.wiki, media.prts.wiki"
    assert controller.plugin.config.saved == 1
    assert controller.plugin.reloads == 1


def test_settings_save_reports_validation_error_without_touching_config(monkeypatch):
    controller, _ = make_controller()
    _patch_payload(monkeypatch, {"enabled": {"sra": False, "akedata": False, "prts": False}})

    result = asyncio.run(controller.settings_save())

    assert result["status"] == 400
    assert "数据源" in result["error"]
    assert controller.plugin.config.saved == 0
    assert controller.plugin.reloads == 0


def test_settings_save_surfaces_reload_failure(monkeypatch):
    controller, _ = make_controller()
    _patch_payload(monkeypatch, {"proxy": "http://127.0.0.1:7890"})

    async def boom():
        raise RuntimeError("客户端重建失败")

    monkeypatch.setattr(controller.plugin, "reload_source_settings", boom)

    result = asyncio.run(controller.settings_save())

    assert result["status"] == 500
    assert "客户端重建失败" in result["error"]
