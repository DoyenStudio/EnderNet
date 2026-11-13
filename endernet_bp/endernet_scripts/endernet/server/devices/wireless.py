# coding=utf-8
"""无线连接器与无线连接工具（2026-11-13，`.claude/17` §5，照 ExtendedAE 的 ME 无线连接器）。

## 无线连接器

整方块载体（`definitions.WIRELESS_CONNECTOR_ID`，分类 `wireless`）：六面照致密光缆连（不接主控），
线路树上与致密光缆同一档（32u，不占带宽）。用无线连接工具与另一台配对后，两台之间多一条远程
连边（`links_of`，经 `TopologyService.add_remote_links` 登记），两边合成一张网。

**配对数据**：每台的方块实体里有一个实例标记（复用终端库存标记那个字段，
`world.terminal_inventory_id`：新放下的方块就是新标记）与对端的 `"维度,x,y,z,标记"`
（`world.wireless_link`，没配对为空串）。注册时读进记录的 `config`（`read_config`，生命周期的
config 读口），之后改配对先写方块实体、写成了才改记录，拓扑探邻只读记录。

**互指才算配对**：A 记着 B 的位置与标记、B 也记着 A 的，两台都已加载、在同一维度、相距不超过
`definitions.wireless_max_distance()`（256 格）。对端被拆了再放一台，标记不同，配对自然失效，
不需要额外清理。远端没加载时这条边不存在：远端不是成员，不触发「成员未加载整网离线」，近端照
自身情况判定（不做区块常驻）。一对一：再与第三台配对时，先解除双方旧的那一对。

**耗能**：每台 `4 + 每个距离单位 1 EN/t`，单位数 `wireless_distance_units(距离)`
（`distance_units`，装配层的 `_energy_inputs_of` 交给能源服务）；没配对为 0。

方块实体刚加载时可能还读不到（区块边缘读成空气，照光缆的处理）：记录先打上
`CONFIG_PENDING`，隔 `SETTLE_TICKS` 帧再读，最多读 `READ_ATTEMPTS` 次（`advance`）。

## 无线连接工具（`WIRELESS_LINK_TOOL_ID`，意图见 `common/channels/cable_intent.py`）

| 手持工具 | 点到 | 结果 |
| --- | --- | --- |
| 右键（`wireless_link`） | 一台无线连接器，工具空着 | 选中它：工具记下位置与标记 |
| 右键 | 另一台，工具记着一台 | 同维度、不超距、选中的那台还在且已加载 → 两台互写配对，清空工具 |
| 潜行右键（`wireless_unlink`） | 一台无线连接器 | 解除它的配对（对端还指着它就一并清掉） |
| 潜行右键 | 别处（任何方块） | 清空工具 |

工具记的东西在 `userData` 的 `endernet_link` 一项 `{"link": "维度,x,y,z,标记"}`，物品说明在原说明
后面加一行待配对的位置（读写经 `interaction.ItemExchange`，与配置卡共用）。
"""

import math

from ...common.channels.cable_intent import ACTION_WIRELESS_LINK, ACTION_WIRELESS_UNLINK
from ...common.definitions import (
    WIRELESS_CONNECTOR_ID,
    WIRELESS_LINK_TOOL_ID,
    wireless_distance_units,
)
from ...common.identity import NodeKey
from ..interaction import ItemExchange, instance_tips
from ..runtime import ChangeKind
from .cable_intents import IntentResult

#: 记录 `config` 里的键：实例标记、对端（`"维度,x,y,z,标记"`，没配对为空串）、方块实体还没读到
CONFIG_MARK = "wireless_mark"
CONFIG_LINK = "wireless_link"
CONFIG_PENDING = "wireless_pending"

#: 工具 `userData` 里本模组那一项
TOOL_DATA_KEY = "endernet_link"
TOOL_LINK = "link"

#: 方块实体读不到时隔多少帧再读、最多读几次（照光缆的 `chunk_settle_ticks`）
SETTLE_TICKS = 30
READ_ATTEMPTS = 5

