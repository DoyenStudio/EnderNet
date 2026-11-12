# coding=utf-8
"""P2P 网络隧道（2026-11-13，`.claude/17` §4）：频率、一入多出、配置卡，以及拓扑的远程连边。

## 隧道是什么

面板形态（`definitions.TUNNEL_PARTS`），挂在玻璃或普通光缆的一个面上，占宿主线路 1u、2 EN/t。
每块隧道在自己那一面的面板配置（`PanelService.part_config`，宿主方块实体
`endernet:part_config_<面>`）里记 `{"freq": "3F2A", "role": "input" | "output"}`，键和值都是
`str`；挂上一块新隧道时配置被清空（`PanelService._fresh_face`），拆下来不带走频率（同 AE2）。

**一入多出**：同一频率的隧道是一组；组里恰好一个输入时，各端**正面那一格**（`PartKey.front_key()`）
两两连通——这就是拓扑的远程连边（`links_of`，经 `TopologyService.add_remote_links` 登记）。
没有输入、或出现第二个输入（复制方块实体之类），整组不通。宿主光缆本身不经隧道连出去：面板
占住这一面，宿主线芯照旧不外连。

**正面那一格能不能接**：要有设备、已加载（光缆还要读到了方块实体），分类照光缆的连接表能接
（光缆、设备、无线连接器、处理器连接器；面板底座、处理器不行），而且朝隧道那一面开着（没被扳手
切断、没挂面板）。基带与主控不接：基带的外露面是线路的根，隧道正面对着基带时请隔一格致密光缆。

**结构连通、准入把关**（评估的 T1）：组接通了，两端正面后面的东西就算同一张网；宿主网络有没有
给隧道面板分到带宽、能不能运行，只在准入层决定隧道后面的设备能不能工作
（`network/admission.py` 的隧道把关，问这里的 `endpoints_at`）。

## 派生索引（不存档）

只记**已加载**的端点：宿主记录在、已加载、读到了方块实体，那一面挂着隧道且配置读得出频率与
角色。频率与角色另有一份缓存（`_configs`，宿主卸载时也留着，重新加载不必再读方块实体）。

刷新时机（`refresh_host`）：宿主的生命周期变化（放下、加载、卸载、拆掉、原地换方块）、按面数据
变了（挂上 / 拆下面板、重读方块实体，`ServerBootstrap._on_cable_faces_changed`）、面板离开
（`PART_DETACHED`，缓存一并丢掉）、配置卡改写。一组的成员或接通与否变了，就把**这一组前后所有
端点的正面格**标脏（`topology.mark_dirty`），拓扑重建时再来问 `links_of`。

## 配置卡（`CONFIG_CARD_ID`，意图见 `common/channels/cable_intent.py`）

| 手持配置卡 | 点到 | 结果 |
| --- | --- | --- |
| 潜行右键（`tunnel_input`） | 一块隧道 | 它已是某组输入：把频率记进卡；否则生成一个没人用的新频率、设为输入、记进卡 |
| 潜行右键 | 别处（任何方块） | 清空卡 |
| 右键（`tunnel_output`） | 一块隧道 | 设为卡里那个频率的输出；点到的正是本组输入时不改 |

卡里只存频率：`userData` 的 `endernet_card` 一项 `{"kind": "tunnel", "freq": "3F2A"}`，物品说明
在原说明后面加一行频率（读写经 `interaction.ItemExchange.held_data` / `set_held_data`，与无线连接
工具共用）。点空气没有方块事件，不清卡。
"""

import random

from ...common.cable_shapes import PART_PANEL
from ...common.channels.cable_intent import ACTION_TUNNEL_INPUT, ACTION_TUNNEL_OUTPUT
from ...common.definitions import (
    CATEGORY_BASEBAND,
    CATEGORY_CABLE,
    CATEGORY_CONTROLLER,
    CONFIG_CARD_ID,
    PART_KIND_TUNNEL,
    ConnectionRules,
)
from ...common.identity import FACING_NAMES, PartKey, opposite_face
from ..interaction import ItemExchange
from ..runtime import ChangeKind
from .cable_intents import IntentResult
from .cables import is_pending

#: 角色
ROLE_INPUT = "input"
ROLE_OUTPUT = "output"
ROLES = (ROLE_INPUT, ROLE_OUTPUT)

#: 面板配置里的两个键
CONFIG_FREQ = "freq"
CONFIG_ROLE = "role"

