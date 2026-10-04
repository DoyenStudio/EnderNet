# coding=utf-8
"""订单完成提示与声音的引擎边界。"""

from ...common.resource_amount import format_resource_amount

ORDER_FINISHED_SOUND = "note.endernet.order_finished"


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
        from ....tooldelta.api.server.tips import NotifyOneMessage

        message = NotifyOneMessage
    if command is None:
        from ....tooldelta.api.server.command import SetCommand

        command = SetCommand
    name_of = name_of if name_of is not None else resource_display_name
    name = name_of(order.target) or order.target.name
    if isinstance(name, bytes):
        name = name.decode("utf-8")
    amount = format_resource_amount(order.target.kind, order.amount, native=True)
    message(order.player_id, u"§7[§ai§7] §a订单已完成： %sx%s" % (amount, name))
    command("/playsound %s @s" % ORDER_FINISHED_SOUND, order.player_id)
    return True
