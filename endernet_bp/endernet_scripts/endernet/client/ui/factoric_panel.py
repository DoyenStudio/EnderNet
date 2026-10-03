# coding=utf-8
"""工厂终端界面（11-06）：一条网络的活跃订单、剩余内存与配方树进度。

**它不是「通用终端 + 一块附加区」**，与 `template_panel` 那种写法不同：
交接第 4 节明写「本界面没有真实物品槽或玩家背包」，因此这里**没有**
`TerminalListView`、没有背包、没有网络物品列表，也就没有搜索与排序。
界面上的东西全部来自服务端每帧推的 `order_state`（`OrderStateMessage`）。

从 `TerminalPanelBase` 继承的只有三样：关闭按钮（`#EnderNetTerminal.close`）、
ESC 关屏、以及设备状态栏与详情弹窗那组绑定（`DeviceStatusBindingsMixin`）。
基类里与列表、背包、虚格有关的那批绑定名**本界面的 JSON 一个都不引用**，
而引擎只按 JSON 里写了的绑定名来问，因此继承进来是惰性的。

## 视图种类是 `VIEW_ORDER`，不是 `VIEW_ITEM`

`NetworkClient` 建的时候要显式传 `view_kind=VIEW_ORDER`（见 `OnCreate`）。
服务端 `_service_network` 按这个字段分流：物品视图那条会话拿物品快照，
订单视图这条拿 `order_state`。**在 11-06 之前 `VIEW_ORDER` 没有生产者**，
开会话会被拒成 `unsupported`。

## 唯一一处本地决定的东西：选中了哪一单

服务端推的是**全量快照**（全部活跃订单 + 全部配方树），客户端不比较新旧、
拿到就整份替换。本地只记「详情窗开着的是哪一单」这一个状态，而它也有唯一
的清理点：那一单从推送里消失（结单、被取消）时自动关窗——不这么做，详情会
停在一份**永远不会再更新**的旧画面上。

## 详情窗是按需建的，不在主面板上

`order_description_window` **不写在 `content.controls` 里**（User 2026-10-03 定）：
点第一行订单时才 `AddElement` 出来，建好立刻藏起来，**之后留着不销毁**——
再打开只是把可见性恢复回去。理由与做法见 `FactoricView` 详情窗那一段。

**显隐只有 `SetVisible` 一个写者**，没有 `#visible` 绑定：绑定每帧重新求值，
会与运行期写进去的值互相盖。

## 已知缺项（如实记，不补假实现）

- **提交订单、材料缺口、规划预览、阻塞原因没有控件**：交接第 1 节明写这四件
  本次未补设，`plans/ui/factoric_terminal_panel.md` 第 8 节也标着待确认。
  界面上因此**没有下单入口**，只有取消。
- **失败原因没有显示位置**：与通用／合成／样板三台同一处境，原因码只进
  `last_error` 与 `diagnostics()`。
- **详情窗的防穿透与重复打开策略**：交接第 5 节自己标着「待确认」。这扇窗
  **没有全屏遮罩**，因此它开着时点得到底下的控件。
- **详情窗里的绑定能不能生效，真机没验过**：它是在运行期 `AddElement` 出来的，
  而实测过的两条路都只在**静态**控件上验过类体绑定（11-13）与运行期注册
  （后者在本引擎上无效）。模板根上那条显隐已经不靠绑定了，行内的文字／颜色／
  进度仍靠它——落空的表现是「窗口开着、里面是空的」。
- **内存单位是字节（B）**：任务文档定的口径；JSON 里那句静态「0KB」是资源
  初值，运行期一律以这里写的为准。
"""

from ....tooldelta.ui import Binder, RegistToolDeltaScreen

from . import stack_size
from .terminal_panel import TerminalPanelBase
from .widgets import device_status
from ..network_client import NetworkClient
from ...common.identity import DeviceRef
from ...common.protocol import (
    MSG_ORDER_STATE,
    ORDER_SOURCE_FEED_DONE,
    ORDER_SOURCE_FEED_WAITING_INPUTS,
    ORDER_SOURCE_FEED_WAITING_OUTPUTS,
    ORDER_SOURCE_STAGE_DONE,
    ORDER_SOURCE_STAGE_RUNNING,
    ORDER_SOURCE_STAGE_WAITING,
    STATUS_OK,
    VIEW_ORDER,
)

try:
    INTEGER_TYPES = (int, long)
except NameError:
    INTEGER_TYPES = (int,)

try:
    TEXT_TYPE = unicode
except NameError:
    TEXT_TYPE = str

#: 本界面新增控件的绑定前缀。**从 `TerminalPanelBase` 继承的那几条一律沿用
#: `#EnderNetTerminal.*`**（关闭、ESC），不另立一套。
BINDING_PREFIX = "#EnderNetFactoric."

#: 两个集合名，**必须与 JSON 里 grid 的 `collection_name` 逐字相同**。
#: 名字是交接文档第 3、4 节早就定下的（`orders` / `order_sources`），
#: 这里只是把它们接到绑定上。
ORDER_COLLECTION = "orders"
SOURCE_COLLECTION = "order_sources"

#: 取消订单的线上动作名。**与 `sync/terminal.py` 的 `ORDER_ACTIONS` 表逐字对应**
#: ——那张表把它路由成 `CommandKind.ORDER` 并换名成 `cancel`。客户端不需要
#: 知道路由这件事，它照发普通命令即可。
ACTION_CANCEL = "order_cancel"

