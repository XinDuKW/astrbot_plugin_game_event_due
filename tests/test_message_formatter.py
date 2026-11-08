"""推送消息的格式与图片片段。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

from astrbot_plugin_game_event_due.services.message_formatter import (
    COVER_MODE_IMAGE,
    COVER_MODE_LINK,
    COVER_MODE_OFF,
    DEFAULT_ACTIVITY_SEPARATOR,
    DEFAULT_GAME_SHORT_NAMES,
    DEFAULT_URGENCY_LEVELS,
    DEFAULT_URGENCY_RULES,
    PART_IMAGE,
    PART_TEXT,
    MessagePart,
    MessageSettings,
    build_overview_blocks,
    build_overview_message,
    build_overview_parts,
    build_reminder_message,
    build_reminder_parts,
    build_single_reminder_message,
    build_single_reminder_parts,
    build_urgent_dm_body_parts,
    build_urgent_dm_groups,
    build_urgent_dm_headline,
    build_urgent_dm_parts,
    build_urgent_dm_summary,
    build_urgent_dm_title,
    normalize_cover_mode,
    normalize_game_aliases,
    parse_urgency_rules,
    parts_to_text,
    short_game_name,
    truncate,
    urgency_level_for,
)
from astrbot_plugin_game_event_due.services.reminder_service import ReminderPlan
from astrbot_plugin_game_event_due.services.template_preview import (
    build_template_preview,
    build_urgent_preview,
    format_game_pairs,
    format_urgency_rules,
)
from astrbot_plugin_game_event_due.sources.base import Activity, GAME_TIMEZONE
from astrbot_plugin_game_event_due.sources.sra import SRA_GAMES

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=GAME_TIMEZONE)
GAME = next(game for game in SRA_GAMES if game.game_id == "sr")
SETTINGS = MessageSettings()
# 关闭新加的标记与标题剩余时间，用于验证与这些无关的排版行为
PLAIN_SETTINGS = MessageSettings(urgency_marker=False, headline_remaining=False)
COVER = "https://example.com/cover.png"


def images_of(parts) -> list[str]:
    return [part.value for part in parts if part.is_image]


def make_activity(
    name: str = "测试活动",
    *,
    start: timedelta | None = timedelta(days=-1),
    end: timedelta | None = timedelta(days=5),
    activity_id: str = "a1",
    **kwargs,
) -> Activity:
    return Activity(
        game_id="sr",
        activity_id=activity_id,
        name=name,
        start_time=None if start is None else NOW + start,
        end_time=None if end is None else NOW + end,
        **kwargs,
    )


def make_plan(activity: Activity, reasons=("new",), tier: str = "") -> ReminderPlan:
    return ReminderPlan(
        activity=activity,
        reasons=tuple(reasons),
        tier=tier,
        remaining=activity.remaining(NOW),
    )


def test_truncate_collapses_and_limits():
    assert truncate("  多个   空格  ", 100) == "多个 空格"
    long_text = "字" * 50
    assert len(truncate(long_text, 10)) == 10
    assert truncate(long_text, 10).endswith("…")


def test_cross_year_end_time_shows_year():
    activity = make_activity(
        "跨年活动",
        start=timedelta(days=-100),
        end=timedelta(days=200),
    )
    assert activity.end_time is not None
    assert activity.start_time is not None
    assert activity.end_time.year != activity.start_time.year

    text = build_reminder_message(GAME, [make_plan(activity)], NOW, SETTINGS)
    assert f"开始：{activity.start_time:%m-%d %H:%M}" in text
    assert f"结束：{activity.end_time:%Y-%m-%d %H:%M}" in text


def test_same_year_times_omit_year():
    activity = make_activity("当年活动", start=timedelta(days=-2), end=timedelta(days=3))
    text = build_reminder_message(GAME, [make_plan(activity)], NOW, SETTINGS)
    assert activity.start_time is not None and activity.end_time is not None
    assert f"开始：{activity.start_time:%m-%d %H:%M}" in text
    assert f"结束：{activity.end_time:%m-%d %H:%M}" in text
    assert str(NOW.year) not in text


def test_cross_year_extra_time_shows_year():
    activity = make_activity(
        "活动",
        start=timedelta(days=-2),
        end=timedelta(days=5),
        extra_times=(("兑换结束", NOW + timedelta(days=150)),),
    )
    text = build_reminder_message(GAME, [make_plan(activity)], NOW, SETTINGS)
    exchange = NOW + timedelta(days=150)
    assert f"兑换结束：{exchange:%Y-%m-%d %H:%M}" in text


def test_build_reminder_message_contains_header_and_labels():
    activity = make_activity(
        "方寸大冒险",
        start=timedelta(days=1),
        end=timedelta(days=3, hours=6),
        cover="https://example.com/c.png",
        tags=("限时活动",),
        extra_times=(("兑换结束", NOW + timedelta(days=7)),),
        description="一段描述",
    )
    text = build_reminder_message(
        GAME, [make_plan(activity, ("new", "d3"), "d3")], NOW, SETTINGS
    )

    assert text.startswith("🎮 崩坏：星穹铁道 · 活动提醒")
    assert "【新活动 · 剩余不足3天】方寸大冒险" in text
    assert "开始：" in text
    assert "结束：" in text
    assert "剩余 3天6小时" in text
    assert "标签：限时活动" in text
    assert "兑换结束：" in text
    assert "封面：https://example.com/c.png" in text


def test_d1_uses_24_hour_label():
    activity = make_activity(end=timedelta(hours=5))
    text = build_single_reminder_message(
        GAME, make_plan(activity, ("d1",), "d1"), NOW, SETTINGS
    )
    assert "【剩余不足24小时】" in text
    assert "剩余 5小时" in text


def test_single_reminder_message_repeats_header():
    activity = make_activity()
    text = build_single_reminder_message(
        GAME, make_plan(activity), NOW, SETTINGS
    )
    assert text.count("🎮") == 1


def test_settings_can_disable_optional_fields():
    settings = MessageSettings(
        include_description=False,
        cover_mode=COVER_MODE_OFF,
        include_tags=False,
        include_extra_times=False,
    )
    activity = make_activity(
        "活动",
        cover=COVER,
        tags=("限时活动",),
        description="描述",
        extra_times=(("兑换结束", NOW + timedelta(days=1)),),
    )
    text = build_reminder_message(GAME, [make_plan(activity)], NOW, settings)

    assert "封面" not in text
    assert "标签" not in text
    assert "兑换结束" not in text
    assert "说明" not in text


def test_description_included_when_enabled():
    settings = MessageSettings(include_description=True)
    activity = make_activity("活动", description="很长的描述" * 100)
    text = build_reminder_message(GAME, [make_plan(activity)], NOW, settings)
    assert "说明：" in text
    assert "…" in text


def test_url_rendered_as_detail_link():
    activity = make_activity("活动", url="https://prts.wiki/w/测试")
    text = build_reminder_message(GAME, [make_plan(activity)], NOW, SETTINGS)
    assert "详情：https://prts.wiki/w/测试" in text


def test_overview_groups_active_and_upcoming():
    activities = [
        make_activity("进行中", activity_id="a", start=timedelta(days=-2), end=timedelta(days=3)),
        make_activity("即将开始", activity_id="b", start=timedelta(days=5), end=timedelta(days=20)),
    ]
    text = build_overview_message(GAME, activities, NOW, SETTINGS, source_name="SRA 公共 API")

    assert "🎮 崩坏：星穹铁道 · 当前活动" in text
    assert re.search(r"▎进行中（.{0,8}1）", text)
    assert re.search(r"▎即将开始（.{0,8}1）", text)
    assert "（数据来源：SRA 公共 API）" in text
    assert text.index("进行中") < text.index("即将开始")


def test_overview_shows_remaining_only_for_active():
    activities = [
        make_activity("即将开始", start=timedelta(days=5), end=timedelta(days=20)),
    ]
    text = build_overview_message(GAME, activities, NOW, SETTINGS)
    assert "剩余" not in text


def test_overview_empty_state():
    text = build_overview_message(GAME, [], NOW, SETTINGS)
    assert "当前没有进行中或即将开始的活动。" in text


def test_overview_orders_active_by_end_time():
    activities = [
        make_activity("晚结束", activity_id="a", start=timedelta(days=-2), end=timedelta(days=9)),
        make_activity("早结束", activity_id="b", start=timedelta(days=-2), end=timedelta(days=2)),
    ]
    text = build_overview_message(GAME, activities, NOW, SETTINGS)
    assert text.index("早结束") < text.index("晚结束")


def test_overview_limits_list_length():
    activities = [
        make_activity(
            f"活动{i}",
            activity_id=f"a{i}",
            start=timedelta(days=-2),
            end=timedelta(days=10 + i),
        )
        for i in range(20)
    ]
    text = build_overview_message(GAME, activities, NOW, SETTINGS)
    assert "另有 5 个进行中的活动" in text


# ---- 封面模式 ----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("image", COVER_MODE_IMAGE),
        ("IMAGE", COVER_MODE_IMAGE),
        ("link", COVER_MODE_LINK),
        ("off", COVER_MODE_OFF),
        (True, COVER_MODE_IMAGE),
        (False, COVER_MODE_OFF),
        ("true", COVER_MODE_IMAGE),
        ("false", COVER_MODE_OFF),
        ("图片", COVER_MODE_IMAGE),
        ("链接", COVER_MODE_LINK),
        (None, COVER_MODE_IMAGE),
        ("乱七八糟", COVER_MODE_IMAGE),
    ],
)
def test_normalize_cover_mode(raw, expected):
    assert normalize_cover_mode(raw) == expected


def test_default_mode_emits_image_part():
    activity = make_activity("活动", cover=COVER)
    parts = build_reminder_parts(GAME, [make_plan(activity)], NOW, SETTINGS)
    assert images_of(parts) == [COVER]
    # 图片模式下正文里不再重复封面链接
    assert COVER not in "".join(p.value for p in parts if not p.is_image)


def test_link_mode_emits_text_line_only():
    settings = MessageSettings(cover_mode=COVER_MODE_LINK)
    activity = make_activity("活动", cover=COVER)
    parts = build_reminder_parts(GAME, [make_plan(activity)], NOW, settings)
    assert images_of(parts) == []
    assert f"封面：{COVER}" in parts_to_text(parts)


def test_off_mode_has_no_cover_at_all():
    settings = MessageSettings(cover_mode=COVER_MODE_OFF)
    activity = make_activity("活动", cover=COVER)
    parts = build_reminder_parts(GAME, [make_plan(activity)], NOW, settings)
    assert images_of(parts) == []
    assert COVER not in parts_to_text(parts)


def test_activity_without_cover_produces_no_image():
    activity = make_activity("无封面活动", cover="")
    parts = build_reminder_parts(GAME, [make_plan(activity)], NOW, SETTINGS)
    assert images_of(parts) == []


def test_all_covers_become_parts_without_a_count_cap():
    """图片数量不在这里限制，改由发送层的体积预算决定。"""
    plans = [
        make_plan(
            make_activity(
                f"活动{i}",
                activity_id=f"a{i}",
                cover=f"https://example.com/{i}.png",
            )
        )
        for i in range(6)
    ]
    parts = build_reminder_parts(GAME, plans, NOW, SETTINGS)
    assert images_of(parts) == [
        f"https://example.com/{i}.png" for i in range(6)
    ]
    # 图片模式下正文不出现封面链接
    assert "封面：" not in "".join(p.value for p in parts if not p.is_image)


def test_cover_budget_settings_are_exposed():
    assert MessageSettings().cover_max_total_bytes == 5 * 1024 * 1024
    assert MessageSettings(cover_max_total_mb=0).cover_max_total_bytes == 0
    assert MessageSettings(cover_max_total_mb=-1).cover_max_total_bytes == 0
    assert MessageSettings(cover_max_total_mb=12).cover_max_total_bytes == 12 * 1024 * 1024
    assert MessageSettings(cover_max_total_mb="abc").cover_max_total_bytes == 5 * 1024 * 1024


def test_cover_compression_defaults_on():
    settings = MessageSettings()
    assert settings.cover_compress is True
    assert settings.cover_max_width == 720


def test_assemble_merges_adjacent_text_and_keeps_block_order():
    plans = [
        make_plan(make_activity("甲", activity_id="a", cover="https://e.com/1.png")),
        make_plan(make_activity("乙", activity_id="b", cover="https://e.com/2.png")),
    ]
    parts = build_reminder_parts(GAME, plans, NOW, SETTINGS)

    kinds = [part.kind for part in parts]
    assert kinds == [PART_TEXT, PART_IMAGE, PART_TEXT, PART_IMAGE]
    # 图片之后的文本块以空行开头，保证换行排版
    assert parts[2].value.startswith("\n\n")
    assert "甲" in parts[0].value and "乙" in parts[2].value


def test_single_reminder_parts_structure():
    activity = make_activity("活动", cover=COVER)
    parts = build_single_reminder_parts(
        GAME, make_plan(activity, ("d1",), "d1"), NOW, SETTINGS
    )
    assert [part.kind for part in parts] == [PART_TEXT, PART_IMAGE]
    assert "【剩余不足24小时】" in parts[0].value


def test_overview_parts_carry_images_and_section_titles():
    activities = [
        make_activity("进行中", activity_id="a", cover="https://e.com/a.png"),
        make_activity(
            "即将开始",
            activity_id="b",
            start=timedelta(days=5),
            end=timedelta(days=20),
            cover="https://e.com/b.png",
        ),
    ]
    parts = build_overview_parts(GAME, activities, NOW, SETTINGS, source_name="SRA")
    assert images_of(parts) == ["https://e.com/a.png", "https://e.com/b.png"]

    text = parts_to_text(parts)
    # 分段标题必须紧挨着它自己那一批活动，不能被挤到消息开头
    # （活动标题前会带剩余时间标记，这里用正则跳过它）
    assert re.search(r"▎进行中（.{0,8}1）\n\n\S+ 进行中", text)
    assert re.search(r"▎即将开始（.{0,8}1）\n\n\S+ 即将开始", text)
    assert (
        text.index("▎进行中")
        < text.index("▎即将开始")
        < text.index("（数据来源：SRA）")
    )


def test_overview_parts_without_source_name():
    parts = build_overview_parts(GAME, [], NOW, SETTINGS)
    assert "数据来源" not in parts_to_text(parts)
    assert "当前没有进行中或即将开始的活动。" in parts_to_text(parts)


def test_parts_to_text_renders_images_as_links():
    parts = [
        MessagePart(PART_TEXT, "文本"),
        MessagePart(PART_IMAGE, COVER),
        MessagePart(PART_TEXT, "结尾"),
    ]
    assert parts_to_text(parts) == f"文本\n封面：{COVER}\n结尾"
    assert parts_to_text([]) == ""


def test_parts_to_text_does_not_double_blank_line_after_image():
    """图片后的续接文本自带空行，不应再多补一个换行。"""
    parts = [
        MessagePart(PART_TEXT, "第一段"),
        MessagePart(PART_IMAGE, COVER),
        MessagePart(PART_TEXT, "\n\n第二段"),
    ]
    assert parts_to_text(parts) == f"第一段\n封面：{COVER}\n\n第二段"
    assert "\n\n\n" not in parts_to_text(parts)


def test_parts_to_text_skips_empty_chunks():
    parts = [
        MessagePart(PART_TEXT, "文本"),
        MessagePart(PART_TEXT, ""),
    ]
    assert parts_to_text(parts) == "文本"


# ---- 活动之间的分隔线 ----


def make_plans(names: list[str], *, with_cover: bool = False) -> list[ReminderPlan]:
    return [
        make_plan(
            make_activity(
                name,
                activity_id=f"id-{name}",
                cover=f"https://e.com/{name}.png" if with_cover else "",
            )
        )
        for name in names
    ]


def test_separator_inserted_between_activities():
    parts = build_reminder_parts(GAME, make_plans(["甲", "乙"]), NOW, PLAIN_SETTINGS)
    text = parts_to_text(parts)
    assert DEFAULT_ACTIVITY_SEPARATOR in text
    # 分隔线紧跟在「甲」那一块之后，紧挨着「乙」的标题
    assert f"\n\n{DEFAULT_ACTIVITY_SEPARATOR}\n\n【新活动】乙" in text


def test_separator_not_before_first_activity():
    parts = build_reminder_parts(GAME, make_plans(["甲", "乙"]), NOW, PLAIN_SETTINGS)
    text = parts_to_text(parts)

    header, _, rest = text.partition("\n\n")
    assert header == "🎮 崩坏：星穹铁道 · 活动提醒"
    # 头部之后直接就是第一个活动，中间没有分隔线
    assert rest.startswith("【新活动】甲")
    assert DEFAULT_ACTIVITY_SEPARATOR not in text.split("【新活动】甲")[0]


def test_separator_count_matches_gaps():
    parts = build_reminder_parts(
        GAME, make_plans(["甲", "乙", "丙", "丁"]), NOW, SETTINGS
    )
    text = parts_to_text(parts)
    assert text.count(DEFAULT_ACTIVITY_SEPARATOR) == 3


def test_single_activity_has_no_separator():
    parts = build_reminder_parts(GAME, make_plans(["独苗"]), NOW, SETTINGS)
    assert DEFAULT_ACTIVITY_SEPARATOR not in parts_to_text(parts)

    single = build_single_reminder_parts(
        GAME, make_plan(make_activity("独苗")), NOW, SETTINGS
    )
    assert DEFAULT_ACTIVITY_SEPARATOR not in parts_to_text(single)


def test_empty_separator_falls_back_to_blank_line():
    settings = MessageSettings(
        activity_separator="", urgency_marker=False, headline_remaining=False
    )
    parts = build_reminder_parts(GAME, make_plans(["甲", "乙"]), NOW, settings)
    text = parts_to_text(parts)
    assert DEFAULT_ACTIVITY_SEPARATOR not in text
    assert "\n\n【新活动】乙" in text


def test_custom_separator_is_used():
    settings = MessageSettings(
        activity_separator="------", urgency_marker=False, headline_remaining=False
    )
    parts = build_reminder_parts(GAME, make_plans(["甲", "乙"]), NOW, settings)
    assert "\n\n------\n\n【新活动】乙" in parts_to_text(parts)


def test_separator_lands_after_the_previous_cover_image():
    parts = build_reminder_parts(
        GAME, make_plans(["甲", "乙"], with_cover=True), NOW, SETTINGS
    )
    kinds = [part.kind for part in parts]
    assert kinds == [PART_TEXT, PART_IMAGE, PART_TEXT, PART_IMAGE]
    # 第二个文本片段（图片之后）必须以分隔线开头
    assert parts[2].value.startswith(f"\n\n{DEFAULT_ACTIVITY_SEPARATOR}\n\n")


def test_separator_not_inserted_after_section_heading():
    activities = [
        make_activity("进行中1", activity_id="a1"),
        make_activity("进行中2", activity_id="a2"),
        make_activity(
            "即将开始1",
            activity_id="b1",
            start=timedelta(days=5),
            end=timedelta(days=20),
        ),
    ]
    parts = build_overview_parts(GAME, activities, NOW, SETTINGS, source_name="SRA")
    text = parts_to_text(parts)

    # 分段标题紧跟它自己的第一个活动，不能被分隔线割开
    assert re.search(r"▎进行中（.{0,8}2）\n\n\S+ 进行中1", text)
    assert re.search(r"▎即将开始（.{0,8}1）\n\n\S+ 即将开始1", text)
    # 只有「进行中1 / 进行中2」这一处插入分隔线
    assert text.count(DEFAULT_ACTIVITY_SEPARATOR) == 1
    assert re.search(
        rf"\n\n{DEFAULT_ACTIVITY_SEPARATOR}\n\n\S+ 进行中2", text
    )


def test_separator_no_extra_blank_lines():
    parts = build_reminder_parts(
        GAME, make_plans(["甲", "乙"], with_cover=True), NOW, SETTINGS
    )
    text = parts_to_text(parts)
    assert "\n\n\n" not in text


# ---- 剩余时间标记与排序 ----


def test_urgency_level_picks_most_urgent_bucket():
    rules = parse_urgency_rules(DEFAULT_URGENCY_RULES)
    assert urgency_level_for(timedelta(hours=6), rules).marker == "🟥"
    assert urgency_level_for(timedelta(days=2), rules).marker == "🟧"
    assert urgency_level_for(timedelta(days=6), rules).marker == "🟩"
    assert urgency_level_for(timedelta(days=30), rules).marker == "🟦"
    assert urgency_level_for(timedelta(seconds=-1), rules) is None
    assert urgency_level_for(None, rules) is None


def test_urgency_rules_accept_color_names():
    rules = parse_urgency_rules("2:红,5:橙")
    assert [level.marker for level in rules] == ["🟥", "🟧"]
    assert urgency_level_for(timedelta(days=9), rules) is None


def test_urgency_rules_support_fallback_bucket():
    rules = parse_urgency_rules("2:红,*:蓝")
    assert urgency_level_for(timedelta(days=9), rules).marker == "🟦"


def test_urgency_rules_fall_back_to_defaults_on_garbage():
    assert parse_urgency_rules("完全看不懂") == DEFAULT_URGENCY_LEVELS
    assert parse_urgency_rules("") == DEFAULT_URGENCY_LEVELS
    assert parse_urgency_rules(None) == DEFAULT_URGENCY_LEVELS


def test_headline_carries_marker_and_remaining_time():
    activity = make_activity("活动", end=timedelta(hours=6))
    text = build_reminder_message(
        GAME, [make_plan(activity, ("d1",), "d1")], NOW, SETTINGS
    )
    assert "🟥 【剩余不足24小时】活动（剩余 6小时）" in text


def test_marker_and_summary_can_be_disabled():
    settings = MessageSettings(urgency_marker=False)
    activity = make_activity("活动", end=timedelta(hours=6))
    text = build_reminder_message(GAME, [make_plan(activity)], NOW, settings)
    assert "🟥" not in text
    assert text.startswith("🎮 崩坏：星穹铁道 · 活动提醒\n")


def test_header_carries_tier_summary():
    plans = [
        make_plan(make_activity("急", activity_id="a", end=timedelta(hours=6))),
        make_plan(make_activity("缓", activity_id="b", end=timedelta(days=6))),
    ]
    text = build_reminder_message(GAME, plans, NOW, SETTINGS)
    assert "🟥1 | 🟩1" in text


def test_tier_summary_can_be_disabled():
    settings = MessageSettings(tier_summary=False)
    plans = [make_plan(make_activity("急", end=timedelta(hours=6)))]
    text = build_reminder_message(GAME, plans, NOW, settings)
    assert text.startswith("🎮 崩坏：星穹铁道 · 活动提醒\n")


def test_sort_by_urgency_puts_the_soonest_first():
    plans = [
        make_plan(make_activity("缓", activity_id="b", end=timedelta(days=6))),
        make_plan(make_activity("急", activity_id="a", end=timedelta(hours=2))),
    ]
    text = build_reminder_message(GAME, plans, NOW, SETTINGS)
    assert text.index("急") < text.index("缓")

    kept = parts_to_text(
        build_reminder_parts(GAME, plans, NOW, MessageSettings(sort_by_urgency=False))
    )
    assert kept.index("缓") < kept.index("急")


def test_overview_also_marks_activities():
    activities = [
        make_activity("进行中", activity_id="a", start=timedelta(days=-2), end=timedelta(days=2)),
    ]
    text = build_overview_message(GAME, activities, NOW, SETTINGS)
    assert "🟧 进行中（剩余 2天）" in text


# ---- 游戏显示名（别名） ----


def test_normalize_game_aliases_parses_pairs():
    assert normalize_game_aliases("sr=星铁, arknights:舟游") == (
        ("sr", "星铁"),
        ("arknights", "舟游"),
    )
    assert normalize_game_aliases("ys=原神\nZZZ＝绝区零") == (
        ("ys", "原神"),
        ("zzz", "绝区零"),
    )
    assert normalize_game_aliases(["ys=原神", "  ", "坏的"]) == (("ys", "原神"),)
    assert normalize_game_aliases("") == ()
    # 同一个键只认第一条
    assert normalize_game_aliases("sr=A,sr=B") == (("sr", "A"),)


def test_display_game_name_uses_alias():
    aliased = MessageSettings(game_aliases=(("sr", "星铁"),))
    text = build_reminder_message(GAME, [make_plan(make_activity("活动"))], NOW, aliased)
    assert text.startswith("🎮 星铁 · 活动提醒")

    # 内置别名（starrail）同样能命中
    by_alias = MessageSettings(game_aliases=(("starrail", "SR"),))
    assert build_reminder_message(
        GAME, [make_plan(make_activity("活动"))], NOW, by_alias
    ).startswith("🎮 SR · 活动提醒")

    # 完整名称也能命中
    by_name = MessageSettings(game_aliases=(("崩坏：星穹铁道", "崩铁"),))
    assert build_reminder_message(
        GAME, [make_plan(make_activity("活动"))], NOW, by_name
    ).startswith("🎮 崩铁 · 活动提醒")

    # 没配置就保持原样
    assert build_reminder_message(
        GAME, [make_plan(make_activity("活动"))], NOW, MessageSettings()
    ).startswith("🎮 崩坏：星穹铁道 · 活动提醒")


def test_image_mode_hides_detail_link_by_default():
    activity = make_activity(
        "活动",
        cover="https://example.com/a.jpg",
        url="https://example.com/a",
    )
    text = build_reminder_message(
        GAME, [make_plan(activity)], NOW, MessageSettings()
    )
    assert "详情：" not in text

    # 显式打开开关后仍然附链接
    text_with_link = build_reminder_message(
        GAME,
        [make_plan(activity)],
        NOW,
        MessageSettings(image_mode_detail_link=True),
    )
    assert "详情：https://example.com/a" in text_with_link


def test_image_mode_keeps_detail_link_without_cover():
    text = build_reminder_message(
        GAME,
        [make_plan(make_activity("活动", url="https://example.com/a"))],
        NOW,
        MessageSettings(),
    )
    assert "详情：" in text


def test_alias_applies_to_overview_header():
    settings = MessageSettings(game_aliases=(("sr", "星铁"),))
    text = build_overview_message(GAME, [make_activity("活动")], NOW, settings)
    assert text.startswith("🎮 星铁 · 当前活动")


# ---- 游戏简称与紧急提醒分组 ----

GAMES_BY_ID = {game.game_id: game for game in SRA_GAMES}


def make_game_activity(
    game_id: str, name: str, *, hours: int, activity_id: str = ""
) -> Activity:
    return Activity(
        game_id=game_id,
        activity_id=activity_id or f"{game_id}-{name}",
        name=name,
        start_time=NOW - timedelta(days=1),
        end_time=NOW + timedelta(hours=hours),
    )


def test_short_game_name_uses_builtin_defaults():
    assert DEFAULT_GAME_SHORT_NAMES
    assert short_game_name(GAME, SETTINGS) == "铁"
    assert short_game_name(GAMES_BY_ID["ys"], SETTINGS) == "原"


def test_short_game_name_can_be_disabled_and_overridden():
    off = MessageSettings(use_short_names=False)
    assert short_game_name(GAME, off) == GAME.name
    custom = MessageSettings(game_short_names=(("sr", "崩"),))
    assert short_game_name(GAME, custom) == "崩"
    # 没配简称的游戏退回显示名
    assert short_game_name(GAMES_BY_ID["ys"], custom) == "原神"


def test_urgent_groups_split_by_game_with_most_urgent_first():
    activities = [
        make_game_activity("ys", "原神活动", hours=30),
        make_game_activity("sr", "星铁慢", hours=20),
        make_game_activity("sr", "星铁急", hours=5),
    ]
    groups = build_urgent_dm_groups(
        activities, NOW, SETTINGS, games=GAMES_BY_ID
    )
    assert [group.game_id for group in groups] == ["sr", "ys"]
    assert [a.name for a in groups[0].activities] == ["星铁急", "星铁慢"]
    assert groups[0].short == "铁"


def test_urgent_title_lists_per_game_counts():
    activities = [
        make_game_activity("sr", "A", hours=5),
        make_game_activity("sr", "B", hours=20),
        make_game_activity("ys", "C", hours=40),
    ]
    groups = build_urgent_dm_groups(
        activities, NOW, SETTINGS, games=GAMES_BY_ID
    )
    title = build_urgent_dm_title(groups, SETTINGS)
    assert title.startswith("🚨 3 个活动马上就要结束了！")
    assert "铁:2" in title and "原:1" in title


def test_urgent_parts_separate_games():
    activities = [
        make_game_activity("sr", "A", hours=5),
        make_game_activity("ys", "C", hours=40),
    ]
    text = parts_to_text(
        build_urgent_dm_parts(activities, NOW, SETTINGS, games=GAMES_BY_ID)
    )
    assert "===========" in text
    assert "🎮 崩坏：星穹铁道（1 个）" in text
    assert "🎮 原神（1 个）" in text


def test_urgent_parts_without_registry_fall_back_to_ids():
    activities = [make_game_activity("zzz", "活动", hours=3)]
    text = parts_to_text(build_urgent_dm_parts(activities, NOW, SETTINGS))
    assert "🎮 zzz（1 个）" in text

def test_urgent_headline_and_summary_stay_separate_for_forward_nodes():
    activities = [
        make_game_activity("sr", "A", hours=5),
        make_game_activity("ys", "C", hours=40),
    ]
    groups = build_urgent_dm_groups(activities, NOW, SETTINGS, games=GAMES_BY_ID)
    assert build_urgent_dm_headline(groups) == "🚨 2 个活动马上就要结束了！"
    assert build_urgent_dm_summary(groups) == "铁:1 | 原:1"
    assert build_urgent_dm_title(groups, SETTINGS) == (
        "🚨 2 个活动马上就要结束了！\n🚨 铁:1 | 原:1"
    )


def test_urgent_summary_lists_every_game_even_single():
    activities = [make_game_activity("sr", "A", hours=5)]
    groups = build_urgent_dm_groups(activities, NOW, SETTINGS, games=GAMES_BY_ID)
    assert build_urgent_dm_summary(groups) == "铁:1"
    assert build_urgent_dm_title(groups, SETTINGS).endswith("\n🚨 铁:1")


def test_urgent_body_starts_with_summary_and_has_no_leading_blank():
    activities = [
        make_game_activity("sr", "A", hours=5),
        make_game_activity("ys", "C", hours=40),
    ]
    groups = build_urgent_dm_groups(activities, NOW, SETTINGS, games=GAMES_BY_ID)
    # 转发时正文首行取自标题第二行（带 🚨），与 delivery_service 保持一致
    headline = build_urgent_dm_title(groups, SETTINGS).partition("\n")[2]
    text = parts_to_text(
        build_urgent_dm_body_parts(groups, NOW, SETTINGS, headline=headline)
    )
    assert not text.startswith("\n")
    assert text.split("\n")[0] == "🚨 铁:1 | 原:1"
    assert "🎮 崩坏：星穹铁道（1 个）" in text


def test_urgent_body_without_headline_has_no_leading_blank():
    activities = [make_game_activity("zzz", "活动", hours=3)]
    groups = build_urgent_dm_groups(activities, NOW, SETTINGS, games=GAMES_BY_ID)
    text = parts_to_text(build_urgent_dm_body_parts(groups, NOW, SETTINGS))
    assert text.startswith("🎮 ")

def test_overview_blocks_split_per_activity():
    activities = [
        make_activity("进行中", activity_id="a"),
        make_activity(
            "即将开始",
            activity_id="b",
            start=timedelta(days=5),
            end=timedelta(days=20),
        ),
    ]
    header, blocks = build_overview_blocks(
        GAME, activities, NOW, SETTINGS, source_name="SRA"
    )

    # 标题自带档位统计，不点开也知道有几条；放得下才带「▎进行中」字样
    assert "🟩1" in header
    assert header.startswith("🎮 崩坏：星穹铁道 · 当前活动")
    # 每个活动一个节点，最后一条是数据来源
    assert len(blocks) == 3
    assert parts_to_text(blocks[0]).startswith("🟩 进行中")
    assert parts_to_text(blocks[-1]) == "（数据来源：SRA）"


def test_overview_blocks_without_activities_explains_empty():
    header, blocks = build_overview_blocks(GAME, [], NOW, SETTINGS)
    assert header == "🎮 崩坏：星穹铁道 · 当前活动"
    assert len(blocks) == 1
    assert "没有进行中" in parts_to_text(blocks[0])


def test_urgent_forward_node_name_defaults_to_reminder():
    assert SETTINGS.forward_node_name == "活动提醒"
    assert MessageSettings(forward_node_name="催一下").forward_node_name == "催一下"


def test_format_game_pairs_and_urgency_rules_roundtrip():
    assert format_game_pairs((("sr", "铁"), ("ys", "原"))) == "sr=铁\nys=原"
    assert format_game_pairs(()) == ""
    assert format_urgency_rules(DEFAULT_URGENCY_LEVELS) == DEFAULT_URGENCY_RULES
    assert (
        parse_urgency_rules(format_urgency_rules(DEFAULT_URGENCY_LEVELS))
        == DEFAULT_URGENCY_LEVELS
    )


def test_template_preview_covers_both_delivery_forms():
    activities = [
        make_activity("活动甲", activity_id="a", end=timedelta(hours=5)),
        make_activity("活动乙", activity_id="b", end=timedelta(days=2)),
    ]
    preview = build_template_preview(GAME, activities, NOW, SETTINGS, source_name="SRA")

    assert preview["sender"]["name"] == "游戏活动日历"
    assert preview["sender"]["avatar"] == "logo.png"
    assert preview["plain"]["header"].startswith("🎮 崩坏：星穹铁道 · 当前活动")
    assert preview["plain"]["sections"][0]["kind"] == "heading"
    # 标题节点 + 每个活动一个节点
    assert len(preview["forward"]["nodes"]) == 2
    assert preview["forward"]["card_title"] == "群聊的聊天记录"
    assert preview["forward"]["footer"] == "查看 3 条转发消息"


def test_template_preview_separator_only_between_activities():
    activities = [
        make_activity("甲", activity_id="a", end=timedelta(hours=5)),
        make_activity("乙", activity_id="b", end=timedelta(days=2)),
        make_activity("丙", activity_id="c", end=timedelta(days=6)),
    ]
    preview = build_template_preview(GAME, activities, NOW, SETTINGS)
    kinds = [section["kind"] for section in preview["plain"]["sections"]]
    assert kinds == [
        "heading",
        "activity",
        "separator",
        "activity",
        "separator",
        "activity",
    ]


def test_template_preview_uses_short_names_for_nodes():
    activities = [make_activity("甲", activity_id="a")]
    preview = build_template_preview(GAME, activities, NOW, SETTINGS)
    assert preview["short_name"] == "铁"
    assert preview["forward"]["nodes"][0]["name"] == "铁"

    full = build_template_preview(
        GAME, activities, NOW, MessageSettings(use_short_names=False)
    )
    assert full["forward"]["nodes"][0]["name"] == "崩坏：星穹铁道"


def test_template_preview_without_activities_explains_empty():
    preview = build_template_preview(GAME, [], NOW, SETTINGS)
    assert [section["kind"] for section in preview["plain"]["sections"]] == ["note"]
    assert preview["forward"]["nodes"] == []
    assert preview["forward"]["footer"] == "查看 1 条转发消息"


def test_urgent_preview_simulates_several_games_at_once():
    preview = build_urgent_preview(SRA_GAMES, NOW, SETTINGS, count=3)

    assert preview["count"] == 3
    # 3 个游戏各 2 个活动，标题里的数字是活动数
    assert "6 个活动马上就要结束了" in preview["headline"]
    # 标题节点 + 游戏分布节点 + 每个活动一个节点
    assert {node["name"] for node in preview["nodes"][:2]} == {
        SETTINGS.forward_node_name
    }
    assert len(preview["nodes"]) == 2 + 6
    assert {game["count"] for game in preview["games"]} == {2}
    assert {node["name"] for node in preview["nodes"][2:]} == {
        game["short"] for game in preview["games"]
    }


def test_urgent_preview_clamps_count_and_shows_every_colour():
    preview = build_urgent_preview(SRA_GAMES, NOW, SETTINGS, count=99)

    assert preview["count"] == 6
    # 6 个游戏各一个活动，剩余时间抖动，四档颜色都应出现
    assert len(preview["nodes"]) == 2 + 6
    body = "\n".join(preview["plain_lines"])
    for marker in ("🟥", "🟧", "🟩", "🟦"):
        assert marker in body


def test_urgent_preview_never_leaves_sample_activities_out():
    preview = build_urgent_preview(SRA_GAMES[:2], NOW, SETTINGS, count=2)

    assert preview["count"] == 2
    assert len(preview["games"]) == 2
    assert sum(game["count"] for game in preview["games"]) == 4
    assert preview["ack_hint"] in "\n".join(preview["plain_lines"])


def test_urgent_preview_without_games_returns_empty():
    preview = build_urgent_preview([], NOW, SETTINGS, count=3)

    assert preview["nodes"] == []
    assert preview["games"] == []
    assert preview["plain_lines"] == []