#: 配置卡 `userData` 里本模组那一项，以及它的种类（只存隧道频率）
CARD_DATA_KEY = "endernet_card"
CARD_KIND = "kind"
CARD_KIND_TUNNEL = "tunnel"

#: 频率是 16 位，显示成四位十六进制
FREQ_LIMIT = 1 << 16
#: 随机挑新频率的次数，挑不到再按顺序找
_RANDOM_TRIES = 64

#: 一组的状态
GROUP_LINKED = "linked"
GROUP_NO_INPUT = "no_input"
GROUP_MULTIPLE_INPUTS = "multiple_inputs"
GROUP_NO_OUTPUT = "no_output"

#: 隧道正面对着它们就不接（见模块说明）
_CLOSED_FRONT_CATEGORIES = frozenset([CATEGORY_BASEBAND, CATEGORY_CONTROLLER])

#: 处理结果（诊断按它计数）
REASON_TUNNEL_INPUT = "tunnel_input"
REASON_TUNNEL_OUTPUT = "tunnel_output"
REASON_CARD_CLEARED = "card_cleared"
REASON_CARD_EMPTY = "card_empty"
REASON_CARD_UNREADABLE = "card_unreadable"
REASON_CARD_WRITE_FAILED = "card_write_failed"
REASON_NOT_A_TUNNEL = "not_a_tunnel"
REASON_ALREADY_INPUT = "already_input"
REASON_NO_FREE_FREQUENCY = "no_free_frequency"
REASON_TUNNEL_WRITE_FAILED = "tunnel_write_failed"

#: 弹出提示的副标题
CARD_TITLE = "§6配置卡§f"

try:  # pragma: no cover - 仅在 Python 2 下成立
    _TEXT_TYPES = (str, unicode)
except NameError:  # pragma: no cover - 仅在 Python 3 下成立
    _TEXT_TYPES = (str,)

_HEX_DIGITS = frozenset("0123456789ABCDEF")


def _as_key(key):
    return key.as_key()


def format_freq(number):
    # type: (int) -> str
    """频率编号 → 四位十六进制（大写），如 `3F2A`。"""
    return str("%04X" % (int(number) % FREQ_LIMIT))


def parse_freq(value):
    # type: (object) -> object
    """读到的频率文本 → 规范写法（`str`，四位大写十六进制）；不是合法频率返回 None。"""
    if not isinstance(value, _TEXT_TYPES):
        return None
    text = value.strip().upper()
    if len(text) != 4 or any(char not in _HEX_DIGITS for char in text):
        return None
    return str(text)


def endpoint_of_config(config):
    # type: (object) -> object
    """面板配置 → `(频率, 角色)`；没设过（新挂上、被清空）或读不懂返回 None。"""
    if not isinstance(config, dict):
        return None
    freq = parse_freq(config.get(CONFIG_FREQ))
    role = config.get(CONFIG_ROLE)
    if freq is None or role not in ROLES:
        return None
    return freq, str(role)


