"""AstrBot 游戏活动日历插件。

聚合 SRA 公共 API、AKEData（终末地）与 PRTS（明日方舟）的活动数据，按会话
订阅推送到群聊 / 私聊，支持新活动与剩余 7 天 / 3 天 / 24 小时分级提醒。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from collections.abc import Sequence

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .services.cover_service import CoverResolver
from .services.delivery_service import ActivityDeliveryService
from .services.message_formatter import (
    COVER_MODE_IMAGE,
    COVER_MODE_LINK,
    COVER_MODE_OFF,
    DEFAULT_ACTIVITY_SEPARATOR,
    DEFAULT_COVER_MAX_TOTAL_MB,
    DEFAULT_COVER_MAX_WIDTH,
    DEFAULT_COVER_MODE,
    DEFAULT_FORWARD_NODE_NAME,
    DEFAULT_GAME_SHORT_NAMES,
    DEFAULT_URGENCY_RULES,
    MessageSettings,
    build_overview_parts,
    display_game_name,
    normalize_cover_mode,
    normalize_game_aliases,
    normalize_game_short_names,
    parse_urgency_rules,
    short_game_name,
)
from .services.template_preview import format_game_pairs, format_urgency_rules
from .services.polling_service import ActivityPollingService, PollingSettings
from .services.reminder_service import ReminderPolicy, normalize_thresholds
from .services.subscription_service import (
    SubscriptionService,
    resolve_message_settings,
    resolve_policy,
)
from .sources import (
    DEFAULT_DIRECT_HOSTS,
    GAME_TIMEZONE,
    AkedataSource,
    JsonHttpClient,
    PrtsSource,
    SraSource,
    normalize_direct_hosts,
)
from .services.urgent_dm_service import (
    TARGET_SETTING_KEYS,
    UrgentDmSettings,
    describe_target_settings,
    is_ack_text,
    normalize_ack_keywords,
    normalize_games,
    normalize_targets,
    parse_quiet_hours,
    parse_target_settings,
    target_settings,
)
from .sources.registry import GameRegistry

try:  # pragma: no cover - 取决于 AstrBot 版本
    from .activity_webui import ActivityWebUIController
except ModuleNotFoundError as exc:  # pragma: no cover
    if exc.name != "astrbot.api.web":
        raise
    ActivityWebUIController = None  # type: ignore[assignment]

PLUGIN_NAME = "astrbot_plugin_game_event_due"

USAGE_SUBSCRIBE = (
    "用法: /活动订阅 <游戏名> [游戏名 ...]\n"
    "可用游戏请查看 /活动游戏"
)
# 未订阅时统一提示：所有会话级指令都从这里取，避免各写一份又慢慢走样
NO_SUBSCRIBE_TEXT = "当前会话没有订阅任何游戏\n" + USAGE_SUBSCRIBE
USAGE_THRESHOLD = (
    "用法: /活动阈值 <天数...>\n"
    "例如 /活动阈值 7 3 1 表示剩余不足 7 天 / 3 天 / 24 小时各提醒一次；\n"
    "/活动阈值 关闭 关闭倒计时提醒；/活动阈值 默认 恢复全局默认。"
)
USAGE_URGENT_DM = (
    "用法:\n"
    "/活动私人提醒 @某人       把某人加入紧急私聊提醒（管理员）\n"
    "/活动私人提醒 设置 @某人 3天 2小时 3次   单独设置某人的节奏（管理员）\n"
    "/活动私人提醒 设置 3天 2小时 3次         在私聊里直接设置自己的\n"
    "/活动私人提醒 移除 @某人  把某人移出紧急提醒（管理员）\n"
    "/活动私人提醒 列表        查看提醒目标与各自的节奏\n"
    "/活动私人提醒 停止        暂停对自己的紧急提醒（设置与记录保留）\n"
    "/活动私人提醒 开启        重新开启暂停的紧急提醒\n"
    "/活动私人提醒 测试        给自己发一条测试私聊\n"
    "设置的单位：3天=剩余不足 3 天就开始催，2小时=每 2 小时催一次，"
    "3次=每天最多 3 次；\n"
    "填「默认」恢复跟随全局配置。也可以直接贴 aiocqhttp:FriendMessage:123456 "
    "这种会话号指定目标。\n"
    "被提醒的人回一句 /我已知晓 就停掉最近那一轮提醒；跟上活动名只停那一个，\n"
    "写「全部」则一次停掉所有活动。"
)
USAGE_FILTER = (
    "用法:\n"
    "/活动过滤 黑名单 <关键词...>  排除命中关键词的活动\n"
    "/活动过滤 白名单 <关键词...>  只推送命中关键词的活动\n"
    "/活动过滤 清空                清空全部关键词"
)

ACK_ALL_TOKENS = {"全部", "所有", "全选", "all", "全部活动"}
BLACKLIST_TOKENS = {"黑名单", "blacklist", "black"}
WHITELIST_TOKENS = {"白名单", "whitelist", "white"}
RESET_TOKENS = {"默认", "default", "重置", "reset"}
CLEAR_TOKENS = {"清空", "clear", "none"}
ON_TOKENS = {"开启", "开", "on", "true", "1"}
OFF_TOKENS = {"关闭", "关", "off", "false", "0"}

COVER_IMAGE_TOKENS = {"图片", "图", "image", "pic"}
COVER_LINK_TOKENS = {"链接", "link", "文字", "url"}
COVER_MODE_LABELS = {
    COVER_MODE_IMAGE: "直接发送图片",
    COVER_MODE_LINK: "只发图片链接",
    COVER_MODE_OFF: "不附带封面",
}

USAGE_COVER = (
    "用法: /活动封面 <图片|链接|关闭|默认>\n"
    "图片 = 直接发送活动封面（默认）；链接 = 只发封面链接文本；\n"
    "关闭 = 完全不附带封面；默认 = 跟随插件全局配置。"
)

# 分隔符过长会撑爆消息，这里做一个上限保护
MAX_SEPARATOR_LENGTH = 40


class GameActivityPlugin(Star):
    """游戏活动日历插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.interval_minutes = max(1, self._cfg_int("basic", "poll_interval_minutes", 30))
        self.refresh_on_start = self._cfg_bool("basic", "refresh_on_start", True)
        self.request_timeout = max(
            5, self._cfg_int("basic", "request_timeout_seconds", 20)
        )
        self.proxy = str(self._cfg_str("basic", "proxy", "") or "").strip()
        # 配了代理时，国内站点默认直连（见 sources.base.DEFAULT_DIRECT_HOSTS）；
        # 清空这一项表示所有请求都走代理。
        self.direct_hosts = normalize_direct_hosts(
            self._cfg("basic", "direct_hosts", ", ".join(DEFAULT_DIRECT_HOSTS))
        )
        self.cache_dir = self._resolve_cache_dir(
            self._cfg_str("basic", "cache_dir", "")
        )

        self.enable_sra = self._cfg_bool("sources", "enable_sra", True)
        self.sra_api_base = self._cfg_str(
            "sources",
            "sra_api_base",
            "https://starrailassistant.top/api/v1/activity",
        )
        self.enable_akedata = self._cfg_bool("sources", "enable_akedata", True)
        self.akedata_api_base = self._cfg_str(
            "sources", "akedata_api_base", "https://data.akedata.wiki"
        )
        self.akedata_include_pools = self._cfg_bool(
            "sources", "akedata_include_gacha_pools", True
        )
        self.enable_prts = self._cfg_bool("sources", "enable_prts", True)
        self.prts_api_base = self._cfg_str(
            "sources", "prts_api_base", "https://prts.wiki/api.php"
        )
        self.prts_min_interval = max(
            3,
            self._cfg_int("sources", "prts_min_request_interval_seconds", 20),
        )
        self.prts_past_days = max(0, self._cfg_int("sources", "prts_past_days", 7))
        self.game_source_prefs = self._resolve_source_prefs()

        self.default_policy = ReminderPolicy(
            thresholds=normalize_thresholds(
                self._cfg_str("reminder", "default_thresholds", "7,3,1")
            ),
            notify_new=self._cfg_bool("reminder", "notify_new_activity", True),
            lookahead_days=max(
                0, self._cfg_int("reminder", "new_activity_lookahead_days", 45)
            ),
            remind_only_active=self._cfg_bool(
                "reminder", "remind_only_active", True
            ),
        )
        self.message_settings = self._build_message_settings()

        # 紧急私聊提醒：进档后按间隔重复私聊，直到对方确认知晓
        self.urgent_dm = UrgentDmSettings(
            enabled=self._cfg_bool("urgent_dm", "enable", False),
            targets=normalize_targets(self._cfg("urgent_dm", "targets", "")),
            trigger_days=max(1, self._cfg_int("urgent_dm", "trigger_days", 1)),
            repeat_minutes=max(
                5, self._cfg_int("urgent_dm", "repeat_minutes", 120)
            ),
            daily_limit=max(0, self._cfg_int("urgent_dm", "daily_limit", 3)),
            quiet_hours=parse_quiet_hours(
                self._cfg_str("urgent_dm", "quiet_hours", "23-8")
            ),
            ack_keywords=normalize_ack_keywords(
                self._cfg("urgent_dm", "ack_keywords", "")
            ),
            games=normalize_games(self._cfg("urgent_dm", "games", "")),
        )

        self.http_client = JsonHttpClient(
            proxy=self.proxy or None,
            timeout=float(self.request_timeout),
            cache_dir=self.cache_dir,
            direct_hosts=self.direct_hosts,
        )
        self.registry = GameRegistry(
            self._build_sources(), preferred=self.game_source_prefs
        )
        if not self.registry.games:
            logger.warning(
                "所有数据源均已关闭，插件不会提供任何游戏。"
                "请在插件配置中启用至少一个数据源。"
            )

        self.polling_settings = PollingSettings(
            interval_minutes=self.interval_minutes,
            lookahead_days=self.default_policy.lookahead_days,
            keep_past_days=3,
            max_new_per_cycle=max(
                0, self._cfg_int("reminder", "max_new_per_cycle", 8)
            ),
            notify_on_subscribe=self._cfg_bool(
                "reminder", "notify_on_subscribe", True
            ),
            retention_days=max(
                1, self._cfg_int("reminder", "state_retention_days", 30)
            ),
            urgent_dm=self.urgent_dm,
        )

        self.subscriptions = SubscriptionService(
            self._get_kv_data, self._put_kv_data
        )
        self.cover_resolver = CoverResolver(
            compress=self.message_settings.cover_compress,
            max_width=self.message_settings.cover_max_width,
            timeout=float(self.request_timeout),
            proxy=self.proxy or None,
            direct_hosts=self.direct_hosts,
        )
        self.delivery = ActivityDeliveryService(
            context, self.message_settings, self.cover_resolver
        )
        self.polling = ActivityPollingService(
            self.registry,
            self.subscriptions,
            self.delivery,
            self.polling_settings,
            self.default_policy,
            self.message_settings,
        )

        self._webui_controller = None
        register_web_api = getattr(context, "register_web_api", None)
        if ActivityWebUIController is not None and callable(register_web_api):
            try:
                self._webui_controller = ActivityWebUIController(self, context)
            except Exception as exc:  # pragma: no cover
                logger.warning(f"游戏活动管理 WebUI 注册失败: {exc}")
        else:
            logger.info("当前 AstrBot 版本不支持 Plugin Pages，跳过后台管理页")

    def _build_message_settings(self) -> MessageSettings:
        """按当前配置构造消息格式设置；保存模板后也走这里重建。"""
        return MessageSettings(
        include_description=self._cfg_bool(
            "message_format", "include_description", False
        ),
        cover_mode=normalize_cover_mode(
            self._cfg("message_format", "cover_mode", DEFAULT_COVER_MODE)
        ),
        include_tags=self._cfg_bool("message_format", "include_tags", True),
        include_extra_times=self._cfg_bool(
            "message_format", "include_extra_times", True
        ),
        single_message_per_game=self._cfg_bool(
            "message_format", "single_message_per_game", True
        ),
        use_forward_node=self._cfg_bool(
            "message_format", "use_forward_node", False
        ),
        cover_compress=self._cfg_bool(
            "message_format", "cover_compress", True
        ),
        cover_max_width=max(
            0,
            self._cfg_int(
                "message_format", "cover_max_width", DEFAULT_COVER_MAX_WIDTH
            ),
        ),
        cover_max_total_mb=self._cfg_int(
            "message_format",
            "cover_max_total_mb",
            DEFAULT_COVER_MAX_TOTAL_MB,
        ),
        activity_separator=self._cfg_str(
            "message_format",
            "activity_separator",
            DEFAULT_ACTIVITY_SEPARATOR,
        ).strip("\r\n")[:MAX_SEPARATOR_LENGTH],
        urgency_marker=self._cfg_bool(
            "message_format", "urgency_marker", True
        ),
        urgency_rules=parse_urgency_rules(
            self._cfg_str(
                "message_format", "urgency_rules", DEFAULT_URGENCY_RULES
            )
        ),
        headline_remaining=self._cfg_bool(
            "message_format", "headline_remaining", True
        ),
        tier_summary=self._cfg_bool("message_format", "tier_summary", True),
        sort_by_urgency=self._cfg_bool(
            "message_format", "sort_by_urgency", True
        ),
        game_aliases=normalize_game_aliases(
            self._cfg("message_format", "game_aliases", "")
        ),
        image_mode_detail_link=self._cfg_bool(
            "message_format", "image_mode_detail_link", False
        ),
        use_short_names=self._cfg_bool(
            "message_format", "use_short_names", True
        ),
        game_short_names=(
            normalize_game_short_names(
                self._cfg("message_format", "game_short_names", "")
            )
            or DEFAULT_GAME_SHORT_NAMES
        ),
        forward_node_name=(
            str(
                self._cfg(
                    "message_format",
                    "forward_node_name",
                    DEFAULT_FORWARD_NODE_NAME,
                )
            ).strip()
            or DEFAULT_FORWARD_NODE_NAME
        ),
        )

    def reload_message_settings(self) -> None:
        """配置被改后重建设置，并同步给已经持有旧对象的调用方。"""
        self.message_settings = self._build_message_settings()
        self.delivery.settings = self.message_settings
        self.polling.message_settings = self.message_settings
        self.cover_resolver.compress = self.message_settings.cover_compress
        self.cover_resolver.max_width = self.message_settings.cover_max_width

    def message_format_values(self) -> dict[str, Any]:
        """把当前消息格式设置还原成页面可编辑的原始值。"""
        settings = self.message_settings
        return {
            "use_forward_node": settings.use_forward_node,
            "forward_node_name": settings.forward_node_name,
            "use_short_names": settings.use_short_names,
            "game_short_names": format_game_pairs(settings.game_short_names),
            "game_aliases": format_game_pairs(settings.game_aliases),
            "urgency_marker": settings.urgency_marker,
            "urgency_rules": format_urgency_rules(settings.urgency_rules),
            "tier_summary": settings.tier_summary,
            "headline_remaining": settings.headline_remaining,
            "sort_by_urgency": settings.sort_by_urgency,
            "activity_separator": settings.activity_separator,
            "include_description": settings.include_description,
            "include_tags": settings.include_tags,
            "include_extra_times": settings.include_extra_times,
            "cover_mode": settings.cover_mode,
            "image_mode_detail_link": settings.image_mode_detail_link,
        }

    # ---- 配置读取辅助 ----

    def _cfg(self, block: str, key: str, default: Any) -> Any:
        block_config = self.config.get(block)
        if isinstance(block_config, dict):
            value = block_config.get(key)
            if value is not None:
                return value
        return default

    def _cfg_int(self, block: str, key: str, default: int) -> int:
        try:
            return int(self._cfg(block, key, default))
        except (TypeError, ValueError):
            logger.warning(f"配置 {block}.{key} 不是整数，已回退为 {default}")
            return default

    def _cfg_bool(self, block: str, key: str, default: bool) -> bool:
        value = self._cfg(block, key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ON_TOKENS

    def _cfg_str(self, block: str, key: str, default: str) -> str:
        value = self._cfg(block, key, default)
        return default if value is None else str(value)

    def _resolve_cache_dir(self, configured: str) -> Path | None:
        text = str(configured or "").strip()
        if text:
            try:
                path = Path(text).expanduser()
                path.mkdir(parents=True, exist_ok=True)
                return path
            except OSError as exc:
                logger.warning(f"缓存目录不可用（{text}）：{exc}，回退为插件数据目录")

        try:
            data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
            data_dir.mkdir(parents=True, exist_ok=True)
            return data_dir
        except Exception as exc:  # pragma: no cover
            logger.warning(f"无法获取插件数据目录，缓存已禁用: {exc}")
            return None

    # 同一款游戏有多个来源时，可选的来源（第一项是默认值）
    SOURCE_CHOICES: dict[str, tuple[str, ...]] = {
        "arknights": ("prts", "sra"),
        "endfield": ("akedata", "sra"),
    }

    # 数据源的人类可读名称（管理页与日志共用）
    SOURCE_LABELS: dict[str, str] = {
        "sra": "SRA 公共 API",
        "akedata": "AKEData（终末地配置表）",
        "prts": "PRTS（明日方舟 Wiki）",
    }

    def _resolve_source_prefs(self) -> dict[str, str]:
        """读取「同一游戏用哪个数据源」的偏好；非法值回退到默认来源。"""
        prefs: dict[str, str] = {}
        for game_id, choices in self.SOURCE_CHOICES.items():
            default = choices[0]
            raw = self._cfg_str("sources", f"{game_id}_source", default).strip().lower()
            prefs[game_id] = raw if raw in choices else default
        return prefs

    def _build_sources(self) -> list[Any]:
        sources: list[Any] = []
        if self.enable_sra:
            sources.append(
                SraSource(self.http_client, api_base=self.sra_api_base)
            )
        if self.enable_akedata:
            sources.append(
                AkedataSource(
                    self.http_client,
                    api_base=self.akedata_api_base,
                    include_gacha_pools=self.akedata_include_pools,
                    cache_dir=self.cache_dir,
                )
            )
        if self.enable_prts:
            sources.append(
                PrtsSource(
                    self.http_client,
                    api_base=self.prts_api_base,
                    min_request_interval_seconds=self.prts_min_interval,
                    past_days=self.prts_past_days,
                )
            )
        return sources

    # ---- 数据源与网络设置（管理页可编辑） ----

    def _source_enabled_flags(self) -> dict[str, bool]:
        return {
            "sra": self.enable_sra,
            "akedata": self.enable_akedata,
            "prts": self.enable_prts,
        }

    def _reload_source_config(self) -> None:
        """从 config 重读数据源与网络字段（不重建任何客户端）。"""
        self.proxy = str(self._cfg_str("basic", "proxy", "") or "").strip()
        self.direct_hosts = normalize_direct_hosts(
            self._cfg("basic", "direct_hosts", ", ".join(DEFAULT_DIRECT_HOSTS))
        )
        self.enable_sra = self._cfg_bool("sources", "enable_sra", True)
        self.enable_akedata = self._cfg_bool("sources", "enable_akedata", True)
        self.enable_prts = self._cfg_bool("sources", "enable_prts", True)
        self.game_source_prefs = self._resolve_source_prefs()

    def _rebuild_registry(self) -> None:
        """按当前开关重建注册表，并让轮询服务改用它。"""
        self.registry = GameRegistry(
            self._build_sources(), preferred=self.game_source_prefs
        )
        self.polling.registry = self.registry

    async def reload_source_settings(self) -> None:
        """热应用数据源 / 代理改动，无需重启 AstrBot。"""
        self._reload_source_config()
        await self.http_client.close()
        self.http_client = JsonHttpClient(
            proxy=self.proxy or None,
            timeout=float(self.request_timeout),
            cache_dir=self.cache_dir,
            direct_hosts=self.direct_hosts,
        )
        self._rebuild_registry()
        self.polling.invalidate_memo()
        self.cover_resolver.proxy = self.proxy or None
        self.cover_resolver.direct_hosts = self.direct_hosts
        await self.cover_resolver.close()
        self.cover_resolver.clear_cache()
        enabled = [
            label
            for source_id, label in self.SOURCE_LABELS.items()
            if self._source_enabled_flags()[source_id]
        ]
        logger.info(
            "数据源设置已更新：启用 "
            f"{' / '.join(enabled) or '（无）'}；代理 "
            f"{self.proxy or '未设置'}；直连 "
            f"{', '.join(self.direct_hosts) or '（无）'}"
        )

    def _current_source_id(self, game_id: str) -> str:
        source = self.registry.source_for(game_id)
        return source.source_id if source else ""

    def source_settings_values(self) -> dict[str, Any]:
        """给管理页用的数据源 / 网络设置快照。"""
        enabled = self._source_enabled_flags()
        sources = [
            {
                "id": source_id,
                "name": self.SOURCE_LABELS[source_id],
                "enabled": enabled[source_id],
            }
            for source_id in ("sra", "akedata", "prts")
        ]
        game_sources: list[dict[str, Any]] = []
        for game_id, choices in self.SOURCE_CHOICES.items():
            game = self.registry.get(game_id)
            game_sources.append(
                {
                    "game_id": game_id,
                    "name": game.name if game else game_id,
                    "current": self.registry.source_name(game_id),
                    "source_id": self._current_source_id(game_id),
                    "options": [
                        {
                            "id": choice,
                            "name": self.SOURCE_LABELS[choice],
                            "enabled": enabled[choice],
                        }
                        for choice in choices
                    ],
                }
            )
        return {
            "sources": sources,
            "game_sources": game_sources,
            "network": {
                "proxy": self.proxy,
                "direct_hosts": ", ".join(self.direct_hosts),
                "default_direct_hosts": ", ".join(DEFAULT_DIRECT_HOSTS),
            },
        }

    # ---- KV 委托 ----

    async def _get_kv_data(self, key: str, default: Any) -> Any:
        return await self.get_kv_data(key, default)

    async def _put_kv_data(self, key: str, value: Any) -> None:
        await self.put_kv_data(key, value)

    # ---- 生命周期 ----

    async def initialize(self):
        logger.info(
            f"游戏活动日历插件初始化中，已启用 {len(self.registry.games)} 款游戏"
        )
        await self.polling.start(immediate=self.refresh_on_start)
        logger.info("游戏活动日历插件初始化完成")

    async def terminate(self):
        await self.polling.stop()
        await self.http_client.close()
        await self.cover_resolver.close()
        logger.info("游戏活动日历插件已停止")

    # ---- 通用辅助 ----

    @staticmethod
    def _args(event: AstrMessageEvent) -> list[str]:
        """指令名之后的参数列表（不含指令本身）。"""
        return event.message_str.strip().split()[1:]

    def _session_games(
        self,
        session: dict[str, Any] | None,
        tokens: Sequence[str],
    ) -> tuple[list[Any], list[str]]:
        """解析目标游戏；未给出参数时使用当前会话已订阅的游戏。"""
        if tokens:
            return self.registry.resolve_many(tokens)

        games = []
        for game_id in (session or {}).get("games") or []:
            game = self.registry.get(game_id)
            if game is not None:
                games.append(game)
        return games, []

    async def _patch_session(
        self,
        umo: str,
        patch: dict[str, Any],
        current: dict[str, Any],
    ) -> dict[str, Any]:
        """更新会话设置并返回最新会话；会话已不存在时沿用旧值。"""
        return await self.subscriptions.update_session(umo, patch) or current

    @staticmethod
    def _format_policy(policy: ReminderPolicy) -> str:
        if policy.thresholds:
            threshold_text = "、".join(f"{days}天" for days in policy.thresholds)
        else:
            threshold_text = "已关闭"
        new_text = "开启" if policy.notify_new else "关闭"
        return f"倒计时提醒: {threshold_text}\n新活动提醒: {new_text}"

    @staticmethod
    def _format_filters(session: dict[str, Any]) -> str:
        whitelist = session.get("whitelist") or []
        blacklist = session.get("blacklist") or []
        white_text = "、".join(whitelist) if whitelist else "（未设置）"
        black_text = "、".join(blacklist) if blacklist else "（未设置）"
        return f"白名单: {white_text}\n黑名单: {black_text}"

    def _format_cover_mode(self, session: dict[str, Any]) -> str:
        effective = resolve_message_settings(
            session, self.message_settings
        ).cover_mode
        label = COVER_MODE_LABELS.get(effective, effective)
        origin = "本会话设置" if session.get("cover_mode") else "全局默认"
        return f"封面形式: {label}（{origin}）"

    def _render_session_summary(
        self,
        session: dict[str, Any],
        *,
        title: str = "当前活动订阅",
    ) -> str:
        names = []
        for game_id in session.get("games") or []:
            game = self.registry.get(game_id)
            names.append(game.name if game else game_id)
        games_text = "、".join(names) if names else "（无）"

        policy = resolve_policy(session, self.default_policy)
        status = "开启" if session.get("enabled") else "关闭"
        return (
            f"📋 {title}\n"
            f"推送状态: {status}\n"
            f"订阅游戏: {games_text}\n"
            f"{self._format_policy(policy)}\n"
            f"{self._format_cover_mode(session)}\n"
            f"{self._format_filters(session)}"
        )

    # ---- 指令 ----

    @filter.command("活动订阅", alias={"game_sub", "活动关注"})
    async def subscribe_games(self, event: AstrMessageEvent):
        """订阅游戏活动，格式: /活动订阅 <游戏名> [游戏名 ...]。"""
        tokens = self._args(event)
        if not tokens:
            yield event.plain_result(USAGE_SUBSCRIBE)
            return

        games, unknown = self.registry.resolve_many(tokens)
        if not games:
            yield event.plain_result(
                f"没有识别到游戏: {'、'.join(unknown)}\n{USAGE_SUBSCRIBE}"
            )
            return

        umo = event.unified_msg_origin
        await self.subscriptions.add_games(
            umo, [game.game_id for game in games]
        )
        names = "、".join(game.name for game in games)
        message = f"✅ 已订阅: {names}"
        if unknown:
            message += f"\n⚠️ 未识别: {'、'.join(unknown)}"
        yield event.plain_result(message)

        await self.polling.send_subscription_overview(umo, games)

    @filter.command("活动退订", alias={"game_unsub", "活动取消订阅"})
    async def unsubscribe_games(self, event: AstrMessageEvent):
        """退订游戏活动，格式: /活动退订 <游戏名...> 或 /活动退订 全部。"""
        tokens = self._args(event)
        umo = event.unified_msg_origin
        session = await self.subscriptions.get_session(umo)
        if not session:
            yield event.plain_result("当前会话没有订阅任何游戏")
            return

        # 清空是破坏性操作，必须显式写出「全部」，避免裸指令误删订阅
        if any(token in ("全部", "all") for token in tokens):
            await self.subscriptions.remove_session(umo)
            notify_state = await self.subscriptions.get_notify_state()
            self.subscriptions.drop_session_state(notify_state, umo)
            await self.subscriptions.save_notify_state(notify_state)
            yield event.plain_result("✅ 已清空当前会话的全部活动订阅")
            return

        if not tokens:
            yield event.plain_result(
                "用法: /活动退订 <游戏名...>\n"
                "如需清空全部订阅请使用 /活动退订 全部"
            )
            return

        games, unknown = self.registry.resolve_many(tokens)
        if not games:
            yield event.plain_result(
                f"没有识别到游戏: {'、'.join(unknown)}\n"
                "用法: /活动退订 <游戏名...> 或 /活动退订 全部"
            )
            return

        result = await self.subscriptions.remove_games(
            umo, [game.game_id for game in games]
        )
        names = "、".join(game.name for game in games)
        message = f"✅ 已退订: {names}"
        if unknown:
            message += f"\n⚠️ 未识别: {'、'.join(unknown)}"
        if result is None:
            message += "\n当前会话已没有订阅的游戏，订阅记录已清除。"
        yield event.plain_result(message)

    @filter.command("活动列表", alias={"game_list", "活动订阅列表"})
    async def list_subscriptions(self, event: AstrMessageEvent):
        """查看当前会话的活动订阅与设置。"""
        session = await self.subscriptions.get_session(
            event.unified_msg_origin
        )
        if not session:
            yield event.plain_result(
                NO_SUBSCRIBE_TEXT
            )
            return
        yield event.plain_result(self._render_session_summary(session))

    @filter.command("活动查询", alias={"game_query"})
    async def query_activities(self, event: AstrMessageEvent):
        """查看当前活动，格式: /活动查询 [游戏名...]。"""
        umo = event.unified_msg_origin
        session = await self.subscriptions.get_session(umo)
        tokens = self._args(event)
        games, unknown = self._session_games(session, tokens)

        if not games:
            if unknown:
                yield event.plain_result(
                    f"没有识别到游戏: {'、'.join(unknown)}\n{USAGE_SUBSCRIBE}"
                )
            else:
                yield event.plain_result(
                    NO_SUBSCRIBE_TEXT
                )
            return

        now = datetime.now(GAME_TIMEZONE)
        settings = resolve_message_settings(session or {}, self.message_settings)
        for game in games:
            try:
                activities = await self.polling.fetch_activities(game.game_id)
            except Exception as exc:
                logger.warning(f"查询 {game.name} 活动失败: {exc}")
                yield event.plain_result(
                    f"⚠️ 暂时无法获取 {game.name} 的活动数据: {exc}"
                )
                continue
            source_name = self.registry.source_name(game.game_id)
            if settings.use_forward_node:
                forward = await self.delivery.build_overview_forward_chain(
                    game, activities, now, settings, source_name=source_name
                )
                if forward is not None:
                    yield event.chain_result(forward)
                    continue
            parts = build_overview_parts(
                game,
                activities,
                now,
                settings,
                source_name=source_name,
            )
            yield event.chain_result(
                await self.delivery.build_event_chain(
                    parts,
                    replace(settings, use_forward_node=False),
                    node_name=short_game_name(game, settings),
                    forward_title=(
                        f"🎮 {display_game_name(game, settings)} · 当前活动"
                    ),
                )
            )

    @filter.command("活动过滤", alias={"game_filter", "活动关键词"})
    async def filter_keywords(self, event: AstrMessageEvent):
        """设置活动关键词过滤，格式: /活动过滤 [黑名单|白名单] <关键词...>。"""
        umo = event.unified_msg_origin
        session = await self.subscriptions.get_session(umo)
        if not session:
            yield event.plain_result(
                NO_SUBSCRIBE_TEXT
            )
            return

        tokens = self._args(event)
        if not tokens:
            yield event.plain_result(
                self._format_filters(session) + "\n\n" + USAGE_FILTER
            )
            return

        action = tokens[0]
        if action in CLEAR_TOKENS:
            session = await self._patch_session(
                umo, {"whitelist": [], "blacklist": []}, session
            )
            yield event.plain_result(
                "✅ 已清空关键词过滤\n" + self._format_filters(session)
            )
            return

        if action in BLACKLIST_TOKENS:
            field = "blacklist"
        elif action in WHITELIST_TOKENS:
            field = "whitelist"
        else:
            yield event.plain_result(USAGE_FILTER)
            return

        keywords = tokens[1:]
        if not keywords:
            yield event.plain_result(
                f"请提供关键词，例如: /活动过滤 {action} 双倍掉落\n\n{USAGE_FILTER}"
            )
            return

        session = await self._patch_session(umo, {field: keywords}, session)
        label = "黑名单" if field == "blacklist" else "白名单"
        yield event.plain_result(
            f"✅ 已更新{label}: {'、'.join(keywords)}\n"
            + self._format_filters(session)
        )

    @filter.command("活动封面", alias={"game_cover", "活动图片"})
    async def set_cover_mode(self, event: AstrMessageEvent):
        """设置本会话的封面形式: /活动封面 图片|链接|关闭|默认。"""
        umo = event.unified_msg_origin
        session = await self.subscriptions.get_session(umo)
        if not session:
            yield event.plain_result(
                NO_SUBSCRIBE_TEXT
            )
            return

        tokens = self._args(event)
        if not tokens:
            yield event.plain_result(self._format_cover_mode(session) + "\n\n" + USAGE_COVER)
            return

        token = tokens[0].lower()
        if token in RESET_TOKENS:
            mode: str | None = None
        elif token in COVER_IMAGE_TOKENS:
            mode = COVER_MODE_IMAGE
        elif token in COVER_LINK_TOKENS:
            mode = COVER_MODE_LINK
        elif token in OFF_TOKENS:
            mode = COVER_MODE_OFF
        else:
            yield event.plain_result(USAGE_COVER)
            return

        session = await self._patch_session(umo, {"cover_mode": mode}, session)
        yield event.plain_result("✅ 已更新\n" + self._format_cover_mode(session))

    @filter.command("活动开关", alias={"game_toggle", "活动推送"})
    async def toggle_push(self, event: AstrMessageEvent):
        """开启或关闭当前会话的活动推送: /活动开关 开启|关闭。"""
        tokens = self._args(event)
        if not tokens:
            yield event.plain_result("用法: /活动开关 开启 或 /活动开关 关闭")
            return

        action = tokens[0].lower()
        if action in ON_TOKENS:
            enabled = True
        elif action in OFF_TOKENS:
            enabled = False
        else:
            yield event.plain_result("用法: /活动开关 开启 或 /活动开关 关闭")
            return

        session = await self.subscriptions.update_session(
            event.unified_msg_origin, {"enabled": enabled}
        )
        if session is None:
            yield event.plain_result(
                NO_SUBSCRIBE_TEXT
            )
            return
        yield event.plain_result(
            f"✅ 活动推送已{'开启' if enabled else '关闭'}"
        )

    @filter.command("活动阈值", alias={"game_threshold", "活动提醒时间"})
    async def set_thresholds(self, event: AstrMessageEvent):
        """设置倒计时提醒档位: /活动阈值 7 3 1 / 关闭 / 默认。"""
        umo = event.unified_msg_origin
        session = await self.subscriptions.get_session(umo)
        if not session:
            yield event.plain_result(
                NO_SUBSCRIBE_TEXT
            )
            return

        tokens = self._args(event)
        if not tokens:
            policy = resolve_policy(session, self.default_policy)
            yield event.plain_result(
                self._format_policy(policy) + "\n\n" + USAGE_THRESHOLD
            )
            return

        if tokens[0] in RESET_TOKENS:
            session = await self._patch_session(umo, {"thresholds": None}, session)
            policy = resolve_policy(session, self.default_policy)
            yield event.plain_result(
                "✅ 已恢复全局默认档位\n" + self._format_policy(policy)
            )
            return

        if tokens[0] in CLEAR_TOKENS or tokens[0] in OFF_TOKENS:
            session = await self._patch_session(umo, {"thresholds": []}, session)
            yield event.plain_result(
                "✅ 已关闭倒计时提醒\n" + self._format_policy(resolve_policy(session, self.default_policy))
            )
            return

        thresholds = normalize_thresholds(tokens)
        if not thresholds:
            yield event.plain_result(USAGE_THRESHOLD)
            return

        session = await self._patch_session(
            umo, {"thresholds": list(thresholds)}, session
        )
        yield event.plain_result(
            "✅ 已更新提醒档位\n"
            + self._format_policy(resolve_policy(session, self.default_policy))
        )

    @filter.command("活动游戏", alias={"game_games", "活动支持"})
    async def list_games(self, event: AstrMessageEvent):
        """列出支持的游戏。"""
        games = self.registry.ordered()
        if not games:
            yield event.plain_result("当前没有启用任何数据源，请在插件配置中开启")
            return

        grouped: dict[str, list[Any]] = {}
        for game in games:
            grouped.setdefault(game.source_id, []).append(game)

        lines = [f"📋 支持的游戏（{len(games)}）", ""]
        for items in grouped.values():
            source_name = self.registry.source_name(items[0].game_id)
            lines.append(f"▎{source_name}")
            for game in items:
                shown = display_game_name(game, self.message_settings)
                aliases = "/".join(str(alias) for alias in game.aliases[:5])
                suffix = f"，别名：{aliases}" if aliases else ""
                options = self.registry.source_options(game.game_id)
                if options:
                    choices = "/".join(source.source_id for source in options)
                    suffix += f"，可切换：{choices}"
                lines.append(f"• {shown}（{game.game_id}{suffix}）")
            lines.append("")
        lines.append("用法: /活动订阅 <游戏名或代号>")
        lines.append("想改成自己的叫法：插件配置 → 消息格式 → 自定义游戏显示名称")
        yield event.plain_result("\n".join(lines).rstrip())

    @filter.command("活动私人提醒", alias={"game_dm", "活动私聊提醒"})
    async def urgent_dm_command(self, event: AstrMessageEvent):
        """管理紧急私聊提醒目标: /活动私人提醒 @某人 | 移除 @某人 | 列表 | 停止 | 开启 | 测试。"""
        umo = event.unified_msg_origin
        self_umo = self._self_private_umo(event)
        tokens = self._args(event)
        action = tokens[0].lower() if tokens else "列表"
        is_admin = self._event_is_admin(event)

        if action in ("列表", "list", "查看"):
            data = await self.subscriptions.get_urgent_dm()
            lines: list[str] = []
            for target, entry in data.items():
                if not entry.get("enabled"):
                    continue
                if not is_admin and target != self_umo:
                    continue
                games = "、".join(entry.get("games") or []) or "跟随订阅"
                pace = describe_target_settings(
                    target_settings(self.urgent_dm, entry)
                )
                pending = [
                    str(state.get("name") or uid)
                    for uid, state in (entry.get("activities") or {}).items()
                    if not state.get("acked")
                ]
                detail = f"，正在提醒：{'、'.join(pending)}" if pending else ""
                lines.append(f"· {target}（{games}）{pace}{detail}")
            if not lines:
                yield event.plain_result(
                    "当前没有紧急私聊提醒目标。\n\n" + USAGE_URGENT_DM
                )
                return
            yield event.plain_result("紧急私聊提醒目标:\n" + "\n".join(lines))
            return

        if action in ("停止", "暂停", "退订", "stop", "pause"):
            # 「停止」只暂停，保留节奏设置与活动记录；真要删掉用「移除」
            data = await self.subscriptions.get_urgent_dm()
            if not isinstance(data.get(self_umo), dict):
                yield event.plain_result("当前没有对你生效的紧急提醒。")
                return
            SubscriptionService.upsert_urgent_target(data, self_umo, enabled=False)
            await self.subscriptions.save_urgent_dm(data)
            yield event.plain_result(
                "✅ 已暂停对你的紧急私聊提醒，节奏设置与活动记录都保留着。\n"
                "想重新开启：/活动私人提醒 开启；要彻底删掉：/活动私人提醒 移除"
            )
            return

        if action in ("开启", "恢复", "start", "resume"):
            data = await self.subscriptions.get_urgent_dm()
            if not isinstance(data.get(self_umo), dict):
                yield event.plain_result(
                    "你还不在紧急私聊提醒名单里。\n发送 /活动私人提醒 即可加入。"
                )
                return
            SubscriptionService.upsert_urgent_target(data, self_umo, enabled=True)
            await self.subscriptions.save_urgent_dm(data)
            yield event.plain_result("✅ 已重新开启对你的紧急私聊提醒。")
            return

        if action in ("测试", "test"):
            # 测试消息必须落在执行者自己的私聊：群里执行也要私聊收到
            ok = await self.delivery.send_text(
                self_umo, "🔔 这是一条测试提醒：主动私聊可用。"
            )
            yield event.plain_result(
                f"✅ 测试消息已发到你的私聊（{self_umo}），去私聊看看。"
                if ok
                else "❌ 测试消息发送失败，请确认适配器支持主动私聊。"
            )
            return

        targets = self._mention_targets(event, umo)
        if not targets and "FriendMessage" in umo:
            targets = [umo]

        if action in ("设置", "set", "配置"):
            rest = tokens[1:]
            pasted = [item for item in rest if self._is_umo_token(item)]
            for item in pasted:
                if item not in targets:
                    targets.append(item)
            if not targets:
                yield event.plain_result(
                    "请 @ 要设置的人，或者直接贴 平台:FriendMessage:QQ号。\n\n"
                    + USAGE_URGENT_DM
                )
                return
            if not is_admin and any(target != self_umo for target in targets):
                yield event.plain_result("只有管理员可以给别人设置提醒节奏。")
                return

            parsed = parse_target_settings(
                [item for item in rest if not self._is_umo_token(item)]
            )
            data = await self.subscriptions.get_urgent_dm()
            for target in targets:
                SubscriptionService.upsert_urgent_target(data, target, enabled=True)
                patch = (
                    dict.fromkeys(TARGET_SETTING_KEYS) if parsed is None else parsed
                )
                SubscriptionService.update_urgent_settings(data, target, patch)
            await self.subscriptions.save_urgent_dm(data)

            lines = [
                f"· {target}｜"
                f"{describe_target_settings(target_settings(self.urgent_dm, data.get(target)))}"
                for target in targets
            ]
            yield event.plain_result("✅ 提醒节奏已更新:\n" + "\n".join(lines))
            return

        if action in ("移除", "删除", "remove", "del"):
            if not targets:
                yield event.plain_result("请 @ 要移除的人。\n\n" + USAGE_URGENT_DM)
                return
            if not is_admin and any(target != umo for target in targets):
                yield event.plain_result("只有管理员可以移除别人。")
                return
            data = await self.subscriptions.get_urgent_dm()
            removed = [
                target
                for target in targets
                if SubscriptionService.remove_urgent_target(data, target)
            ]
            if removed:
                await self.subscriptions.save_urgent_dm(data)
            yield event.plain_result(
                f"✅ 已移出紧急提醒：{'、'.join(removed)}"
                if removed
                else "没有找到这些目标。"
            )
            return

        if not targets:
            yield event.plain_result(USAGE_URGENT_DM)
            return
        if not is_admin and any(target != umo for target in targets):
            yield event.plain_result("只有管理员可以把别人加入紧急提醒。")
            return

        data = await self.subscriptions.get_urgent_dm()
        for target in targets:
            SubscriptionService.upsert_urgent_target(data, target, enabled=True)
        await self.subscriptions.save_urgent_dm(data)
        yield event.plain_result(
            "✅ 已加入紧急私聊提醒：\n"
            + "\n".join(f"· {target}" for target in targets)
            + f"\n\n{USAGE_URGENT_DM}"
        )

    @filter.command("我已知晓", alias={"活动已知晓", "game_ack", "已知晓"})
    async def ack_urgent_dm(self, event: AstrMessageEvent):
        """确认已知晓: /我已知晓 [活动名关键词|全部]，停止对自己的紧急提醒。"""
        tokens = self._args(event)
        acked, ack_all = await self._ack_now(self._self_private_umo(event), tokens)
        if not acked:
            yield event.plain_result(self._ack_no_match_reply(tokens))
            return
        yield event.plain_result(self._ack_reply(acked, ack_all=ack_all))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def ack_by_keyword(self, event: AstrMessageEvent):
        """不带指令前缀的「我已知晓」同样能停止提醒（关键词可在配置里改）。"""
        if not self.urgent_dm.enabled:
            return
        text = event.message_str.strip()
        if not text or not is_ack_text(text, self.urgent_dm.ack_keywords):
            return

        acked, ack_all = await self._ack_now(
            self._self_private_umo(event), self._ack_scope(text)
        )
        if not acked:
            return
        event.stop_event()
        yield event.plain_result(self._ack_reply(acked, ack_all=ack_all))

    async def _ack_now(
        self, umo: str, scope: Sequence[str]
    ) -> tuple[list[str], bool]:
        """确认知晓并落盘，返回 (被确认的活动名, 是否全量确认)。

        不带任何活动名时只确认「最近一轮被催的那批活动」——用户回一句「我已知晓」
        想停的是刚看到的那条提醒，不该把历史上催过的所有活动一起停掉。要全量确认
        得显式写「我已知晓 全部」；写了活动名但一个都没命中时不做任何确认。
        """
        data = await self.subscriptions.get_urgent_dm()
        if not data.get(umo):
            return ([], False)
        now = datetime.now(GAME_TIMEZONE)

        lowered = [token.casefold() for token in scope]
        ack_all = any(token in ACK_ALL_TOKENS for token in lowered)
        if ack_all:
            acked = SubscriptionService.ack_urgent_activities(data, umo, now, ())
        else:
            filters = tuple(
                token for token in scope
                if token.casefold() not in ACK_ALL_TOKENS
            )
            acked = (
                SubscriptionService.ack_urgent_activities(data, umo, now, filters)
                if filters
                else SubscriptionService.ack_latest_batch(data, umo, now)
            )
        if acked:
            await self.subscriptions.save_urgent_dm(data)
        return (acked, ack_all)

    @staticmethod
    def _ack_reply(acked: Sequence[str], *, ack_all: bool = False) -> str:
        names = "、".join(acked[:6])
        extra = "" if len(acked) <= 6 else f" 等 {len(acked)} 个活动"
        if ack_all:
            return f"✅ 已记下「已知晓」，全部这些活动都不再提醒：{names}{extra}"
        return (
            f"✅ 已记下「已知晓」，本轮这些活动不会再催你：{names}{extra}\n"
            "（要一次停掉所有活动：/我已知晓 全部；彻底关掉私人提醒：/活动私人提醒 停止）"
        )

    @staticmethod
    def _ack_no_match_reply(scope: Sequence[str]) -> str:
        if not scope:
            return "当前没有对你生效的紧急提醒。"
        return (
            f"没找到名字里有「{'、'.join(scope)}」的待确认活动。\n"
            "不带活动名回 /我已知晓 可以停掉最近那一轮提醒。"
        )

    def _ack_scope(self, text: str) -> tuple[str, ...]:
        """从确认语句里剥掉关键词，剩下的当作活动名过滤条件。"""
        remaining = text
        for keyword in self.urgent_dm.ack_keywords:
            if keyword:
                remaining = remaining.replace(keyword, " ")
        tokens: list[str] = []
        for chunk in remaining.replace("，", " ").replace(",", " ").split():
            if chunk and chunk not in tokens:
                tokens.append(chunk)
        return tuple(tokens)

    @staticmethod
    def _self_private_umo(event: AstrMessageEvent) -> str:
        """执行者自己的私聊会话。

        紧急提醒的目标一律是「某个人的私聊」，所以在群里执行 /活动私人提醒 测试、
        停止、/我已知晓（或直接说「我已知晓」）时，都必须落到**执行者本人**的私聊上，
        而不是当前群——否则消息会发到群里，也永远匹配不到目标记录。
        """
        umo = event.unified_msg_origin
        platform = umo.split(":", 1)[0] if ":" in umo else ""
        sender = ""
        getter = getattr(event, "get_sender_id", None)
        if callable(getter):
            try:
                sender = str(getter() or "").strip()
            except Exception:  # pragma: no cover - 取决于 AstrBot 版本
                sender = ""
        if platform and sender:
            return f"{platform}:FriendMessage:{sender}"
        return umo

    @staticmethod
    def _event_is_admin(event: AstrMessageEvent) -> bool:
        checker = getattr(event, "is_admin", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:  # pragma: no cover - 取决于 AstrBot 版本
                return False
        return False

    @staticmethod
    def _is_umo_token(token: str) -> bool:
        """判断一个参数是不是 平台:FriendMessage:QQ号 形式的会话号。"""
        parts = str(token or "").split(":")
        if len(parts) < 3 or not all(part.strip() for part in parts[:3]):
            return False
        return not any("\u4e00" <= char <= "\u9fff" for char in str(token))

    @staticmethod
    def _mention_targets(event: AstrMessageEvent, umo: str) -> list[str]:
        """把消息里 @ 到的 QQ 号拼成完整 UMO（主动私聊只能靠它定位）。"""
        platform = umo.split(":", 1)[0] if ":" in umo else ""
        if not platform:
            return []
        message_obj = getattr(event, "message_obj", None)
        chain = getattr(message_obj, "message", None) or []
        targets: list[str] = []
        for component in chain:
            qq = getattr(component, "qq", None)
            if qq is None:
                continue
            text = str(qq).strip()
            target = f"{platform}:FriendMessage:{text}"
            if text and target not in targets:
                targets.append(target)
        return targets

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("活动状态", alias={"game_status"})
    async def show_status(self, event: AstrMessageEvent):
        """查看数据源与轮询状态（管理员）。"""
        report = self.polling.last_report
        sessions = await self.subscriptions.get_all()
        lines = [
            "📊 游戏活动日历状态",
            f"轮询间隔: {self.interval_minutes} 分钟",
            f"订阅会话: {len(sessions)} 个",
            f"已启用游戏: {len(self.registry.games)} 款",
        ]
        if report.finished_at is not None:
            lines.append(
                f"上轮完成: {report.finished_at:%Y-%m-%d %H:%M:%S}"
            )
            lines.append(
                f"上轮结果: 数据源 {report.fetched_games} 个 / "
                f"活动 {report.activities} 条 / 提醒 {report.reminders} 条"
            )
        else:
            lines.append("上轮结果: 尚未执行")

        if report.failures:
            lines.append("")
            lines.append("⚠️ 数据源异常:")
            for game_id, detail in report.failures.items():
                game = self.registry.get(game_id)
                lines.append(f"• {game.name if game else game_id}: {detail}")

        sources = self._build_sources_status()
        if sources:
            lines.append("")
            lines.append("数据源:")
            lines.extend(sources)

        yield event.plain_result("\n".join(lines))

    def _build_sources_status(self) -> list[str]:
        lines: list[str] = []
        for source_id, enabled, note in (
            ("sra", self.enable_sra, self.sra_api_base),
            ("akedata", self.enable_akedata, self.akedata_api_base),
            ("prts", self.enable_prts, self.prts_api_base),
        ):
            mark = "🟢" if enabled else "⚪"
            label = {"sra": "SRA 公共 API", "akedata": "AKEData", "prts": "PRTS"}[
                source_id
            ]
            lines.append(f"{mark} {label}: {note}")

        for game_id in self.SOURCE_CHOICES:
            game = self.registry.get(game_id)
            options = self.registry.source_options(game_id)
            if game is None or not options:
                continue
            names = "、".join(source.display_name for source in options)
            lines.append(
                f"🔀 {game.name}: {self.registry.source_name(game_id)}"
                f"（可选 {names}）"
            )
        return lines

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("活动刷新", alias={"game_refresh"})
    async def refresh_now(self, event: AstrMessageEvent):
        """立即执行一轮活动检查（管理员）。"""
        yield event.plain_result("正在检查活动数据，请稍候…")
        self.polling.invalidate_memo()
        report = await self.polling.run_cycle()
        lines = [
            "✅ 检查完成",
            f"数据源 {report.fetched_games} 个 / 活动 {report.activities} 条 / "
            f"会话 {report.sessions} 个 / 提醒 {report.reminders} 条",
        ]
        if report.failures:
            lines.append("⚠️ 异常:")
            for game_id, detail in report.failures.items():
                lines.append(f"• {game_id}: {detail}")
        yield event.plain_result("\n".join(lines))