#: 弹出提示的副标题
TOOL_TITLE = "§6无线连接工具§f"

#: 处理结果（诊断按它计数）
REASON_SELECTED = "selected"
REASON_LINKED = "linked"
REASON_UNLINKED = "unlinked"
REASON_TOOL_CLEARED = "tool_cleared"
REASON_NOT_A_CONNECTOR = "not_a_connector"
REASON_CONNECTOR_NOT_READY = "connector_not_ready"
REASON_SAME_CONNECTOR = "same_connector"
REASON_OTHER_DIMENSION = "other_dimension"
REASON_TOO_FAR = "too_far"
REASON_NOT_LINKED = "not_linked"
REASON_TOOL_UNREADABLE = "tool_unreadable"
REASON_LINK_WRITE_FAILED = "link_write_failed"
REASON_TOOL_WRITE_FAILED = "tool_write_failed"

try:  # pragma: no cover - 仅在 Python 2 下成立
    _TEXT_TYPES = (str, unicode)
except NameError:  # pragma: no cover - 仅在 Python 3 下成立
    _TEXT_TYPES = (str,)


def link_text(node_key, mark):
    # type: (NodeKey, str) -> str
    """对端 → 方块实体与工具里存的文本 `"维度,x,y,z,标记"`。"""
    return str("%d,%d,%d,%d,%s" % (
        node_key.dimension, node_key.x, node_key.y, node_key.z, mark))


def parse_link(text):
    # type: (object) -> object
    """`link_text` 的逆：`(NodeKey, 标记)`；空串或读不懂返回 None。"""
    if not isinstance(text, _TEXT_TYPES) or not text:
        return None
    pieces = text.split(",")
    if len(pieces) != 5 or not pieces[4]:
        return None
    try:
        dimension, x, y, z = [int(piece) for piece in pieces[:4]]
    except ValueError:
        return None
    return NodeKey(dimension, x, y, z), str(pieces[4])


def distance_between(first, second):
    # type: (NodeKey, NodeKey) -> float
    """两格方块中心的直线距离（格）。"""
    return math.sqrt(
        (first.x - second.x) ** 2 + (first.y - second.y) ** 2 + (first.z - second.z) ** 2
    )


def position_text(node_key):
    # type: (NodeKey) -> str
    return "(%d, %d, %d)" % (node_key.x, node_key.y, node_key.z)


