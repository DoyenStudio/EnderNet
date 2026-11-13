# coding=utf-8
"""终端交互用例：背包存取、槽位点击、光标、合成格（11-04）与样板终端（11-05）。

**从 `StorageService` 拆出（2026-11-12）**：这些动作改的是玩家背包、光标与
终端自己的格子，网络库存只是其中一个端点。命令仍经主控准入、由存储服务的
`handle` 按动作名分派到这里（`TerminalActions.ACTIONS`）；动网络库存一律走
存储服务的公开入口（`transfer()` / `commit_*` / `network_endpoint()` /
`inventory_endpoint_for()`），不读它的私有成员。

终端自己的状态（工作台、样板终端、配方来源与两份未定口径）归本对象所有。
"""

from ....common.crafting_recipe import match
from ....common.definitions import (
    ITEM_TEMPLATES,
    PATTERN_MODE_CRAFTING,
    PATTERN_MODE_FACTORY,
    PATTERN_MODES,
    TEMPLATE_PANEL_ITEM_ID,
)
from ....common.protocol import (
    BlockReason,
    CommandResult,
    ORIGIN_EXTERNAL,
    ORIGIN_INTERNAL,
    Reason,
    TransferReceipt,
    TransferRequest,
    TransferStatus,
    validate_slot_batch,
)
from ....common.resources import ResourceKey, item_dict_from_key, item_key_from_dict
from ...storage import (
    DIRECTION_READ,
    DIRECTION_WRITE,
    ContainerEndpoint,
    SlotView,
    receipt_result,
)
from ...storage.transfer import (
    CURSOR_IGNORE,
    SlotAnchor,
    anchored_extract_endpoint,
    anchored_insert_endpoint,
)
from ..patterns import PatternStack, PatternValidationError
from .slots import DeviceSlots
from .template import (
    CELL_COUNT_OF,
    GROUPS,
    GROUP_OUTPUTS,
    GROUP_SOURCE,
    INPUT_SLOT,
    INTEGER_TYPES,
    PUT_COUNT_ONE,
    RESULT_SLOT,
    TemplatePolicy,
    detect_crafting_output,
    encode_template_item,
    pattern_from_config,
)
from .workbench import (
    IMPORT_REFUSE,
    CraftingPolicy,
    WorkbenchContainer,
    plan_import,
    plan_refill,
)


