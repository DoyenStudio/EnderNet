# coding=utf-8
"""Read-only catalogues for the client item and fluid selectors."""

from ...common.fluid_catalog import build_fluid_catalog
from ...common.item_catalog import build_event_classes

_SERVICE = None


def load_catalog():
    from mod.server.extraServerApi import GetEngineCompFactory, GetLevelId
    from ....tooldelta.api.server.item import ItemExists
    from ....tooldelta.define.item import Item

    registered_ids = GetEngineCompFactory().CreateItem(GetLevelId()).GetLoadItems(True) or []
    # Fluid blocks may be excluded from creative categories; scan before filtering.
    fluid_ids, buckets = build_fluid_catalog(registered_ids, ItemExists)
    return {"item_ids": filter_item_ids(registered_ids, Item),
            "fluid_ids": fluid_ids, "bucket_fluid_ids": buckets}


def filter_item_ids(item_ids, item_factory):
    # type: (list[str], object) -> list[str]
    result = []
    for item_id in sorted(set(item_ids)):
        try:
            category = item_factory(item_id).GetBasicInfo().itemCategory
        except ValueError:
            # `GetLoadItems(True)` 会把 `minecraft:air` 这类**没有物品定义**的
            # 条目也报上来，而 `Item.GetBasicInfo()` 对它们抛 ValueError
            # （`tooldelta/define/item.py:127-128`）。它们本来就选不中，跳过即可。
            # **不能让异常穿出去**：那样症状不是「少一条」，而是整张目录为空——
            # `ItemCatalogService.receive()` 在发包之前就断了，客户端只能一直等。
            continue
        if category not in ("", "command", "none"):
            result.append(item_id)
    return result


class ItemCatalogService(object):
    def __init__(self, bus, events, loader=load_catalog):
        self.bus = bus
        self.request, self.response = build_event_classes(events)
        self.loader = loader
        self.catalog = None

    def start(self):
        # 不带 `static=True`：它会给 listener 挂 `_td_static`，而 Python 2 的
        # **绑定方法**不能挂属性（`AttributeError: 'instancemethod' object has
        # no attribute '_td_static'`），真机上会让整个模组在 `OnServerInited`
        # 里断掉。那个标记本来也只对同一个函数对象重复装饰有意义，绑定方法
        # 每次访问都是新对象，去重由 `stop()` 的注销负责。
        self.bus.ListenEvent(self.request, 0)(self.receive)

    def receive(self, event):
        if not event.player_id:
            return
        if self.catalog is None:
            self.catalog = self.loader()
        self.response(**self.catalog).send(event.player_id)

    def stop(self):
        self.bus._dyn_unlisten(self.request, self.receive, 0)
        self.catalog = None


def start_catalog():
    global _SERVICE
    from ....tooldelta import events
    from ....tooldelta.events.event_bus import GetMCServerEventBus

    stop_catalog()
    _SERVICE = ItemCatalogService(GetMCServerEventBus(), events)
    _SERVICE.start()


def stop_catalog():
    global _SERVICE
    if _SERVICE is not None:
        _SERVICE.stop()
        _SERVICE = None
