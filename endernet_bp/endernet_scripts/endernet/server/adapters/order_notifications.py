# coding=utf-8
"""订单完成提示与声音的引擎边界。"""

from ...common.resource_amount import format_resource_amount
from .engine_text import engine_text

ORDER_FINISHED_SOUND = "note.endernet.order_finished"


def send_message(player_id, text):
    # type: (str, str) -> None
    from mod.server.extraServerApi import GetEngineCompFactory

    GetEngineCompFactory().CreateMsg(engine_text(player_id)).NotifyOneMessage(
        engine_text(player_id), engine_text(text)
    )


def execute_sound_command(text, player_id):
    # type: (str, str) -> bool
    from mod.server.extraServerApi import GetEngineCompFactory, GetLevelId

    return GetEngineCompFactory().CreateCommand(GetLevelId()).SetCommand(
        engine_text(text), engine_text(player_id), False
    )


def resource_display_name(key):
    # type: (object) -> str
    from ....tooldelta.define.item import Item

    return Item(key.name, key.aux).GetBasicInfo().itemName


def notify_order_completed(order, message=None, command=None, name_of=None,
                           online=None):
    # type: (object, object, object, object, object) -> bool
    """只通知在线发起者；命令以该玩家为执行者，@s 不会广播给旁人。"""
    if not order.player_id:
        return False
    if online is None:
        from mod.server.extraServerApi import GetPlayerList

        online = lambda player_id: player_id in GetPlayerList()
    if not online(order.player_id):
        return False
    if message is None:
        message = send_message
    if command is None:
        command = execute_sound_command
    name_of = name_of if name_of is not None else resource_display_name
    name = name_of(order.target) or order.target.name
    if isinstance(name, bytes):
        name = name.decode("utf-8")
    amount = format_resource_amount(order.target.kind, order.amount, native=True)
    # 引擎消息接口接收 py2 的普通 UTF-8 str，不直接传 unicode。
    text = engine_text("§7[§ai§7] §a订单已完成： %sx%s") % (
        engine_text(amount), engine_text(name)
    )
    message(order.player_id, text)
    played = command("/playsound %s" % ORDER_FINISHED_SOUND,
                     order.player_id)
    if played is False:
        raise RuntimeError("order finished sound command failed")
    return True