class TerminalActions(object):
    """终端交互用例。每个存储服务（每网络）一份，由它的 `handle` 分派过来。"""

    #: 动作名 -> 方法名。存储服务按它建分派表；新增终端动作只改这里。
    ACTIONS = (
        ("inv_take", "_cmd_inv_take"),
        ("inv_put", "_cmd_inv_put"),
        ("inv_split", "_cmd_inv_split"),
        ("inv_merge", "_cmd_inv_merge"),
        ("inv_swap", "_cmd_inv_swap"),
        ("inv_quick_move", "_cmd_inv_quick_move"),
        ("slot_click", "_cmd_slot_click"),
        ("slot_batch", "_cmd_slot_batch"),
        ("grid_move", "_cmd_grid_move"),
        ("cursor_return", "_cmd_cursor_return"),
        ("cursor_drop", "_cmd_cursor_drop"),
        ("craft_import", "_cmd_craft_import"),
        ("craft_take", "_cmd_craft_take"),
        ("craft_clear", "_cmd_craft_clear"),
        ("craft_fill", "_cmd_craft_fill"),
        ("template_mode", "_cmd_template_mode"),
        ("template_cell_put", "_cmd_template_cell_put"),
        ("template_import", "_cmd_template_import"),
        ("template_universal_import", "_cmd_template_universal_import"),
        ("template_cell_clear", "_cmd_template_cell_clear"),
        ("template_clear", "_cmd_template_clear"),
        ("template_cell_count", "_cmd_template_cell_count"),
        ("template_encode", "_cmd_template_encode"),
        ("template_input_put", "_cmd_template_input_put"),
        ("template_input_take", "_cmd_template_input_take"),
        ("template_output_take", "_cmd_template_output_take"),
    )

    def __init__(
        self,
        storage,
        workbench=None,
        recipes=None,
        crafting_policy=None,
        grid_stack_limit=None,
        templates=None,
        template_policy=None,
    ):
        # type: (object, object, object, object, object, object, object) -> None
        #: 本网络的存储服务：网络库存、背包端点与转移协调器都从它取。
        self.storage = storage
        #: 合成终端的工作台集合（11-04）。**由启动层注入**，与驱动器
        #: 同一地位：九宫格里的东西是真实原料，不能因为运行时重建就没了。
        #: 未接线时为 None，合成命令一律 `SERVICE_NOT_READY`——那是
        #: 「装了但还不能用」，不是「这台设备没有九宫格」。
        self.workbench = workbench
        #: 工作台配方来源（`adapters/recipes.py` 的 `EngineRecipeSource`）。
        #: 未接线时为 None，导入一律明确拒绝，**不返回空候选**——
        #: 空候选会被读成「这个产物没有配方」。
        self.recipes = recipes
        #: 未定口径（`workbench.py` 的 `CraftingPolicy`）。
        self.crafting_policy = (
            crafting_policy if crafting_policy is not None else CraftingPolicy()
        )
        self.grid_stack_limit = grid_stack_limit
        #: 样板终端的状态集合（11-05）。与 `workbench` 同一地位：两个真实
        #: 样板槽里是真物品，不能因为运行时重建就没了。未接线时为 None，
        #: 样板命令一律 `SERVICE_NOT_READY`。
        self.templates = templates
        #: 样板终端的未定口径（`template.py` 的 `TemplatePolicy`）。
        self.template_policy = (
            template_policy if template_policy is not None else TemplatePolicy()
        )
        #: 借出后放不回去／产物收回失败的次数。**应当恒为 0**：
        #: 它不为零就说明有一条路径在做了一半，得去查是哪一次。
        self.craft_undo_failures = 0

    def handlers(self):
        # type: () -> dict
        """`动作名 -> 绑定方法`，供存储服务的分派表合并。"""
        return dict((action, getattr(self, name)) for action, name in self.ACTIONS)

    def _reject(self, command, code, detail):
        # type: (object, str, str) -> CommandResult
        self.storage.stats["refused"] += 1
        return CommandResult.rejected(command.request_id, [BlockReason(code, detail)])

    @staticmethod
    def _receipt_result(command, receipt, action="transfer"):
        # type: (object, object, str) -> CommandResult
        return receipt_result(command.request_id, receipt, action)

    @staticmethod
    def _anchor_of(payload):
        # type: (dict) -> object
        """从载荷里读槽锚。缺 `slot` 时返回 None（表示「不按槽定位」）。"""
        slot = payload.get("slot")
        if slot is None:
            return None
        raw = payload.get("key")
        key = ResourceKey.from_dict(raw) if isinstance(raw, dict) else None
        return SlotAnchor(slot, key, payload.get("slot_version"))

    def _cursor_refusal(self, command, payload):
        # type: (object, dict) -> object
        """R12 的显式拦截：客户端说手里还拿着东西时怎么办**尚未定案**。

        替身口径是「拒绝接管」。没有这个拦截，一份没有规则的东西会被
        「看起来能用」的实现悄悄定下来（全局约束 7）。
        """
        if not payload.get("cursor"):
            return None
        if self.storage.transfer_policy.cursor_disposition == CURSOR_IGNORE:
            return None
        return CommandResult.rejected(
            command.request_id,
            [
                BlockReason(
                    Reason.CURSOR_UNRESOLVED,
                    "R12 未定案：本模组目前不接管关闭界面时手里的物品",
                )
            ],
        )

    def _transfer_payload(self, command, payload, direction):
        # type: (object, dict, str) -> CommandResult
        """`inv_take` / `inv_put` 的公共实现。

        **带槽锚时只动那个槽，但走的是同一条转移路径。** 锚被做成一个
        「只能动这个槽」的端点视图（`AnchoredInventoryView`），于是预检、
        去重、取出、放入、余量回退全都与普通路径共用一段代码。

        早先锚定路径是一条**旁路**：核对锚、调 `anchored_extract`、完事。
        那条路会同时漏掉三样东西——网络那一侧根本没人动（`inv_take` 于是把
        背包里的东西凭空删掉、`inv_put` 凭空造出来）、去重账本不参与
        （重发就再动一次）、外部输入通知不发（手动存入不算回流）。
        """
        cursor = self._cursor_refusal(command, payload)
        if cursor is not None:
            return cursor
        endpoint, refusal = self.storage.inventory_endpoint_for(command, payload)
        if refusal is not None:
            return refusal
        raw = payload.get("key")
        count = payload.get("count")
        if not isinstance(raw, dict) or isinstance(count, bool) or not isinstance(count, int):
            return self._reject(command, Reason.BAD_PARAMETER, "需要 key 与整数 count")
        if count <= 0:
            return self._reject(command, Reason.OUT_OF_RANGE, "count 必须为正")
        key = ResourceKey.from_dict(raw)
        try:
            anchor = self._anchor_of(payload)
        except (TypeError, ValueError) as error:
            return self._reject(command, Reason.BAD_PARAMETER, "槽锚不合法：%s" % (error,))

        op_id = payload.get("op_id") or command.scoped_request_id()
        network = self.storage.network_endpoint()
        if direction == DIRECTION_WRITE:
            source, target = network, endpoint
        else:
            source, target = endpoint, network

        if anchor is not None:
            if anchor.expected_key is None:
                return self._reject(
                    command, Reason.BAD_PARAMETER,
                    "按槽操作必须给出期望物品指纹（key）",
                )
            if direction == DIRECTION_READ:
                source = anchored_extract_endpoint(endpoint, anchor)
            else:
                target = anchored_insert_endpoint(endpoint, anchor)
            # **锚先过一道**，为的是给出准确的原因码：预检只会说
            # 「来源没有可动的 / 目标放不下」，而玩家要做的是**刷新界面**。
            view = source if direction == DIRECTION_READ else target
            ok, reason, detail = view.anchor_verdict()
            if not ok:
                return self._receipt_result(
                    command,
                    TransferReceipt(
                        op_id, count, 0, TransferStatus.REFUSED, reason, key,
                        endpoint.holder_id(),
                    ),
                    "inv",
                )

        request = TransferRequest(
            op_id,
            source.endpoint_id(),
            target.endpoint_id(),
            key,
            count,
            command.topology_version,
            owner_id=(command.source.player_id if command.source is not None else ""),
            origin=ORIGIN_EXTERNAL if direction == DIRECTION_READ else ORIGIN_INTERNAL,
        )
        receipt = self.storage.transfer().commit(request, source, target)
        return self._receipt_result(command, receipt, "inv")

    def _workbench_for(self, command):
        # type: (object) -> tuple
        """取这台设备的工作台。返回 `(grid, refusal)`。

        走 `grid_for` 而不是 `of`：`grid_for` 对**不是合成终端的方块**返回
        `None`，于是「这台方块没有工作台」是一条明确的拒绝，而不是凭空
        变出一台空的。九宫格是设备库存，不是每条命令都能拿到的东西。
        """
        if self.workbench is None:
            return None, CommandResult.blocked(
                command.request_id,
                [BlockReason(Reason.SERVICE_NOT_READY, "存储服务未接入工作台")],
            )
        grid = self.workbench.grid_for(command.device_ref)
        if grid is None:
            return None, self._reject(
                command, Reason.UNSUPPORTED, "该设备没有工作台（不是合成终端）"
            )
        return grid, None

    def _craft_recipes_for(self, output_key):
        # type: (object) -> tuple
        """按产物查工作台配方。返回 `(recipes, blocked)`。

        **引擎那边炸了不当作 BadParameter。** `EngineRecipeSource` 在参数
        类型不对时会抛（实测引擎返回 `False` 而不是空表），那条消息若被
        `handle` 的兜底读成「参数不合法」，玩家会去改自己点的那件物品，
        而真正坏的是适配器。
        """
        if self.recipes is None:
            return None, None
        try:
            return list(self.recipes.recipes_for(output_key)), None
        except Exception as error:
            self.storage.last_error = "%s: %s" % (type(error).__name__, error)
            return None, CommandResult.blocked(
                "",
                [BlockReason(Reason.NO_ADAPTER,
                             "暂时读不到配方，请稍后再试")],
            )

    def _retained_recipe(self, grid):
        # type: (object) -> object
        """把「保留的配方」取回成一条真配方，取不回返回 `None`。

        九宫格只存 digest 与产物名字——**产物名字必须留着**，否则查不回来：
        引擎只能按产物或按原料查，而九宫格给不出产物是哪个。取回来之后
        调用方还要**再核一次格子**，因为玩家可能把格子动过，而备忘不会知道。
        """
        if not grid.recipe or not grid.recipe_output:
            return None
        name = grid.recipe_output.get("name")
        if not name:
            return None
        output = ResourceKey.item(name, aux=grid.recipe_output.get("aux", 0) or 0)
        recipes, _blocked = self._craft_recipes_for(output)
        for recipe in recipes or ():
            if recipe.digest() == grid.recipe:
                return recipe
        return None

    def _craft_grid_payload(self, grid):
        # type: (object) -> dict
        """九宫格的只读投影，附在回执里让客户端立刻刷新。"""
        return {
            "revision": grid.revision,
            "recipe": grid.recipe or "",
            "occupied": list(grid.occupied_slots()),
        }

    def _cmd_craft_import(self, command, payload):
        # type: (object, dict) -> CommandResult
        """「导入」：按产物找到配方，尽量从玩家背包与网络凑齐原料（11-04）。

        已确认的照做，未定的记账：

        - **每个原料槽位只摆一个**（文档 5.2 第 5 条），不是一组；
        - **缺料不取消整次导入**，缺的格子留空；
        - 九宫格非空时**拒绝**（`CraftingPolicy.import_when_occupied`）——
          文档明令不得覆盖或删除已有实物，而在「旧料怎么办」有定论之前，
          把选择权交回玩家（点「清空」）是唯一不丢物的做法；
        - **来源优先级未定**（文档第 8 节）。本轮取「先背包、后网络」并记
          在策略里，**不是定案**。
        """
        grid, refusal = self._workbench_for(command)
        if refusal is not None:
            return refusal
        raw_key = payload.get("key")
        if not isinstance(raw_key, dict):
            return self._reject(command, Reason.BAD_PARAMETER, "导入需要给出产物 key")
        try:
            output = ResourceKey.from_dict(raw_key)
        except (KeyError, TypeError, ValueError):
            return self._reject(command, Reason.BAD_PARAMETER, "产物 key 不合法")

        policy = self.crafting_policy
        if IMPORT_REFUSE == policy.import_when_occupied and not grid.is_empty():
            return self._reject(
                command, Reason.CRAFT_GRID_OCCUPIED,
                "九宫格里还有物品；先点「清空」再导入",
            )

        recipes, blocked = self._craft_recipes_for(output)
        if blocked is not None:
            return CommandResult.blocked(command.request_id, blocked.reasons)
        if recipes is None:
            return self._reject(command, Reason.NO_ADAPTER, "存储服务未接入配方来源")
        if not recipes:
            return self._reject(
                command, Reason.CRAFT_NO_RECIPE, "这个产物没有工作台配方",
            )

        digest = payload.get("recipe")
        if digest:
            chosen = None
            for recipe in recipes:
                if recipe.digest() == digest:
                    chosen = recipe
                    break
            if chosen is None:
                return self._reject(
                    command, Reason.CRAFT_NO_RECIPE,
                    "这条配方已不在候选里（配方表变了），请重选",
                )
        elif len(recipes) == 1:
            chosen = recipes[0]
        else:
            # 多条候选：**不替玩家挑一条**。候选原样交回，由界面开选择窗口，
            # 玩家点完带 digest 再来一次。
            return CommandResult.ok(command.request_id, {
                "action": "craft_import",
                "needs_choice": True,
                "output": {"name": output.name, "aux": output.aux},
                "candidates": [recipe.to_dict() for recipe in recipes],
            })

        movements = []
        imported, missing = self._fill_grid(command, payload, grid, chosen, movements)
        grid.set_recipe(chosen.digest(), {"name": output.name, "aux": output.aux})
        return CommandResult.ok(command.request_id, {
            "action": "craft_import",
            "recipe": chosen.digest(),
            "imported": imported,
            "movements": movements,
            "missing": list(missing),
            "grid": self._craft_grid_payload(grid),
        })

    def _fill_grid(self, command, payload, grid, recipe, movements):
        # type: (object, dict, object, object) -> tuple
        """按计划往九宫格里摆原料。返回 `(摆了几格, 缺料的格子)`。"""
        container = WorkbenchContainer(grid, limit_of=self.grid_stack_limit)
        endpoint, refusal = self.storage.inventory_endpoint_for(command, payload)
        if refusal is not None:
            # 背包读不了**不是**导入失败的理由：网络那一半还能用。
            endpoint = None
        op_id = payload.get("op_id") or command.scoped_request_id()
        moved = 0
        missing = []
        for slot, alternatives, count in plan_import(
                recipe, self.crafting_policy.import_per_slot):
            if grid.count_at(slot) > 0:
                moved += 1
                continue
            if self._fill_slot(op_id, container, slot, alternatives, count,
                               endpoint, movements):
                moved += 1
            else:
                # **不因为一格缺料取消整次导入**（文档 5.2 第 5 条）。
                missing.append(slot)
        return moved, missing

    def _fill_slot(self, op_id, container, slot, alternatives, count, inventory, movements=None):
        # type: (str, object, int, tuple, int, object) -> bool
        """给某一格凑一件原料。凑到返回真。"""
        for ingredient in alternatives:
            key = ResourceKey.item(ingredient.name, aux=_aux_or_zero(ingredient))
            borrowed = _find_inventory_slot(inventory, key)
            # **每一次借用必须有各自的 `op_id`。** 全部用同一个的话，
            # 第二格拿到的会是第一次那条去重回执（`_commit` 直接返回
            # 原回执、**一件都不动**），而回执上写着 `moved: 1`——
            # 于是九宫格里凭空多出一件没从任何地方扣掉的原料。
            # 这不是理论：`test_one_item_per_required_slot` 一开始就是红的。
            borrow_id = u"%s:slot%d" % (op_id, slot)
            origin = self._borrow(borrow_id, key, count, inventory, borrowed)
            if origin is None:
                continue
            receipt = container.insert_at(borrow_id, slot, key, count)
            if receipt.moved >= count:
                if movements is not None:
                    movements.append({"key": key.to_dict(), "from": origin[0],
                                      "slot": origin[2], "cell": slot, "to": "grid"})
                return True
            # 借来了却放不进去：**原路还回去**。留下的那半件对不上任何配方，
            # 只会让玩家看见一格来路不明的物品。
            if not self._return_borrowed(borrow_id, key, count - receipt.moved, origin):
                self.craft_undo_failures += 1
            return False
        return False

    def _borrow(self, op_id, key, count, inventory, borrowed):
        # type: (str, object, int, object, tuple) -> object
        """从玩家背包或网络借几件原料。返回来源标识，借不到返回 `None`。

        来源优先级**未定**（文档第 8 节），这里取「先背包、后网络」并记在
        `CraftingPolicy` 里。两个来源各走各自经过核对的原语：背包用带槽锚的
        `anchored_extract`，网络用提交台账里的 `commit_extract`——
        **都拿 `moved` 说话，不凭调用方声称**。
        """
        slot, version = borrowed
        if slot is not None:
            receipt = inventory.anchored_extract(
                op_id, SlotAnchor(slot, key, version), count)
            if receipt.moved >= count:
                return ("inventory", inventory, slot, version)
        if self.storage.is_ready():
            receipt = self.storage.commit_extract(op_id, key, count)
            if receipt.moved >= count:
                return ("network", None, None, None)
        return None

    def _return_borrowed(self, op_id, key, count, origin):
        # type: (str, object, int, object) -> bool
        """把刚借来又没用上的那几件放回去。放不回返回假。

        放不回**不抛**——那会把一条已经做了一半的导入变成异常。但也不装作
        没事：调用方把它记进 `craft_undo_failures`，诊断里能看见。
        """
        if count <= 0:
            return True
        try:
            if origin[0] == "inventory":
                endpoint, slot, version = origin[1], origin[2], origin[3]
                receipt = endpoint.anchored_insert(
                    "%s:undo" % (op_id,), SlotAnchor(slot, key, version), count)
                return receipt.moved >= count
            if origin[0] == "network" and self.storage.is_ready():
                receipt = self.storage.commit_insert("%s:undo" % (op_id,), key, count)
                return receipt.moved >= count
        except Exception:
            return False
        return False

    def _cmd_craft_take(self, command, payload):
        # type: (object, dict) -> CommandResult
        """点击输出槽：执行**一次**合成（11-04）。

        一次 = 一次配方执行，不是自动连做。判定看的是**格子里实际有什么**，
        不是保留的配方备忘——玩家可能把格子动过。

        产物进**玩家背包**（User 2026-10-01 裁定）。顺序是**先把产物放进
        背包、再扣原料**：反过来的话，背包满了就会出现「料没了、东西也没
        拿到」的丢物分支。放不下时整次拒绝，一格原料都不动。
        """
        grid, refusal = self._workbench_for(command)
        if refusal is not None:
            return refusal
        if payload.get("slot_version") is not None and payload["slot_version"] != grid.revision:
            return self._reject(command, Reason.STALE_VERSION, "合成格已变化")
        recipe = self._retained_recipe(grid)
        if recipe is None:
            return self._reject(command, Reason.CRAFT_NO_RECIPE, "还没有导入配方")
        found = match(recipe, grid.cells())
        if found is None:
            return self._reject(
                command, Reason.INSUFFICIENT_STOCK, "九宫格里的原料不满足这条配方",
            )
        endpoint, refusal = self.storage.inventory_endpoint_for(command, payload)
        if refusal is not None:
            return refusal
        op_id = payload.get("op_id") or command.scoped_request_id()

        destination = payload.get("target")
        cursor = None
        if destination == "cursor":
            cursor = self.storage.inventories.cursor_of(command.source.player_id)
            if cursor.snapshot()["revision"] != (payload.get("config") or {}).get("revision"):
                return self._reject(command, Reason.STALE_VERSION, "持物已变化")
            endpoint = ContainerEndpoint(cursor, "cursor:%s" % command.source.player_id)
        original = grid.cells()
        before = endpoint.inventory_view()["slots"] if hasattr(endpoint, "inventory_view") else ()
        delivered = []
        for index, item in enumerate(recipe.outputs):
            key = ResourceKey.item(item.name, aux=_aux_or_zero(item))
            receipt = endpoint.commit_insert(
                "%s:out%d" % (op_id, index), key, item.count)
            if receipt.moved < item.count:
                self._undo_delivery(endpoint, op_id, delivered, key, receipt.moved)
                return self._reject(
                    command, Reason.NO_CAPACITY, "背包放不下这次合成的产物",
                )
            delivered.append((key, receipt.moved))

        for slot, count in found.consumed:
            grid.take_at(slot, count)
        movements = []
        if destination in ("cursor", "bag"):
            container = WorkbenchContainer(grid, limit_of=self.grid_stack_limit)
            for cell, alternatives, need in plan_refill(recipe, original):
                if grid.count_at(cell) == 0:
                    self._fill_slot(op_id + ":refill", container, cell, alternatives, need,
                                    None, movements)
        return CommandResult.ok(command.request_id, {
            "cursor": cursor.snapshot() if cursor else None,
            "movements": movements,
            "bag_slots": [self._new_stack_slots(endpoint, before, key) if before else []
                          for key, _ in delivered],
            "action": "craft_take",
            "outputs": [{"name": key.name, "aux": key.aux, "count": count}
                        for key, count in delivered],
            "grid": self._craft_grid_payload(grid),
        })

    def _undo_delivery(self, endpoint, op_id, delivered, key, moved):
        # type: (object, str, list, object, int) -> None
        """产物没送全时，把已经送进去的收回来。收不回就记一笔，不抛。"""
        pending = list(delivered)
        if moved > 0:
            pending.append((key, moved))
        for index, (sent_key, count) in enumerate(pending):
            receipt = endpoint.commit_extract(
                "%s:undo%d" % (op_id, index), sent_key, count)
            if receipt.moved < count:
                self.craft_undo_failures += 1

    def _cmd_craft_fill(self, command, payload):
        # type: (object, dict) -> CommandResult
        """仅从网络补满已有槽；同身份原料按实际取出量优先补齐较少的堆叠。"""
        grid, refusal = self._workbench_for(command)
        if refusal is not None:
            return refusal
        revision = (payload.get("config") or {}).get("grid_revision")
        if (isinstance(revision, bool) or not isinstance(revision, INTEGER_TYPES)
                or revision != grid.revision):
            return self._reject(command, Reason.STALE_VERSION, "原料格已变化")
        container = WorkbenchContainer(grid, limit_of=self.grid_stack_limit)
        groups = {}
        for slot in grid.occupied_slots():
            groups.setdefault(grid.key_at(slot), []).append(slot)
        moved = 0
        movements = []
        for key, slots in sorted(groups.items(), key=lambda pair: pair[1][0]):
            limit = container.limit_of(key)
            counts = dict((slot, grid.count_at(slot)) for slot in slots
                          if grid.count_at(slot) < limit)
            wanted = sum(limit - count for count in counts.values())
            if not wanted:
                continue
            # 一种材料只取一次，再分配实际回执数量；预留与带宽限制由网络处理。
            receipt = self.storage.commit_extract(
                "%s:fill:%d" % (command.scoped_request_id(), slots[0]), key, wanted)
            remaining = receipt.moved
            while remaining > 0:
                lowest = min(counts.values())
                low_slots = sorted(slot for slot in counts if counts[slot] == lowest)
                next_level = min([limit] + [count for count in counts.values()
                                           if count > lowest])
                amount = min(remaining, (next_level - lowest) * len(low_slots))
                each, extra = divmod(amount, len(low_slots))
                for index, slot in enumerate(low_slots):
                    counts[slot] += each + (1 if index < extra else 0)
                remaining -= amount
            for slot in sorted(counts):
                added = counts[slot] - grid.count_at(slot)
                if added > 0:
                    # 主线程内已核对同种槽位及容量，直接落到设备实物库存。
                    grid.add_at(slot, key, added)
                    moved += added
                    movements.append({"from": "network", "to": "grid",
                                      "cell": slot, "key": key.to_dict()})
        return CommandResult.ok(command.request_id, {
            "action": "craft_fill", "moved": moved, "movements": movements,
            "grid": self._craft_grid_payload(grid),
        })

    def _cmd_craft_clear(self, command, payload):
        # type: (object, dict) -> CommandResult
        """「清空」：把九宫格里的实物**全部返还网络**（11-04）。

        默认返网（文档 5.4）。**网络收不下的部分留在格子里**——那条硬约束
        是「不能删除尚未成功存入网络的物品」，因此逐格提交、按回执扣格，
        收不下的原样留在原格，既不静默丢弃、也不改送背包。

        全部清空后连保留的配方一起清掉（文档把配方去留列为未定项，「清空」
        的字面就是清空）；**清不干净时保留配方**，免得界面显示一条与格子
        对不上的配方。
        """
        grid, refusal = self._workbench_for(command)
        if refusal is not None:
            return refusal
        op_id = payload.get("op_id") or command.scoped_request_id()
        returned = 0
        movements = []
        kept = []
        for slot in grid.occupied_slots():
            key, count = grid.entry(slot)
            receipt = self.storage.commit_insert("%s:%d" % (op_id, slot), key, count)
            if receipt.moved > 0:
                grid.take_at(slot, receipt.moved)
                returned += receipt.moved
                movements.append({"from": "grid", "cell": slot, "key": key.to_dict(), "to": "network"})
            if receipt.moved < count:
                kept.append(slot)
        if not kept:
            grid.set_recipe(None, None)
        return CommandResult.ok(command.request_id, {
            "action": "craft_clear",
            "returned": returned,
            "movements": movements,
            "kept": list(kept),
            "grid": self._craft_grid_payload(grid),
        })

    # -- 样板终端（11-05）----------------------------------------------

    def _template_state_for(self, command):
        # type: (object) -> tuple
        """取这台设备的样板终端状态。返回 `(state, refusal)`。

        走 `state_for` 而不是 `of`：对**不是样板终端的方块**返回 `None`，
        于是「这台方块没有样板槽」是一条明确的拒绝，而不是凭空变出一台空的。
        """
        if self.templates is None:
            return None, CommandResult.blocked(
                command.request_id,
                [BlockReason(Reason.SERVICE_NOT_READY, "存储服务未接入样板终端状态")],
            )
        state = self.templates.state_for(command.device_ref)
        if state is None:
            return None, self._reject(
                command, Reason.UNSUPPORTED, "该设备没有样板槽（不是样板终端）"
            )
        return state, None

    def _template_state_payload(self, state):
        # type: (object) -> dict
        """状态的只读投影，附在回执里让客户端立刻刷新。

        与 `_craft_grid_payload` 同一分工：**只给下标与数量，不给资源名**。
        带名字的那种投影是同步层的活（`bootstrap._sync_template_of`），
        它手上有定义表；回执这份只回答「刚才那一下之后，格子在哪几个、
        各有多少」。
        """
        return {
            "revision": state.revision,
            "mode": state.config.mode,
            "source": list(state.config.occupied(GROUP_SOURCE)),
            "outputs": list(state.config.occupied(GROUP_OUTPUTS)),
            "input": state.slots.count_at(INPUT_SLOT),
            "result": state.slots.count_at(RESULT_SLOT),
        }

    def _template_gate(self, command, payload, state):
        # type: (object, dict, object) -> object
        """配置编辑的过期闸。返回一条拒绝或 `None`。

        **只对虚拟配置的编辑生效**：真实槽那三条命令的 `slot_version` 是
        背包槽锚，意思完全不同。

        客户端把「最后一次收到的配置修订号」回填进来，并**每发出一条编辑
        就把它加一**（服务端每成功改一次也只加一）。因此连续快速点击不会
        互相判死，而真正过期的客户端（别人刚改过）会被挡住，
        等界面重新同步再改。
        """
        expected = payload.get("slot_version")
        if expected is None:
            return self._reject(
                command, Reason.BAD_PARAMETER, "配置编辑需要带上配置修订号"
            )
        if expected != state.revision:
            return self._reject(
                command, Reason.STALE_VERSION,
                "样板配置刚被修改，请稍后再试",
            )
        return None

    def _template_cell_of(self, command, payload):
        # type: (object, dict) -> tuple
        """读出一条虚格编辑指向哪一组、哪一格。返回 `(group, index, refusal)`。

        分组走 `target` 字段（`source` / `outputs`），下标走 `cell`——
        **两者都不与真实槽号共用字段**：交接明令不把虚格下标当作真实槽号，
        共用的代价是「这个数属于哪个空间」逐动作去猜。
        """
        group = payload.get("target")
        if group not in GROUPS:
            return None, None, self._reject(
                command, Reason.BAD_PARAMETER,
                "虚格编辑要给出分组（%s）" % ("、".join(GROUPS),),
            )
        cell = payload.get("cell")
        count = CELL_COUNT_OF[group]
        if (isinstance(cell, bool) or not isinstance(cell, INTEGER_TYPES)
                or not 0 <= cell < count):
            return None, None, self._reject(
                command, Reason.OUT_OF_RANGE,
                "%s 的虚格下标要在 0 到 %d 之间" % (group, count - 1),
            )
        return group, cell, None

    def _template_count_of(self, command, payload, default=None):
        # type: (object, dict, object) -> tuple
        """读数量。返回 `(count, refusal)`。缺席时用 `default`。"""
        count = payload.get("count")
        if count is None:
            if default is None:
                return None, self._reject(
                    command, Reason.BAD_PARAMETER, "这条命令需要给出数量")
            return default, None
        if (isinstance(count, bool) or not isinstance(count, INTEGER_TYPES)
                or not 1 <= count <= self.template_policy.max_config_count):
            return None, self._reject(
                command, Reason.OUT_OF_RANGE,
                "数量要在 1 到 %d 之间" % (self.template_policy.max_config_count,),
            )
        return count, None

    def _template_detect(self, state):
        # type: (object) -> tuple
        """合成模式当前摆位能做出什么。返回 `(preview, blocked)`。

        工厂模式不看它（产物由玩家自己配），直接给 `(None, None)`。

        **适配器炸了不当作「没有配方」。** 那条路会把一次查询故障表现成
        「你摆的东西没有配方」，玩家只会去改配方——与 `_craft_recipes_for`
        同一个理由。
        """
        if state.config.mode != PATTERN_MODE_CRAFTING:
            return None, None
        if self.recipes is None:
            return None, CommandResult.blocked(
                "", [BlockReason(Reason.NO_ADAPTER, "暂时读不到配方，请稍后再试")],
            )
        try:
            return detect_crafting_output(
                state.config.cells(GROUP_SOURCE), self.recipes), None
        except Exception as error:
            self.storage.last_error = "%s: %s" % (type(error).__name__, error)
            return None, CommandResult.blocked(
                "", [BlockReason(Reason.NO_ADAPTER, "暂时读不到配方，请稍后再试")],
            )

    def _cmd_template_mode(self, command, payload):
        # type: (object, dict) -> CommandResult
        """切换合成／工厂样板（11-05）。

        **保留输入九宫格、只清输出格**（User 2026-10-02 定）——两个模式的
        输入配方基本通用，而与模式绑定的输出格必须跟着走。重复点当前模式
        什么都不改，因此也不推新修订号（不推就不会白让客户端收一条推送）。
        """
        state, refusal = self._template_state_for(command)
        if refusal is not None:
            return refusal
        mode = payload.get("target")
        if mode not in PATTERN_MODES:
            return self._reject(
                command, Reason.BAD_PARAMETER,
                "样板模式必须是 %s" % ("、".join(sorted(PATTERN_MODES)),),
            )
        gate = self._template_gate(command, payload, state)
        if gate is not None:
            return gate
        state.config.set_mode(mode, self.template_policy)
        return CommandResult.ok(command.request_id, {
            "action": "template_mode",
            "state": self._template_state_payload(state),
        })

    def _cmd_template_import(self, command, payload):
        # type: (object, dict) -> CommandResult
        """按产物导入虚拟配方；多配方先返回候选，不转移任何实物。"""
        state, refusal = self._template_state_for(command)
        if refusal is not None:
            return refusal
        gate = self._template_gate(command, payload, state)
        if gate is not None:
            return gate
        if state.config.mode != PATTERN_MODE_CRAFTING:
            return self._reject(command, Reason.TEMPLATE_CELL_LOCKED,
                                "只有合成样板支持导入配方")
        try:
            output = ResourceKey.from_dict(payload.get("key"))
        except (KeyError, TypeError, ValueError):
            return self._reject(command, Reason.BAD_PARAMETER, "产物 key 不合法")
        if output.kind != "item":
            return self._reject(command, Reason.BAD_PARAMETER, "产物必须是物品")
        recipes, blocked = self._craft_recipes_for(output)
        if blocked is not None:
            return CommandResult.blocked(command.request_id, blocked.reasons)
        if recipes is None:
            return self._reject(command, Reason.NO_ADAPTER, "未接入配方来源")
        digest = payload.get("recipe")
        if not recipes:
            return CommandResult.ok(command.request_id, {"action": "template_import"})
        chosen = next((row for row in recipes if row.digest() == digest), None)
        if digest and chosen is None:
            return self._reject(command, Reason.CRAFT_NO_RECIPE, "配方已失效，请重选")
        if not digest and len(recipes) > 1:
            return CommandResult.ok(command.request_id, {
                "action": "template_import", "needs_choice": True,
                "revision": state.revision,
                "output": {"name": output.name, "aux": output.aux},
                "candidates": [self._template_recipe_candidate(row) for row in recipes],
            })
        if chosen is None:
            chosen = recipes[0]
        source = [None] * 9
        for index, alternatives, count in plan_import(chosen):
            ingredient = alternatives[0]
            key = ResourceKey.item(ingredient.name, aux=_aux_or_zero(ingredient))
            invalid = self._template_stack_error(state, GROUP_SOURCE, index, key, count)
            if invalid is not None or not 1 <= count <= self.template_policy.max_config_count:
                return self._reject(command, Reason.PATTERN_INVALID,
                                    invalid or "配方原料数量越界")
            source[index] = (key, count)
        state.config.replace_source(source)
        return CommandResult.ok(command.request_id, {
            "action": "template_import", "recipe": chosen.digest(),
            "state": self._template_state_payload(state),
        })

    @staticmethod
    def _template_recipe_candidate(recipe):
        # type: (object) -> dict
        """按实际导入的槽位展示候选，包含没有固定摆位的无序配方。"""
        candidate = recipe.to_dict()
        slots = [None] * 9
        for index, alternatives, count in plan_import(recipe):
            entry = alternatives[0].to_dict()
            entry["count"] = count
            slots[index] = entry
        candidate["slots"] = slots
        return candidate

    def _cmd_template_universal_import(self, command, payload):
        # type: (object, dict) -> CommandResult
        """采集器一次替换最多 9 输入、3 输出，仅修改虚拟配置。"""
        state, refusal = self._template_state_for(command)
        if refusal is not None:
            return refusal
        gate = self._template_gate(command, payload, state)
        if gate is not None:
            return gate
        if state.config.mode != PATTERN_MODE_FACTORY:
            return self._reject(command, Reason.TEMPLATE_CELL_LOCKED,
                                "只有工厂模式支持万用导入")
        config = payload.get("config")
        if not isinstance(config, dict):
            return self._reject(command, Reason.BAD_PARAMETER, "缺少采集配置")
        groups = {}
        for field, group in (("inputs", GROUP_SOURCE), ("outputs", GROUP_OUTPUTS)):
            items = config.get(field)
            if not isinstance(items, list):
                return self._reject(command, Reason.BAD_PARAMETER, "采集配置必须是物品列表")
            cells = [None] * CELL_COUNT_OF[group]
            for index, item in enumerate(items[:len(cells)]):
                if not isinstance(item, dict):
                    return self._reject(command, Reason.BAD_PARAMETER, "采集物品格式不合法")
                try:
                    key = ResourceKey.from_dict({
                        "kind": "item", "name": item["itemName"],
                        "aux": item.get("auxValue", 0),
                    })
                except (KeyError, TypeError, ValueError):
                    return self._reject(command, Reason.BAD_PARAMETER, "采集物品标识不合法")
                if key.name == "minecraft:air":
                    return self._reject(command, Reason.BAD_PARAMETER, "不能导入空气")
                # GetUiItem 没有数量；沿用手动配置新格子的默认数量。
                count = self.template_policy.encode_count_per_cell
                invalid = self._template_stack_error(state, group, index, key, count)
                if invalid is not None:
                    return self._reject(command, Reason.PATTERN_INVALID, invalid)
                cells[index] = (key, count)
            groups[group] = cells
        state.config.replace_factory(groups[GROUP_SOURCE], groups[GROUP_OUTPUTS])
        return CommandResult.ok(command.request_id, {
            "action": "template_universal_import",
            "state": self._template_state_payload(state),
        })

    def _cmd_template_cell_put(self, command, payload):
        # type: (object, dict) -> CommandResult
        """把一个物品放进虚格（或覆盖已有配置）。

        **虚格里的东西是配置，不是实物**：这一下不进网络、不进背包，
        也不生成任何物品（交接第 1 节）。覆盖是允许的（User 2026-10-02）。
        """
        state, refusal = self._template_state_for(command)
        if refusal is not None:
            return refusal
        group, cell, refusal = self._template_cell_of(command, payload)
        if refusal is not None:
            return refusal
        gate = self._template_gate(command, payload, state)
        if gate is not None:
            return gate
        if state.config.locked(group, cell):
            return self._reject(
                command, Reason.TEMPLATE_CELL_LOCKED,
                "这一格在当前模式下不能改（合成的产物由服务端检测）",
            )
        raw_key = payload.get("key")
        if not isinstance(raw_key, dict):
            return self._reject(command, Reason.BAD_PARAMETER, "放入虚格需要给出 key")
        try:
            key = ResourceKey.from_dict(raw_key)
        except (KeyError, TypeError, ValueError):
            return self._reject(command, Reason.BAD_PARAMETER, "key 不合法")
        count, refusal = self._template_count_of(
            command, payload, self.template_policy.encode_count_per_cell)
        if refusal is not None:
            return refusal
        invalid = self._template_stack_error(state, group, cell, key, count)
        if invalid is not None:
            return self._reject(command, Reason.PATTERN_INVALID, invalid)
        state.config.set_cell(group, cell, key, count)
        return CommandResult.ok(command.request_id, {
            "action": "template_cell_put",
            "state": self._template_state_payload(state),
        })

    def _template_stack_error(self, state, group, cell, key, count):
        # type: (object, str, int, object, int) -> object
        """这一格能不能装下这个东西。装不下返回一句说明，能装返回 `None`。

        **用一个真条目试一遍**，而不是在这里重写一遍规则：合成样板的输入
        带槽位摆放，流体不能占槽位、槽位不能重复……那些判据全在
        `PatternStack` 与 `PatternDefinition` 里。放进去之后才在编码那一步
        报错的话，玩家会先看到格子上了图标、再被告知「不行」。
        """
        try:
            if state.config.mode == PATTERN_MODE_CRAFTING:
                PatternStack(key, count, slot=cell)
            else:
                PatternStack(key, count)
        except PatternValidationError as error:
            return error.detail or "这个物品不能放进样板"
        except (TypeError, ValueError):
            return "这个物品不能放进样板"
        return None

    def _cmd_template_clear(self, command, payload):
        # type: (object, dict) -> CommandResult
        """原子清空九宫格与产物配置，保留模式和真实样板槽。"""
        state, refusal = self._template_state_for(command)
        if refusal is not None:
            return refusal
        gate = self._template_gate(command, payload, state)
        if gate is not None:
            return gate
        state.config.clear()
        return CommandResult.ok(command.request_id, {
            "action": "template_clear",
            "state": self._template_state_payload(state),
        })

    def _cmd_template_cell_clear(self, command, payload):
        # type: (object, dict) -> CommandResult
        """清空一格虚格。**空格子算成功**——重复点不该报错。"""
        state, refusal = self._template_state_for(command)
        if refusal is not None:
            return refusal
        group, cell, refusal = self._template_cell_of(command, payload)
        if refusal is not None:
            return refusal
        gate = self._template_gate(command, payload, state)
        if gate is not None:
            return gate
        if state.config.locked(group, cell):
            return self._reject(
                command, Reason.TEMPLATE_CELL_LOCKED,
                "这一格在当前模式下不能改（合成的产物由服务端检测）",
            )
        state.config.clear_cell(group, cell)
        return CommandResult.ok(command.request_id, {
            "action": "template_cell_clear",
            "state": self._template_state_payload(state),
        })

    def _cmd_template_cell_count(self, command, payload):
        # type: (object, dict) -> CommandResult
        """改一格虚格的数量（长按虚格 → 数量窗 → 确认）。"""
        state, refusal = self._template_state_for(command)
        if refusal is not None:
            return refusal
        group, cell, refusal = self._template_cell_of(command, payload)
        if refusal is not None:
            return refusal
        gate = self._template_gate(command, payload, state)
        if gate is not None:
            return gate
        if state.config.locked(group, cell):
            return self._reject(
                command, Reason.TEMPLATE_CELL_LOCKED,
                "这一格在当前模式下不能改（合成的产物由服务端检测）",
            )
        count, refusal = self._template_count_of(command, payload)
        if refusal is not None:
            return refusal
        if state.config.cell(group, cell) is None:
            return self._reject(
                command, Reason.BAD_PARAMETER, "格子是空的，没有数量可以改")
        state.config.set_count(group, cell, count)
        return CommandResult.ok(command.request_id, {
            "action": "template_cell_count",
            "state": self._template_state_payload(state),
        })

    def _cmd_template_encode(self, command, payload):
        # type: (object, dict) -> CommandResult
        """点「OK」：把当前配置编码进输入槽那块样板，输出到产物槽（11-05）。

        已确认的执行顺序（交接第 5 节「服务端须在执行时重新检查」）：

        1. 组一份 `PatternDefinition`——合成模式**有序**取玩家摆的格子、
           产物取服务端检测到的那一个；工厂模式**无序**、产物取玩家配的
           输出格（第一格主产物，其余副产物，那只是显示顺序）；
        2. 输入槽必须是**工厂样板**且够扣；
        3. 产物槽**空，或已有样板与本次配置完全相同且有空间**（User
           2026-10-02）——「配置相同」直接用资源键比：`customTips` 与
           `userData` 都进身份，同一份配置编两次得到同一个键；
        4. **先造产物、再扣输入**（同 `_cmd_craft_take`）：反过来的话，
           产物槽放不下时就会「原料扣了、样板没出来」。
        """
        state, refusal = self._template_state_for(command)
        if refusal is not None:
            return refusal
        gate = self._template_gate(command, payload, state)
        if gate is not None:
            return gate
        preview, blocked = self._template_detect(state)
        if blocked is not None:
            return CommandResult.blocked(command.request_id, blocked.reasons)
        try:
            definition = pattern_from_config(
                state.config, preview, self.template_policy)
        except PatternValidationError as error:
            return self._reject(
                command, Reason.TEMPLATE_CONFIG_INVALID,
                error.detail or "样板配置不完整",
            )

        entry = state.slots.entry(INPUT_SLOT)
        cost = self.template_policy.encode_input_cost
        if entry is None:
            return self._reject(
                command, Reason.OUT_OF_RANGE, "输入槽缺少工厂样板",
            )
        if entry[0].name != TEMPLATE_PANEL_ITEM_ID:
            return self._reject(
                command, Reason.PATTERN_KIND_MISMATCH,
                "输入槽只能放空白工厂样板",
            )
        if entry[1] < cost:
            return self._reject(
                command, Reason.OUT_OF_RANGE, "输入槽里的工厂样板不足",
            )

        key, _item = encode_template_item(definition, name_of=self.templates.name_of)
        room = self.templates.room_for(state, RESULT_SLOT, key)
        result = state.slots.entry(RESULT_SLOT)
        if result is not None and result[0] != key:
            return self._reject(
                command, Reason.SLOT_OCCUPIED,
                "无法与输出槽内的样板堆叠，请先取出",
            )
        if room < self.template_policy.encode_output_count:
            return self._reject(
                command, Reason.NO_CAPACITY, "输出槽放不下这一块样板",
            )

        added = state.slots.add_at(
            RESULT_SLOT, key, self.template_policy.encode_output_count)
        if added < self.template_policy.encode_output_count:
            # 走到这里说明「有空间」那个判断与实际不符——**不吞原料**。
            state.slots.take_at(RESULT_SLOT, added)
            self.craft_undo_failures += 1
            return self._reject(
                command, Reason.NO_CAPACITY, "输出槽放不下这一块样板",
            )
        state.slots.take_at(INPUT_SLOT, cost)
        return CommandResult.ok(command.request_id, {
            "action": "template_encode",
            "template": {"name": key.name, "count": self.template_policy.encode_output_count},
            "digest": definition.digest(),
            "state": self._template_state_payload(state),
        })

    def _template_input_error(self, item_key):
        # type: (object) -> tuple
        """放入输入槽的物品是不是本模组的样板。返回 `(reason, detail)`。"""
        if item_key.name == TEMPLATE_PANEL_ITEM_ID:
            return None, ""
        if item_key.name in ITEM_TEMPLATES:
            return (Reason.PATTERN_KIND_MISMATCH,
                    "输入槽只能放入工厂样板，%s 是另一种样板" % (item_key.name,))
        return (Reason.PATTERN_NOT_A_TEMPLATE,
                "%s 不是本模组的样板" % (item_key.name,))

    def _cmd_template_input_put(self, command, payload):
        # type: (object, dict) -> CommandResult
        """把背包里的一块工厂样板放进输入槽。

        源是**玩家背包**（User 2026-10-02）：`slot` + `slot_version` 是背包槽锚，
        因此这条命令**不走**配置修订号那道闸。

        **空白与已编码的样板都收**（User 2026-10-02）：放一块已编码的进去就是
        「打算更新它」。但类型必须对——合成样板不收。
        """
        state, refusal = self._template_state_for(command)
        if refusal is not None:
            return refusal
        raw_key = payload.get("key")
        if not isinstance(raw_key, dict):
            return self._reject(command, Reason.BAD_PARAMETER, "放入样板槽需要给出 key")
        try:
            item_key = ResourceKey.from_dict(raw_key)
        except (KeyError, TypeError, ValueError):
            return self._reject(command, Reason.BAD_PARAMETER, "key 不合法")
        reason, detail = self._template_input_error(item_key)
        if reason is not None:
            return self._reject(command, reason, detail)
        anchor = self._anchor_of(payload)
        if anchor is None or anchor.slot is None:
            return self._reject(
                command, Reason.BAD_PARAMETER, "放入样板槽需要给出背包槽号")
        endpoint, refusal = self.storage.inventory_endpoint_for(command, payload)
        if refusal is not None:
            return refusal

        current = state.slots.entry(INPUT_SLOT)
        if current is not None and current[0] != item_key:
            return self._reject(
                command, Reason.SLOT_OCCUPIED, "输入槽里已是另一块样板")
        room = self.templates.room_for(state, INPUT_SLOT, item_key)
        if room <= 0:
            return self._reject(command, Reason.SLOT_OCCUPIED, "输入槽已满")
        want = room
        if self.template_policy.device_slot_put_count == PUT_COUNT_ONE:
            want = 1

        op_id = payload.get("op_id") or command.scoped_request_id()
        # **拿回执说话**：客户端说的是「那一格里有几块」，实不实由端点核对。
        receipt = endpoint.anchored_extract(op_id, anchor, want)
        if receipt.moved <= 0:
            # 同 `_template_slot_take`：如实回带端点给的原因，别一律说成
            # 「那一格是空的」——被拒的真实理由可能是槽里的东西换了。
            return self._reject(
                command,
                receipt.reason or Reason.INSUFFICIENT_STOCK,
                "未能从背包取出此样板，请检查背包",
            )
        added = state.slots.add_at(INPUT_SLOT, item_key, receipt.moved)
        if added < receipt.moved:
            # 放不进去的**原路还回背包**：留下来的那半件对不上任何东西。
            back = endpoint.anchored_insert(
                "%s:undo" % (op_id,), anchor, receipt.moved - added)
            if back.moved < receipt.moved - added:
                self.craft_undo_failures += 1
        return CommandResult.ok(command.request_id, {
            "action": "template_input_put",
            "moved": added,
            "state": self._template_state_payload(state),
        })

    def _cmd_template_input_take(self, command, payload):
        # type: (object, dict) -> CommandResult
        """把输入槽那块样板取回背包。"""
        return self._template_slot_take(command, payload, INPUT_SLOT)

    def _cmd_template_output_take(self, command, payload):
        # type: (object, dict) -> CommandResult
        """把产物槽那块已配置的样板取回背包。"""
        return self._template_slot_take(command, payload, RESULT_SLOT)

    def _template_slot_take(self, command, payload, index):
        # type: (object, dict, int) -> CommandResult
        """真实槽 → 背包的公共实现。

        顺序与 `_cmd_craft_take` 相同：**先把东西放进背包、再扣格子**。
        反过来的话，背包满了就会出现「格子空了、东西也没拿到」。
        给出背包槽锚时放进那一格，否则由端点自己找第一个装得下的。
        """
        state, refusal = self._template_state_for(command)
        if refusal is not None:
            return refusal
        key = state.slots.key_at(index)
        count = state.slots.count_at(index)
        if key is None or count <= 0:
            return self._reject(command, Reason.OUT_OF_RANGE, "这一格是空的")
        endpoint, refusal = self.storage.inventory_endpoint_for(command, payload)
        if refusal is not None:
            return refusal
        op_id = payload.get("op_id") or command.scoped_request_id()
        anchor = self._anchor_of(payload)
        before = endpoint.inventory_view()["slots"] if anchor is None else ()
        if anchor is not None and anchor.slot is not None:
            # **锚上的 key 必须换成「要放进去的那一件」。** `anchored_insert`
            # 把 `expected_key` 同时当**插入物**与**目标槽的指纹**用
            # （见 `transfer.anchored_insert` → `slot_insert` 的 `resource_key`）。
            # 而客户端在「选中背包槽 → 点产物槽」时报上来的 key 是**那一格
            # 现在装着的东西**（它想表达的是「放进这一格，我看到的它是这个」）。
            # 两者在 06 的搬运里天然相同，在这里**不同**：东西从设备这一格来。
            # 不换的话，插入物就成了目标格里那件无关物品——玩家的东西被复制
            # 一份、设备里的样板同时被扣掉（2026-10-02 读代码发现，未上机复现）。
            # 槽号与版本锚原样保留：目标格现在装着别的东西时，`slot_insert`
            # 仍会拒绝（`slot_occupied`），那一格空着则照常放入。
            anchor = SlotAnchor(anchor.slot, key, anchor.expected_version)
            receipt = endpoint.anchored_insert(op_id, anchor, count)
        else:
            receipt = endpoint.commit_insert(op_id, key, count)
        if receipt.moved <= 0:
            # **如实回带端点说的原因**，不统一写成「放不下」：端点拒绝的理由
            # 有四种（槽被占、槽里的东西换了、容器不确定、确实放不下），
            # 一句话盖住全部会让排障从「看一眼回执」变成「猜」。
            # 2026-10-02 就是这么被误导过一次。
            return self._reject(
                command,
                receipt.reason or Reason.NO_CAPACITY,
                "背包放不下这块样板",
            )
        state.slots.take_at(index, receipt.moved)
        return CommandResult.ok(command.request_id, {
            "action": "template_input_take" if index == INPUT_SLOT
            else "template_output_take",
            "moved": receipt.moved, "key": key.to_dict(),
            "bag_slots": self._new_stack_slots(endpoint, before, key) if before else [anchor.slot],
            "state": self._template_state_payload(state),
        })

    def _cmd_cursor_return(self, command, payload):
        bag, error = self.storage.inventory_endpoint_for(command, payload)
        if error is not None:
            return error
        cursor = self.storage.inventories.cursor_of(command.source.player_id)
        if cursor is None or not cursor.return_to_player(bag.adapter):
            return self._reject(command, Reason.CONTAINER_UNCERTAIN, "持有的物品状态未确认，请稍后再试")
        return CommandResult.ok(command.request_id, {"cursor": cursor.snapshot()})

    def _cmd_cursor_drop(self, command, payload):
        """Drop the held stack (or one item) outside the window, as vanilla does."""
        bag, error = self.storage.inventory_endpoint_for(command, payload)
        if error is not None:
            return error
        cursor = self.storage.inventories.cursor_of(command.source.player_id)
        if cursor is None:
            return self._reject(command, Reason.NO_ADAPTER, "此玩家没有持物适配器")
        hand = cursor.snapshot(bag.adapter)
        options = payload.get("config") or {}
        if options.get("revision") != hand["revision"]:
            return self._reject(command, Reason.STALE_VERSION, "持物已变化")
        if not hand["count"]:
            return self._reject(command, Reason.BAD_PARAMETER, "手上没有物品")
        if not cursor.drop(1 if options.get("count") == "one" else None):
            return self._reject(command, Reason.CONTAINER_UNCERTAIN, "持有的物品状态未确认，请稍后再试")
        return CommandResult.ok(command.request_id, {"cursor": cursor.snapshot()})

    def _cmd_grid_move(self, command, payload):
        """Two-tap Pocket transfer between a crafting slot and a named endpoint."""
        grid, error = self._workbench_for(command)
        if error is not None:
            return error
        config = payload.get("config") or {}
        cell = payload.get("cell")
        if (not isinstance(cell, INTEGER_TYPES) or not 0 <= cell < 9
                or config.get("grid_revision") != grid.revision):
            return self._reject(command, Reason.STALE_VERSION, "原料格已变化")
        inventory, error = self.storage.inventory_endpoint_for(command, payload)
        if error is not None:
            return error
        area = payload.get("target")
        adapter = DeviceSlots(grid, inventory.adapter.stack_limit_of)
        device = SlotView(adapter, cell, "workbench:%s" % command.device_ref)
        placing = config.get("placing") is True
        key = self._key_from(payload) if placing else grid.key_at(cell)
        if key is None or key.kind != "item":
            return self._reject(command, Reason.BAD_PARAMETER, "需要物品")
        if area == "bag":
            slot = payload.get("slot")
            if not isinstance(slot, INTEGER_TYPES) or not 0 <= slot < 36:
                return self._reject(command, Reason.BAD_PARAMETER, "背包槽号无效")
            view = inventory.slot_view(slot)
            if view["version"] != payload.get("slot_version") or view["canonical"] != config.get("fingerprint"):
                return self._reject(command, Reason.SLOT_CHANGED, "背包槽已变化")
            other = SlotView(inventory.adapter, slot, inventory.endpoint_id())
        elif area == "network":
            other = self.storage.network_endpoint()
        elif area == "grid":
            into = payload.get("into_slot")
            if not isinstance(into, INTEGER_TYPES) or not 0 <= into < 9 or cell == into:
                return self._reject(command, Reason.BAD_PARAMETER, "目标格无效")
            other = SlotView(adapter, into, "workbench:%s" % command.device_ref)
        else:
            return self._reject(command, Reason.BAD_PARAMETER, "目标区域无效")
        src, dst = (other, device) if placing else (device, other)
        request = TransferRequest(command.scoped_request_id(), src.endpoint_id(), dst.endpoint_id(),
                                  key, inventory.adapter.stack_limit_of(key), command.topology_version,
                                  owner_id=command.source.player_id,
                                  origin=ORIGIN_INTERNAL)
        receipt = self.storage.transfer().commit(request, src, dst)
        result = self._receipt_result(command, receipt, "grid_move")
        if receipt.moved:
            self._match_manual_grid(grid)
            result.payload["key"] = key.to_dict()
        return result

    def _cmd_slot_batch(self, command, payload):
        """Check initial anchors once; stop at the first incomplete placement."""
        config = payload.get("config")
        validate_slot_batch(config)
        bag, error = self.storage.inventory_endpoint_for(command, payload)
        if error is not None:
            return error
        cursor = self.storage.inventories.cursor_of(command.source.player_id)
        if cursor is None:
            return self._reject(command, Reason.NO_ADAPTER, "此玩家没有持物适配器")
        hand = cursor.snapshot()
        if hand["revision"] != config["revision"] or not hand["key"]:
            return self._reject(command, Reason.STALE_VERSION, "持物已变化")
        if cursor.primitives.cursor_record(command.source.player_id).get("pending"):
            return self._reject(command, Reason.CONTAINER_UNCERTAIN, "持有的物品状态未确认，请稍后再试")
        devices = {}
        for entry in config["placements"]:
            area = entry["target"]
            if area == "bag":
                view = bag.slot_view(entry["cell"])
                if (view["version"] != entry["slot_version"]
                        or view["canonical"] != entry["fingerprint"]):
                    return self._reject(command, Reason.SLOT_CHANGED, "背包槽已变化")
            else:
                group = "grid" if area == "grid" else "template"
                if group not in devices:
                    device, error = (self._workbench_for(command) if group == "grid"
                                     else self._template_state_for(command))
                    if error is not None:
                        return error
                    devices[group] = device
                if devices[group].revision != entry["slot_version"]:
                    return self._reject(command, Reason.STALE_VERSION, "设备槽已变化")

        results, applied = [], False
        status, reasons = "ok", ()
        for entry in config["placements"]:
            area = entry["target"]
            fields = dict(entry)
            fields["config"] = {"gesture": "place", "revision": cursor.snapshot()["revision"]}
            if area == "bag":
                fields["slot"] = entry["cell"]
                fields["config"]["fingerprint"] = entry["fingerprint"]
            else:
                fields["slot_version"] = devices["grid" if area == "grid" else "template"].revision
            virtual = area in ("source", "outputs")
            fields["key"] = hand["key"]
            try:
                handler = self._cmd_template_cell_put if virtual else self._cmd_slot_click
                result = handler(command, fields)
            except Exception as error:
                # Earlier placements are committed; preserve their receipt even if
                # a later engine write fails. Its cursor journal remains authoritative.
                result = self._reject(command, Reason.INTERNAL, "分配中断：%s" % (error,))
            amount = (entry["count"] if result.is_applied() else 0) if virtual else result.payload.get("moved", 0)
            results.append({"target": area, "cell": entry["cell"], "count": amount})
            applied = applied or result.is_applied()
            if result.status != "ok" or amount != entry["count"]:
                status = "partial" if applied else result.status
                reasons = result.reasons or (BlockReason(Reason.NO_CAPACITY, "未完成全部分配"),)
                break
        out = {"placements": results, "cursor": cursor.snapshot()}
        out["bag_after"] = [bag.slot_view(entry["cell"]) for entry in config["placements"]
                            if entry["target"] == "bag"]
        for group, device in devices.items():
            out[group + "_revision"] = device.revision
        return CommandResult(command.request_id, status, out, reasons)

    def _cmd_slot_click(self, command, payload):
        """Resolve one physical slot gesture against authoritative stacks."""
        bag, error = self.storage.inventory_endpoint_for(command, payload)
        if error is not None:
            return error
        owner = command.source.player_id
        cursor = self.storage.inventories.cursor_of(owner)
        if cursor is None:
            return self._reject(command, Reason.NO_ADAPTER, "此玩家没有持物适配器")
        area = payload.get("target")
        pending = cursor.primitives.cursor_record(owner).get("pending")
        if pending:
            if pending["area"] == "bag":
                cursor.reconcile(bag.adapter, "bag", owner)
            elif area == pending["area"] and area in ("grid", "input", "result"):
                if area == "grid":
                    slots, refusal = self._workbench_for(command)
                else:
                    device, refusal = self._template_state_for(command)
                    slots = device.slots if device is not None else None
                if refusal is None:
                    cursor.reconcile(DeviceSlots(slots, bag.adapter.stack_limit_of),
                                     area, str(command.device_ref))
            if cursor.primitives.cursor_record(owner).get("pending"):
                return self._reject(command, Reason.CONTAINER_UNCERTAIN, "持有的物品状态未确认，请稍后再试")
        hand = cursor.snapshot()
        options = payload.get("config") or {}
        mode = options.get("gesture")
        if (mode not in ("left", "right", "one_in", "one_out", "quick", "place", "take", "collect",
                         "collect_to_slot")
                or options.get("revision") != hand["revision"]):
            return self._reject(command, Reason.STALE_VERSION, "持物已变化")
        collecting = mode in ("collect", "collect_to_slot")
        if collecting and area != "bag":
            return self._reject(command, Reason.BAD_PARAMETER, "双击补叠需要背包槽")
        held = ResourceKey.from_dict(hand["key"]) if hand["key"] else None
        hand_end = ContainerEndpoint(cursor, "cursor:%s" % owner)
        area = payload.get("target")
        index = payload.get("cell")
        network = self.storage.network_endpoint()
        limit = bag.adapter.stack_limit_of
        grid = None
        state = None
        if area == "network":
            key = self._key_from(payload) if payload.get("key") else None
            if key is not None and key.kind != "item":
                return self._reject(command, Reason.BAD_PARAMETER, "需要物品")
            available = network.probe_supply(key, limit(key)) if key else 0
            other = network
        elif area == "bag":
            slot = payload.get("slot")
            if not isinstance(slot, INTEGER_TYPES) or not 0 <= slot < 36:
                return self._reject(command, Reason.BAD_PARAMETER, "背包槽号无效")
            view = bag.slot_view(slot)
            if (view["version"] != payload.get("slot_version")
                    or view["canonical"] != options.get("fingerprint")):
                return self._reject(command, Reason.SLOT_CHANGED, "背包槽已变化")
            key = ResourceKey.from_dict(view["key"]) if view["key"] else None
            available = view["count"]
            if collecting:
                wanted = self._key_from(payload)
                to_slot = mode == "collect_to_slot"
                if wanted is None or wanted != (held or key) or (to_slot and held):
                    return self._reject(command, Reason.SLOT_CHANGED, "补叠物品已变化")
                return self._collect_stack(command, bag, cursor, view, wanted, to_slot)
            other = SlotView(bag.adapter, slot, bag.endpoint_id())
        elif area in ("grid", "input", "result"):
            if area == "grid":
                grid, error = self._workbench_for(command)
                if error is not None:
                    return error
                slots, revision = grid, grid.revision
                if not isinstance(index, INTEGER_TYPES) or not 0 <= index < 9:
                    return self._reject(command, Reason.BAD_PARAMETER, "工作台槽号无效")
            else:
                state, error = self._template_state_for(command)
                if error is not None:
                    return error
                slots, revision = state.slots, state.revision
                index = INPUT_SLOT if area == "input" else RESULT_SLOT
            if revision != payload.get("slot_version"):
                return self._reject(command, Reason.STALE_VERSION, "设备槽已变化")
            adapter = DeviceSlots(slots, limit)
            other = SlotView(adapter, index, "%s:%s" % (area, command.device_ref))
            key, available = slots.entry(index) or (None, 0)
        else:
            return self._reject(command, Reason.BAD_PARAMETER, "槽位区域无效")

        putting = held is not None and mode not in ("one_out", "quick")
        source, target = (hand_end, other) if putting else (other, hand_end)
        if mode == "quick":
            target = network if area == "bag" else bag
        if mode in ("one_in", "place") and not putting:
            return self._reject(command, Reason.INSUFFICIENT_STOCK, "手中没有物品")
        if mode == "take" and putting:
            # 键鼠长按拿取：空手按住一格，松手时拿走按住时长算出的数量。
            return self._reject(command, Reason.BAD_PARAMETER, "长按拿取要空手")
        if putting and area == "result":
            return self._reject(command, Reason.UNSUPPORTED, "成品槽只能取出")
        if putting and area == "input":
            reason, detail = self._template_input_error(held)
            if reason is not None:
                return self._reject(command, reason, detail)
        if putting and key is not None and key != held and area != "network":
            if mode != "left" or area not in ("bag", "grid"):
                return self._reject(command, Reason.SLOT_OCCUPIED, "槽位已占用")
            if not cursor.exchange(other.adapter, other.slot, area, owner if area == "bag" else str(command.device_ref)):
                return self._reject(command, Reason.CONTAINER_UNCERTAIN, "物品状态未确认，请重新打开界面检查")
            result = CommandResult.ok(command.request_id, {"moved": hand["count"],
                                      "reverse": key.to_dict()})
        else:
            resource = held if putting else key
            if resource is None:
                return self._reject(command, Reason.INSUFFICIENT_STOCK, "槽位为空")
            count = min(hand["count"] if putting else available or 0, limit(resource))
            if mode == "right":
                count = 1 if putting else (count + 1) // 2
            elif mode in ("one_in", "one_out"):
                count = 1
            elif mode in ("place", "take"):
                if not isinstance(payload.get("count"), INTEGER_TYPES):
                    return self._reject(command, Reason.BAD_PARAMETER, "无效分配数量")
                count = min(count, payload["count"])
            if putting and state is not None:
                count = min(count, self.templates.room_for(state, index, held))
            if count <= 0:
                return self._reject(command, Reason.INSUFFICIENT_STOCK, "可移动数量为零")
            if mode != "quick" and area in ("bag", "grid", "input", "result"):
                # The two physical slots have readable before/after images. Journal
                # the cursor before changing the other owner, including ordinary pickups.
                capacity = target.probe_space(resource, count)
                moved = min(count, capacity or 0)
                if moved <= 0:
                    return self._reject(command, Reason.NO_CAPACITY, "目标槽已满")
                hand_count = hand["count"] + (-moved if putting else moved)
                cell_count = available + (moved if putting else -moved)
                after = item_dict_from_key(resource, cell_count) if cell_count else None
                hand_after = item_dict_from_key(resource, hand_count) if hand_count else None
                identity = owner if area == "bag" else str(command.device_ref)
                if not cursor.replace_pair(other.adapter, other.slot, area, identity, after, hand_after):
                    return self._reject(command, Reason.CONTAINER_UNCERTAIN, "物品状态未确认，请重新打开界面检查")
                result = CommandResult.ok(command.request_id, {"moved": moved})
            else:
                bag_before = bag.inventory_view()["slots"] if target is bag else ()
                request = TransferRequest(command.scoped_request_id(), source.endpoint_id(), target.endpoint_id(),
                                          resource, count, command.topology_version, owner_id=owner,
                                          origin=ORIGIN_EXTERNAL if target is network else ORIGIN_INTERNAL)
                receipt = self.storage.transfer().commit(request, source, target)
                result = self._receipt_result(command, receipt, "slot_click")
                if bag_before and receipt.moved and result.payload is not None:
                    result.payload["bag_slots"] = self._new_stack_slots(bag, bag_before, resource)
        if grid is not None and result.is_applied():
            self._match_manual_grid(grid)
        if result.payload is not None:
            result.payload["cursor"] = cursor.snapshot()
            if area == "bag":
                result.payload["bag_after"] = bag.slot_view(slot)
            result.payload["putting"] = putting
            result.payload["key"] = (held if putting else key).to_dict()
            if grid is not None:
                result.payload["device_revision"] = grid.revision
        return result

    def _collect_stack(self, command, bag, cursor, view, key, to_slot):
        # type: (object, object, object, dict, ResourceKey, bool) -> CommandResult
        """Collect exact matches on the journalled cursor, then land a Pocket stack in its slot."""
        owner = command.source.player_id
        slot = view["slot"]
        initial = cursor.snapshot()["count"]
        limit = bag.adapter.stack_limit_of(key) - (view["count"] if to_slot else 0)
        count = initial
        sources = [i for i in range(bag.adapter.slot_count()) if i != slot]
        if not to_slot:
            sources.insert(0, slot)
        taken_slots = []
        result = CommandResult.ok(command.request_id)
        for source in sources:
            if count >= limit:
                break
            data = bag.adapter.read_slot(source)
            if not data or not data.get("count") or item_key_from_dict(data) != key:
                continue
            moved = min(data["count"], limit - count)
            after = dict(data, count=data["count"] - moved) if moved < data["count"] else None
            if not cursor.replace_pair(bag.adapter, source, "bag", owner, after,
                                       item_dict_from_key(key, count + moved)):
                result = self._reject(command, Reason.CONTAINER_UNCERTAIN, "物品状态未确认，请重新打开界面检查")
                break
            count += moved
            taken_slots.append(source)

        network_moved = 0
        if result.is_applied() and count < limit:
            network = self.storage.network_endpoint()
            target = ContainerEndpoint(cursor, "cursor:%s" % owner)
            request = TransferRequest(command.scoped_request_id(), network.endpoint_id(),
                                      target.endpoint_id(), key, limit - count,
                                      command.topology_version, owner_id=owner, origin=ORIGIN_INTERNAL)
            receipt = self.storage.transfer().commit(request, network, target)
            network_moved = receipt.moved
            result = self._receipt_result(command, receipt, "slot_click")
            if taken_slots and not result.is_applied() and receipt.status != TransferStatus.UNCERTAIN:
                result = CommandResult.partial(command.request_id, result.payload, result.reasons)
        collected = cursor.snapshot()["count"]
        if to_slot and result.is_applied() and collected:
            # An interrupted landing stays with the persistent cursor until reconciliation.
            current = bag.adapter.read_slot(slot)
            landing = view["count"] + collected
            if (not current or current.get("count") != view["count"]
                    or item_key_from_dict(current) != key
                    or not cursor.replace_pair(bag.adapter, slot, "bag", owner,
                                               item_dict_from_key(key, landing), None)):
                result = self._reject(command, Reason.CONTAINER_UNCERTAIN, "物品状态未确认，请重新打开界面检查")
        if to_slot and not result.is_applied() and collected:
            cursor.return_to_player(bag.adapter)
        result.payload.update(cursor=cursor.snapshot(), key=key.to_dict(), bag_after=bag.slot_view(slot),
                              bag_slots=taken_slots, network_moved=network_moved)
        return result

    @staticmethod
    def _new_stack_slots(bag, before, key):
        return [new["slot"] for old, new in zip(before, bag.inventory_view()["slots"])
                if new["canonical"] == key.canonical() and new["count"] > old["count"]]

    def _match_manual_grid(self, grid):
        cells = grid.cells()
        recipe = self._retained_recipe(grid)
        if recipe is not None and match(recipe, cells) is not None:
            return
        if not self.recipes:
            grid.set_recipe(None, None)
            return
        seen = set()
        for i in grid.occupied_slots():
            key = grid.key_at(i)
            if key in seen:
                continue
            seen.add(key)
            try:
                candidates = self.recipes.recipes_for_input(key)
            except Exception:
                # The slot transaction already committed; preserve its receipt.
                self.storage.stats["recipe_lookup_errors"] = self.storage.stats.get("recipe_lookup_errors", 0) + 1
                grid.set_recipe(None, None)
                return
            for recipe in candidates:
                if match(recipe, cells) is not None:
                    item = recipe.output_preview()
                    grid.set_recipe(recipe.digest(), {"name": item.name, "aux": item.aux or 0})
                    return
        grid.set_recipe(None, None)

    def _cmd_inv_take(self, command, payload):
        # type: (object, dict) -> CommandResult
        """从背包取出，存入网络。"""
        return self._transfer_payload(command, payload, DIRECTION_READ)

    def _cmd_inv_put(self, command, payload):
        # type: (object, dict) -> CommandResult
        """从网络取出，放进背包。**按 maxStackSize 拆分，不生成超堆叠物品。**
        """
        return self._transfer_payload(command, payload, DIRECTION_WRITE)

    def _cmd_inv_split(self, command, payload):
        # type: (object, dict) -> CommandResult
        """拆分：从某个槽取走**一部分**存进网络，其余留在原槽。

        实现上就是带槽锚的 `inv_take`——分开命名是因为它在界面上是另一个
        手势，而「同一次点击重发只生效一次」这条要求对两者都一样。
        """
        if payload.get("slot") is None:
            return self._reject(command, Reason.BAD_PARAMETER, "拆分必须给出 slot")
        return self._transfer_payload(command, payload, DIRECTION_READ)

    def _cmd_inv_merge(self, command, payload):
        # type: (object, dict) -> CommandResult
        """合并：把 `slot` 的内容并进 `into_slot`。**背包内部移动，不经过网络。**

        仍然要服务端做，因为客户端看到的两个槽可能已经过期：两个槽锚各自
        核对一遍，任一不过就整体拒绝，而不是「并一半」。
        """
        cursor = self._cursor_refusal(command, payload)
        if cursor is not None:
            return cursor
        endpoint, refusal = self.storage.inventory_endpoint_for(command, payload)
        if refusal is not None:
            return refusal
        from_slot = payload.get("slot")
        into_slot = payload.get("into_slot")
        if from_slot is None or into_slot is None:
            return self._reject(
                command, Reason.BAD_PARAMETER, "合并需要 slot 与 into_slot"
            )
        if from_slot == into_slot:
            return self._reject(command, Reason.BAD_PARAMETER, "两个槽不能是同一个")
        try:
            source_anchor = SlotAnchor(
                from_slot, self._key_from(payload), payload.get("slot_version")
            )
            target_view = endpoint.slot_view(into_slot)
            target_key = (
                ResourceKey.from_dict(target_view["key"])
                if target_view["key"] is not None
                else source_anchor.expected_key
            )
            target_anchor = SlotAnchor(
                into_slot, target_key, payload.get("into_slot_version")
            )
        except (TypeError, ValueError, KeyError) as error:
            return self._reject(command, Reason.BAD_PARAMETER, "槽锚不合法：%s" % (error,))

        op_id = payload.get("op_id") or command.scoped_request_id()
        view = endpoint.slot_view(source_anchor.slot)
        count = min(view["count"], int(payload.get("count") or view["count"]))
        if count <= 0:
            return self._reject(command, Reason.INSUFFICIENT_STOCK, "源槽是空的")

        ok, reason, detail = endpoint.anchor_verdict(source_anchor)
        if not ok:
            return CommandResult.rejected(
                command.request_id, [BlockReason(reason, detail)]
            )
        ok, reason, detail = endpoint.anchor_verdict(target_anchor)
        if not ok and target_view["key"] is not None:
            return CommandResult.rejected(
                command.request_id, [BlockReason(reason, detail)]
            )
        # 先取后放，两次都锚定。取出的那部分若放不进去，**退回原槽**——
        # 这比留在「手里」强，因为手里不属于任何一处权威状态。
        taken = endpoint.anchored_extract(op_id + ":out", source_anchor, count)
        if taken.moved <= 0:
            return self._receipt_result(command, taken)
        placed = endpoint.anchored_insert(op_id + ":in", target_anchor, taken.moved)
        if placed.moved < taken.moved:
            undo = endpoint.anchored_insert(
                op_id + ":undo", source_anchor, taken.moved - placed.moved
            )
            if undo.moved < taken.moved - placed.moved:
                return CommandResult.blocked(
                    command.request_id,
                    [
                        BlockReason(
                            Reason.CONTAINER_UNCERTAIN,
                            "合并只完成了一部分，且有 %d 个物品没能退回原槽"
                            % (taken.moved - placed.moved - undo.moved,),
                        )
                    ],
                )
        return self._receipt_result(
            command, placed if placed.moved >= taken.moved else
            TransferReceipt(
                op_id, count, placed.moved, TransferStatus.PARTIAL,
                Reason.CONTAINER_PARTIAL, taken.resource_key, endpoint.endpoint_id(),
            )
        )

    def _cmd_inv_swap(self, command, payload):
        # type: (object, dict) -> CommandResult
        """交换：把 `slot` 与 `into_slot` 的内容对调。**背包内部，不经过网络。**

        与 `inv_merge` 是同一族的第五个动作（2026-09-30 由 User 补定，见
        `plans/blocks.md` 的「背包槽之间的拆分、合并与交换」）。仍然要服务端
        做，理由与合并一样：客户端看到的两个槽可能已经过期。

        **与 `inv_merge` 的一处差别是有意的**：合并遇上空目标槽要把目标锚
        那一关跳过，而交换**天然**允许目标为空——与空槽交换就是移动。
        因此这里不用 `inv_merge` 那个 `target_view["key"] is not None` 的
        旁路判断，两个锚直接交给 `anchored_swap`，由它的 `allow_empty`
        区分严格程度（源槽严格、目标槽只核版本）。**别把这一条「补」成
        两个锚都严格**，那会把移动到空格判死。
        """
        cursor = self._cursor_refusal(command, payload)
        if cursor is not None:
            return cursor
        endpoint, refusal = self.storage.inventory_endpoint_for(command, payload)
        if refusal is not None:
            return refusal
        from_slot = payload.get("slot")
        into_slot = payload.get("into_slot")
        if from_slot is None or into_slot is None:
            return self._reject(
                command, Reason.BAD_PARAMETER, "交换需要 slot 与 into_slot"
            )
        if from_slot == into_slot:
            return self._reject(command, Reason.BAD_PARAMETER, "两个槽不能是同一个")
        try:
            source_anchor = SlotAnchor(
                from_slot, self._key_from(payload), payload.get("slot_version")
            )
            # 目标槽为空时没有指纹可取，沿用源指纹占位——`anchor_verdict`
            # 会按 `allow_empty` 只核版本，不看这个占位值。做法与
            # `_cmd_inv_merge` 一致，但那一边是在服务层做判断，这边不是。
            target_view = endpoint.slot_view(into_slot)
            target_key = (
                ResourceKey.from_dict(target_view["key"])
                if target_view["key"] is not None
                else source_anchor.expected_key
            )
            target_anchor = SlotAnchor(
                into_slot, target_key, payload.get("into_slot_version")
            )
        except (TypeError, ValueError, KeyError) as error:
            return self._reject(command, Reason.BAD_PARAMETER, "槽锚不合法：%s" % (error,))

        op_id = payload.get("op_id") or command.scoped_request_id()
        receipt = endpoint.anchored_swap(op_id, source_anchor, target_anchor)
        return self._receipt_result(command, receipt)

    @staticmethod
    def _key_from(payload):
        # type: (dict) -> object
        raw = payload.get("key")
        return ResourceKey.from_dict(raw) if isinstance(raw, dict) else None

    def _cmd_inv_quick_move(self, command, payload):
        # type: (object, dict) -> CommandResult
        """快捷转移：把某个槽整槽搬走，方向由 `target` 决定。

        `target == "network"` 走 `inv_take`（进网络），
        `target == "backpack"` 走 `inv_put`（从网络回背包）。
        载荷里的 `count` 缺省为整槽——快捷转移的语义就是「整槽」。
        """
        target = payload.get("target")
        if target not in ("network", "backpack"):
            return self._reject(
                command, Reason.BAD_PARAMETER, "target 必须是 network 或 backpack"
            )
        if not payload.get("count"):
            endpoint, refusal = self.storage.inventory_endpoint_for(command, payload)
            if refusal is not None:
                return refusal
            slot = payload.get("slot")
            if slot is None:
                return self._reject(command, Reason.BAD_PARAMETER, "快捷转移必须给出 slot")
            view = endpoint.slot_view(slot)
            if view["count"] <= 0:
                return self._reject(command, Reason.INSUFFICIENT_STOCK, "该槽是空的")
            payload = dict(payload)
            payload["count"] = view["count"]
            payload["key"] = payload.get("key") or view["key"]
        if target == "network":
            return self._transfer_payload(command, payload, DIRECTION_READ)
        return self._transfer_payload(command, payload, DIRECTION_WRITE)


