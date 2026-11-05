"""SRA（StarRailAssistant）公共 API 数据源。

接口：``GET https://starrailassistant.top/api/v1/activity/{game}.json``

该接口无需认证，返回结构统一：版本信息 + ``activities`` 列表，每条活动
都带 ``name`` / ``description`` / ``startTime`` / ``endTime`` / ``cover``。

覆盖 18 款游戏。其中明日方舟与终末地在 SRA 里的远端标识是 ``ak`` / ``end``，插件内部沿用
``arknights`` / ``endfield``，与 PRTS / AKEData 的 game_id 保持一致，这样才能
「一款游戏、两个来源」互相切换（见 ``SRA_REMOTE_IDS``）。
"""

from __future__ import annotations

from typing import Any
from collections.abc import Sequence

from .base import (
    Activity,
    ActivitySource,
    Game,
    JsonHttpClient,
    make_activity_id,
    parse_datetime,
    strip_html,
)

DEFAULT_SRA_API_BASE = "https://starrailassistant.top/api/v1/activity"

# 插件内部 game_id 与 SRA 远端标识不一致的游戏（远端用短名 ak / end）
SRA_REMOTE_IDS: dict[str, str] = {
    "arknights": "ak",
    "endfield": "end",
}

SRA_GAMES: tuple[Game, ...] = (
    Game(
        "sr",
        "崩坏：星穹铁道",
        "sra",
        ("崩铁", "星铁", "星穹铁道", "starrail", "hsr"),
        "星穹铁道",
    ),
    Game("ys", "原神", "sra", ("genshin", "genshinimpact"), "原神"),
    Game("zzz", "绝区零", "sra", ("zenless", "zenlesszonezero"), "绝区零"),
    Game("ww", "鸣潮", "sra", ("wuwa", "wutheringwaves"), "鸣潮"),
    Game("nte", "异环", "sra", ("neverness", "nevernesstoeverness"), "异环"),
    Game(
        "ba-cn",
        "蔚蓝档案（国服）",
        "sra",
        ("蔚蓝档案", "碧蓝档案", "ba", "蔚蓝国服", "ba国服"),
        "蔚蓝档案国服",
    ),
    Game(
        "ba-global",
        "蔚蓝档案（国际服）",
        "sra",
        ("蔚蓝档案", "碧蓝档案", "蔚蓝国际服", "ba国际服", "bluearchiveglobal"),
        "蔚蓝档案国际服",
    ),
    Game(
        "ba-jp",
        "蔚蓝档案（日服）",
        "sra",
        ("蔚蓝档案", "碧蓝档案", "蔚蓝日服", "ba日服", "bluearchivejp"),
        "蔚蓝档案日服",
    ),
    # 下面两款同时由 PRTS / AKEData 提供，主源由配置决定
    Game(
        "arknights",
        "明日方舟",
        "sra",
        ("舟", "方舟", "ak", "arknights", "明日方舟"),
        "明日方舟",
    ),
    Game(
        "endfield",
        "明日方舟：终末地",
        "sra",
        ("终末地", "zmd", "endfield", "明日方舟终末地"),
        "终末地",
    ),
    # 以下仅由 SRA 提供（PRTS / AKEData 没有对应数据）
    Game("xtlr", "星塔旅人", "sra", ("星塔", "xtlr"), "星塔旅人"),
    Game(
        "infinitynikki",
        "无限暖暖",
        "sra",
        ("暖暖", "infinitynikki", "infinity"),
        "无限暖暖",
    ),
    Game(
        "gf2",
        "少女前线2：追放",
        "sra",
        ("少前2", "少前", "少女前线2", "少女前线", "gf2"),
        "少前2",
    ),
    Game(
        "pgr",
        "战双帕弥什",
        "sra",
        ("战双", "pgr", "punishinggrayraven"),
        "战双",
    ),
    Game(
        "czn",
        "卡厄斯梦境（国际服）",
        "sra",
        ("卡厄斯", "卡厄斯梦境", "czn"),
        "卡厄斯梦境国际服",
    ),
    Game(
        "bd2",
        "棕色尘埃2",
        "sra",
        ("棕尘", "棕色尘埃", "bd2", "browndust2"),
        "棕色尘埃2",
    ),
    Game(
        "nikke",
        "NIKKE（国际服）",
        "sra",
        ("妮姬", "胜利女神", "nikke"),
        "NIKKE国际服",
    ),
    Game(
        "hyrz",
        "火影忍者",
        "sra",
        ("火影", "hyrz", "naruto"),
        "火影忍者",
    ),
)


class SraSource(ActivitySource):
    """SRA 公开活动接口数据源，一个数据源覆盖多款游戏。"""

    source_id = "sra"
    display_name = "SRA 公共 API"

    def __init__(
        self,
        client: JsonHttpClient,
        *,
        api_base: str = DEFAULT_SRA_API_BASE,
    ) -> None:
        self._client = client
        self._api_base = (api_base or DEFAULT_SRA_API_BASE).strip().rstrip("/")
        self._cache: dict[str, list[Activity]] = {}
        self.versions: dict[str, str] = {}
        self.notes: dict[str, str] = {}

    def games(self) -> tuple[Game, ...]:
        return SRA_GAMES

    async def fetch(self, game_id: str) -> Sequence[Activity]:
        remote_id = SRA_REMOTE_IDS.get(game_id, game_id)
        cache_key = f"sra_{remote_id}"
        url = f"{self._api_base}/{remote_id}.json"
        result = await self._client.get_json(url, cache_key=cache_key)

        if result.not_modified:
            cached = self._cache.get(game_id)
            if cached is None:
                raw = self._client.read_cache(cache_key)
                cached = self._parse(raw, game_id) if raw is not None else []
                self._cache[game_id] = cached
            return cached

        activities = self._parse(result.data, game_id)
        self._cache[game_id] = activities
        self._record_meta(result.data, game_id)
        self.notes[game_id] = "使用缓存数据" if result.stale else ""
        return activities

    def _record_meta(self, data: Any, game_id: str) -> None:
        if not isinstance(data, dict):
            return
        version = str(data.get("version") or "").strip()
        version_name = strip_html(data.get("versionName"))
        if version:
            self.versions[game_id] = (
                f"{version} {version_name}".strip() if version_name else version
            )

    def _parse(self, data: Any, game_id: str) -> list[Activity]:
        if not isinstance(data, dict):
            return []

        activities: list[Activity] = []
        for item in data.get("activities") or []:
            if not isinstance(item, dict):
                continue
            name = strip_html(item.get("name"))
            if not name:
                continue

            start_time = parse_datetime(item.get("startTime"))
            end_time = parse_datetime(item.get("endTime"))
            if end_time is None:
                # 没有结束时间就无法做倒计时提醒，且会被长期保留在相关活动集合里
                continue
            anchor = start_time or end_time
            activities.append(
                Activity(
                    game_id=game_id,
                    activity_id=make_activity_id(
                        name,
                        anchor.isoformat() if anchor else "",
                    ),
                    name=name,
                    start_time=start_time,
                    end_time=end_time,
                    description=strip_html(item.get("description")),
                    cover=str(item.get("cover") or "").strip(),
                )
            )
        return activities
