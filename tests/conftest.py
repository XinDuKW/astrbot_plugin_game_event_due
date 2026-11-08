"""测试初始化。

1. 让插件可以按固定包名导入——**不依赖插件所在目录的名字**（从 GitHub 克隆或解压
   出来时目录可能叫 ``repo`` / ``astrbot_plugin_game_event_due-main`` 等）；
2. 未安装 AstrBot 时注入最小存根（只提供 ``astrbot.api.logger``），让脱离运行时的
   单元测试也能导入依赖 logger 的模块。**真实环境不会被覆盖** —— 只有在
   ``import astrbot.api`` 失败时才注入，因此插件在 AstrBot 里跑测试时用的仍是真日志器。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

PACKAGE_NAME = "astrbot_plugin_game_event_due"

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PARENT_DIR = PLUGIN_DIR.parent

if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))

if PLUGIN_DIR.name != PACKAGE_NAME:
    # 目录名与包名不一致（克隆/解压后的常见情况）：显式注册一个指向真实目录的包对象，
    # 否则测试里的 `from astrbot_plugin_game_event_due...` 会 ModuleNotFoundError。
    _package = types.ModuleType(PACKAGE_NAME)
    _package.__path__ = [str(PLUGIN_DIR)]
    sys.modules[PACKAGE_NAME] = _package


class StubLogger:
    """记录日志调用的极简日志器，便于断言某条降级路径确实被记录。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _log(
        self,
        level: str,
        message: object,
        *args: object,
        **kwargs: object,
    ) -> None:
        text = str(message)
        if args:
            try:
                text = text % args
            except (TypeError, ValueError):
                pass
        self.records.append((level, text))

    def debug(self, message: object, *args: object, **kwargs: object) -> None:
        self._log("debug", message, *args, **kwargs)

    def info(self, message: object, *args: object, **kwargs: object) -> None:
        self._log("info", message, *args, **kwargs)

    def warning(self, message: object, *args: object, **kwargs: object) -> None:
        self._log("warning", message, *args, **kwargs)

    def error(self, message: object, *args: object, **kwargs: object) -> None:
        self._log("error", message, *args, **kwargs)

    def critical(self, message: object, *args: object, **kwargs: object) -> None:
        self._log("critical", message, *args, **kwargs)

    def exception(self, message: object, *args: object, **kwargs: object) -> None:
        self._log("error", message, *args, **kwargs)

    def messages(self, level: str | None = None) -> list[str]:
        return [
            text
            for recorded_level, text in self.records
            if level is None or recorded_level == level
        ]

    def clear(self) -> None:
        self.records.clear()


class StubPlain:
    """``Comp.Plain`` 的替身：只保留 text。"""

    def __init__(self, text: str = "") -> None:
        self.text = text

    def __eq__(self, other: object) -> bool:
        return isinstance(other, StubPlain) and other.text == self.text

    def __repr__(self) -> str:
        return f"Plain({self.text!r})"


class StubImage:
    """``Comp.Image`` 的替身：记录 url 或压缩后的字节。"""

    def __init__(self, url: str = "", data: bytes | None = None) -> None:
        self.url = url
        self.data = data

    @classmethod
    def fromURL(cls, url: str, *args: object, **kwargs: object) -> "StubImage":
        return cls(url=url)

    @classmethod
    def fromBytes(cls, data: bytes, *args: object, **kwargs: object) -> "StubImage":
        return cls(data=data)

    def __repr__(self) -> str:
        return f"Image({self.url or len(self.data or b'')}B)"


class StubNode:
    """``Comp.Node`` 的替身：转发节点的名字与内容。"""

    def __init__(
        self,
        content: object = None,
        name: str = "",
        uin: str = "0",
    ) -> None:
        self.content = list(content or [])
        self.name = name
        self.uin = uin

    def to_dict(self) -> dict:
        return {
            "type": "node",
            "data": {
                "uin": self.uin,
                "name": self.name,
                "content": [
                    {"type": "text", "data": {"text": getattr(item, "text", "")}}
                    for item in self.content
                ],
            },
        }

    def __repr__(self) -> str:
        return f"Node({self.name!r}, {len(self.content)})"


class StubNodes:
    """``Comp.Nodes`` 的替身：一条合并转发。"""

    def __init__(self, nodes: object = None) -> None:
        self.nodes = list(nodes or [])

    def __repr__(self) -> str:
        return f"Nodes({len(self.nodes)})"


class StubMessageChain:
    """``MessageChain`` 的替身。"""

    def __init__(self, chain: object = None, **kwargs: object) -> None:
        self.chain = list(chain or [])


STUB_LOGGER: StubLogger | None = None


def _install_astrbot_stub() -> None:
    """仅在真实 AstrBot 无法导入时注入存根。"""
    global STUB_LOGGER

    try:
        import astrbot.api  # noqa: F401
    except Exception:
        pass
    else:
        return

    stub = StubLogger()

    components_module = types.ModuleType("astrbot.api.message_components")
    components_module.Plain = StubPlain  # type: ignore[attr-defined]
    components_module.Image = StubImage  # type: ignore[attr-defined]
    components_module.Node = StubNode  # type: ignore[attr-defined]
    components_module.Nodes = StubNodes  # type: ignore[attr-defined]

    event_module = types.ModuleType("astrbot.api.event")
    event_module.MessageChain = StubMessageChain  # type: ignore[attr-defined]

    astrbot_module = types.ModuleType("astrbot")
    api_module = types.ModuleType("astrbot.api")
    api_module.logger = stub  # type: ignore[attr-defined]
    api_module.message_components = components_module  # type: ignore[attr-defined]
    api_module.event = event_module  # type: ignore[attr-defined]
    astrbot_module.api = api_module  # type: ignore[attr-defined]

    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = api_module
    sys.modules["astrbot.api.message_components"] = components_module
    sys.modules["astrbot.api.event"] = event_module
    STUB_LOGGER = stub


_install_astrbot_stub()


@pytest.fixture
def astrbot_logs() -> StubLogger | None:
    """返回存根日志器；已安装真实 AstrBot 时返回 ``None``，相关断言应跳过。"""
    if STUB_LOGGER is None:
        return None
    STUB_LOGGER.clear()
    return STUB_LOGGER