class WirelessService(object):
    """无线连接器的配对、远程连边、按距离的耗能，以及无线连接工具。可以脱离引擎构造。

    - `topology`：`mark_dirty`（配对变了，两端重建）；
    - `world`：`terminal_inventory_id`（实例标记）、`wireless_link` / `write_wireless_link`；
    - `items`：`interaction.ItemExchange`（工具的读写）；
    - `clock`：`current_tick()`，方块实体重读的节奏。
    """

    def __init__(self, definitions, registry, topology, world, items=None, clock=None):
        self.definitions = definitions
        self.registry = registry
        self.topology = topology
        self.world = world
        self.items = items if items is not None else ItemExchange(None, world)
        self.clock = clock
        #: 方块实体待重读：NodeKey -> (到期的帧, 已读次数)
        self._due = {}
        self.stats = dict.fromkeys(
            ("entity_reads", "entity_retries", "entity_unreadable", "link_writes",
             "link_write_failures"),
            0,
        )

    # -- 登记 ------------------------------------------------------------

    def register(self, intents):
        # type: (object) -> None
        tool = self.definitions.get_tool(WIRELESS_LINK_TOOL_ID).label
        hint = "要手持%s" % (tool,)
        intents.register(
            ACTION_WIRELESS_LINK, self.link, self.is_tool, hint, any_block=True, needs_hit=False)
        intents.register(
            ACTION_WIRELESS_UNLINK, self.unlink, self.is_tool, hint, any_block=True,
            needs_hit=False)

    @staticmethod
    def is_tool(item_id):
        # type: (str) -> bool
        return item_id == WIRELESS_LINK_TOOL_ID

    @staticmethod
    def is_connector(record):
        # type: (object) -> bool
        return record is not None and record.device_id == WIRELESS_CONNECTOR_ID

    # -- 方块实体 ----------------------------------------------------------

    def read_config(self, node_key, device_id):
        # type: (object, str) -> object
        """注册时的 config（生命周期 config 读口的一份）：不是无线连接器返回 None；方块实体
        还拿不到 → 打上 `CONFIG_PENDING`，稍后重读（`observe` 排进 `advance`）。"""
        if device_id != WIRELESS_CONNECTOR_ID:
            return None
        fields = self._read_entity(node_key)
        if fields is None:
            return {CONFIG_PENDING: True}
        return {CONFIG_MARK: fields[0], CONFIG_LINK: fields[1]}

    def _read_entity(self, node_key):
        # type: (object) -> object
        """`(实例标记, 配对文本)`；读不到返回 None。"""
        self.stats["entity_reads"] += 1
        try:
            mark = self.world.terminal_inventory_id(node_key)
            link = self.world.wireless_link(node_key)
        except Exception:
            return None
        if mark is None or link is None:
            return None
        return str(mark), str(link)

    def observe(self, change):
        # type: (object) -> None
        """同一批设备变化：方块实体没读到的连接器排一次重读；拆掉的不再读。"""
        if change.device_id != WIRELESS_CONNECTOR_ID or change.node_key is None:
            return
        if change.kind == ChangeKind.REMOVED:
            self._due.pop(change.node_key, None)
            return
        if change.kind not in (ChangeKind.ADDED, ChangeKind.LOADED):
            return
        record = self.registry.get(change.node_key)
        if record is not None and record.config.get(CONFIG_PENDING):
            self._due[change.node_key] = (self._now() + SETTLE_TICKS, 0)

    def advance(self, tick=None):
        # type: (int) -> int
        """到期的连接器重读方块实体，读到了就写进记录、两端标脏。返回读了几台。"""
        if not self._due:
            return 0
        now = self._now() if tick is None else tick
        done = 0
        for node_key in sorted((key for key, due in self._due.items() if due[0] <= now),
                               key=lambda key: key.as_key()):
            _ready, attempts = self._due.pop(node_key)
            record = self.registry.get(node_key)
            if not self.is_connector(record) or not record.config.get(CONFIG_PENDING):
                continue
            done += 1
            fields = self._read_entity(node_key)
            if fields is None:
                if attempts + 1 < READ_ATTEMPTS:
                    self._due[node_key] = (now + SETTLE_TICKS, attempts + 1)
                    self.stats["entity_retries"] += 1
                else:
                    self.stats["entity_unreadable"] += 1
                continue
            if self.registry.update_config(node_key, {
                CONFIG_MARK: fields[0], CONFIG_LINK: fields[1], CONFIG_PENDING: None,
            }):
                self._mark(node_key, parse_link(fields[1]))
        return done

    def _now(self):
        # type: () -> int
        return self.clock.current_tick() if self.clock is not None else 0

    # -- 配对 ------------------------------------------------------------

    def _ready(self, record):
        # type: (object) -> bool
        """已加载、方块实体读到了、有实例标记的连接器。"""
        return (
            self.is_connector(record)
            and record.loaded
            and not record.config.get(CONFIG_PENDING)
            and bool(record.config.get(CONFIG_MARK))
        )

    def partner_of(self, node_key):
        # type: (object) -> object
        """配对成立时的对端（`NodeKey`）：互指、标记对得上、两端已加载、同维度、不超距；
        否则 None。"""
        record = self.registry.get(node_key)
        if not self._ready(record):
            return None
        link = parse_link(record.config.get(CONFIG_LINK))
        if link is None:
            return None
        partner_key, partner_mark = link
        if partner_key == node_key or partner_key.dimension != node_key.dimension:
            return None
        partner = self.registry.get(partner_key)
        if not self._ready(partner) or partner.config.get(CONFIG_MARK) != partner_mark:
            return None
        if parse_link(partner.config.get(CONFIG_LINK)) != (node_key, record.config.get(CONFIG_MARK)):
            return None
        if distance_between(node_key, partner_key) > self.definitions.wireless_max_distance():
            return None
        return partner_key

    def links_of(self, node_key):
        # type: (object) -> list
        """远程连边的提供者：配对成立就连对端，不经隧道段（连接器本身是 32u 载体）。"""
        partner = self.partner_of(node_key)
        return [(partner, None)] if partner is not None else []

    def distance_units(self, ref):
        # type: (object) -> object
        """这台连接器按配对距离计耗能的单位数；没配对为 0，方块实体还没读到为 None（待确认）。"""
        record = self.registry.get_by_ref(ref)
        if record is None or record.config.get(CONFIG_PENDING):
            return None
        partner = self.partner_of(record.node_key)
        if partner is None:
            return 0
        return wireless_distance_units(distance_between(record.node_key, partner))

    def pair(self, first, second):
        # type: (object, object) -> bool
        """两台互写配对。各自旧的那一对先解除（对端还指着它们就一并清掉）。写不进去返回 False
        （先写成的那一台退回没配对）。"""
        both = set([first.node_key, second.node_key])
        for record in (first, second):
            old = parse_link(record.config.get(CONFIG_LINK))
            if old is not None and old[0] not in both:
                self._clear_back_link(old[0], both)
        if not self._write_link(first.node_key, link_text(second.node_key, second.config[CONFIG_MARK])):
            return False
        if not self._write_link(second.node_key, link_text(first.node_key, first.config[CONFIG_MARK])):
            self._write_link(first.node_key, "")
            return False
        return True

    def _clear_back_link(self, node_key, targets):
        # type: (object, set) -> None
        """`node_key` 那台还指着 `targets` 里的某一台，就把它清掉（没加载的写不了，留着无害：
        互指不成立）。"""
        record = self.registry.get(node_key)
        if not self._ready(record):
            return
        link = parse_link(record.config.get(CONFIG_LINK))
        if link is not None and link[0] in targets:
            self._write_link(node_key, "")

    def _write_link(self, node_key, text):
        # type: (object, str) -> bool
        """先写方块实体、写成了才改记录，然后这台、它原来的对端与新的对端都标脏。"""
        record = self.registry.get(node_key)
        if record is None:
            return False
        writer = getattr(self.world, "write_wireless_link", None)
        ok = False
        if writer is not None:
            try:
                ok = bool(writer(node_key, text))
            except Exception:
                ok = False
        if not ok:
            self.stats["link_write_failures"] += 1
            return False
        self.stats["link_writes"] += 1
        old = parse_link(record.config.get(CONFIG_LINK))
        self.registry.update_config(node_key, {CONFIG_LINK: text})
        self._mark(node_key, old)
        self._mark(None, parse_link(text))
        return True

    def _mark(self, node_key, link):
        # type: (object, object) -> None
        for key in (node_key, link[0] if link is not None else None):
            if key is not None and self.registry.get(key) is not None:
                self.topology.mark_dirty(key)

    # -- 无线连接工具 --------------------------------------------------------

    def link(self, context):
        # type: (object) -> IntentResult
        """右键连接器：工具空着就选中它；记着另一台就配对。"""
        record = context.record
        if not self.is_connector(record):
            return IntentResult(False, REASON_NOT_A_CONNECTOR, "这不是无线连接器", TOOL_TITLE)
        if not self._ready(record):
            return IntentResult(
                False, REASON_CONNECTOR_NOT_READY, "这台无线连接器还没加载好，请稍后再试", TOOL_TITLE)
        data = self.items.held_data(context.player_id, WIRELESS_LINK_TOOL_ID, TOOL_DATA_KEY)
        if data is None:
            return IntentResult(False, REASON_TOOL_UNREADABLE, "读不到无线连接工具，请再试一次", TOOL_TITLE)
        node_key = record.node_key
        chosen = parse_link(data.get(TOOL_LINK))
        if chosen is None:
            return self._select(
                context, record, "已选中无线连接器 %s，再右键另一台完成配对" % (position_text(node_key),))
        chosen_key, chosen_mark = chosen
        if chosen_key == node_key and chosen_mark == record.config.get(CONFIG_MARK):
            return IntentResult(
                False, REASON_SAME_CONNECTOR, "这就是选中的那台，再右键另一台完成配对", TOOL_TITLE)
        other = self.registry.get(chosen_key)
        if not self._ready(other) or other.config.get(CONFIG_MARK) != chosen_mark:
            return self._select(
                context, record,
                "选中的那台无线连接器已经不在了或所在区块没加载，已改为选中 %s" % (position_text(node_key),))
        if chosen_key.dimension != node_key.dimension:
            return IntentResult(
                False, REASON_OTHER_DIMENSION, "两台无线连接器不在同一个维度，不能配对", TOOL_TITLE)
        distance = distance_between(chosen_key, node_key)
        limit = self.definitions.wireless_max_distance()
        if distance > limit:
            return IntentResult(
                False, REASON_TOO_FAR,
                "两台相距 %d 格，超过 %d 格的上限" % (int(round(distance)), int(limit)), TOOL_TITLE)
        if not self.pair(other, record):
            return IntentResult(False, REASON_LINK_WRITE_FAILED, "没有写进去，请再试一次", TOOL_TITLE)
        message = "已连接 %s 与 %s，相距 %d 格" % (
            position_text(chosen_key), position_text(node_key), int(round(distance)))
        if not self._write_tool(context, None):
            message += "（工具没有清空，请潜行右键别处清空）"
        return IntentResult(True, REASON_LINKED, message, TOOL_TITLE)

    def unlink(self, context):
        # type: (object) -> IntentResult
        """潜行右键：连接器解除配对；别处清空工具。"""
        record = context.record
        if not self.is_connector(record):
            if not self._write_tool(context, None):
                return IntentResult(
                    False, REASON_TOOL_WRITE_FAILED, "工具没有写进去，请再试一次", TOOL_TITLE)
            return IntentResult(True, REASON_TOOL_CLEARED, "已清空无线连接工具", TOOL_TITLE)
        if not self._ready(record):
            return IntentResult(
                False, REASON_CONNECTOR_NOT_READY, "这台无线连接器还没加载好，请稍后再试", TOOL_TITLE)
        link = parse_link(record.config.get(CONFIG_LINK))
        if link is None:
            return IntentResult(False, REASON_NOT_LINKED, "这台无线连接器没有配对", TOOL_TITLE)
        self._clear_back_link(link[0], set([record.node_key]))
        if not self._write_link(record.node_key, ""):
            return IntentResult(False, REASON_LINK_WRITE_FAILED, "没有写进去，请再试一次", TOOL_TITLE)
        return IntentResult(
            True, REASON_UNLINKED, "已解除与 %s 的配对" % (position_text(link[0]),), TOOL_TITLE)

    def _select(self, context, record, message):
        # type: (object, object, str) -> IntentResult
        if not self._write_tool(context, record):
            return IntentResult(
                False, REASON_TOOL_WRITE_FAILED, "工具没有写进去，请再试一次", TOOL_TITLE)
        return IntentResult(True, REASON_SELECTED, message, TOOL_TITLE)

    def _write_tool(self, context, record):
        # type: (object, object) -> bool
        """工具记下 `record` 那台（None = 清空），物品说明随之改写。"""
        value = None
        pending = ""
        if record is not None:
            value = {TOOL_LINK: link_text(record.node_key, record.config[CONFIG_MARK])}
            pending = "§b待配对：%s" % (position_text(record.node_key),)
        tip = instance_tips("§7" + self.definitions.get_tool(WIRELESS_LINK_TOOL_ID).tip, pending)
        return self.items.set_held_data(
            context.player_id, WIRELESS_LINK_TOOL_ID, TOOL_DATA_KEY, value, tip)

    def diagnostics(self):
        # type: () -> dict
        return dict(self.stats, queued=len(self._due))