#: 三阶段的背景色（交接第 4 节，User 指定的 RGB 归一化到 0～1）。
#: JSON 里的静态初值就是「制作中」那一档，运行期一律以这里为准。
COLOR_WAITING = (200 / 255.0, 200 / 255.0, 1.0)
COLOR_RUNNING = (1.0, 1.0, 200 / 255.0)
COLOR_DONE = (200 / 255.0, 1.0, 200 / 255.0)
STAGE_COLORS = {
    ORDER_SOURCE_STAGE_WAITING: COLOR_WAITING,
    ORDER_SOURCE_STAGE_RUNNING: COLOR_RUNNING,
    ORDER_SOURCE_STAGE_DONE: COLOR_DONE,
}

#: 阶段与投料状态的**中文文案**。三态词汇由 `common/protocol.py` 定
#: （走线上的是整数），中文只在这里落一次——服务端不往线上搬文案。
STAGE_TEXTS = {
    ORDER_SOURCE_STAGE_WAITING: u"等待中",
    ORDER_SOURCE_STAGE_RUNNING: u"制作中",
    ORDER_SOURCE_STAGE_DONE: u"已完成",
}
FEED_TEXTS = {
    ORDER_SOURCE_FEED_WAITING_INPUTS: u"等待投料",
    ORDER_SOURCE_FEED_WAITING_OUTPUTS: u"等待产物",
    ORDER_SOURCE_FEED_DONE: u"已完成",
}

#: 订单状态的显示文案。**只列界面用得到的几个**，其余原样显示英文词——
#: 认不出来时显示原始值，比显示一个编出来的中文更诚实。
STATUS_TEXTS = {
    "planning": u"规划中",
    "queued": u"排队中",
    "running": u"制作中",
    "blocked": u"受阻",
    "cancelling": u"取消中",
    "completed": u"已完成",
    "cancelled": u"已取消",
    "failed": u"失败",
}

#: 「活跃订单列表 X/Y」里的 X 数的是**正在执行**的那些。协议把状态当文本送
#: （它是显示提示，不是控制输入），客户端只需要认得这一个值。
STATUS_RUNNING = "running"

#: 详情窗：**按需创建**（User 2026-10-03 定），因此它不在 `content.controls` 里，
#: 而是运行期 `AddElement` 出来的。三个常量必须与 JSON 里那个顶层模板**逐字
#: 相同**（`Screen.AddElement` 查不到定义时会抛）。
DETAIL_DEFINITION = "factoric_terminal_panel_ui.order_description_window"
DETAIL_NAME = "endernet_order_detail"
#: 与 `select_item_window` 同一个层：盖住主面板，又不遮住设备状态弹窗（40）。
DETAIL_LAYER = 50

#: 内存文案。**单位是字节（B）**，任务文档（11.md「11-06 的已确认显示需求」）
#: 定的口径；JSON 里那句「剩余可用内存 0KB」是资源初值。
MEMORY_FORMAT = u"剩余可用内存 %s B"
#: 活跃订单提示。含义由 User 2026-10-02 定为「运行中／活跃总数」。
ACTIVE_TIP_FORMAT = u"活跃订单列表 %s/%s"
#: 详情窗标题。静态初值是「当前正在制作： 未知」，这里按同一形状写。
DETAIL_TITLE_FORMAT = u"当前正在制作： %s"

#: 长物品名在 56px 宽的标签里放不下时怎么处置，**本轮不决定**：
#: `plans/ui/factoric_terminal_panel.md` 第 8 节把「长文本处理、名称查询回退
#: 及数量精度」明确标着「待确认／验证」。本轮**不截断**——截到几个字是一句
#: 玩法口径，凭「56px 大概放得下 N 个字」估一个数就是在替它定案。真的溢出时
#: 界面上看得见，而那是要交给 UI 侧去改标签或加省略的。

#: 留多少条回执供诊断。与其余三台终端同一口径与同一理由。
RECEIPT_MEMORY = 8

#: `BF_BindFloat` 与 `BF_BindColor` 的旗标。**与
#: `device_status_bindings.COLOR_BINDING_FLAG` 同一处境**：`Binder` 是引擎在
#: 运行期给的类，本仓库此前没用过浮点旗标，离线也拿不到它的属性表。取不到就
#: **不注册那几条绑定**——进度条与背景色退回模板里的静态值，界面的其余部分
#: 照常工作。直接写 `Binder.BF_BindFloat` 的代价不对称：引擎一旦没有它，它在
#: **导入期**就 `AttributeError`，`register_screens()` 失败，客户端半边整个
#: 起不来。
FLOAT_BINDING_FLAG = getattr(Binder, "BF_BindFloat", None)
COLOR_BINDING_FLAG = getattr(Binder, "BF_BindColor", None)

#: 资源名 → 显示名的缓存。与 `stack_size` 同一条理由（物品的常量，按名字索引，
#: 条目数被物品种类数封顶），但缓存的是**另一个字段**，因此各留一份。
_LABELS = {}


def _bind(flag, method, *args):
    # type: (object, object, object) -> object
    """按旗标注册一条绑定；**旗标缺席时原样返回函数**（即不注册）。

    写成通用的一层，是因为这里要用到三种组合（浮点全局、浮点集合、颜色集合），
    而每一处各抄一遍「取不到就跳过」的代价不是行数，是**漏抄一处就整半边起不来**
    ——那个失败发生在导入期，与用得着用不着的那个旗标毫无关系。
    """
    def decorate(func):
        if flag is None:
            return func
        return method(flag, *args)(func)

    return decorate


