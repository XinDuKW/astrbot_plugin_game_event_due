"""游戏活动日历的 Plugin Pages 后端接口。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from .services.message_formatter import (
    COVER_MODES,
    DEFAULT_FORWARD_NODE_NAME,
    build_overview_message,
    normalize_game_aliases,
    normalize_game_short_names,
    parse_urgency_rules,
)
from .services.template_preview import (
    build_template_preview,
    build_urgent_preview,
    format_game_pairs,
    format_urgency_rules,
)
from .services.reminder_service import normalize_keywords, normalize_thresholds
from .services.subscription_service import (
    SubscriptionService,
    resolve_message_settings,
    resolve_policy,
)
from .services.urgent_dm_service import (
    MAX_DAILY_LIMIT,
    MAX_REPEAT_MINUTES,
    MAX_TRIGGER_DAYS,
    MIN_DAILY_LIMIT,
    MIN_REPEAT_MINUTES,
    MIN_TRIGGER_DAYS,
    SETTING_LIMIT,
    SETTING_REPEAT,
    SETTING_TRIGGER,
    TARGET_SETTING_KEYS,
    clamp_setting,
    describe_target_settings,
    target_settings,
)
from .sources import GAME_TIMEZONE, Activity, Game, normalize_direct_hosts

PLUGIN_NAME = "astrbot_plugin_game_event_due"
MESSAGE_TYPES = {"GroupMessage", "FriendMessage", "OtherMessage"}
GROUP_LIST_TIMEOUT_SECONDS = 6
MAX_UMO_LENGTH = 512


class ActivityWebUIController:
    """注册并实现活动订阅管理页面所需的 Web API。"""

    def __init__(self, plugin: Any, context: Any):
        self.plugin = plugin
        self.context = context
        self._register_routes()

    def _register_routes(self) -> None:
        routes = (
            ("overview", self.overview, ["GET"], "游戏活动订阅概览"),
            ("session/update", self.update_session, ["POST"], "修改活动订阅设置"),
            ("session/remove", self.remove_session, ["POST"], "移除活动订阅"),
            ("preview", self.preview, ["POST"], "预览某个游戏的当前活动"),
            ("template", self.template, ["GET"], "读取消息模板配置"),
            ("template-preview", self.template_preview, ["POST"], "按草稿模板预览排版"),
            ("template-save", self.template_save, ["POST"], "保存消息模板配置"),
            ("refresh", self.refresh, ["POST"], "立即执行一轮活动检查"),
            ("settings", self.settings, ["GET"], "读取数据源与代理设置"),
            ("settings-save", self.settings_save, ["POST"], "保存数据源与代理设置"),
            ("urgent-targets", self.urgent_targets, ["GET"], "读取紧急私聊提醒配置"),
            ("urgent-save", self.urgent_save, ["POST"], "保存紧急私聊提醒配置"),
        )
        for endpoint, handler, methods, description in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{endpoint}",
                handler,
                methods,
                description,
            )

    # ---- 通用工具 ----

    @staticmethod
    def _meta_value(meta: Any, field: str, default: str = "") -> str:
        if isinstance(meta, dict):
            return str(meta.get(field) or default)
        return str(getattr(meta, field, None) or default)

    @staticmethod
    def _parse_umo(value: Any) -> tuple[str, str, str] | None:
        if not isinstance(value, str) or not value or len(value) > MAX_UMO_LENGTH:
            return None
        if any(ord(char) < 32 for char in value):
            return None
        parts = value.split(":", 2)
        if len(parts) != 3 or not all(parts):
            return None
        platform_id, message_type, session_id = parts
        if message_type not in MESSAGE_TYPES:
            return None
        return platform_id, message_type, session_id

    def _platform_instances(self) -> list[Any]:
        manager = getattr(self.context, "platform_manager", None)
        if manager is None:
            return []
        instances = getattr(manager, "platform_insts", None)
        if instances is None:
            get_insts = getattr(manager, "get_insts", None)
            instances = get_insts() if callable(get_insts) else []
        return list(instances or [])

    def _aiocqhttp_platforms(self) -> list[Any]:
        result = []
        for platform in self._platform_instances():
            try:
                meta = platform.meta()
            except Exception:
                continue
            if self._meta_value(meta, "name").casefold() == "aiocqhttp":
                result.append(platform)
        return result

    async def _fetch_platform_groups(self, platform: Any) -> dict[str, str]:
        """返回 ``{group_id: group_name}``，失败时返回空字典。"""
        try:
            meta = platform.meta()
            platform_id = self._meta_value(meta, "id")
            if not platform_id:
                return {}
            client = platform.get_client()
            response = await asyncio.wait_for(
                client.call_action(action="get_group_list"),
                timeout=GROUP_LIST_TIMEOUT_SECONDS,
            )
            groups = (
                response.get("data", response.get("groups", []))
                if isinstance(response, dict)
                else response
            )
            if not isinstance(groups, (list, tuple)):
                return {}
            result: dict[str, str] = {}
            for group in groups:
                if not isinstance(group, dict):
                    continue
                group_id = str(group.get("group_id") or "").strip()
                if not group_id:
                    continue
                result[group_id] = str(group.get("group_name") or "").strip()
            return result
        except Exception as exc:
            logger.debug(f"获取 QQ 群列表失败: {exc}")
            return {}

    async def _group_names(self) -> dict[str, str]:
        platforms = self._aiocqhttp_platforms()
        if not platforms:
            return {}
        results = await asyncio.gather(
            *(self._fetch_platform_groups(platform) for platform in platforms),
            return_exceptions=True,
        )
        names: dict[str, str] = {}
        for item in results:
            if isinstance(item, dict):
                names.update(item)
        return names

    @staticmethod
    def _session_label(
        message_type: str,
        session_id: str,
        group_names: dict[str, str],
    ) -> str:
        if message_type == "GroupMessage":
            name = group_names.get(session_id) or ""
            return name or f"群聊 {session_id}"
        if message_type == "FriendMessage":
            return f"私聊 {session_id}"
        return f"会话 {session_id}"

    # ---- 接口实现 ----

    async def overview(self):
        try:
            return json_response(await self._build_overview())
        except Exception as exc:
            logger.error(f"读取活动订阅数据失败: {exc}")
            return error_response("读取订阅数据失败", status_code=500)

    async def _build_overview(self) -> dict[str, Any]:
        plugin = self.plugin
        sessions, group_names = await asyncio.gather(
            plugin.subscriptions.get_all(),
            self._group_names(),
        )

        rows: list[dict[str, Any]] = []
        for umo, session in sessions.items():
            parsed = self._parse_umo(umo)
            if parsed is None:
                platform_id, message_type, session_id = "", "Unknown", umo
            else:
                platform_id, message_type, session_id = parsed

            policy = resolve_policy(session, plugin.default_policy)
            message_settings = resolve_message_settings(
                session, plugin.message_settings
            )
            names = []
            for game_id in session.get("games") or []:
                game = plugin.registry.get(game_id)
                names.append(game.name if game else game_id)

            rows.append(
                {
                    "umo": umo,
                    "platform_id": platform_id,
                    "session_type": message_type,
                    "session_id": session_id,
                    "session_name": self._session_label(
                        message_type, session_id, group_names
                    ),
                    "enabled": bool(session.get("enabled")),
                    "games": list(session.get("games") or []),
                    "game_names": names,
                    "thresholds": list(policy.thresholds),
                    "thresholds_overridden": session.get("thresholds") is not None,
                    "notify_new": policy.notify_new,
                    "notify_new_overridden": session.get("notify_new") is not None,
                    "cover_mode": session.get("cover_mode"),
                    "effective_cover_mode": message_settings.cover_mode,
                    "cover_mode_overridden": session.get("cover_mode") is not None,
                    "whitelist": list(session.get("whitelist") or []),
                    "blacklist": list(session.get("blacklist") or []),
                }
            )

        rows.sort(key=lambda row: (row["session_type"], row["session_name"]))

        games = [
            {
                "id": game.game_id,
                "name": game.name,
                "short_name": game.short_name,
                "source_id": game.source_id,
                "source_name": plugin.registry.source_name(game.game_id),
            }
            for game in plugin.registry.ordered()
        ]

        report = plugin.polling.last_report
        cycle = {
            "finished_at": (
                report.finished_at.strftime("%Y-%m-%d %H:%M:%S")
                if report.finished_at
                else ""
            ),
            "fetched_games": report.fetched_games,
            "activities": report.activities,
            "sessions": report.sessions,
            "reminders": report.reminders,
            "failures": dict(report.failures),
        }

        return {
            "games": games,
            "sessions": rows,
            "cycle": cycle,
            "defaults": {
                "thresholds": list(plugin.default_policy.thresholds),
                "notify_new": plugin.default_policy.notify_new,
                "lookahead_days": plugin.default_policy.lookahead_days,
                "interval_minutes": plugin.interval_minutes,
                "cover_mode": plugin.message_settings.cover_mode,
            },
            "sources": [
                {
                    "id": "sra",
                    "name": "SRA 公共 API",
                    "enabled": plugin.enable_sra,
                    "note": plugin.sra_api_base,
                },
                {
                    "id": "akedata",
                    "name": "AKEData（终末地）",
                    "enabled": plugin.enable_akedata,
                    "note": plugin.akedata_api_base,
                },
                {
                    "id": "prts",
                    "name": "PRTS（明日方舟）",
                    "enabled": plugin.enable_prts,
                    "note": plugin.prts_api_base,
                },
            ],
        }

    async def update_session(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)

        umo = payload.get("umo")
        if self._parse_umo(umo) is None:
            return error_response("会话 UMO 格式不正确", status_code=400)

        unknown_games = [
            game_id
            for game_id in payload.get("games", [])
            if self.plugin.registry.get(str(game_id)) is None
        ]
        if unknown_games:
            return error_response(
                f"未知的游戏: {'、'.join(map(str, unknown_games))}",
                status_code=400,
            )

        changes: dict[str, Any] = {}
        if "games" in payload:
            games = [str(item) for item in payload.get("games") or []]
            if not games:
                return error_response(
                    "至少要保留一个订阅游戏，如需清空请使用移除", status_code=400
                )
            changes["games"] = games
        if "enabled" in payload:
            changes["enabled"] = bool(payload.get("enabled"))
        if "notify_new" in payload:
            changes["notify_new"] = bool(payload.get("notify_new"))
        if "cover_mode" in payload:
            raw = payload.get("cover_mode")
            if raw in (None, "", "default", "默认", "跟随"):
                changes["cover_mode"] = None
            else:
                # 这里不能用 normalize_cover_mode：它会把未知值静默回退成默认，
                # 导致非法输入被固化成一个看起来合法的覆盖值。
                mode = str(raw).strip().lower()
                if mode not in COVER_MODES:
                    return error_response(
                        "封面形式只能是 image / link / off / default",
                        status_code=400,
                    )
                changes["cover_mode"] = mode
        if "thresholds" in payload:
            raw = payload.get("thresholds")
            if raw is None:
                changes["thresholds"] = None
            else:
                changes["thresholds"] = list(normalize_thresholds(raw))
        if "whitelist" in payload:
            changes["whitelist"] = normalize_keywords(payload.get("whitelist"))
        if "blacklist" in payload:
            changes["blacklist"] = normalize_keywords(payload.get("blacklist"))

        if not changes:
            return error_response("没有可保存的设置", status_code=400)

        session = await self.plugin.subscriptions.update_session(umo, changes)
        if session is None:
            return error_response("没有找到这个会话的订阅", status_code=404)
        return json_response({"saved": True, "umo": umo})

    async def remove_session(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        umo = payload.get("umo")
        if self._parse_umo(umo) is None:
            return error_response("会话 UMO 格式不正确", status_code=400)

        removed = await self.plugin.subscriptions.remove_session(umo)
        if not removed:
            return error_response("没有找到这个会话的订阅", status_code=404)

        notify_state = await self.plugin.subscriptions.get_notify_state()
        self.plugin.subscriptions.drop_session_state(notify_state, umo)
        await self.plugin.subscriptions.save_notify_state(notify_state)
        return json_response({"removed": True, "umo": umo})

    async def preview(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)

        game_id = str(payload.get("game_id") or "").strip()
        game = self.plugin.registry.get(game_id)
        if game is None:
            return error_response("未知的游戏", status_code=404)

        try:
            activities = await self.plugin.polling.fetch_activities(game_id)
        except Exception as exc:
            logger.warning(f"预览 {game_id} 活动失败: {exc}")
            return error_response(f"获取活动数据失败: {exc}", status_code=502)

        now = datetime.now(GAME_TIMEZONE)
        return json_response(
            {
                "game_id": game_id,
                "game_name": game.name,
                "count": len(activities),
                "text": build_overview_message(
                    game,
                    activities,
                    now,
                    self.plugin.message_settings,
                    source_name=self.plugin.registry.source_name(game_id),
                ),
            }
        )

    async def refresh(self):
        self.plugin.polling.invalidate_memo()
        report = await self.plugin.polling.run_cycle()
        return json_response(
            {
                "fetched_games": report.fetched_games,
                "activities": report.activities,
                "sessions": report.sessions,
                "reminders": report.reminders,
                "failures": dict(report.failures),
            }
        )

    # ---- 紧急私聊提醒 ----

    @staticmethod
    def _urgent_limits() -> dict[str, list[int]]:
        return {
            SETTING_TRIGGER: [MIN_TRIGGER_DAYS, MAX_TRIGGER_DAYS],
            SETTING_REPEAT: [MIN_REPEAT_MINUTES, MAX_REPEAT_MINUTES],
            SETTING_LIMIT: [MIN_DAILY_LIMIT, MAX_DAILY_LIMIT],
        }

    async def urgent_targets(self):
        try:
            return json_response(await self._build_urgent_overview())
        except Exception as exc:
            logger.error(f"读取紧急提醒配置失败: {exc}")
            return error_response("读取紧急提醒配置失败", status_code=500)

    async def _build_urgent_overview(self) -> dict[str, Any]:
        plugin = self.plugin
        data = await plugin.subscriptions.get_urgent_dm()
        base = plugin.urgent_dm

        rows: list[dict[str, Any]] = []
        for umo, entry in data.items():
            parsed = self._parse_umo(umo)
            message_type, session_id = (parsed[1], parsed[2]) if parsed else ("", umo)
            effective = target_settings(base, entry)
            activities = [
                (uid, state)
                for uid, state in (entry.get("activities") or {}).items()
                if isinstance(state, dict)
            ]
            activities.sort(key=lambda item: str(item[1].get("end") or ""))
            rows.append(
                {
                    "umo": umo,
                    "session_id": session_id,
                    "session_name": self._session_label(message_type, session_id, {}),
                    "enabled": bool(entry.get("enabled", True)),
                    "games": list(entry.get("games") or []),
                    "note": str(entry.get("note") or ""),
                    "settings": dict(entry.get("settings") or {}),
                    "effective": {
                        SETTING_TRIGGER: effective.trigger_days,
                        SETTING_REPEAT: effective.repeat_minutes,
                        SETTING_LIMIT: effective.daily_limit,
                    },
                    "describe": describe_target_settings(effective),
                    "activities": [
                        {
                            "uid": str(uid),
                            "activity_id": str(uid),
                            "name": str(state.get("name") or ""),
                            "end": str(state.get("end") or ""),
                            "acked": bool(state.get("acked")),
                            "sent_total": int(state.get("sent_total") or 0),
                            "last_sent_at": str(state.get("last_sent_at") or ""),
                        }
                        for uid, state in activities
                    ],
                }
            )

        rows.sort(key=lambda row: str(row["session_id"]))
        quiet_start, quiet_end = base.quiet_hours
        return {
            "enabled": bool(base.enabled),
            "defaults": {
                SETTING_TRIGGER: base.trigger_days,
                SETTING_REPEAT: base.repeat_minutes,
                SETTING_LIMIT: base.daily_limit,
                "quiet_hours": f"{quiet_start}-{quiet_end}",
                "ack_keywords": list(base.ack_keywords),
                "games": list(base.games),
            },
            "limits": self._urgent_limits(),
            "games": [
                {"id": game.game_id, "name": game.name}
                for game in plugin.registry.ordered()
            ],
            "targets": rows,
        }

    async def urgent_save(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)

        umo = payload.get("umo")
        if self._parse_umo(umo) is None:
            return error_response("私聊 UMO 格式不正确", status_code=400)

        plugin = self.plugin
        data = await plugin.subscriptions.get_urgent_dm()
        entry = data.get(umo) or {}

        if payload.get("remove"):
            if not SubscriptionService.remove_urgent_target(data, umo):
                return error_response("没有找到这个提醒目标", status_code=404)
            await plugin.subscriptions.save_urgent_dm(data)
            return json_response(await self._build_urgent_overview())

        if payload.get("reset_ack"):
            target_id = str(payload.get("activity_id") or "").strip()
            states = entry.get("activities") if isinstance(entry, dict) else None
            if not isinstance(states, dict) or not states:
                return error_response("这个目标还没有被提醒过的活动", status_code=404)
            cleared = 0
            for uid, state in states.items():
                if target_id and uid != target_id:
                    continue
                if isinstance(state, dict) and state.get("acked"):
                    state["acked"] = False
                    state.pop("ack_at", None)
                    cleared += 1
            if not cleared:
                return error_response("没有需要解除的活动", status_code=404)
            await plugin.subscriptions.save_urgent_dm(data)
            return json_response(await self._build_urgent_overview())

        games: list[str] | None = None
        if "games" in payload:
            raw_games = payload.get("games")
            if not isinstance(raw_games, list):
                return error_response("游戏列表格式不正确", status_code=400)
            games = [str(game).strip() for game in raw_games if str(game).strip()]
            unknown = [game for game in games if plugin.registry.get(game) is None]
            if unknown:
                return error_response(
                    f"未知的游戏: {'、'.join(unknown)}", status_code=400
                )

        patch: dict[str, Any] = {}
        raw_settings = payload.get("settings")
        if isinstance(raw_settings, Mapping):
            for key in TARGET_SETTING_KEYS:
                if key not in raw_settings:
                    continue
                value = raw_settings[key]
                if value is None or str(value).strip() == "":
                    patch[key] = None
                    continue
                try:
                    number = int(str(value).strip())
                except (TypeError, ValueError):
                    return error_response(f"{key} 必须是整数", status_code=400)
                clamped = clamp_setting(key, number)
                if clamped is None:
                    low, high = self._urgent_limits()[key]
                    return error_response(
                        f"{key} 超出范围（{low}~{high}）", status_code=400
                    )
                patch[key] = clamped

        if games is None and not patch and "enabled" not in payload:
            return error_response("没有可保存的设置", status_code=400)

        enabled = (
            bool(payload.get("enabled"))
            if "enabled" in payload
            else bool(entry.get("enabled", True))
        )
        SubscriptionService.upsert_urgent_target(
            data, umo, enabled=enabled, games=games, note=str(payload.get("note") or ""),
        )
        if patch:
            SubscriptionService.update_urgent_settings(data, umo, patch)
        await plugin.subscriptions.save_urgent_dm(data)
        return json_response(await self._build_urgent_overview())

    # ---- 消息模板编辑器 ----

    @staticmethod
    def _read_bool(values: dict, key: str, default: bool) -> bool:
        if key not in values:
            return default
        value = values[key]
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _read_text(values: dict, key: str, default: str) -> str:
        if key not in values:
            return default
        return str(values[key] if values[key] is not None else "").strip()

    def _draft_settings(self, values: Any):
        """把页面提交的模板值套到当前设置上，得到一个用于预览的草稿。"""
        base = self.plugin.message_settings
        if not isinstance(values, dict) or not values:
            return base

        cover_mode = base.cover_mode
        if "cover_mode" in values:
            raw = str(values.get("cover_mode") or "").strip().lower()
            if raw not in COVER_MODES:
                # 这里不能走 normalize_cover_mode：它会静默退回默认值
                raise ValueError(f"封面形式只能是 {' / '.join(COVER_MODES)}")

        overrides: dict[str, Any] = {
            "use_forward_node": self._read_bool(
                values, "use_forward_node", base.use_forward_node
            ),
            "forward_node_name": self._read_text(
                values, "forward_node_name", base.forward_node_name
            )
            or DEFAULT_FORWARD_NODE_NAME,
            "use_short_names": self._read_bool(
                values, "use_short_names", base.use_short_names
            ),
            "game_short_names": normalize_game_short_names(
                values["game_short_names"]
            )
            if "game_short_names" in values
            else base.game_short_names,
            "game_aliases": normalize_game_aliases(values["game_aliases"])
            if "game_aliases" in values
            else base.game_aliases,
            "urgency_marker": self._read_bool(
                values, "urgency_marker", base.urgency_marker
            ),
            "urgency_rules": parse_urgency_rules(values["urgency_rules"])
            if "urgency_rules" in values
            else base.urgency_rules,
            "tier_summary": self._read_bool(
                values, "tier_summary", base.tier_summary
            ),
            "headline_remaining": self._read_bool(
                values, "headline_remaining", base.headline_remaining
            ),
            "sort_by_urgency": self._read_bool(
                values, "sort_by_urgency", base.sort_by_urgency
            ),
            "activity_separator": self._read_text(
                values, "activity_separator", base.activity_separator
            ),
            "include_description": self._read_bool(
                values, "include_description", base.include_description
            ),
            "include_tags": self._read_bool(
                values, "include_tags", base.include_tags
            ),
            "include_extra_times": self._read_bool(
                values, "include_extra_times", base.include_extra_times
            ),
            "cover_mode": cover_mode,
            "image_mode_detail_link": self._read_bool(
                values, "image_mode_detail_link", base.image_mode_detail_link
            ),
        }
        return replace(base, **overrides)

    @staticmethod
    def _sample_activities(game: Game, now: datetime) -> list[Activity]:
        """内置示例活动，覆盖四个倒计时档位，保证预览随时可用。"""
        game_id = game.game_id
        return [
            Activity(
                game_id,
                "sample-1",
                "限时挑战·星轨回响",
                start_time=now - timedelta(days=3),
                end_time=now + timedelta(hours=5),
                description="示例活动，用来展示剩余不足 24 小时的排版。",
                tags=("限时", "挑战"),
            ),
            Activity(
                game_id,
                "sample-2",
                "「登录活动」签到领奖励",
                start_time=now - timedelta(days=1),
                end_time=now + timedelta(days=2, hours=3),
                description="示例活动，用来展示剩余不足 3 天的排版。",
                tags=("登录", "福利"),
            ),
            Activity(
                game_id,
                "sample-3",
                "复刻·往期活动返场",
                start_time=now - timedelta(hours=6),
                end_time=now + timedelta(days=5),
                tags=("复刻",),
            ),
            Activity(
                game_id,
                "sample-4",
                "版本主题活动",
                start_time=now - timedelta(hours=2),
                end_time=now + timedelta(days=12),
                tags=("版本",),
            ),
            Activity(
                game_id,
                "sample-5",
                "即将开始的预热活动",
                start_time=now + timedelta(days=2),
                end_time=now + timedelta(days=10),
            ),
        ]

    async def _save_plugin_config(self) -> None:
        """把内存里的配置落盘，兼容不同 AstrBot 版本的保存接口。"""
        saver = getattr(self.plugin.config, "save_config_async", None)
        if saver is not None:
            result = await saver()
            if result is False:
                raise RuntimeError("配置写入失败")
            return
        sync_saver = getattr(self.plugin.config, "save_config", None)
        if sync_saver is not None:
            await asyncio.to_thread(sync_saver)
            return
        raise RuntimeError("当前 AstrBot 版本不支持保存插件配置")

    async def template(self):
        """返回模板编辑器需要的当前配置与可选游戏。"""
        return json_response(
            {
                "values": self.plugin.message_format_values(),
                "games": [
                    {"game_id": game.game_id, "name": game.name}
                    for game in self.plugin.registry.ordered()
                ],
                "cover_modes": list(COVER_MODES),
            }
        )

    async def template_preview(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)

        try:
            settings = self._draft_settings(payload.get("values"))
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

        now = datetime.now(GAME_TIMEZONE)
        game_id = str(payload.get("game_id") or "").strip()
        game = self.plugin.registry.get(game_id) if game_id else None

        note = ""
        activities: list[Activity] = []
        if game is not None:
            try:
                fetched = await self.plugin.polling.fetch_activities(game_id)
            except Exception as exc:
                logger.warning(f"模板预览获取 {game_id} 活动失败: {exc}")
                note = f"获取真实数据失败，已改用示例活动（{exc}）"
            else:
                activities = [
                    item
                    for item in fetched
                    if item.is_active(now) or item.is_upcoming(now)
                ]
                if not activities:
                    note = "这个游戏当前没有进行中的活动，已改用示例活动。"

        if not activities:
            if game is None:
                games = self.plugin.registry.ordered()
                game = games[0] if games else Game("sample", "示例游戏", "sample")
            activities = self._sample_activities(game, now)
            note = note or "示例活动，只用来展示排版效果。"

        preview = build_template_preview(
            game,
            activities,
            now,
            settings,
            source_name=self.plugin.registry.source_name(game.game_id),
        )
        preview["note"] = note
        try:
            urgent_count = int(payload.get("urgent_games", 3))
        except (TypeError, ValueError):
            urgent_count = 3
        preview["urgent"] = build_urgent_preview(
            self.plugin.registry.ordered(),
            now,
            settings,
            count=urgent_count,
        )
        return json_response(preview)

    async def template_save(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        values = payload.get("values")
        if not isinstance(values, dict):
            return error_response("缺少要保存的模板内容", status_code=400)

        try:
            settings = self._draft_settings(values)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

        block = self.plugin.config.get("message_format")
        block = dict(block) if isinstance(block, dict) else {}
        block.update(
            {
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
        )
        self.plugin.config["message_format"] = block
        try:
            await self._save_plugin_config()
        except Exception as exc:
            logger.warning(f"保存消息模板配置失败: {exc}")
            return error_response(f"保存配置失败: {exc}", status_code=500)

        self.plugin.reload_message_settings()
        return json_response({"saved": True, "values": self.plugin.message_format_values()})

    # ---- 数据源与网络设置 ----

    PROXY_SCHEMES: tuple[str, ...] = (
        "http://",
        "https://",
        "socks5://",
        "socks5h://",
    )

    async def settings(self):
        return json_response(self.plugin.source_settings_values())

    async def settings_save(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)

        config = self.plugin.config
        current_sources = config.get("sources")
        current_sources = (
            dict(current_sources) if isinstance(current_sources, dict) else {}
        )
        current_basic = config.get("basic")
        current_basic = dict(current_basic) if isinstance(current_basic, dict) else {}

        try:
            sources_block, basic_block = self._draft_source_settings(
                payload, current_sources, current_basic
            )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

        config["sources"] = {**current_sources, **sources_block}
        config["basic"] = {**current_basic, **basic_block}
        try:
            await self._save_plugin_config()
        except Exception as exc:
            logger.warning(f"保存数据源配置失败: {exc}")
            return error_response(f"保存配置失败: {exc}", status_code=500)

        try:
            await self.plugin.reload_source_settings()
        except Exception as exc:
            logger.warning(f"应用数据源配置失败: {exc}")
            return error_response(
                f"配置已保存，但重新加载数据源失败: {exc}", status_code=500
            )

        return json_response({"saved": True, **self.plugin.source_settings_values()})

    def _draft_source_settings(
        self, payload: dict, current_sources: dict, current_basic: dict
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """校验并整理要写回配置的数据源 / 网络字段。"""
        plugin = self.plugin
        flags = {
            "sra": self._read_bool(current_sources, "enable_sra", True),
            "akedata": self._read_bool(current_sources, "enable_akedata", True),
            "prts": self._read_bool(current_sources, "enable_prts", True),
        }
        raw_enabled = payload.get("enabled")
        if isinstance(raw_enabled, dict):
            for source_id in flags:
                if source_id in raw_enabled:
                    flags[source_id] = self._read_bool(
                        raw_enabled, source_id, flags[source_id]
                    )
        if not any(flags.values()):
            raise ValueError("至少要启用一个数据源")

        block: dict[str, Any] = {
            "enable_sra": flags["sra"],
            "enable_akedata": flags["akedata"],
            "enable_prts": flags["prts"],
        }

        raw_prefs = payload.get("game_sources")
        raw_prefs = raw_prefs if isinstance(raw_prefs, dict) else {}
        for game_id, choices in plugin.SOURCE_CHOICES.items():
            if game_id in raw_prefs:
                choice = str(raw_prefs[game_id] or "").strip().lower()
                if choice not in choices:
                    raise ValueError(f"{game_id} 不支持的数据源：{choice or '（空）'}")
                if not flags[choice]:
                    raise ValueError(
                        f"{game_id} 选的数据源（{plugin.SOURCE_LABELS[choice]}）未启用"
                    )
            else:
                saved = str(current_sources.get(f"{game_id}_source") or choices[0])
                choice = saved.strip().lower()
                if choice not in choices or not flags[choice]:
                    choice = next(
                        (item for item in choices if flags[item]), choices[0]
                    )
            block[f"{game_id}_source"] = choice

        proxy = str(
            payload.get("proxy", current_basic.get("proxy", "")) or ""
        ).strip()
        if proxy and not proxy.lower().startswith(self.PROXY_SCHEMES):
            raise ValueError(
                "代理地址要以 http:// 或 socks5:// 开头；留空表示不使用代理"
            )
        direct_raw = payload.get("direct_hosts", current_basic.get("direct_hosts"))
        if direct_raw is None:
            direct_raw = ""
        direct_hosts = normalize_direct_hosts(direct_raw)

        return block, {
            "proxy": proxy,
            "direct_hosts": ", ".join(direct_hosts),
        }