def _aux_or_zero(ingredient):
    # type: (object) -> int
    """配方条目要求的 aux；通配（`None`）按 0 处理。

    通配条目在引擎给的数据里是「没写 `data`」，而我们这条路径要造一个
    具体的 `ResourceKey` 去取货。取 0 是**实测样本上唯一见过的写法**
    （凡是带 `data` 的一律是 0），但严格说这是把「任意 aux」收窄成了
    「aux 0」——记在 `RecipePolicy.aux_wildcard_when_absent` 那条旁边。
    """
    return 0 if ingredient.aux is None else ingredient.aux


def _find_inventory_slot(inventory, key):
    # type: (object, object) -> tuple
    """在玩家背包里找一格装有这种资源的槽。返回 `(槽号, 槽版本)`。

    找不到返回 `(None, None)`。**只按名字与 aux 比**，与配方匹配同口径：
    原版合成不看附魔与自定义数据，这里也不看，否则一把附魔剑就永远
    配不齐原料。
    """
    if inventory is None:
        return (None, None)
    try:
        view = inventory.inventory_view()
    except Exception:
        return (None, None)
    for row in view.get("slots") or ():
        if row.get("count", 0) <= 0 or row.get("key") is None:
            continue
        candidate = row["key"]
        if isinstance(candidate, dict):
            name = candidate.get("name")
            aux = candidate.get("aux", 0)
        else:
            name, aux = candidate.name, candidate.aux
        if name == key.name and aux == key.aux:
            return (row["slot"], row.get("version"))
    return (None, None)
