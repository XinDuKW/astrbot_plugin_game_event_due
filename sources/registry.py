"""游戏注册表：聚合各数据源提供的游戏，并支持按用户输入解析。

同一款游戏可能由多个数据源提供（例如明日方舟既能走 PRTS，也能走 SRA）。
注册表保留全部候选来源，按「偏好配置」挑一个当主源：偏好没配或配了但那个源
没启用时，回退到先注册的源，保证插件在任何开关组合下都能启动。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from .base import ActivitySource, Game, normalize_token

# 同一款游戏有多个来源时的默认主源（用户没配置时用它，保持历史行为不变）
DEFAULT_PRIMARY_SOURCES: dict[str, str] = {
    "arknights": "prts",
    "endfield": "akedata",
}


class GameRegistry:
    """把多个数据源提供的游戏汇总成统一的查询入口。"""

    def __init__(
        self,
        sources: Iterable[ActivitySource] = (),
        *,
        preferred: Mapping[str, str] | None = None,
    ) -> None:
        self._games: dict[str, Game] = {}
        self._sources: dict[str, ActivitySource] = {}
        self._candidates: dict[str, list[tuple[ActivitySource, Game]]] = {}
        self._preferred: dict[str, str] = {
            str(k): str(v) for k, v in (preferred or {}).items() if str(v).strip()
        }
        for source in sources:
            self.register(source)

    def register(self, source: ActivitySource) -> None:
        for game in source.games():
            game_id = game.game_id
            candidates = self._candidates.setdefault(game_id, [])
            if any(item is source for item, _ in candidates):
                continue
            candidates.append((source, game))
            self._select_primary(game_id)

    def _select_primary(self, game_id: str) -> None:
        candidates = self._candidates.get(game_id) or []
        if not candidates:
            return
        wanted = self._preferred.get(game_id) or DEFAULT_PRIMARY_SOURCES.get(game_id)
        chosen = next(
            ((src, game) for src, game in candidates if src.source_id == wanted),
            candidates[0],
        )
        self._games[game_id] = chosen[1]
        self._sources[game_id] = chosen[0]

    @property
    def games(self) -> dict[str, Game]:
        return dict(self._games)

    def get(self, game_id: str) -> Game | None:
        return self._games.get(str(game_id or "").strip())

    def source_for(self, game_id: str) -> ActivitySource | None:
        return self._sources.get(str(game_id or "").strip())

    def sources_for(self, game_id: str) -> tuple[ActivitySource, ...]:
        """返回该游戏的全部候选来源（按注册顺序）。"""
        return tuple(src for src, _ in self._candidates.get(str(game_id or "").strip(), []))

    def source_options(self, game_id: str) -> tuple[ActivitySource, ...]:
        """候选来源；只有一个来源时返回空，表示这类游戏没什么可切换的。"""
        options = self.sources_for(game_id)
        return options if len(options) > 1 else ()

    def source_name(self, game_id: str) -> str:
        source = self.source_for(game_id)
        return source.display_name if source else ""

    def ordered(self) -> list[Game]:
        """按注册顺序返回游戏列表。"""
        return list(self._games.values())

    def resolve(self, token: str) -> list[Game]:
        """把用户输入解析为游戏列表。

        支持 game_id、完整名称与别名；无法精确匹配时会退化为子串匹配，
        例如 ``档案`` 可以匹配到蔚蓝档案系列。
        """
        wanted = normalize_token(token)
        if not wanted:
            return []

        exact = [game for game in self._games.values() if game.matches(token)]
        if exact:
            return exact

        return [
            game
            for game in self._games.values()
            if wanted in normalize_token(game.name)
            or wanted in normalize_token(game.short_name)
            or any(wanted in normalize_token(alias) for alias in game.aliases)
        ]

    def resolve_many(self, tokens: Sequence[str]) -> tuple[list[Game], list[str]]:
        """批量解析，返回 ``(命中的游戏, 未识别的输入)``。"""
        matched: list[Game] = []
        seen: set[str] = set()
        unknown: list[str] = []
        for token in tokens:
            found = self.resolve(token)
            if not found:
                unknown.append(token)
                continue
            for game in found:
                if game.game_id not in seen:
                    seen.add(game.game_id)
                    matched.append(game)
        return matched, unknown