def reset_label_cache():
    # type: () -> None
    """清显示名缓存。给测试用——游戏里没有需要清的时刻（同
    `stack_size.reset` 那条）。"""
    _LABELS.clear()


def _text(value):
    # type: (object) -> object
    """把过线来的字节串转成文本，其余原样。

    **必须收 `str`（py2 的字节串）**：服务端那一侧 `ResourceKey.name` 是
    unicode，但过一趟线回来就是 `str`。只认 unicode 的写法会把一个好好的名字
    换成空串，症状是「有订单、画面上没有图标」。
    """
    if isinstance(value, TEXT_TYPE):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def item_label(item_id):
    # type: (object) -> object
    """资源名 → 中文显示名；取不到就退回资源名。

    引擎导入放在函数体内，与 `stack_size` 同一条纪律：模块级导入会让本模块
    离线不可用，而 `endernet_tests` 要能整条跑。
    """
    if not isinstance(item_id, TEXT_TYPE) or not item_id:
        return u""
    cached = _LABELS.get(item_id)
    if cached is not None:
        return cached
    label = item_id
    try:
        from ....tooldelta.define.item import Item
        info = Item(item_id).GetBasicInfo()
        name = _text(getattr(info, "itemName", None))
        if isinstance(name, TEXT_TYPE) and name:
            label = name
    except Exception:
        # **不抛**：拿不到显示名不是错误，退成资源名即可——难看，但不是空。
        label = item_id
    _LABELS[item_id] = label
    return label


def _ratio(done, total):
    # type: (object, object) -> float
    """`done / total`，**分母为 0 时回 0.0**。

    交接第 4 节明令「总数为零或未确定时不得除零或显示虚假的 100%」。
    回 0.0 而不是 1.0：一个还没开始的条应当看起来是空的，不是满的。
    """
    if not isinstance(done, INTEGER_TYPES) or isinstance(done, bool):
        return 0.0
    if not isinstance(total, INTEGER_TYPES) or isinstance(total, bool):
        return 0.0
    if total <= 0:
        return 0.0
    value = float(done) / float(total)
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


