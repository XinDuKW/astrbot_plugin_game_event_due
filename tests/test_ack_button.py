"""紧急提醒的「我已知晓」按钮：只给支持 inline keyboard 的平台挂按钮。"""

from __future__ import annotations

import asyncio

import astrbot.api.message_components as Comp

from astrbot_plugin_game_event_due.services.delivery_service import (
    ACK_BUTTON_ID,
    ACK_BUTTON_LABEL,
    ACK_BUTTON_TEST_DATA,
    ACK_CANCEL_BUTTON_ID,
    ACK_CANCEL_BUTTON_LABEL,
    ACK_CONFIRM_BUTTON_ID,
    ACK_CONFIRM_BUTTON_LABEL,
    ActivityDeliveryService,
)
from astrbot_plugin_game_event_due.services.message_formatter import (
    PART_TEXT,
    MessagePart,
    MessageSettings,
)
from astrbot_plugin_game_event_due.services.polling_service import (
    ActivityPollingService,
)

SETTINGS = MessageSettings()
QQ_UMO = "qq_official:FriendMessage:openid-abc"
ONEBOT_UMO = "aiocqhttp:FriendMessage:123456"


class FakeContext:
    """记录发送出去的会话与消息链。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def send_message(self, umo: str, chain: object) -> None:
        self.sent.append((umo, chain))


def send(umo: str, *, ack_button: bool = True, ack_data=None):
    context = FakeContext()
    service = ActivityDeliveryService(context, SETTINGS, resolver=None)
    ok = asyncio.run(
        service.send_parts(
            umo,
            [MessagePart(PART_TEXT, "活动要结束了")],
            SETTINGS,
            ack_button=ack_button,
            ack_data=ack_data,
        )
    )
    assert ok is True
    assert len(context.sent) == 1
    return list(context.sent[0][1].chain)


def rows(chain):
    return [item for item in chain if isinstance(item, Comp.ActionRow)]


def test_supports_button_only_on_qq_official():
    service = ActivityDeliveryService(FakeContext(), SETTINGS, resolver=None)
    assert service.supports_button(QQ_UMO) is True
    assert service.supports_button("qq_official_webhook:FriendMessage:x") is True
    assert service.supports_button(ONEBOT_UMO) is False
    assert service.supports_button("") is False


def test_platform_type_resolves_custom_platform_ids():
    """UMO 第一段是用户自己起的平台 id，能力判断必须按适配器类型来。"""

    class FakeMeta:
        def __init__(self, id_: str, name: str) -> None:
            self.id = id_
            self.name = name

    class FakeInst:
        def __init__(self, id_: str, name: str) -> None:
            self._meta = FakeMeta(id_, name)

        def meta(self):
            return self._meta

    class Manager:
        def get_insts(self):
            return [FakeInst("Fairy", "qq_official"), FakeInst("Ponderer", "aiocqhttp")]

    class Context(FakeContext):
        platform_manager = Manager()

    service = ActivityDeliveryService(Context(), SETTINGS, resolver=None)
    assert service.platform_type("Fairy:FriendMessage:openid") == "qq_official"
    assert service.supports_button("Fairy:FriendMessage:openid") is True
    assert service.supports_markdown("Fairy:FriendMessage:openid") is True
    assert service.supports_forward("Ponderer:GroupMessage:1") is True
    # 查不到的类型照旧按原样比较
    assert service.platform_type("telegram:FriendMessage:1") == "telegram"
    assert service.supports_forward("telegram:FriendMessage:1") is False


def test_ack_row_carries_action_id_and_label():
    row = ActivityDeliveryService.build_ack_row()
    assert isinstance(row, Comp.ActionRow)
    assert len(row.buttons) == 1
    button = row.buttons[0]
    assert button.id == ACK_BUTTON_ID
    assert button.label == ACK_BUTTON_LABEL
    assert button.action.type == "callback"
    assert button.action.data is None


def test_send_parts_appends_button_on_qq_official():
    chain = send(QQ_UMO)
    found = rows(chain)
    assert len(found) == 1
    # 按钮必须在最后，正文在前
    assert chain[-1] is found[0]
    assert found[0].buttons[0].label == ACK_BUTTON_LABEL


def test_send_parts_skips_button_on_onebot():
    chain = send(ONEBOT_UMO)
    assert rows(chain) == []


def test_send_parts_skips_button_when_disabled():
    chain = send(QQ_UMO, ack_button=False)
    assert rows(chain) == []


def test_ack_row_carries_callback_data():
    row = ActivityDeliveryService.build_ack_row(ACK_BUTTON_TEST_DATA)
    assert row.buttons[0].action.data == ACK_BUTTON_TEST_DATA


def test_send_parts_forwards_ack_data_to_button():
    chain = send(QQ_UMO, ack_data=ACK_BUTTON_TEST_DATA)
    button = rows(chain)[0].buttons[0]
    assert button.action.data == {"test": "ack"}


def test_confirm_row_uses_its_own_action_id_and_label():
    """二次确认按钮必须是一套独立的 action id，否则 click 无法区分两步。"""
    row = ActivityDeliveryService.build_ack_row(confirm=True)
    assert isinstance(row, Comp.ActionRow)
    button = row.buttons[0]
    assert button.id == ACK_CONFIRM_BUTTON_ID
    assert button.label == ACK_CONFIRM_BUTTON_LABEL
    assert button.action.type == "callback"
    assert button.id != ACK_BUTTON_ID


def test_confirm_row_has_a_cancel_button():
    """二次确认那一行既要能确认，也要能反悔。"""
    row = ActivityDeliveryService.build_ack_row(confirm=True)
    assert len(row.buttons) == 2
    confirm, cancel = row.buttons
    assert cancel.id == ACK_CANCEL_BUTTON_ID
    assert cancel.label == ACK_CANCEL_BUTTON_LABEL
    assert cancel.id not in {ACK_BUTTON_ID, ACK_CONFIRM_BUTTON_ID}
    assert cancel.action.type == "callback"


def test_send_ack_prompt_carries_confirm_button():
    context = FakeContext()
    service = ActivityDeliveryService(context, SETTINGS, resolver=None)
    ok = asyncio.run(
        service.send_ack_prompt(QQ_UMO, "再点一下确认", SETTINGS)
    )
    assert ok is True
    chain = list(context.sent[0][1].chain)
    ids = [button.id for button in chain[-1].buttons]
    assert ids == [ACK_CONFIRM_BUTTON_ID, ACK_CANCEL_BUTTON_ID]
    # 二次确认消息不走转发，否则按钮会被塞进节点里不渲染
    assert not any(isinstance(item, Comp.Nodes) for item in chain)


def test_send_ack_prompt_forwards_test_data_to_both_buttons():
    """测试指令发的二次确认消息也要带测试标记，否则点击会动真实提醒状态。"""
    context = FakeContext()
    service = ActivityDeliveryService(context, SETTINGS, resolver=None)
    asyncio.run(
        service.send_ack_prompt(
            QQ_UMO, "（测试）", SETTINGS, data=ACK_BUTTON_TEST_DATA
        )
    )
    buttons = list(context.sent[0][1].chain[-1].buttons)
    assert [button.id for button in buttons] == [
        ACK_CONFIRM_BUTTON_ID,
        ACK_CANCEL_BUTTON_ID,
    ]
    assert all(button.action.data == ACK_BUTTON_TEST_DATA for button in buttons)


def test_urgent_settings_default_to_two_step_confirm():
    from astrbot_plugin_game_event_due.services.urgent_dm_service import (
        UrgentDmSettings,
    )

    assert UrgentDmSettings().ack_button is True
    assert UrgentDmSettings().ack_button_confirm is True


def test_ack_hint_mentions_button_only_when_enabled():
    with_button = ActivityPollingService._urgent_ack_hint(True)
    without = ActivityPollingService._urgent_ack_hint(False)
    # 有按钮就不再让人手打 /我已知晓（Markdown 下还白占一行）
    assert "我已知晓" in with_button
    assert "/我已知晓" not in with_button
    assert "按钮" not in without
    assert "/我已知晓" in without
