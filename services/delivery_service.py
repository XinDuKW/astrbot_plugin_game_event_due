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
    MessagePart,
    MessageSettings,
    UrgentGroup,
    build_activity_block,
    build_overview_blocks,
    build_reminder_parts,
    build_single_reminder_parts,
    short_game_name,
    sort_plans_by_urgency,
    fit_forward_title,
    summarize_markers,
)
from .reminder_service import ReminderPlan

NODE_NAME = "游戏活动日历"

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
    ) -> list[Any] | None:
        """「当前活动一览」的转发链：标题节点 + 每个活动一个节点。

        节点名统一用游戏简称，点开后逐条都是完整活动（含封面）。
        """
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
    ) -> list[Any]:
        """供指令回复使用：解析封面后返回可直接 yield 的消息链。

        开启合并转发时返回转发链（标题节点 + 正文节点，保证外层显示「群聊的
        聊天记录」）；平台不支持时原样返回普通链。
        """
        effective = settings or self.settings
        chain = self.build_chain(await self.resolve_parts(parts, effective))
        if effective.use_forward_node:
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
    ) -> bool:
        """把若干节点内容合并成一条转发消息发送。

        一个游戏只发一条转发消息，游戏内的每个活动各自成为一个节点。返回
        False 表示当前环境不支持或发送失败，调用方应回退为普通消息。
        """
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
        if effective.use_forward_node:
            nodes = [self._plain_chain(forward_title), chain]
            if await self._send_as_forward_nodes(umo, nodes, node_name):
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

    async def send_parts(
        self,
        umo: str,
        parts: Sequence[MessagePart],
        settings: MessageSettings | None = None,
        *,
        node_name: str = NODE_NAME,
        forward_title: str = "",
    ) -> bool:
        """发送消息片段；失败时逐级降级，尽量把内容送达。

        node_name 决定转发消息里每条的前缀（用游戏显示名），forward_title 是
        转发里的标题节点。
        """
        if not parts:
            return True

        effective = settings or self.settings
        items = await self.items_of(parts, effective)

        if await self._try_send(
            umo,
            self.build_chain(items),
            effective,
            node_name=node_name,
            forward_title=forward_title,
        ):
            return True

        if any(item.is_image for item in items):
            logger.warning(f"含封面的消息未能送达 {umo}，改为发送封面链接重试")
            if await self._try_send(
                umo,
                self.build_chain(items, images_as_links=True),
                effective,
                node_name=node_name,
                forward_title=forward_title,
            ):
                return True

        logger.error(f"活动推送最终发送失败 {umo}")
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

        # 合并转发：一个游戏一条转发消息，游戏内的每个活动各自成为一个节点。
        # 多个游戏之间不合并——每个游戏仍然是自己的一条转发消息。
        if effective.use_forward_node:
            ordered = self._ordered_plans(plans, now, effective)
            chains = await self._build_forward_nodes(ordered, now, effective)
            node_name = short_game_name(game, effective)
            title = self._forward_title(node_name, ordered, now, effective)
            nodes = [self._plain_chain(title)] + chains
            if await self._send_as_forward_nodes(umo, nodes, node_name):
                return list(plans)

        node_name = short_game_name(game, effective)
        title = self._forward_title(node_name, plans, now, effective)
        if effective.single_message_per_game:
            parts = build_reminder_parts(game, plans, now, effective)
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
            parts = build_single_reminder_parts(game, plan, now, effective)
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