class FactoricView(object):
    """订单视图的**客户端显示模型**。引擎无关，可离线整条跑。

    **它不持有任何权威状态**：订单、进度、内存全部照抄服务端推来的
    `order_state`；本地只有两样东西——最近几条回执，和详情窗开着的是哪一单。
    """

    def __init__(self, screen, client=None):
        # type: (object, object) -> None
        self.screen = screen
        self.client = (
            client if client is not None
            else NetworkClient(view_kind=VIEW_ORDER)
        )
        self.closed = False
        #: `{capacity, used, available, slots}`，单位字节。没收到过时全 0，
        #: 界面按 0 画一根空条——**不是「内存是 0」**，是「还没收到」。
        self.memory = {"capacity": 0, "used": 0, "available": 0, "slots": 0}
        #: 活跃订单摘要行（服务端那个形状，原样存）。
        self.orders = ()
        #: 全量配方树，扁平一张表，用 `order_id` 认领。
        self.sources = ()
        #: 详情窗开着的是哪一单，或 `None`。
        self.selected = None
        #: 详情窗那个控件根（按需建，建好留着复用）。**与 `selected` 分开**：
        #: 一个是「用户在看哪一单」，一个是「那扇窗建出来没有」——窗建不出来
        #: 时前者照样要有，否则连「点了哪一行」都记不住。
        self._detail = None
        #: `AddElement` 失败过一次就不再重试（见 `_ensure_detail_window`）。
        self._detail_broken = False
        self.receipts = []
        self.last_error = None
        self.errors = 0
        #: `request_id -> action`，用来认领回执。
        self._requests = {}

    # -- 收包 ----------------------------------------------------------

    def apply(self, message):
        # type: (object) -> bool
        """收一份 `order_state`。**整份替换**，返回是否采纳。

        没有版本号可比：服务端在内容没变时根本不会发，发来的每一条都是
        「现在的真相」。旧包乱序到达时会被更新的那份盖掉——这与物品增量的
        版本比对不同，因为这里**收到即是全量**，不存在「两份要合并」。
        """
        if self.closed:
            return False
        memory = getattr(message, "memory", None)
        if isinstance(memory, dict):
            self.memory = dict(
                (name, _count(memory.get(name)))
                for name in ("capacity", "used", "available", "slots")
            )
        self.orders = _rows(getattr(message, "orders", None))
        self.sources = _rows(getattr(message, "sources", None))
        # 详情开着的那一单没了（结单、被取消）就得关窗：不关的话它会停在
        # 一份永远不会再更新的旧画面上，看起来像界面卡住了。
        if self.selected is not None and not self.has_order(self.selected):
            self.on_detail_close()
        return True

    # -- 订单列表（绑定读的就是这几个） --------------------------------

    def order_count(self):
        # type: () -> int
        """集合要几行。返回**当前活跃订单数**，不是上限。"""
        return len(self.orders)

    def has_order(self, order_id):
        # type: (str) -> bool
        """这一单还在不在推送里。"""
        for row in self.orders:
            if row.get("order_id") == order_id:
                return True
        return False

    def order_row(self, index):
        # type: (object) -> object
        """第 `index` 行订单，越界或下标非法返回 `None`。

        `bool` 与越界一律挡掉：集合回调的下标是外部输入，
        `isinstance(True, int)` 为真，而 `rows[-1]` 不报错、取的是最后一行。
        """
        if isinstance(index, bool) or not isinstance(index, INTEGER_TYPES):
            return None
        if not 0 <= index < len(self.orders):
            return None
        row = self.orders[index]
        return row if isinstance(row, dict) else None

    def order_id(self, index):
        # type: (object) -> str
        """第 `index` 行的**稳定订单标识**。

        交接第 5 节明令「绑定被点击项的稳定订单 ID，不能只传显示下标」：
        下标会随列表增删漂移，而命令要打在对的那一单上。
        """
        row = self.order_row(index)
        if row is None:
            return ""
        value = _text(row.get("order_id"))
        return value if isinstance(value, TEXT_TYPE) else ""

    def order_visible(self, index):
        # type: (object) -> bool
        """有数据才画。**空格子不画**（交接第 4 节：空列表不生成占位订单）。"""
        return self.order_row(index) is not None

    def order_item_id_aux(self, index):
        # type: (object) -> int
        """主产物图标。画不出来时回 0（空图标），不回一个假的。"""
        row = self.order_row(index)
        if row is None:
            return 0
        name = _text(row.get("name"))
        if not isinstance(name, TEXT_TYPE) or not name:
            return 0
        return _item_id_aux(name, row.get("aux"))

    def order_name(self, index):
        # type: (object) -> object
        """「数量 x 显示名」，与 JSON 里那句静态「999 x 某某某」同形。"""
        row = self.order_row(index)
        if row is None:
            return u""
        amount = _count(row.get("amount"))
        name = item_label(_text(row.get("name")))
        return u"%s x %s" % (amount, name)

    def order_progress_text(self, index):
        # type: (object) -> object
        """整体进度文字。**已完成配方任务数 / 订单树配方任务总数**
        （交接第 4 节：同配方执行 10 次计 10 个任务）。"""
        row = self.order_row(index)
        if row is None:
            return u""
        return u"%s/%s" % (
            _count(row.get("tasks_completed")), _count(row.get("tasks_total")))

    def order_progress(self, index):
        # type: (object) -> float
        row = self.order_row(index)
        if row is None:
            return 0.0
        return _ratio(row.get("tasks_completed"), row.get("tasks_total"))

    def order_selected(self, index):
        # type: (object) -> bool
        """详情窗开着的是不是这一单。给选中效果用（JSON 里目前没有该控件，
        留着是为了接入时不至于要改绑定名）。"""
        return self.order_id(index) == self.selected and self.selected is not None

    # -- 配方树（详情窗） ----------------------------------------------

    def source_rows(self):
        # type: () -> tuple
        """**详情窗开着的那一单**的配方树。没开窗时是空表。"""
        if self.selected is None:
            return ()
        return tuple(
            row for row in self.sources
            if isinstance(row, dict) and row.get("order_id") == self.selected
        )

    def source_count(self):
        # type: () -> int
        return len(self.source_rows())

    def source_row(self, index):
        # type: (object) -> object
        rows = self.source_rows()
        if isinstance(index, bool) or not isinstance(index, INTEGER_TYPES):
            return None
        if not 0 <= index < len(rows):
            return None
        return rows[index]

    def source_visible(self, index):
        # type: (object) -> bool
        return self.source_row(index) is not None

    def source_item_id_aux(self, index):
        # type: (object) -> int
        row = self.source_row(index)
        if row is None:
            return 0
        name = _text(row.get("name"))
        if not isinstance(name, TEXT_TYPE) or not name:
            return 0
        return _item_id_aux(name, row.get("aux"))

    def source_name(self, index):
        # type: (object) -> object
        row = self.source_row(index)
        if row is None:
            return u""
        return item_label(_text(row.get("name")))

    def source_stat(self, index):
        # type: (object) -> object
        """「制作阶段 已完成数/总数 投料／产物状态」（交接第 4 节的三要素）。

        完整格式示例：`等待中 0/10 等待投料` / `制作中 3/10 等待产物`。
        """
        row = self.source_row(index)
        if row is None:
            return u""
        stage = STAGE_TEXTS.get(_count(row.get("stage")), u"")
        feed = FEED_TEXTS.get(_count(row.get("feed")), u"")
        return u"%s %s/%s %s" % (
            stage, _count(row.get("done")), _count(row.get("total")), feed)

    def source_progress(self, index):
        # type: (object) -> float
        row = self.source_row(index)
        if row is None:
            return 0.0
        return _ratio(row.get("done"), row.get("total"))

    def source_color(self, index):
        # type: (object) -> list
        """整项背景按阶段变色。**改的是这一项图片本身的 `color`**，
        不是只改进度条或文字（交接第 4 节）。"""
        row = self.source_row(index)
        stage = ORDER_SOURCE_STAGE_RUNNING if row is None else _count(
            row.get("stage"))
        return list(STAGE_COLORS.get(stage, COLOR_RUNNING))

    # -- 内存与提示 ----------------------------------------------------

    def memory_text(self):
        # type: () -> object
        return MEMORY_FORMAT % _count(self.memory.get("available"))

    def memory_ratio(self):
        # type: () -> float
        return _ratio(self.memory.get("available"), self.memory.get("capacity"))

    def active_tip(self):
        # type: () -> object
        """「活跃订单列表 运行中/活跃总数」。"""
        running = 0
        for row in self.orders:
            if isinstance(row, dict) and row.get("status") == STATUS_RUNNING:
                running += 1
        return ACTIVE_TIP_FORMAT % (running, len(self.orders))

    # -- 详情窗 --------------------------------------------------------
    #
    # **它不挂在主面板上，按需创建**（User 2026-10-03 定）。理由有两条：
    #
    # 1. 主面板上多一个 180×200 的常驻实例，就等于每次开界面都白建一棵
    #    200 行的控件树——而绝大多数开界面的人根本不会点开某一单；
    # 2. 省掉「主界面关掉时那个实例还在不在、层还压着谁」这一类状态。
    #
    # 创建之后**留在手里不销毁**：再打开时只是把可见性恢复回去。这一条是
    # User 的原话（「之后再 visible False 再次打开时再恢复可见性」），也是
    # 本仓库 `select_item_window` / `item_list_window` 那两扇子窗口的做法。
    #
    # **可见性走 `SetVisible`，不走绑定**：`#visible` 那条绑定每帧都会重新
    # 求值，与运行期写进去的值互相盖，谁赢取决于引擎那一帧先做哪一步——
    # 表现是「窗口闪一下又没了」。一个属性只有一个写者。

    def detail_visible(self):
        # type: () -> bool
        """详情窗**当前是不是开着的**。给诊断与用例读，不再是绑定。"""
        return self.selected is not None

    def detail_window(self):
        # type: () -> object
        """详情窗那个已经建出来的控件根；**没建过或建失败时返回 `None`**。"""
        return self._detail

    def _ensure_detail_window(self):
        # type: () -> object
        """按需建出详情窗，**建好之后立刻 SetVisible(False)**。

        失败只记一次（`_detail_broken`）：`AddElement` 抛异常时每帧／每次点击
        重试都只会再抛一次，而症状是「点了没反应 + 日志刷屏」。
        建不出来时**订单列表照常工作**，只是点不开详情。
        """
        if self._detail is not None or self._detail_broken:
            return self._detail
        try:
            root = self.screen.AddElement(DETAIL_DEFINITION, DETAIL_NAME)
            # 与 `select_item_window` 同一个层：盖住主面板、又不遮住状态弹窗。
            root.SetLayer(DETAIL_LAYER)
            # **建完先藏起来**（User 2026-10-03 定）。模板里那句 `visible: false`
            # 是资源初值，运行期以这里写进去的为准。
            root.SetVisible(False)
        except Exception:
            self._detail_broken = True
            self.errors += 1
            return None
        self._detail = root
        return root

    def _set_detail_visible(self, visible):
        # type: (bool) -> None
        """开／关详情窗。`SetVisible` 是**唯一**那个写者（见上面那段说明）。

        **关的时候没建过就不建**：为一个没人看过的窗口造一棵控件树，然后在
        同一帧里把它藏起来，是白花的开销。开的时候才建。
        """
        if not visible and self._detail is None:
            return
        root = self._ensure_detail_window()
        if root is None:
            return
        try:
            root.SetVisible(bool(visible))
        except Exception:
            self.errors += 1

    def detail_title(self):
        # type: () -> object
        if self.selected is None:
            return u""
        for row in self.orders:
            if isinstance(row, dict) and row.get("order_id") == self.selected:
                return DETAIL_TITLE_FORMAT % item_label(_text(row.get("name")))
        return u""

    def detail_order_id(self):
        # type: () -> object
        return self.selected or ""

    def status_text(self, order_id):
        # type: (str) -> object
        """订单状态的显示文案（诊断与标题用）。认不出来时给原值。"""
        for row in self.orders:
            if isinstance(row, dict) and row.get("order_id") == order_id:
                status = _text(row.get("status"))
                return STATUS_TEXTS.get(status, status)
        return u""

    # -- 可用性 --------------------------------------------------------

    def is_unavailable(self):
        # type: () -> bool
        """遮罩的判据。**关掉或没有会话时遮**——与另外三台终端相反的那一支
        在这里不适用：那三台是「列表视图缺失就不遮」，因为列表缺失时它们
        仍然在画背包；本界面没有任何本地内容可画，会话没了就是一屏死数据。
        """
        if self.closed:
            return True
        return not self.client.has_session()

    # -- 交互 ----------------------------------------------------------

    def on_order_click(self, index, params=None):
        # type: (object, object) -> None
        """点一行订单：**按需建出**详情窗并打开它。

        **再点同一行不关窗**：那是「我点重了」，不是「我看完了」；
        关窗有它自己的按钮（交接第 5 节：`confirm_btn` 与 `close` 都只关窗）。
        """
        if self.closed or not self.client.has_session():
            return
        order_id = self.order_id(index)
        if not order_id:
            return
        self.selected = order_id
        self._set_detail_visible(True)

    def on_detail_close(self, params=None):
        # type: (object) -> None
        """只关详情窗，**不关主界面、不动订单**（交接第 5 节）。

        `confirm_btn` 与 `close` 走的是同一个回调——两者都只关窗。
        **控件不销毁**：下一次点开只是把可见性恢复回去，不再 `AddElement`。
        """
        self.selected = None
        self._set_detail_visible(False)

    def on_cancel(self, params=None):
        # type: (object) -> None
        """请求取消详情窗里那一单。**不二次确认**（交接第 5 节：直接请求）。

        客户端**不自行宣告取消成功**：命令发出去之后界面照旧，等回执与下一次
        推送。取消是服务端写操作，而「中止」不等于外部机器里的料能回滚。
        """
        if self.closed or self.selected is None:
            return
        if not self.client.has_session():
            return
        self.send(ACTION_CANCEL, order=self.selected)

    def send(self, action, **fields):
        # type: (str, dict) -> str
        """发一条命令，返回 `request_id`；没有会话时返回空串。

        **只在有会话时发**：`NetworkClient.send_command` 无会话会当场抛，
        而绑定回调里抛异常会在渲染期刷屏。判一遍比让它炸要好。
        """
        if self.closed or not self.client.has_session():
            return ""
        try:
            request_id = self.client.send_command(
                action,
                expected_index_version=self.client.index_version,
                **fields
            )
        except Exception:
            # 会话在判完与发出之间失效（另一侧刚推了 `session_gone`）。
            # 这一条命令没发出去，界面下一帧会因 `has_session()` 为假而遮上。
            self.errors += 1
            return ""
        self._requests[request_id] = action
        return request_id

    # -- 回执 ----------------------------------------------------------

    def tick(self):
        # type: () -> None
        """每帧推进。**自己收回执**。

        其余三台终端的回执由 `TerminalListView._drain_receipts` 取走，而本界面
        没有列表视图——不取的话 `NetworkClient._results` 只涨不落，
        `_in_flight` 永远不释放，表现为「取消按过一次之后再也发不出命令」。
        这条队列是「取走即出队」的，**只能有一处取**，因此这里取完不再转给谁。
        """
        if self.closed:
            return
        taken = self.client.take_command_results()
        if not taken:
            return
        for message in taken:
            result = getattr(message, "result", None)
            if not isinstance(result, dict):
                continue
            self.receipts.append(result)
            if result.get("status") != STATUS_OK:
                self.last_error = result
        del self.receipts[:-RECEIPT_MEMORY]

    def on_results(self, taken):
        # type: (list) -> None
        """给「有人替我取好了回执」留的口子（目前没有调用方）。"""
        for message in taken or ():
            result = getattr(message, "result", None)
            if isinstance(result, dict):
                self.receipts.append(result)
        del self.receipts[:-RECEIPT_MEMORY]

    def close(self):
        # type: () -> None
        """关屏的收尾：**先告诉服务端、再摘回调、最后注销端口**。可以重复调用。

        三步的顺序与 `TerminalListView.close` 一致。`close_session` 要趁会话
        还活着发出去（引擎那条 `destroy` 路径只有这一刻能发）——不发的话服务端
        要等 TTL 才回收，而客户端手里那份状态是自己攒的，界面关了没人看，
        但服务端那边多留一条会话就是多一份每帧的开销。

        **没有「摘绑定」这一步**：绑定在类体里、跟着界面走，不跟会话走；
        收尾靠 `closed`——引擎照样回调，而那些回调一律先看 `self.factoric`
        在不在，面板已经把它置空了。
        """
        if self.closed:
            return
        self.closed = True
        # 详情窗先收起来：界面都要没了，留着一扇开着的子窗口只会在下一次
        # 打开时**先闪一帧**，而那一帧里是上一次看的那一单。
        self.on_detail_close()
        self._detail = None
        self.selected = None
        try:
            self.client.close_session()
        except Exception:
            # 会话早就没了（服务端单方面注销过）——那正是「没得关」的正常状态。
            pass
        try:
            self.client.clear_handlers()
            self.client.port.unlisten()
        except Exception:
            pass

    def diagnostics(self):
        # type: () -> dict
        return {
            "closed": self.closed,
            "orders": len(self.orders),
            "sources": len(self.sources),
            "selected": self.selected,
            "detail_window": self._detail is not None,
            "detail_broken": self._detail_broken,
            "memory": dict(self.memory),
            "receipts": len(self.receipts),
            "last_error": self.last_error,
            "errors": self.errors,
            "client": self.client.diagnostics(),
        }


