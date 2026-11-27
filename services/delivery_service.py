"""活动提醒的消息发送层。

只负责把已经构建好的消息片段（文本 + 封面）送到会话。发送失败的提醒必须
**不**被标记为已推送，因此 :meth:`ActivityDeliveryService.deliver_plans` 返回
的是本轮真正送达的提醒列表，由轮询层据此更新状态。

封面处理链路：
``MessagePart(图片)`` → ``CoverResolver`` 下载并按需压缩 → ``select_within_budget``
按总体积预算挑选 → ``Comp.Image.fromBytes/fromURL``。超出预算的封面降级为文本链接，
含图消息整体发送失败时再降级一次（全部图片换成链接）重试。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
import astrbot.api.message_components as Comp

from ..sources.base import Game
from .cover_service import CoverResolver, select_within_budget
from .message_formatter import (
    MARKDOWN_MODE_OFF,
    MARKDOWN_MODE_ON,
    STYLE_MARKDOWN,
    STYLE_PLAIN,
    MessagePart,
    MessageSettings,
    UrgentGroup,
    build_activity_block,
    build_overview_blocks,
    build_reminder_parts,
    build_single_reminder_parts,
    normalize_markdown_mode,
    short_game_name,
    sort_plans_by_urgency,
    fit_forward_title,
    summarize_markers,
)
from .reminder_service import ReminderPlan

NODE_NAME = "游戏活动日历"

# 只有 aiocqhttp 系（NapCat / Lagrange 等）会真正渲染合并转发。
# QQ 官方机器人适配器会忽略 Node/Nodes 组件，整条消息变空后被直接丢弃，
# 所以必须按平台判断，不能靠发送异常兜底。
FORWARD_PLATFORMS = frozenset({"aiocqhttp"})

# 这些平台的适配器默认把纯文本塞进 msg_type=2 的 markdown 内容里发送
# （AstrBot 的 use_markdown 默认就是开），所以带 markdown 语法的文本会被
# 渲染成排版而不是原样显示。它们同时**不支持**合并转发。
MARKDOWN_PLATFORMS = frozenset({"qq_official", "qq_official_webhook"})

# 只有这几个平台会把 ActionRow 渲染成真正的可点按钮（QQ 官方机器人的
# inline keyboard）。onebot 系能不能显示取决于协议端实现，不能保证，
# 所以宁可不发——点不了的按钮比没有按钮更糟。
BUTTON_PLATFORMS = frozenset({"qq_official", "qq_official_webhook"})

# 按钮的 action id：事件回到插件时靠它认领。改这个值必须同步 main.py。
ACK_BUTTON_ID = "game_event_ack"
ACK_BUTTON_LABEL = "我已知晓"
# 二次确认按钮：点「我已知晓」后插件再发一条带这个按钮的消息，按下它才真的停止提醒。
# QQ 官方没有「更新已发消息里的按钮」接口，所以没法原地把第一个按钮改成确认按钮。
ACK_CONFIRM_BUTTON_ID = "game_event_ack_confirm"
ACK_CONFIRM_BUTTON_LABEL = "确认不再提醒"
# 二次确认消息里的另一半：反悔按钮。它不动提醒状态，只是让「待确认」这一步作废，
# 下一轮该催还催——防止误点「我已知晓」后只能靠再点一次确认来纠正。
ACK_CANCEL_BUTTON_ID = "game_event_ack_cancel"
ACK_CANCEL_BUTTON_LABEL = "取消确认"
# 测试按钮的回调上下文：插件收到它就知道这是一次通路测试，不去动真的提醒状态。
ACK_BUTTON_TEST_DATA = {"test": "ack"}

# 摘要消息底下的「看详情」按钮：所有游戏共用这一个 action id（ButtonActionFilter
# 只按 id 精确匹配，没法给每款游戏单独建一个），具体游戏放在回调 data 里。
DETAIL_BUTTON_ID = "game_event_detail"
DETAIL_ALL = "*"
DETAIL_ALL_LABEL = "全部"
# QQ inline keyboard 的 label 上限，超了会被平台截断，干脆自己先截。
DETAIL_LABEL_MAX = 10
# QQ inline keyboard 的布局上限是 5 行 × 每行 5 个；游戏多了就换行，
# 不再挤在一行里（挤在一行时按钮字会被截到看不出是哪款游戏）。
DETAIL_BUTTONS_PER_ROW = 5
DETAIL_ROWS_MAX = 5
# 一共最多几个游戏按钮：留一格给最后的「全部」。
DETAIL_MAX_BUTTONS = DETAIL_BUTTONS_PER_ROW * DETAIL_ROWS_MAX - 1
# 旧名字，早期是「一行 4 个游戏 + 全部」；现在表示按钮总数上限，调用方仍在用。
DETAIL_ROW_LIMIT = DETAIL_MAX_BUTTONS

SEND_TEXT = "text"
SEND_IMAGE = "image"


@dataclass(frozen=True, slots=True)
class SendItem:
    """已经决定好发送形态的一个片段。"""

    kind: str
    text: str = ""
    url: str = ""
    data: bytes | None = None

    @property
    def is_image(self) -> bool:
        return self.kind == SEND_IMAGE


class ActivityDeliveryService:
    """把活动提醒投递到目标会话。"""

    def __init__(
        self,
        context: Any,
        settings: MessageSettings,
        resolver: CoverResolver | None = None,
    ) -> None:
        self.context = context
        self.settings = settings
        self.resolver = resolver

    # ---- 平台能力 ----

    def platform_type(self, umo: str) -> str:
        """把这个会话的平台类型名取出来（aiocqhttp / qq_official / …）。

        UMO 的第一段是**平台 id**，也就是用户在 AstrBot 配置里给适配器起的名字
        （Fairy、Ponderer、MengDuYW 之类），和适配器类型没有任何关系；拿它跟
        "qq_official" 这种类型名比较，只要用户改过 id 就永远不匹配。所以这里先
        用平台管理器按 id 反查类型，查不到时才退回按原样比较。
        """
        raw = umo.split(":", 1)[0].strip() if umo else ""
        if not raw:
            return ""
        return (self._lookup_platform_type(raw) or raw).casefold()

    def _lookup_platform_type(self, platform_id: str) -> str:
        """按平台 id 找适配器类型；拿不到就返回空串，由调用方退回原样比较。"""
        manager = getattr(self.context, "platform_manager", None)
        getter = getattr(manager, "get_insts", None)
        if not callable(getter):
            return ""
        try:
            insts = getter()
        except Exception as exc:  # pragma: no cover - 取决于 AstrBot 版本
            logger.debug(f"读取平台实例失败，按 UMO 前缀判断: {exc}")
            return ""
        target = platform_id.strip().casefold()
        for inst in insts or []:
            meta_getter = getattr(inst, "meta", None)
            try:
                meta = meta_getter() if callable(meta_getter) else None
            except Exception:  # pragma: no cover - 单个适配器自身报错
                meta = None
            if meta is None:
                continue
            if str(getattr(meta, "id", "")).strip().casefold() != target:
                continue
            name = str(getattr(meta, "name", "") or "").strip()
            if name:
                return name
        return ""

    def supports_forward(self, umo: str) -> bool:
        """当前会话所在平台是否支持合并转发。

        QQ 官方机器人（qq_official）会忽略 Node/Nodes 且不抛异常，直接丢掉整条
        消息，所以这里必须先看平台；其它平台一律走普通消息。
        """
        return self.platform_type(umo) in FORWARD_PLATFORMS

    def supports_markdown(self, umo: str) -> bool:
        """当前会话所在平台是否会把纯文本当 markdown 渲染。

        QQ 官方机器人（qq_official / qq_official_webhook）默认就是这样；onebot
        系发什么都原样显示，塞 markdown 语法只会露出星号，所以不宣称支持。
        """
        return self.platform_type(umo) in MARKDOWN_PLATFORMS

    def supports_button(self, umo: str) -> bool:
        """当前平台能不能画出可点击的「我已知晓」按钮。"""
        return self.platform_type(umo) in BUTTON_PLATFORMS

    @staticmethod
    def build_ack_row(data: Any = None, *, confirm: bool = False) -> Any | None:
        """构造「我已知晓」按钮行；组件缺失时返回 None。

        QQ 一次点击就会回调（AstrBot 在分发事件前已经 ack 掉 interaction），所以
        不会卡 loading。confirm=True 时同一行里放两个按钮：红色「确认不再提醒」
        和一个灰色「取消确认」，二次确认时既可以确认也能反悔。data 是随点击一起
        回传的回调上下文，测试指令靠它区分「测试按钮」和真正的提醒按钮。
        """
        button_class = getattr(Comp, "Button", None)
        row_class = getattr(Comp, "ActionRow", None)
        if not (button_class and row_class):
            return None
        try:
            if confirm:
                buttons = [
                    button_class(
                        id=ACK_CONFIRM_BUTTON_ID,
                        label=ACK_CONFIRM_BUTTON_LABEL,
                        style=button_class.Style.DANGER,
                        action=button_class.CallbackAction(data=data),
                    ),
                    button_class(
                        id=ACK_CANCEL_BUTTON_ID,
                        label=ACK_CANCEL_BUTTON_LABEL,
                        style=button_class.Style.DEFAULT,
                        action=button_class.CallbackAction(data=data),
                    ),
                ]
            else:
                buttons = [
                    button_class(
                        id=ACK_BUTTON_ID,
                        label=ACK_BUTTON_LABEL,
                        style=button_class.Style.PRIMARY,
                        action=button_class.CallbackAction(data=data),
                    )
                ]
            return row_class(buttons=buttons)
        except Exception as exc:  # pragma: no cover - 取决于 AstrBot 版本
            logger.warning(f"构造「我已知晓」按钮失败，本条提醒不带按钮: {exc}")
            return None

    @staticmethod
    def build_detail_row(
        entries: Sequence[tuple[str, Any]],
    ) -> Any | None:
        """构造「看详情」按钮行：entries 是 (按钮文字, 回调数据) 列表。

        组件缺失（旧版 AstrBot）时返回 None，调用方按「没有按钮」降级。
        """
        button_class = getattr(Comp, "Button", None)
        row_class = getattr(Comp, "ActionRow", None)
        if not (button_class and row_class) or not entries:
            return None
        buttons = []
        try:
            for index, (label, data) in enumerate(entries):
                # QQ 官方适配器要求同一份 keyboard 里按钮 id 唯一
                # （_parse_keyboard 里重复 id 直接抛 ValueError，整条消息发不出去），
                # 所以 id 是「固定前缀 + 序号」；处理器用 action_id=None 接住全部点击，
                # 再靠回调数据里的 detail 字段区分点的是哪个游戏。
                buttons.append(
                    button_class(
                        id=f"{DETAIL_BUTTON_ID}_{index}",
                        label=str(label)[:DETAIL_LABEL_MAX] or "详情",
                        style=button_class.Style.DEFAULT,
                        action=button_class.CallbackAction(data=data),
                    )
                )
            return row_class(buttons=buttons)
        except Exception as exc:  # pragma: no cover - 取决于 AstrBot 版本
            logger.warning(f"构造「看详情」按钮失败，本条提醒不带按钮: {exc}")
            return None

    @classmethod
    def build_detail_rows(
        cls,
        entries: Sequence[tuple[str, Any]],
        *,
        all_entry: tuple[str, Any] | None = None,
    ) -> list[Any]:
        """构造「看详情」按钮，按每行 5 个排成多行（QQ 上限 5 行 × 5 个）。

        entries 是 (按钮文字, 回调数据)，按传入顺序排（调用方按紧急程度排好）；
        all_entry 是压在最后的「全部」按钮，用 PRIMARY 样式好认。
        组件缺失（旧版 AstrBot）时返回空列表，调用方按「没有按钮」降级。
        """
        button_class = getattr(Comp, "Button", None)
        row_class = getattr(Comp, "ActionRow", None)
        if not (button_class and row_class) or not entries:
            return []
        flat: list[tuple[str, Any, bool]] = [
            (str(label), data, False) for label, data in entries[:DETAIL_MAX_BUTTONS]
        ]
        if all_entry is not None:
            flat.append((str(all_entry[0]), all_entry[1], True))
        rows: list[Any] = []
        try:
            for start in range(0, len(flat), DETAIL_BUTTONS_PER_ROW):
                chunk = flat[start : start + DETAIL_BUTTONS_PER_ROW]
                buttons = []
                for index, (label, data, primary) in enumerate(chunk, start=start):
                    # id 必须全局唯一（适配器会校验重复 id），所以用整体序号。
                    buttons.append(
                        button_class(
                            id=f"{DETAIL_BUTTON_ID}_{index}",
                            label=label[:DETAIL_LABEL_MAX] or "详情",
                            style=(
                                button_class.Style.PRIMARY
                                if primary
                                else button_class.Style.DEFAULT
                            ),
                            action=button_class.CallbackAction(data=data),
                        )
                    )
                rows.append(row_class(buttons=buttons))
            return rows
        except Exception as exc:  # pragma: no cover - 取决于 AstrBot 版本
            logger.warning(f"构造「看详情」按钮失败，本条提醒不带按钮: {exc}")
            return []

    def message_style(
        self,
        umo: str,
        settings: MessageSettings | None = None,
    ) -> str:
        """这条消息该用哪种排版：``plain`` 还是 ``markdown``。

        ``markdown_mode`` 为 ``on`` 时强制 markdown（哪怕平台不认，用户自己选的），
        ``off`` 时始终纯文本；``auto`` 按平台判断。markdown 与合并转发互斥，
        markdown 生效时调用方不应再走转发分支。
        """
        effective = settings or self.settings
        mode = normalize_markdown_mode(getattr(effective, "markdown_mode", None))
        if mode == MARKDOWN_MODE_OFF:
            return STYLE_PLAIN
        if mode == MARKDOWN_MODE_ON:
            return STYLE_MARKDOWN
        return (
            STYLE_MARKDOWN if self.supports_markdown(umo) else STYLE_PLAIN
        )

    def _can_forward(
        self,
        umo: str,
        settings: MessageSettings | None = None,
    ) -> bool:
        """平台支持转发、且这条消息不是 markdown 排版时才走转发。

        markdown 是「一条消息里排版」，合并转发是「多条消息折叠成一条」，两者混在
        一起会把语法塞进节点里露出原始星号，所以 markdown 生效时一律不转发。
        """
        return self.supports_forward(umo) and (
            self.message_style(umo, settings) == STYLE_PLAIN
        )

    # ---- 片段 → 发送项 ----

    @staticmethod
    def _text_only(parts: Sequence[MessagePart]) -> list[SendItem]:
        return [
            SendItem(
                SEND_TEXT,
                text=f"封面：{part.value}" if part.is_image else part.value,
            )
            for part in parts
        ]

    async def resolve_parts(
        self,
        parts: Sequence[MessagePart],
        settings: MessageSettings | None = None,
    ) -> list[SendItem]:
        """决定每个封面是真发图片还是降级成链接。"""
        effective = settings or self.settings
        urls = [part.value for part in parts if part.is_image]
        if not urls or self.resolver is None:
            return [
                SendItem(SEND_TEXT, text=part.value)
                if not part.is_image
                else SendItem(SEND_IMAGE, url=part.value)
                for part in parts
            ]

        ordered = list(dict.fromkeys(urls))
        payloads = await self.resolver.resolve(ordered)
        chosen, dropped = select_within_budget(
            ordered,
            payloads,
            effective.cover_max_total_bytes,
        )
        if dropped:
            logger.info(
                f"封面总体积超出 {effective.cover_max_total_mb}MB 预算，"
                f"{len(dropped)} 张降级为链接"
            )
        inline = {payload.url: payload for payload in chosen}

        items: list[SendItem] = []
        for part in parts:
            if not part.is_image:
                items.append(SendItem(SEND_TEXT, text=part.value))
                continue
            payload = inline.get(part.value)
            if payload is None:
                items.append(SendItem(SEND_TEXT, text=f"封面：{part.value}"))
                continue
            items.append(
                SendItem(SEND_IMAGE, url=part.value, data=payload.data)
            )
        return items

    # ---- 发送项 → 消息链 ----

    async def items_of(
        self,
        parts: Sequence[MessagePart],
        settings: MessageSettings | None = None,
    ) -> list[SendItem]:
        """决定每个片段怎么发：封面下载/压缩失败时整体降级为文字 + 链接。"""
        effective = settings or self.settings
        try:
            return await self.resolve_parts(parts, effective)
        except Exception as exc:
            logger.warning(f"封面解析失败，改为只发文字与封面链接: {exc}")
            return self._text_only(parts)

    @staticmethod
    def _build_image(item: SendItem) -> Any | None:
        try:
            if item.data is not None:
                from_bytes = getattr(Comp.Image, "fromBytes", None)
                if callable(from_bytes):
                    return from_bytes(item.data)
            from_url = getattr(Comp.Image, "fromURL", None)
            if callable(from_url):
                return from_url(item.url)
        except Exception as exc:
            logger.warning(f"构建图片组件失败，回退为链接 {item.url}: {exc}")
        return None

    def build_chain(
        self,
        items: Sequence[SendItem],
        *,
        images_as_links: bool = False,
    ) -> list[Any]:
        """把发送项转换成 AstrBot 消息组件列表。"""
        chain: list[Any] = []
        for item in items:
            if not item.is_image:
                chain.append(Comp.Plain(item.text))
                continue
            if images_as_links:
                chain.append(Comp.Plain(f"封面：{item.url}"))
                continue
            component = self._build_image(item)
            chain.append(
                component if component is not None else Comp.Plain(f"封面：{item.url}")
            )
        return chain

    @staticmethod
    def _plain_chain(text: str) -> list[Any]:
        return [Comp.Plain(text)] if text else []

    @staticmethod
    def _forward_title(
        game_name: str,
        plans: Sequence[ReminderPlan],
        now,
        settings: MessageSettings,
    ) -> str:
        """转发里的第一条：游戏名 + 档位统计，顺便把节点数撑到 2 个。

        QQ 只有 2 个以上节点时才显示「群聊的聊天记录」，单节点会显示成
        「xxx的聊天记录」，所以固定带一个标题节点。
        """
        summary = summarize_markers(
            [plan.activity for plan in plans], now, settings
        )
        compact = summary.replace(" | ", " ")
        candidates = (
            [
                f"🎮 {game_name} · 活动提醒  {summary}",
                f"{game_name} · 提醒 {compact}",
                f"{game_name} {compact}",
            ]
            if summary
            else [f"🎮 {game_name} · 活动提醒", f"{game_name} · 提醒"]
        )
        return fit_forward_title(candidates)

    @staticmethod
    def build_forward_chain(
        node_chains: Sequence[Sequence[Any]],
        node_name: str,
    ) -> list[Any] | None:
        """把若干节点内容包成一条转发消息链；平台不支持时返回 None。"""
        node_class = getattr(Comp, "Node", None)
        nodes_class = getattr(Comp, "Nodes", None)
        if not (node_class and nodes_class) or not node_chains:
            return None
        nodes = [
            node_class(content=list(chain), name=node_name)
            for chain in node_chains
            if chain
        ]
        if not nodes:
            return None
        return [nodes_class(nodes)]

    @staticmethod
    def build_forward_chain_named(
        nodes: Sequence[tuple[str, Sequence[Any]]],
    ) -> list[Any] | None:
        """每个节点用自己的名字包成一条转发；节点不足两个时返回 None。

        少于两个节点时 QQ 会显示成「xxx的聊天记录」，不如直接发普通消息。
        """
        node_class = getattr(Comp, "Node", None)
        nodes_class = getattr(Comp, "Nodes", None)
        if not (node_class and nodes_class):
            return None
        built = [
            node_class(content=list(chain), name=name or NODE_NAME)
            for name, chain in nodes
            if chain
        ]
        if len(built) < 2:
            return None
        return [nodes_class(built)]

    async def send_urgent_forward(
        self,
        umo: str,
        groups: Sequence[UrgentGroup],
        now,
        settings: MessageSettings,
        *,
        title: str,
        ack_hint: str = "",
        node_name: str = "",
    ) -> bool:
        """紧急私聊提醒的合并转发：标题节点 + 每个活动一个节点。

        标题拆成两个节点：「N 个活动马上就要结束了！」和「各游戏分布 + 停止提醒
        的说明」。转发卡片每个节点只显示一行，拆开才看得到催的是哪几个游戏。
        之后每个活动各占一个节点，节点名是该游戏的简称。
        """
        if not groups:
            return True
        if not self._can_forward(umo, settings):
            return False
        node_name = node_name or settings.forward_node_name or NODE_NAME
        head, _, summary = title.partition("\n")
        tail = f"{summary}\n\n{ack_hint}" if ack_hint else summary
        nodes: list[tuple[str, Sequence[Any]]] = [
            (node_name, self._plain_chain(head))
        ]
        if tail:
            nodes.append((node_name, self._plain_chain(tail)))
        for group in groups:
            for activity in group.activities:
                block = build_activity_block(activity, (), now, settings)
                items = await self.items_of(block, settings)
                nodes.append((group.short, self.build_chain(items)))

        chain = self.build_forward_chain_named(nodes)
        if chain is None:
            return False
        try:
            await self.context.send_message(umo, MessageChain(chain=chain))
            return True
        except Exception as exc:
            logger.warning(f"活动私聊提醒发送失败 {umo}: {exc}")
            return False

    async def build_overview_forward_chain(
        self,
        game: Any,
        activities: Sequence[Any],
        now: Any,
        settings: MessageSettings,
        *,
        source_name: str = "",
        umo: str = "",
    ) -> list[Any] | None:
        """「当前活动一览」的转发链：标题节点 + 每个活动一个节点。

        节点名统一用游戏简称，点开后逐条都是完整活动（含封面）。平台不支持
        合并转发时返回 None，由调用方回退普通消息。
        """
        if not self._can_forward(umo, settings):
            return None
        header, blocks = build_overview_blocks(
            game, activities, now, settings, source_name=source_name
        )
        node_name = short_game_name(game, settings)
        nodes: list[tuple[str, Sequence[Any]]] = [
            (node_name, self._plain_chain(header))
        ]
        for block in blocks:
            nodes.append(
                (node_name, self.build_chain(await self.items_of(block, settings)))
            )
        return self.build_forward_chain_named(nodes)

    async def build_event_chain(
        self,
        parts: Sequence[MessagePart],
        settings: MessageSettings | None = None,
        *,
        node_name: str = NODE_NAME,
        forward_title: str = "",
        umo: str = "",
    ) -> list[Any]:
        """供指令回复使用：解析封面后返回可直接 yield 的消息链。

        开启合并转发时返回转发链（标题节点 + 正文节点，保证外层显示「群聊的
        聊天记录」）；平台不支持时原样返回普通链。
        """
        effective = settings or self.settings
        chain = self.build_chain(await self.resolve_parts(parts, effective))
        if effective.use_forward_node and self._can_forward(umo, effective):
            nodes = [self._plain_chain(forward_title), chain]
            forward = self.build_forward_chain(nodes, node_name)
            if forward is not None:
                return forward
        return chain

    # ---- 发送 ----

    @staticmethod
    def _ordered_plans(
        plans: Sequence[ReminderPlan],
        now,
        settings: MessageSettings,
    ) -> list[ReminderPlan]:
        if settings.sort_by_urgency:
            return sort_plans_by_urgency(plans, now)
        return list(plans)

    async def _build_forward_nodes(
        self,
        plans: Sequence[ReminderPlan],
        now,
        settings: MessageSettings,
    ) -> list[list[Any]]:
        """把每个活动构建成一条独立的转发节点内容。"""
        chains: list[list[Any]] = []
        for plan in plans:
            parts = build_activity_block(
                plan.activity, plan.reasons, now, settings
            )
            chain = self.build_chain(await self.items_of(parts, settings))
            if chain:
                chains.append(chain)
        return chains

    async def _send_as_forward_nodes(
        self,
        umo: str,
        node_chains: Sequence[Sequence[Any]],
        node_name: str,
        settings: MessageSettings | None = None,
    ) -> bool:
        """把若干节点内容合并成一条转发消息发送。

        一个游戏只发一条转发消息，游戏内的每个活动各自成为一个节点。返回
        False 表示当前环境不支持、正在用 markdown 排版或发送失败，调用方应
        回退为普通消息。
        """
        if not self._can_forward(umo, settings):
            return False
        chain = self.build_forward_chain(node_chains, node_name)
        if chain is None:
            return False
        try:
            await self.context.send_message(umo, MessageChain(chain=chain))
            return True
        except Exception as exc:
            logger.warning(f"合并转发发送失败，回退为普通消息: {exc}")
            return False

    async def _try_send(
        self,
        umo: str,
        chain: Sequence[Any],
        settings: MessageSettings | None = None,
        *,
        node_name: str = NODE_NAME,
        forward_title: str = "",
    ) -> bool:
        if not chain:
            return True

        effective = settings or self.settings
        # 与提醒推送共用同一套转发实现：支持就转发，不支持或失败就回退普通消息
        if effective.use_forward_node and self._can_forward(umo, effective):
            nodes = [self._plain_chain(forward_title), chain]
            if await self._send_as_forward_nodes(
                umo, nodes, node_name, effective
            ):
                return True

        try:
            await self.context.send_message(
                umo,
                MessageChain(chain=list(chain)),
            )
            return True
        except Exception as exc:
            logger.warning(f"活动推送发送失败 {umo}: {exc}")
            return False

    def _ack_extra(
        self,
        umo: str,
        settings: MessageSettings,
        enabled: bool,
        data: Any = None,
        confirm: bool = False,
    ) -> list[Any]:
        """按需追加「我已知晓」按钮行。

        走合并转发时按钮会被塞进转发节点里，QQ 不会渲染，所以转发分支必须跳过；
        markdown 与 keyboard 是两个独立字段，可以共存，不用排除。
        """
        if not enabled or not self.supports_button(umo):
            return []
        if settings.use_forward_node and self._can_forward(umo, settings):
            return []
        row = self.build_ack_row(data, confirm=confirm)
        return [row] if row is not None else []

    async def send_parts(
        self,
        umo: str,
        parts: Sequence[MessagePart],
        settings: MessageSettings | None = None,
        *,
        node_name: str = NODE_NAME,
        forward_title: str = "",
        ack_button: bool = False,
        ack_data: Any = None,
        ack_confirm: bool = False,
        extra_rows: Sequence[Any] = (),
    ) -> bool:
        """发送消息片段；失败时逐级降级，尽量把内容送达。

        node_name 决定转发消息里每条的前缀（用游戏显示名），forward_title 是
        转发里的标题节点；ack_button 为真时在消息末尾挂一个「我已知晓」按钮；
        extra_rows 是调用方自己拼好的额外按钮行（摘要消息的「看详情」按钮），
        它们排在「我已知晓」前面。
        """
        if not parts:
            return True

        effective = settings or self.settings
        items = await self.items_of(parts, effective)
        # 不支持 inline keyboard 的平台（OneBot 等）连额外的按钮行也一起跳过，
        # 否则调用方传进来的一行按钮会被当成未知组件塞进消息里。
        extra = (
            [row for row in extra_rows if row is not None]
            if self.supports_button(umo)
            else []
        )
        extra.extend(self._ack_extra(umo, effective, ack_button, ack_data, ack_confirm))

        if await self._try_send(
            umo,
            self.build_chain(items) + extra,
            effective,
            node_name=node_name,
            forward_title=forward_title,
        ):
            return True

        if any(item.is_image for item in items):
            logger.warning(f"含封面的消息未能送达 {umo}，改为发送封面链接重试")
            if await self._try_send(
                umo,
                self.build_chain(items, images_as_links=True) + extra,
                effective,
                node_name=node_name,
                forward_title=forward_title,
            ):
                return True

        logger.error(f"活动推送最终发送失败 {umo}")
        return False

    async def send_ack_prompt(
        self,
        umo: str,
        text: str,
        settings: MessageSettings | None = None,
        data: Any = None,
    ) -> bool:
        """发一条带「确认不再提醒」/「取消确认」按钮的消息，作为二次确认。

        这里**不走转发**：按钮塞进转发节点里 QQ 不会渲染，用户只能看到纯文本。
        data 是随点击回传的回调上下文，测试指令靠它认出“这只是通路测试”。
        """
        chain: list[Any] = [Comp.Plain(text)]
        row = self.build_ack_row(data, confirm=True)
        if row is not None:
            chain.append(row)
        try:
            await self.context.send_message(umo, MessageChain(chain=chain))
            return True
        except Exception as exc:
            logger.warning(f"二次确认消息发送失败 {umo}: {exc}")
            return False

    async def send_text(self, umo: str, text: str) -> bool:
        if not text:
            return True
        return await self._try_send(umo, [Comp.Plain(text)])

    async def deliver_plans(
        self,
        umo: str,
        game: Game,
        plans: list[ReminderPlan],
        now,
        settings: MessageSettings | None = None,
    ) -> list[ReminderPlan]:
        """推送一个游戏本轮的提醒，返回实际送达的提醒。

        合并发送时要么全部成功要么全部失败；逐条发送时只把成功的那些返回，
        失败的会在下一轮重试且不会与已送达的重复。
        """
        if not plans:
            return []

        effective = settings or self.settings
        style = self.message_style(umo, effective)

        # 合并转发：一个游戏一条转发消息，游戏内的每个活动各自成为一个节点。
        # 多个游戏之间不合并——每个游戏仍然是自己的一条转发消息。
        # markdown 排版与转发互斥，_can_forward 里已经把这个条件算进去了。
        if effective.use_forward_node and self._can_forward(umo, effective):
            ordered = self._ordered_plans(plans, now, effective)
            chains = await self._build_forward_nodes(ordered, now, effective)
            node_name = short_game_name(game, effective)
            title = self._forward_title(node_name, ordered, now, effective)
            nodes = [self._plain_chain(title)] + chains
            if await self._send_as_forward_nodes(
                umo, nodes, node_name, effective
            ):
                return list(plans)

        node_name = short_game_name(game, effective)
        title = self._forward_title(node_name, plans, now, effective)
        if effective.single_message_per_game:
            parts = build_reminder_parts(
                game, plans, now, effective, style=style
            )
            return (
                list(plans)
                if await self.send_parts(
                    umo,
                    parts,
                    effective,
                    node_name=node_name,
                    forward_title=title,
                )
                else []
            )

        delivered: list[ReminderPlan] = []
        for plan in plans:
            parts = build_single_reminder_parts(
                game, plan, now, effective, style=style
            )
            if await self.send_parts(
                umo,
                parts,
                effective,
                node_name=node_name,
                forward_title=self._forward_title(
                    node_name, [plan], now, effective
                ),
            ):
                delivered.append(plan)
        return delivered