class TunnelService(object):
    """频率索引、远程连边与配置卡。可以脱离引擎构造：世界读写经 `panels`（面板配置）与
    `items`（主手物品数据）。

    - `topology`：`mark_dirty`（组变了，端点正面格重建）；
    - `panels`：`PanelService`（`part_config` / `set_part_config`、`register_kind`）；
    - `items`：`interaction.ItemExchange`（配置卡的读写）。
    """

    def __init__(self, definitions, registry, catalog, topology, panels, items=None, rng=None):
        self.definitions = definitions
        self.registry = registry
        self.catalog = catalog
        self.topology = topology
        self.panels = panels
        self.items = items if items is not None else ItemExchange(None, None)
        self._random = rng if rng is not None else random.Random()
        #: PartKey -> (频率, 角色) 或 None（读过、没设）：面板配置的缓存，宿主卸载也留着
        self._configs = {}
        #: 宿主 NodeKey -> {PartKey: (频率, 角色)}：已加载的端点
        self._hosts = {}
        #: 频率 -> {PartKey: 角色}
        self._groups = {}
        #: 正面格 NodeKey -> set(PartKey)
        self._fronts = {}
        self.stats = dict.fromkeys(
            (
                "refreshes",
                "fronts_marked",
                "config_unreadable",
                "frequencies_issued",
            ),
            0,
        )

    # -- 登记 ------------------------------------------------------------

    def register(self, intents):
        # type: (object) -> None
        """隧道这一种面板（没有界面：右键点中它不认领，手里的方块照常放下），以及配置卡的两个
        动作。"""
        self.panels.register_kind(PART_KIND_TUNNEL)
        card = self.definitions.get_tool(CONFIG_CARD_ID).label
        hint = "要手持%s" % (card,)
        intents.register(
            ACTION_TUNNEL_INPUT, self.card_input, self.is_card, hint,
            any_block=True, needs_hit=False,
        )
        intents.register(ACTION_TUNNEL_OUTPUT, self.card_output, self.is_card, hint, needs_hit=False)

    @staticmethod
    def is_card(item_id):
        # type: (str) -> bool
        return item_id == CONFIG_CARD_ID

    def is_tunnel(self, part_id):
        # type: (str) -> bool
        return self.definitions.part_kind_of(part_id) == PART_KIND_TUNNEL

    # -- 生命周期 ----------------------------------------------------------

    def observe(self, change):
        # type: (object) -> None
        """`ServerBootstrap._emit` 的同一批设备变化：宿主变了就刷新它的端点。"""
        kind = change.kind
        node_key = change.node_key
        if node_key is None:
            return
        if kind == ChangeKind.PART_DETACHED:
            if self.is_tunnel(change.device_id):
                self._configs.pop(PartKey(node_key, change.detail), None)
                self.refresh_host(node_key)
            return
        if kind not in (
            ChangeKind.ADDED,
            ChangeKind.LOADED,
            ChangeKind.UNLOADED,
            ChangeKind.REMOVED,
            ChangeKind.HOST_CONVERTED,
        ):
            return
        if node_key not in self._hosts and not self.definitions.has_face_data(change.device_id):
            return
        self.refresh_host(node_key)

    def refresh_host(self, node_key):
        # type: (object) -> None
        """按宿主现在的样子重建它的端点；受影响的组把前后所有端点的正面格标脏。"""
        self.stats["refreshes"] += 1
        before = self._hosts.pop(node_key, {})
        for part_key, (freq, _role) in before.items():
            self._unindex(part_key, freq)
        record = self.registry.get(node_key)
        now = {}
        if record is None or not self.definitions.has_face_data(record.device_id):
            for face in FACING_NAMES:
                self._configs.pop(PartKey(node_key, face), None)
        elif not is_pending(record):
            tunnels = set(
                face for face, part_id in self.catalog.parts_of(record) if self.is_tunnel(part_id)
            )
            for face in FACING_NAMES:
                if face not in tunnels:
                    self._configs.pop(PartKey(node_key, face), None)
            if record.loaded:
                for face in FACING_NAMES:
                    if face not in tunnels:
                        continue
                    part_key = PartKey(node_key, face)
                    endpoint = self._config_of(part_key)
                    if endpoint is not None:
                        now[part_key] = endpoint
        for part_key, (freq, role) in now.items():
            self._groups.setdefault(freq, {})[part_key] = role
            self._fronts.setdefault(part_key.front_key(), set()).add(part_key)
        if now:
            self._hosts[node_key] = now
        freqs = set(freq for freq, _role in before.values()) | set(
            freq for freq, _role in now.values()
        )
        self._touch(freqs, [part_key.front_key() for part_key in before])

    def _unindex(self, part_key, freq):
        # type: (PartKey, str) -> None
        members = self._groups.get(freq)
        if members is not None:
            members.pop(part_key, None)
            if not members:
                del self._groups[freq]
        front = part_key.front_key()
        keys = self._fronts.get(front)
        if keys is not None:
            keys.discard(part_key)
            if not keys:
                del self._fronts[front]

    def _touch(self, freqs, extra_fronts=()):
        # type: (set, list) -> None
        """这些组前后所有端点的正面格标脏：拓扑重建时再问 `links_of`。"""
        fronts = set(extra_fronts)
        for freq in freqs:
            for part_key in self._groups.get(freq, ()):
                fronts.add(part_key.front_key())
        for front in sorted(fronts, key=_as_key):
            if self.registry.get(front) is not None:
                self.topology.mark_dirty(front)
                self.stats["fronts_marked"] += 1

    def _config_of(self, part_key):
        # type: (PartKey) -> object
        """这块隧道的 `(频率, 角色)`：先查缓存，没有再读面板配置。读不到（方块实体还拿不到）
        不记缓存，返回 None。"""
        if part_key in self._configs:
            return self._configs[part_key]
        config = self.panels.part_config(part_key)
        if config is None:
            self.stats["config_unreadable"] += 1
            return None
        endpoint = endpoint_of_config(config)
        self._configs[part_key] = endpoint
        return endpoint

    # -- 查询 ------------------------------------------------------------

    def endpoint(self, part_key):
        # type: (PartKey) -> object
        """已加载端点的 `(频率, 角色)`；不是已加载的端点返回 None。"""
        return self._hosts.get(part_key.node_key, {}).get(part_key)

    def members_of(self, freq):
        # type: (str) -> dict
        """一组已加载的端点 `{PartKey: 角色}`。"""
        return dict(self._groups.get(freq, {}))

    def group_state(self, freq):
        # type: (str) -> str
        members = self._groups.get(freq) or {}
        inputs = [key for key, role in members.items() if role == ROLE_INPUT]
        if not inputs:
            return GROUP_NO_INPUT
        if len(inputs) > 1:
            return GROUP_MULTIPLE_INPUTS
        if len(members) == 1:
            return GROUP_NO_OUTPUT
        return GROUP_LINKED

    def endpoints_at(self, via, front_key):
        # type: (str, object) -> tuple
        """频率 `via` 这一组里，正面对着 `front_key` 的已加载端点（准入的隧道把关用）。"""
        return tuple(
            sorted(
                (
                    part_key
                    for part_key in self._fronts.get(front_key, ())
                    if self.endpoint(part_key) is not None and self.endpoint(part_key)[0] == via
                ),
                key=_as_key,
            )
        )

    def links_of(self, node_key):
        # type: (object) -> list
        """远程连边的提供者（`TopologyService.add_remote_links`）：`node_key` 是哪些已接通的组
        的端点正面格，就和同组其余端点的正面格各连一条边，隧道组 = 频率。对称：两端的条件
        （组接通、各自正面开着）都满足才连。"""
        out = []
        seen = set()
        for part_key in sorted(self._fronts.get(node_key, ()), key=_as_key):
            endpoint = self.endpoint(part_key)
            if endpoint is None:
                continue
            freq = endpoint[0]
            if self.group_state(freq) != GROUP_LINKED or not self._front_open(part_key):
                continue
            for other in sorted(self._groups.get(freq, ()), key=_as_key):
                front = other.front_key()
                if other == part_key or front == node_key or front in seen:
                    continue
                if not self._front_open(other):
                    continue
                seen.add(front)
                out.append((front, freq))
        return out

    def _front_open(self, part_key):
        # type: (PartKey) -> bool
        """这块隧道正面那一格能不能接上（见模块说明）。"""
        record = self.registry.get(part_key.front_key())
        if record is None or not record.loaded or is_pending(record):
            return False
        category = self.definitions.category_of(record.device_id)
        if category in _CLOSED_FRONT_CATEGORIES:
            return False
        if not ConnectionRules.accepts(CATEGORY_CABLE, category):
            return False
        return self.catalog.face_verdict(record, opposite_face(part_key.slot)).allowed

    # -- 配置卡 ------------------------------------------------------------

    def card_input(self, context):
        # type: (object) -> IntentResult
        """潜行右键：点中隧道设为输入（已是输入就只记频率），别处清空卡。"""
        part_key = self._tunnel_hit(context)
        if part_key is None:
            return self._write_card(context, None, REASON_CARD_CLEARED, "已清空配置卡")
        endpoint = self._config_of(part_key)
        if endpoint is not None and endpoint[1] == ROLE_INPUT:
            freq = endpoint[0]
            message = "已记下这块输入的频率 #%s" % (freq,)
        else:
            freq = self._new_frequency()
            if freq is None:
                return IntentResult(
                    False, REASON_NO_FREE_FREQUENCY, "没有空闲的频率了", CARD_TITLE)
            if not self._write_tunnel(part_key, freq, ROLE_INPUT):
                return IntentResult(
                    False, REASON_TUNNEL_WRITE_FAILED, "没有写进去，请再试一次", CARD_TITLE)
            self.stats["frequencies_issued"] += 1
            message = "已设为输入，频率 #%s，已存入配置卡" % (freq,)
        return self._write_card(context, freq, REASON_TUNNEL_INPUT, message)

    def card_output(self, context):
        # type: (object) -> IntentResult
        """右键：把点中的隧道设为卡里那个频率的输出。"""
        part_key = self._tunnel_hit(context)
        if part_key is None:
            return IntentResult(False, REASON_NOT_A_TUNNEL, "没有点中 P2P 隧道", CARD_TITLE)
        card = self.items.held_data(context.player_id, CONFIG_CARD_ID, CARD_DATA_KEY)
        if card is None:
            return IntentResult(False, REASON_CARD_UNREADABLE, "读不到配置卡，请再试一次", CARD_TITLE)
        freq = parse_freq(card.get("freq")) if card.get(CARD_KIND) == CARD_KIND_TUNNEL else None
        if freq is None:
            return IntentResult(
                False, REASON_CARD_EMPTY,
                "配置卡是空白的：先潜行右键一块隧道把它设为输入", CARD_TITLE)
        if self._config_of(part_key) == (freq, ROLE_INPUT):
            return IntentResult(
                False, REASON_ALREADY_INPUT, "这块就是频率 #%s 的输入" % (freq,), CARD_TITLE)
        if not self._write_tunnel(part_key, freq, ROLE_OUTPUT):
            return IntentResult(
                False, REASON_TUNNEL_WRITE_FAILED, "没有写进去，请再试一次", CARD_TITLE)
        outputs = len([role for role in self._groups.get(freq, {}).values() if role == ROLE_OUTPUT])
        message = "已设为输出，频率 #%s（本组 %d 个输出）" % (freq, outputs)
        state = self.group_state(freq)
        if state == GROUP_NO_INPUT:
            message += "；本组的输入不在或没加载，暂不连通"
        elif state == GROUP_MULTIPLE_INPUTS:
            message += "；本组有不止一个输入，整组不通，请重设输入"
        return IntentResult(True, REASON_TUNNEL_OUTPUT, message, CARD_TITLE)

    def _tunnel_hit(self, context):
        # type: (object) -> object
        """这一下点中的那块隧道（`PartKey`）；点的不是挂着隧道的那一面返回 None。"""
        record = context.record
        hit = context.hit
        if record is None or hit is None or hit.part != PART_PANEL:
            return None
        if not self.definitions.has_face_data(record.device_id):
            return None
        part_id = self.catalog.part_at(record, hit.face)
        if part_id is None or not self.is_tunnel(part_id):
            return None
        return PartKey(record.node_key, hit.face)

    def _write_tunnel(self, part_key, freq, role):
        # type: (PartKey, str, str) -> bool
        """写这块隧道的面板配置，写成了就更新缓存、刷新宿主（受影响的组随之标脏）。"""
        if not self.panels.set_part_config(part_key, {CONFIG_FREQ: freq, CONFIG_ROLE: role}):
            return False
        self._configs[part_key] = (freq, role)
        self.refresh_host(part_key.node_key)
        return True

    def _write_card(self, context, freq, reason, message):
        # type: (object, object, str, str) -> IntentResult
        """配置卡记下 `freq`（None = 清空），物品说明随之改写。"""
        value = {CARD_KIND: CARD_KIND_TUNNEL, "freq": freq} if freq is not None else None
        if not self.items.set_held_data(
            context.player_id, CONFIG_CARD_ID, CARD_DATA_KEY, value, self.card_tip(freq)
        ):
            if reason == REASON_TUNNEL_INPUT:
                message += "（配置卡没有写进去，请再潜行右键一次）"
                return IntentResult(True, reason, message, CARD_TITLE)
            return IntentResult(
                False, REASON_CARD_WRITE_FAILED, "配置卡没有写进去，请再试一次", CARD_TITLE)
        return IntentResult(True, reason, message, CARD_TITLE)

    def card_tip(self, freq):
        # type: (object) -> str
        """配置卡的物品说明：原说明（`ToolSpec.tip`），记着频率时再加一行。"""
        tip = "\n§7" + self.definitions.get_tool(CONFIG_CARD_ID).tip
        if freq is not None:
            tip += "\n§b频率 #%s" % (freq,)
        return tip

    def _new_frequency(self):
        # type: () -> object
        """一个本次运行里还没见过的频率（缓存里记着的都算用过，含已卸载的）。"""
        used = set(endpoint[0] for endpoint in self._configs.values() if endpoint is not None)
        for _attempt in range(_RANDOM_TRIES):
            freq = format_freq(self._random.randrange(FREQ_LIMIT))
            if freq not in used:
                return freq
        for number in range(FREQ_LIMIT):
            freq = format_freq(number)
            if freq not in used:
                return freq
        return None

    def diagnostics(self):
        # type: () -> dict
        report = dict(self.stats)
        report["groups"] = dict(
            (freq, {"state": self.group_state(freq), "members": len(members)})
            for freq, members in sorted(self._groups.items())
        )
        report["cached_configs"] = len(self._configs)
        return report
