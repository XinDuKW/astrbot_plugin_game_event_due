"""数据源解析、时间处理与游戏解析。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from astrbot_plugin_game_event_due.sources.akedata import AkedataSource
from astrbot_plugin_game_event_due.sources.base import (
    DEFAULT_DIRECT_HOSTS,
    GAME_TIMEZONE,
    Activity,
    JsonHttpClient,
    build_direct_mounts,
    filter_relevant,
    format_remaining,
    is_direct_host,
    make_activity_id,
    normalize_direct_hosts,
    normalize_token,
    parse_datetime,
    strip_html,
)
import hashlib
from urllib.parse import quote

from astrbot_plugin_game_event_due.sources.prts import PrtsSource, parse_smw_timestamp
from astrbot_plugin_game_event_due.sources.registry import GameRegistry
from astrbot_plugin_game_event_due.sources.sra import SraSource

from helpers import FakeClient

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=GAME_TIMEZONE)


# ---- 基础工具 ----


@pytest.mark.parametrize(
    "raw",
    [
        "2026-09-04T12:00:00",
        "2026-09-04T12:00:00+08:00",
        "2026-09-04 12:00:00",
        "2026/09/04 12:00:00",
        "2026-9-4 12:00",
        "2026/9/4 12:00:00",
        "2026-09-04",
    ],
)
def test_parse_datetime_variants(raw):
    parsed = parse_datetime(raw)
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert (parsed.year, parsed.month, parsed.day) == (2026, 9, 4)


def test_parse_datetime_converts_utc_to_shanghai():
    parsed = parse_datetime("2026-09-04T04:00:00Z")
    assert parsed is not None
    assert parsed.hour == 12
    assert parsed.utcoffset() == timedelta(hours=8)


@pytest.mark.parametrize("raw", [None, "", "  ", "not-a-date", "2026-13-45"])
def test_parse_datetime_invalid_returns_none(raw):
    assert parse_datetime(raw) is None


def test_strip_html_removes_tags_and_collapses_space():
    assert strip_html("<p>你好<br/>世界</p>  ok") == "你好世界 ok"
    assert strip_html(None) == ""


def test_normalize_token_ignores_punctuation_and_case():
    assert normalize_token("崩坏：星穹铁道") == normalize_token("崩坏 星穹铁道")
    assert normalize_token("BA-CN") == normalize_token("bacn")
    assert normalize_token("绝区零") == normalize_token("绝区零")


def test_make_activity_id_is_stable_and_unique():
    assert make_activity_id("a", "b") == make_activity_id("a", "b")
    assert make_activity_id("a", "b") != make_activity_id("b", "a")


def test_cache_filename_sanitizes_key():
    name = JsonHttpClient.cache_filename("akedata_table_1.5.3@9885010-4_X.json")
    assert "@" not in name
    assert name.endswith(".json")


def test_format_remaining():
    assert format_remaining(timedelta(days=3, hours=4)) == "3天4小时"
    assert format_remaining(timedelta(hours=5, minutes=30)) == "5小时30分"
    assert format_remaining(timedelta(minutes=20)) == "20分"
    assert format_remaining(timedelta(seconds=-5)) == "已结束"
    assert format_remaining(None) == ""


def test_filter_relevant_drops_old_and_far_future():
    def act(name, start_days, end_days):
        return Activity(
            game_id="sr",
            activity_id=name,
            name=name,
            start_time=NOW + timedelta(days=start_days),
            end_time=NOW + timedelta(days=end_days),
        )

    kept = filter_relevant(
        [
            act("active", -5, 5),
            act("upcoming", 10, 20),
            act("far", 100, 120),
            act("ended", -30, -10),
            act("just-ended", -20, -1),
        ],
        NOW,
        lookahead_days=45,
        keep_past_days=3,
    )
    assert {item.name for item in kept} == {"active", "upcoming", "just-ended"}


# ---- SRA ----


SRA_PAYLOAD = {
    "version": "4.5",
    "versionName": "挥掷千星的筹码",
    "activities": [
        {
            "name": "位面分裂",
            "description": "位面饰品限时双倍掉落！",
            "startTime": "2026-09-07T04:00:00",
            "endTime": "2026-09-21T03:59:59",
            "cover": "https://example.com/a.png",
        },
        {
            "name": "缺时间的活动",
            "startTime": "",
            "endTime": "",
        },
        {"description": "没有名字"},
    ],
}


@pytest.mark.anyio
async def test_sra_parse():
    client = FakeClient({"activity/sr.json": SRA_PAYLOAD})
    source = SraSource(client)
    activities = list(await source.fetch("sr"))

    assert len(activities) == 1
    item = activities[0]
    assert item.name == "位面分裂"
    assert item.game_id == "sr"
    assert item.start_time is not None and item.start_time.hour == 4
    assert item.end_time is not None and item.end_time.hour == 3
    assert item.cover == "https://example.com/a.png"
    assert source.versions["sr"] == "4.5 挥掷千星的筹码"


@pytest.mark.anyio
async def test_sra_not_modified_reuses_memory():
    payload = {"activities": [{"name": "A", "startTime": "2026-09-01T00:00:00"}]}
    client = FakeClient({"activity/sr.json": payload})
    source = SraSource(client)
    first = list(await source.fetch("sr"))

    from astrbot_plugin_game_event_due.sources.base import HttpResult

    async def not_modified(url, **kwargs):
        return HttpResult(not_modified=True)

    client.get_json = not_modified  # type: ignore[assignment]
    second = list(await source.fetch("sr"))
    assert [item.name for item in second] == [item.name for item in first]


# ---- AKEData ----


def akedata_responses() -> dict:
    return {
        "manifest.json": {
            "latest": "1.5.3@9885010-4",
            "versions": [
                {
                    "id": "1.5.3@9885010-4",
                    "gameVersion": "1.5.3",
                    "tableCfgPath": "public/1.5.3/9885010-4/TableCfg",
                },
                {
                    "id": "1.5.2@1111-1",
                    "gameVersion": "1.5.2",
                    "tableCfgPath": "public/1.5.2/1111-1/TableCfg",
                },
            ],
        },
        "ActivityTable.json": {
            "act_one": {
                "name": {"id": 100, "text": ""},
                "desc": {"id": 101, "text": ""},
                "timeId": "time_act_one",
                "tagIds": ["tag_a"],
                "tabImg": "bg_act_one",
            },
            "act_permanent": {
                "name": {"id": 102, "text": ""},
                "timeId": "time_act_permanent",
                "tagIds": [],
            },
        },
        "TimeRangeTable.json": {
            "time_act_one": {
                "timeRangeList": [
                    {"openTime": "2026/9/1 12:00:00", "closeTime": "2026/9/20 3:59:59"}
                ]
            },
            "time_act_permanent": {
                "timeRangeList": [{"openTime": "2026/1/1 04:00:00", "closeTime": ""}]
            },
            "time_pool_one": {
                "timeRangeList": [
                    {"openTime": "2026/9/2 07:00:00", "closeTime": "2026/9/30 12:00:00"}
                ]
            },
        },
        "ActivityTagTable.json": {
            "tag_a": {"name": {"id": 200, "text": ""}, "tagId": "tag_a"}
        },
        "I18nTextTable_CN.json": {
            "100": "测试活动",
            "101": "活动描述",
            "102": "常驻内容",
            "200": "限时活动",
            "300": "限定寻访·测试",
        },
        "GachaCharPoolTable.json": {
            "pool_one": {
                "name": {"id": 300, "text": ""},
                "type": 0,
                "upCharIds": ["char_a"],
                "sortId": 1,
            }
        },
    }


@pytest.mark.anyio
async def test_akedata_parse_activities_and_pools():
    client = FakeClient(akedata_responses())
    source = AkedataSource(client, include_gacha_pools=True)
    activities = list(await source.fetch("endfield"))

    by_name = {item.name: item for item in activities}
    assert set(by_name) == {"测试活动", "限定寻访·测试"}

    activity = by_name["测试活动"]
    assert activity.game_id == "endfield"
    assert activity.start_time is not None and activity.start_time.day == 1
    assert activity.end_time is not None and activity.end_time.day == 20
    assert activity.tags == ("限时活动",)
    assert activity.description == "活动描述"
    assert activity.cover.endswith("bg_act_one.png")

    pool = by_name["限定寻访·测试"]
    assert pool.tags == ("特许寻访",)
    assert source.versions["endfield"] == "1.5.3 @ 1.5.3@9885010-4"


@pytest.mark.anyio
async def test_akedata_skips_permanent_and_optional_pools():
    client = FakeClient(akedata_responses())
    source = AkedataSource(client, include_gacha_pools=False)
    activities = list(await source.fetch("endfield"))
    assert {item.name for item in activities} == {"测试活动"}


@pytest.mark.anyio
async def test_akedata_uses_cache_when_manifest_fails():
    responses = akedata_responses()
    client = FakeClient(responses)
    source = AkedataSource(client, include_gacha_pools=False)
    await source.fetch("endfield")

    broken = FakeClient(responses)
    broken.raises["manifest.json"] = RuntimeError("boom")
    broken.cache = client.cache
    fallback = AkedataSource(broken, include_gacha_pools=False)
    activities = list(await fallback.fetch("endfield"))
    assert {item.name for item in activities} == {"测试活动"}
    assert fallback.notes["endfield"] == "manifest 获取失败，使用缓存数据"


@pytest.mark.anyio
async def test_akedata_skips_download_when_version_unchanged():
    client = FakeClient(akedata_responses())
    source = AkedataSource(client, include_gacha_pools=False)
    await source.fetch("endfield")
    calls_before = len(
        [url for url in client.calls if "ActivityTable" in url]
    )
    await source.fetch("endfield")
    calls_after = len([url for url in client.calls if "ActivityTable" in url])
    assert calls_before == calls_after == 1


# ---- PRTS ----


def prts_payload() -> dict:
    start = datetime(2026, 9, 4, 12, 0, tzinfo=GAME_TIMEZONE)
    end = datetime(2026, 9, 18, 3, 59, tzinfo=GAME_TIMEZONE)
    exchange = datetime(2026, 9, 25, 3, 59, tzinfo=GAME_TIMEZONE)
    return {
        "query": {
            "results": {
                "月行水上": {
                    "printouts": {
                        "活动开始时间": [
                            {"timestamp": str(int(start.timestamp()))}
                        ],
                        "活动结束时间": [{"timestamp": str(int(end.timestamp()))}],
                        "兑换结束时间": [
                            {"timestamp": str(int(exchange.timestamp()))}
                        ],
                        "名称": ["SideStory「月行水上」"],
                        "名称缩短": ["月行水上"],
                        "标题图文件名": ["活动预告 月行水上.jpg"],
                        "分类": [
                            {"fulltext": "分类:有活动信息的页面"},
                            {"fulltext": "分类:支线故事"},
                            {"fulltext": "分类:联动活动"},
                            {"fulltext": "分类:使用Tabber扩展的标签页"},
                            {"fulltext": "分类:正在编辑的页面"},
                        ],
                    },
                    "fullurl": "//prts.wiki/w/月行水上",
                },
                "无结束时间的页面": {
                    "printouts": {
                        "活动开始时间": [{"timestamp": "1790000000"}],
                        "名称缩短": ["无结束时间"],
                        "分类": [{"fulltext": "分类:登录活动"}],
                    },
                    "fullurl": "//prts.wiki/w/other",
                },
            }
        }
    }


def test_parse_smw_timestamp():
    parsed = parse_smw_timestamp({"timestamp": "1790539200"})
    assert parsed is not None
    assert parsed.astimezone(timezone.utc).hour == 20
    assert parsed.hour == 4
    assert parse_smw_timestamp({"timestamp": ""}) is None
    assert parse_smw_timestamp({"timestamp": "abc"}) is None


@pytest.mark.anyio
async def test_prts_parse():
    client = FakeClient({"action=ask": prts_payload()})
    source = PrtsSource(client, min_request_interval_seconds=0)
    activities = list(await source.fetch("arknights"))

    assert len(activities) == 1
    item = activities[0]
    assert item.name == "月行水上"
    assert item.description == "SideStory「月行水上」"
    assert item.start_time is not None and item.start_time.hour == 12
    assert item.end_time is not None and item.end_time.day == 18
    assert item.tags == ("支线故事", "联动活动")
    assert item.url == "https://prts.wiki/w/月行水上"
    assert item.extra_times[0][0] == "兑换结束"
    # 封面按 MediaWiki 的 md5 分片规则拼出（Special:FilePath 在 PRTS 上是 404）
    digest = hashlib.md5("活动预告_月行水上.jpg".encode("utf-8")).hexdigest()
    assert item.cover == (
        f"https://media.prts.wiki/{digest[0]}/{digest[:2]}/"
        + quote("活动预告_月行水上.jpg")
    )


@pytest.mark.anyio
async def test_prts_without_cover_file():
    payload = prts_payload()
    payload["query"]["results"]["月行水上"]["printouts"]["标题图文件名"] = []
    client = FakeClient({"action=ask": payload})
    source = PrtsSource(client, min_request_interval_seconds=0)
    activities = list(await source.fetch("arknights"))
    assert activities[0].cover == ""


@pytest.mark.anyio
async def test_prts_query_contains_time_filter():
    client = FakeClient({"action=ask": {"query": {"results": {}}}})
    source = PrtsSource(client, min_request_interval_seconds=0, past_days=7)
    await source.fetch("arknights")
    assert "action=ask" in client.calls[0]
    assert "%E6%B4%BB%E5%8A%A8%E7%BB%93%E6%9D%9F%E6%97%B6%E9%97%B4" in client.calls[0]


# ---- 游戏注册表 ----


def build_registry() -> GameRegistry:
    client = FakeClient({})
    return GameRegistry([SraSource(client), AkedataSource(client), PrtsSource(client)])


def test_registry_contains_expected_games():
    registry = build_registry()
    assert len(registry.games) == 18
    assert registry.get("endfield") is not None


@pytest.mark.parametrize(
    ("game_id", "name"),
    [
        ("xtlr", "星塔旅人"),
        ("infinitynikki", "无限暖暖"),
        ("gf2", "少女前线2：追放"),
        ("pgr", "战双帕弥什"),
        ("czn", "卡厄斯梦境（国际服）"),
        ("bd2", "棕色尘埃2"),
        ("nikke", "NIKKE（国际服）"),
        ("hyrz", "火影忍者"),
    ],
)
def test_registry_only_sra_games_are_registered(game_id, name):
    registry = build_registry()
    game = registry.get(game_id)
    assert game is not None
    assert game.name == name
    assert game.source_id == "sra"
    assert registry.source_options(game_id) == ()


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("星塔", "xtlr"),
        ("暖暖", "infinitynikki"),
        ("少前2", "gf2"),
        ("少前", "gf2"),
        ("战双", "pgr"),
        ("卡厄斯", "czn"),
        ("棕尘", "bd2"),
        ("bd2", "bd2"),
        ("妮姬", "nikke"),
        ("nikke", "nikke"),
        ("火影", "hyrz"),
    ],
)
def test_registry_resolves_new_sra_game_aliases(token, expected):
    registry = build_registry()
    games = registry.resolve(token)
    assert games
    assert games[0].game_id == expected


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("sr", "sr"),
        ("崩铁", "sr"),
        ("星穹铁道", "sr"),
        ("崩坏：星穹铁道", "sr"),
        ("绝区零", "zzz"),
        ("终末地", "endfield"),
        ("明日方舟", "arknights"),
        ("方舟", "arknights"),
        ("ba", "ba-cn"),
    ],
)
def test_registry_resolves_aliases(token, expected):
    registry = build_registry()
    games = registry.resolve(token)
    assert games
    assert games[0].game_id == expected


def test_registry_generic_alias_matches_all_servers():
    registry = build_registry()
    games = registry.resolve("蔚蓝档案")
    assert {game.game_id for game in games} == {"ba-cn", "ba-global", "ba-jp"}
    assert {game.game_id for game in registry.resolve("碧蓝档案")} == {
        "ba-cn",
        "ba-global",
        "ba-jp",
    }


def test_registry_substring_fallback():
    registry = build_registry()
    games = registry.resolve("档案")
    assert {game.game_id for game in games} == {"ba-cn", "ba-global", "ba-jp"}
    assert [game.game_id for game in registry.resolve("星穹")] == ["sr"]


def test_registry_unknown_token():
    registry = build_registry()
    assert registry.resolve("不存在的游戏") == []


def test_registry_resolve_many_reports_unknown():
    registry = build_registry()
    games, unknown = registry.resolve_many(["sr", "原神", "不存在的游戏"])
    assert [game.game_id for game in games] == ["sr", "ys"]
    assert unknown == ["不存在的游戏"]


# ---- 代理分流：国内站点直连 ----


def test_direct_hosts_default_covers_domestic_sources():
    assert normalize_direct_hosts(None) == DEFAULT_DIRECT_HOSTS
    assert "prts.wiki" in DEFAULT_DIRECT_HOSTS
    assert "data.akedata.wiki" in DEFAULT_DIRECT_HOSTS


@pytest.mark.parametrize(
    "raw",
    [
        "prts.wiki, media.prts.wiki",
        "prts.wiki\nmedia.prts.wiki",
        "https://prts.wiki/w/首页\nhttps://media.prts.wiki/x.png",
        " prts.wiki ，media.prts.wiki ； ",
    ],
)
def test_normalize_direct_hosts_accepts_common_separators(raw):
    assert normalize_direct_hosts(raw) == ("prts.wiki", "media.prts.wiki")


def test_normalize_direct_hosts_strips_scheme_path_and_port():
    hosts = normalize_direct_hosts("HTTP://Data.AKEdata.wiki:443/public/x.png")
    assert hosts == ("data.akedata.wiki",)


def test_normalize_direct_hosts_empty_disables_direct_routing():
    assert normalize_direct_hosts("") == ()
    assert normalize_direct_hosts("   ,  ") == ()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://prts.wiki/api.php", True),
        ("https://media.prts.wiki/a.png", True),
        ("https://data.akedata.wiki/public/x.png", True),
        ("https://starrailassistant.top/api/v1/activity", False),
        ("https://notprts.wiki/x", False),
        ("", False),
    ],
)
def test_is_direct_host_matches_subdomains_only(url, expected):
    assert is_direct_host(url, DEFAULT_DIRECT_HOSTS) is expected


def test_build_direct_mounts_is_empty_without_hosts():
    assert build_direct_mounts(()) == {}


def test_build_direct_mounts_covers_host_and_subdomains():
    pytest.importorskip("httpx")
    mounts = build_direct_mounts(("prts.wiki",))
    assert set(mounts) == {"all://prts.wiki", "all://*.prts.wiki"}


def test_http_client_keeps_direct_hosts():
    client = JsonHttpClient(direct_hosts=DEFAULT_DIRECT_HOSTS)
    assert client.direct_hosts == DEFAULT_DIRECT_HOSTS


# ---- 同一游戏多数据源：可切换来源 ----


def test_registry_defaults_prts_and_akedata_for_shared_games():
    registry = build_registry()
    assert registry.source_for("arknights").source_id == "prts"
    assert registry.source_for("endfield").source_id == "akedata"
    assert registry.get("arknights").source_id == "prts"
    assert {source.source_id for source in registry.sources_for("arknights")} == {
        "sra",
        "prts",
    }


def test_registry_preferred_switches_shared_game_to_sra():
    client = FakeClient()
    registry = GameRegistry(
        [SraSource(client), AkedataSource(client), PrtsSource(client)],
        preferred={"arknights": "sra", "endfield": "sra"},
    )
    assert registry.source_for("arknights").source_id == "sra"
    assert registry.source_for("endfield").source_id == "sra"
    assert registry.get("arknights").source_id == "sra"
    # 其它游戏不受影响
    assert registry.source_for("sr").source_id == "sra"
    assert registry.source_for("ys").source_id == "sra"


def test_registry_falls_back_when_preferred_source_is_disabled():
    client = FakeClient()
    registry = GameRegistry([SraSource(client)], preferred={"arknights": "prts"})
    assert registry.source_for("arknights").source_id == "sra"
    assert registry.source_options("arknights") == ()


def test_registry_source_options_empty_for_single_source_game():
    registry = build_registry()
    assert registry.source_options("sr") == ()
    assert len(registry.source_options("arknights")) == 2
    assert len(registry.source_options("endfield")) == 2


def test_registry_ignores_unknown_preferred_source():
    client = FakeClient()
    registry = GameRegistry([SraSource(client)], preferred={"arknights": "prts"})
    assert registry.source_name("arknights") == "SRA 公共 API"


@pytest.mark.anyio
async def test_sra_fetch_maps_shared_game_ids_to_remote_ids():
    payload = {
        "version": "2026-11",
        "activities": [
            {
                "name": "「冻土与火种」网页活动",
                "startTime": "2026-10-24T20:00:00",
                "endTime": "2026-11-24T03:59:59",
            }
        ],
    }
    client = FakeClient({"activity/ak.json": payload, "activity/end.json": payload})
    source = SraSource(client)

    ak_activities = list(await source.fetch("arknights"))
    end_activities = list(await source.fetch("endfield"))

    assert client.calls[0].endswith("/ak.json")
    assert client.calls[1].endswith("/end.json")
    assert ak_activities[0].game_id == "arknights"
    assert end_activities[0].game_id == "endfield"


def test_sra_games_expose_arknights_and_endfield():
    client = FakeClient()
    source = SraSource(client)
    ids = {game.game_id for game in source.games()}
    assert {"arknights", "endfield"} <= ids