def _rows(value):
    # type: (object) -> tuple
    """过线来的行表。非列表一律当空——**代理层已经校验过形状**，
    这里再判一次只是不让一个坏包把界面炸掉。"""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(row for row in value if isinstance(row, dict))


def _count(value):
    # type: (object) -> int
    """过线来的整数。**拒 bool**（`isinstance(True, int)` 为真），
    其余非整数退成 0——画一个 0 比抛出去好。"""
    if isinstance(value, bool) or not isinstance(value, INTEGER_TYPES):
        return 0
    return int(value)


def _item_id_aux(name, aux):
    # type: (str, object) -> int
    """主产物图标。**带 aux 的走引擎那一条**，取不到时回 0。

    `stack_size.id_aux` 只认资源名（那是堆叠上限的入口），有 aux 的物品
    （羊毛一类）要自己查一次。查不到不是错误：图标空着，文字还在。
    """
    aux_value = _count(aux)
    if not aux_value:
        return stack_size.id_aux(name)
    try:
        from ....tooldelta.define.item import Item
        info = Item(name).GetBasicInfo()
        value = getattr(info, "id_aux", 0)
        if isinstance(value, bool) or not isinstance(value, INTEGER_TYPES):
            return 0
        return int(value)
    except Exception:
        return 0


@RegistToolDeltaScreen("factoric_terminal_panel_ui.main")
class FactoricPanel(TerminalPanelBase):
    """工厂终端：一条网络的订单列表、剩余内存与订单详情。

    接线与 `TemplatePanel.OnCreate` 逐条对应，**少掉的是列表视图与背包**
    （交接第 4 节：本界面没有真实物品槽或玩家背包），多出来的只有
    `factoric` 这一个模型与它那条 `order_state` 收包回调。
    **没有权威身份就整块暂停**（同 11.md 的 UI 前置约定）。
    """

    #: 与 `list_view` / `status` 同一个地位：绑定比 `OnCreate` 先生效，而它
    #: 只在拿到权威身份时才建，因此这里是类属性兜着。没有它时界面上的数字
    #: 全是 0、列表全空——**不是假的**，就是没接到数据。
    factoric = None

    # -- 主界面 --------------------------------------------------------

    @Binder.binding(Binder.BF_BindInt, BINDING_PREFIX + "order_count")
    def order_count(self):
        # type: () -> int
        """订单列表要几行。**这是「现在有几单」，不是上限**——
        JSON 里那个静态 8 只是资源初值（交接第 4 节：不能截断所有运行订单）。"""
        return 0 if self.factoric is None else self.factoric.order_count()

    @Binder.binding(Binder.BF_BindString, BINDING_PREFIX + "memory_text")
    def memory_text(self):
        # type: () -> str
        if self.factoric is None:
            return MEMORY_FORMAT % 0
        return self.factoric.memory_text()

    @_bind(FLOAT_BINDING_FLAG, Binder.binding, BINDING_PREFIX + "memory_ratio")
    def memory_ratio(self):
        # type: () -> float
        """内存条的填充比例。引擎没有浮点旗标时不注册这条（见模块 docstring）。"""
        return 0.0 if self.factoric is None else self.factoric.memory_ratio()

    @Binder.binding(Binder.BF_BindString, BINDING_PREFIX + "active_tip")
    def active_tip(self):
        # type: () -> str
        if self.factoric is None:
            return ACTIVE_TIP_FORMAT % (0, 0)
        return self.factoric.active_tip()

    @Binder.binding(Binder.BF_BindBool, BINDING_PREFIX + "unavailable")
    def unavailable(self):
        # type: () -> bool
        """遮罩。**没有模型时不遮**：绑定比 `OnCreate` 早生效，那时遮上会在
        开屏的第一帧闪一下全黑。真正会话没了的时候模型一定在。"""
        return False if self.factoric is None else self.factoric.is_unavailable()

    # -- 订单行（集合绑定） --------------------------------------------
    #
    # **集合绑定的回调只拿得到集合内下标、拿不到集合名**，所以两片集合各自
    # 一套方法（`terminal_panel.SEGMENTS` 那条）；共用一套的话，配方树会按
    # 订单的下标去读。

    @Binder.binding_collection(
        Binder.BF_BindBool, ORDER_COLLECTION, BINDING_PREFIX + "order_visible")
    def order_visible(self, index):
        # type: (object) -> bool
        return False if self.factoric is None else self.factoric.order_visible(index)

    @Binder.binding_collection(
        Binder.BF_BindInt, ORDER_COLLECTION, BINDING_PREFIX + "order_item_id_aux")
    def order_item_id_aux(self, index):
        # type: (object) -> int
        if self.factoric is None:
            return 0
        return self.factoric.order_item_id_aux(index)

    @Binder.binding_collection(
        Binder.BF_BindString, ORDER_COLLECTION, BINDING_PREFIX + "order_name")
    def order_name(self, index):
        # type: (object) -> str
        return u"" if self.factoric is None else self.factoric.order_name(index)

    @Binder.binding_collection(
        Binder.BF_BindString, ORDER_COLLECTION,
        BINDING_PREFIX + "order_progress_text")
    def order_progress_text(self, index):
        # type: (object) -> str
        if self.factoric is None:
            return u""
        return self.factoric.order_progress_text(index)

    @_bind(FLOAT_BINDING_FLAG, Binder.binding_collection,
           ORDER_COLLECTION, BINDING_PREFIX + "order_progress")
    def order_progress(self, index):
        # type: (object) -> float
        return 0.0 if self.factoric is None else self.factoric.order_progress(index)

    @Binder.binding_collection(
        Binder.BF_ButtonClickUp, ORDER_COLLECTION, BINDING_PREFIX + "order_click")
    def OnOrderClick(self, index, params=None):
        # type: (object, object) -> None
        """点一行订单 → 打开它的详情。

        **绑的是那一行的稳定订单 ID，不是显示下标**（交接第 5 节）：
        视图自己按下标去查当前数据，因此列表中途增删也不会点错单。
        """
        if self.factoric is not None:
            self.factoric.on_order_click(index, params)

    # -- 配方树（详情窗） ----------------------------------------------

    @Binder.binding(Binder.BF_BindInt, BINDING_PREFIX + "source_count")
    def source_count(self):
        # type: () -> int
        return 0 if self.factoric is None else self.factoric.source_count()

    @Binder.binding_collection(
        Binder.BF_BindBool, SOURCE_COLLECTION, BINDING_PREFIX + "source_visible")
    def source_visible(self, index):
        # type: (object) -> bool
        return False if self.factoric is None else self.factoric.source_visible(index)

    @Binder.binding_collection(
        Binder.BF_BindInt, SOURCE_COLLECTION, BINDING_PREFIX + "source_item_id_aux")
    def source_item_id_aux(self, index):
        # type: (object) -> int
        if self.factoric is None:
            return 0
        return self.factoric.source_item_id_aux(index)

    @Binder.binding_collection(
        Binder.BF_BindString, SOURCE_COLLECTION, BINDING_PREFIX + "source_name")
    def source_name(self, index):
        # type: (object) -> str
        return u"" if self.factoric is None else self.factoric.source_name(index)

    @Binder.binding_collection(
        Binder.BF_BindString, SOURCE_COLLECTION, BINDING_PREFIX + "source_stat")
    def source_stat(self, index):
        # type: (object) -> str
        return u"" if self.factoric is None else self.factoric.source_stat(index)

    @_bind(FLOAT_BINDING_FLAG, Binder.binding_collection,
           SOURCE_COLLECTION, BINDING_PREFIX + "source_progress")
    def source_progress(self, index):
        # type: (object) -> float
        return 0.0 if self.factoric is None else self.factoric.source_progress(index)

    @_bind(COLOR_BINDING_FLAG, Binder.binding_collection,
           SOURCE_COLLECTION, BINDING_PREFIX + "source_color")
    def source_color(self, index):
        # type: (object) -> list
        """整项背景按制作阶段变色（交接第 4 节：三阶段三色）。"""
        if self.factoric is None:
            return list(COLOR_RUNNING)
        return self.factoric.source_color(index)

    # -- 详情窗 --------------------------------------------------------

    #: **详情窗的显隐没有绑定**：那扇窗是运行期建出来的，而它的开关由
    #: `FactoricView._set_detail_visible` 直接写 `SetVisible`——一个属性只留
    #: 一个写者。多一条 `#visible` 绑定的话两边会互相盖，谁赢取决于引擎那一帧
    #: 先做哪一步，表现是「窗口闪一下又没了」。

    @Binder.binding(Binder.BF_BindString, BINDING_PREFIX + "detail_title")
    def detail_title(self):
        # type: () -> str
        return u"" if self.factoric is None else self.factoric.detail_title()

    @Binder.binding(Binder.BF_ButtonClickUp, BINDING_PREFIX + "cancel")
    def OnCancelOrder(self, params=None):
        # type: (object) -> None
        """取消订单：**直接请求，不二次确认**（交接第 5 节）。"""
        if self.factoric is not None:
            self.factoric.on_cancel(params)

    @Binder.binding(Binder.BF_ButtonClickUp, BINDING_PREFIX + "detail_close")
    def OnDetailClose(self, params=None):
        # type: (object) -> None
        """详情窗的关闭与确认按钮**走同一个回调**：两者都只关窗，
        不提交、不完成、不取消订单（交接第 5 节）。"""
        if self.factoric is not None:
            self.factoric.on_detail_close(params)

    # -- 生命周期 ------------------------------------------------------

    def OnCreate(self):
        # **不调 `TerminalPanelBase.OnCreate`**：它只做一件事——把引擎里的
        # 玩家背包读进 `BackpackModel`。本界面没有背包控件（交接第 4 节），
        # 读一份没人看的数据是白花的每帧开销。
        params = self._init_params
        ref = params.get("device_ref")
        if ref is None:
            return
        try:
            device_ref = DeviceRef.from_dict(ref) if isinstance(ref, dict) else ref
        except (KeyError, TypeError, ValueError):
            # 身份破了就按「没有权威身份」处理：拿半个位置去开会话只会换来
            # 一条被拒的请求，症状与「终端没反应」完全一样。
            return
        if not isinstance(device_ref, DeviceRef):
            return
        dimension = params.get("dimension", 0)
        #: 视图种类是 `VIEW_ORDER`（见模块 docstring 第 14 行）。原来这里
        #: 写成 `VIEW_ORDER_KIND`——那个名字**全仓都没有过**，连 HEAD 里
        #: 也没有，于是工厂终端一打开就 `NameError`，整块屏建不起来。
        client = NetworkClient(view_kind=VIEW_ORDER)
        # 先挂收包再开会话：`session_ready` 与第一份 `order_state` 都可能比
        # 这个函数返回还早（同 `CommonTerminalPanel` 那条注释）。
        client.port.listen(client.receive)
        self.status = device_status.DeviceStatusModel(device_ref)
        client.on_message(self._make_status_handler(client))
        self.factoric = FactoricView(self, client=client)
        client.on_message(self._make_order_handler(client))
        client.open_session(device_ref, dimension)

    def _make_order_handler(self, client):
        # type: (object) -> object
        """收 `order_state` 的回调。

        两道过滤与另三台终端完全一样，缺一不可：会话号对不上就丢（旧会话的
        包可能在关掉之后才到）。回调**不抛**：它在 `receive()` 的派发循环里
        跑，抛出去会连累同一批里的其它消息。
        """
        def on_order_message(envelope, message):
            # type: (object, object) -> None
            if message is None or message.kind != MSG_ORDER_STATE:
                return
            if envelope.session_id != client.session_id:
                return
            if self.factoric is not None:
                self.factoric.apply(message)

        return on_order_message

    def OnTicking(self):
        # **基类的背包轮询与飞行动画这里都不推进**：本界面没有背包、也没有
        # `flying_item` 控件（交接第 4 节），两个动画器都是「缺控件就永久
        # 退化成什么也不做」，每帧调一次只是白跑。
        if self.factoric is not None:
            self.factoric.tick()

    def OnDeactive(self):
        # 界面隐藏后 `OnTicking` 不再跑，详情窗留着会在下次打开时**先显示
        # 一帧**，而那一帧里是上一次看的订单。与基类收弹窗同一条理由。
        if self.factoric is not None:
            self.factoric.on_detail_close()
        TerminalPanelBase.OnDeactive(self)

    def OnDestroy(self):
        if self.factoric is not None:
            self.factoric.close()
            self.factoric = None
        self.status = None
        TerminalPanelBase.OnDestroy(self)

    @classmethod
    def OpenDemo(cls):
        """客户端手工自查用：在 `UiInitFinishedEvent` 注册过界面之后调。

        **没有权威身份，因此整块暂停**（同 `TemplatePanel.OpenDemo`）：
        订单列表、内存、详情全空着。要走通整条链路就得由服务端
        `PushUIRequest` 推过来（右键工厂终端）。
        """
        return cls.PushUI({})
