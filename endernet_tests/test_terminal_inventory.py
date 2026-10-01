# coding=utf-8
"""面板下半屏：本地模拟背包的守恒用例，与真实背包的取放接线。

文件里有两半，**它们已经不再互相依赖**：

- `InventoryTests` 守的是 `InventoryModel`——那块本地模拟背包。面板换成
  `BackpackModel`（真实玩家背包）之后它**没有生产消费者**了，保留它是为了
  这些边界与守恒用例本身：同格再点取消、部分合堆留余、特殊元数据永不合并
  这些口径在搬运改由服务端做之后仍然成立，是后续手势扩展的现成回归位。
- `ScreenTests` 守面板本身：界面 JSON 与常量的接线，以及点击进到「选中哪
  一格」为止的槽位换算。**格子怎么画不在这里**：下半屏 36 格全部走引擎的
  集合绑定（`TerminalPanelBase` 类体里的 `backpack_*` / `hotbar_*`），
  那几条在 `test_terminal_panel.py` 的 `TestBackpackCellBindings` 里。

**发命令那一半不在这里**：从这里往下走会用到 `NetworkClient` 与
`stack_size`，两者的替身都由 `test_terminal_panel.py` 的夹具常驻提供；本
文件用 `patch.dict` 装临时替身、退出即还原（免得把替身漏给别的测试文件），
因此不碰需要常驻替身的那两条路。真实搬运的用例见 `test_terminal_panel.py`
的 `TestBackpackHandoff`。
"""

import json
import importlib.util
import os
import random
import sys
import unittest
import types
from collections import Counter
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import testsupport  # noqa: E402  设置 sys.path，并提供引擎物品字典的实测形状

from endernet_bp.endernet_scripts.endernet.client.ui.inventory_model import (  # noqa: E402
    InventoryModel, SLOT_COUNT, item_key,
)


class ItemStub(object):
    def __init__(self, name="stone", count=1, limit=64, orig=None, **metadata):
        self.newItemName = "minecraft:" + name
        self.newAuxValue = 0
        self.count = count
        self.limit = limit
        #: 引擎给的原始字典。**只有从引擎读回来的物品才有**，身份靠它算
        #: （`engine_item_key`）；`InventoryModel` 那些手工构造的没有。
        self._orig = orig if orig is not None else {}
        for field in ("enchantData", "modEnchantData", "customTips", "extraId",
                      "userData", "durability"):
            setattr(self, field, metadata.get(field))

    def GetBasicInfo(self):
        return type("BasicInfo", (), {"maxStackSize": self.limit, "maxDurability": 0})()


def model_with(*entries):
    slots = [None] * SLOT_COUNT
    for index, item in entries:
        slots[index] = item
    return InventoryModel(slots)


def totals(model):
    result = Counter()
    for item in model.snapshot():
        if item is not None:
            result[item_key(item).canonical()] += item.count
    return result


class InventoryTests(unittest.TestCase):
    def test_move_between_boundary_slots_and_cancel(self):
        model = model_with((26, ItemStub(count=40)))
        model.click(26)
        self.assertEqual(model.get_item(26).count, 40)
        model.click(26)
        self.assertIsNone(model.selected)
        model.click(26)
        model.click(35)
        self.assertIsNone(model.get_item(26))
        self.assertEqual(model.get_item(35).count, 40)

    def test_partial_merge_retains_remainder_then_empty_target(self):
        model = model_with((0, ItemStub(count=40)), (27, ItemStub(count=50)))
        before = totals(model)
        model.click(0)
        model.click(27)
        self.assertEqual(model.get_item(27).count, 64)
        self.assertEqual(model.get_item(0).count, 26)
        self.assertEqual(model.selected, 0)
        self.assertEqual(model.click(27), ())
        model.click(35)
        self.assertEqual(model.get_item(35).count, 26)
        self.assertEqual(totals(model), before)

    def test_complete_merge_and_nonstandard_stack_limits(self):
        model = model_with((0, ItemStub("egg", 2, 16)), (1, ItemStub("egg", 14, 16)))
        model.click(0)
        model.click(1)
        self.assertIsNone(model.get_item(0))
        self.assertEqual(model.get_item(1).count, 16)
        self.assertIsNone(model.selected)

    def test_swap_and_special_metadata_never_merge(self):
        for metadata in ({"customTips": "named"}, {"extraId": "unique"},
                         {"userData": {"nested": [1, 2]}}, {"durability": 5},
                         {"enchantData": [(1, 2)]}):
            model = model_with((0, ItemStub(count=3, **metadata)), (27, ItemStub(count=5)))
            before = totals(model)
            model.click(0)
            model.click(27)
            self.assertEqual(model.get_item(0).count, 5)
            self.assertEqual(model.get_item(27).count, 3)
            self.assertEqual(totals(model), before)
        model = model_with((0, ItemStub("stone", 4)), (1, ItemStub("dirt", 6)))
        model.click(0)
        model.click(1)
        self.assertEqual(model.get_item(0).newItemName, "minecraft:dirt")

    def test_snapshot_ownership_and_atomic_replace(self):
        original = ItemStub(count=3, userData={"nested": [1]})
        model = model_with((0, original))
        original.userData["nested"].append(2)
        model.get_item(0).userData["nested"].append(3)
        self.assertEqual(model.get_item(0).userData, {"nested": [1]})
        before = totals(model)
        with self.assertRaises(ValueError):
            model.replace([ItemStub(count=65)] + [None] * 35)
        self.assertEqual(totals(model), before)
        with self.assertRaises(ValueError):
            model.replace([None] * 35)
        for index in (-1, 36, True, "0"):
            with self.assertRaises(ValueError):
                model.click(index)

    def test_random_moves_preserve_counts_and_limits(self):
        rng = random.Random(20260924)
        items = [ItemStub(rng.choice(("stone", "dirt", "apple")), rng.randint(1, 64))
                 if index % 3 else None for index in range(SLOT_COUNT)]
        model = InventoryModel(items)
        before = totals(model)
        for unused in range(1000):
            model.click(rng.randrange(SLOT_COUNT))
            self.assertEqual(totals(model), before)
            for item in model.snapshot():
                if item:
                    self.assertTrue(0 < item.count <= item.limit)
        model.cancel()
        self.assertEqual(totals(model), before)


class ScreenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Load production screen with only its engine boundary replaced.
        # This validates Python routing/render calls, not native engine events.
        prefix = "endernet_bp.endernet_scripts."
        ui = types.ModuleType(prefix + "tooldelta.ui")
        item = types.ModuleType(prefix + "tooldelta.define.item")
        item.Item = ItemStub

        class ScreenStub(object):
            def __init__(self, name, instance, params):
                self._init_params = params or {}

        class BinderStub(object):
            # 面板的绑定全部写在类体里（`@Binder.binding` / `binding_collection`），
            # 因此这几个旗标**必须在导入期就存在**——少一个，`terminal_panel`
            # 整个模块导不进来。取值本身无关（见 `test_terminal_panel` 的同名替身）。
            BF_BindString = 1
            BF_BindInt = 2
            BF_BindBool = 3
            BF_ButtonClickUp = 4
            BF_EditChanged = 5
            #: 长按判定的两个相位之一（`inv_split` 的手势）。真引擎的
            #: `ViewBinder` 上确实有它（2026-10-01 用 `dir()` 看过），
            #: 这个桩只是没抄全。
            BF_ButtonClickDown = 6

            @staticmethod
            def binding(flag, name):
                def decorate(func):
                    func.binding_flags = flag
                    func.binding_name = name
                    return func
                return decorate

            @staticmethod
            def binding_collection(flag, collection, name):
                def decorate(func):
                    func.binding_flags = flag
                    func.binding_name = name
                    func.collection_name = collection
                    return func
                return decorate

        ui.ToolDeltaScreen = ScreenStub
        ui.Binder = BinderStub
        ui.RegistToolDeltaScreen = lambda name: lambda screen: screen
        prefix_path = "endernet.client.ui."
        path = os.path.join(ROOT, "endernet_bp", "endernet_scripts", "endernet", "client", "ui")
        spec = importlib.util.spec_from_file_location(
            prefix + prefix_path + "terminal_panel", os.path.join(path, "terminal_panel.py"))
        cls.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {ui.__name__: ui, item.__name__: item}):
            spec.loader.exec_module(cls.module)
            # `backpack_model` 是 `terminal_panel` 的相对导入拉进来的，因此拿到
            # 的就是它用的那一个（不是另加载一份）。要替掉 `_read_engine_items`
            # 必须打在同一个对象上，见 `engine_backpack`。
            cls.backpack = importlib.import_module(
                prefix + prefix_path + "backpack_model")

    def engine_backpack(self, *entries):
        # type: (tuple) -> object
        """装一份假的引擎背包读数（**引擎槽序**：0–8 快捷栏、9–35 背包）。

        返回 `patch.object` 的上下文管理器，用例自己管进出——这样 `with` 之外
        的代码（比如构造面板之后）不会被替身包住。
        """
        items = [None] * SLOT_COUNT
        for slot, name, count in entries:
            items[slot] = ItemStub(
                name, count, orig=testsupport.engine_item("minecraft:" + name, count))
        return patch.object(self.backpack, "_read_engine_items", lambda: items)

    def slot(self, data, name):
        # type: (dict, str) -> dict
        """那一段的格子模板实例。键上挂着 `@` 继承，取的是继承之后的那一份。"""
        for key, value in data.items():
            if key.split("@")[0] == name + "_slot":
                return value
        raise AssertionError("no slot control named %r" % (name + "_slot",))

    def test_json_grids_and_registration(self):
        ui = os.path.join(ROOT, "endernet_rp", "ui")
        with open(os.path.join(ui, "endernet_terminal_inventory.json"), encoding="utf-8") as handle:
            data = json.load(handle)
        controls = dict(entry for control in data["inventory"]["controls"] for entry in control.items())
        for name, count, dimensions in (("backpack", 27, [9, 3]), ("hotbar", 9, [9, 1])):
            self.assertEqual(controls[name]["maximum_grid_items"], count)
            self.assertEqual(controls[name]["grid_dimensions"], dimensions)
            self.assertEqual(controls[name]["collection_name"], "endernet_terminal_" + name)
            # 模板指向两段各自的实例，而不是另一段、也不是共用模板本身：指错了
            # 就是两段都画同一批数据，而界面上看不出哪边错了。
            self.assertEqual(controls[name]["grid_item_template"],
                             "endernet_terminal_inventory." + name + "_slot")
            self.assertEqual(self.slot(data, name)["$item_collection_name"],
                             controls[name]["collection_name"])
        with open(os.path.join(ui, "_ui_defs.json"), encoding="utf-8") as handle:
            self.assertIn("ui/endernet_terminal_inventory.json", json.load(handle)["ui_defs"])

    def test_each_slot_wrapper_overrides_every_variable_the_shared_template_uses(self):
        """两段的薄包装只覆盖变量，视觉结构一个字不动。

        **六个都要覆盖**：漏一个就会静默沿用 `endernet_item_list` 那套默认值
        （另一个窗口的集合与绑定名），而那种格子会去读别人的数据——格子在、
        图也有，只是永远为空、也点不动。

        绑定名与 `terminal_panel.slot_binding` 逐字对齐：名字错一个字符，
        引擎只是查不到绑定、安静地给假值——界面上什么都不会发生。
        """
        ui = os.path.join(ROOT, "endernet_rp", "ui")
        with open(os.path.join(ui, "endernet_terminal_inventory.json"), encoding="utf-8") as handle:
            data = json.load(handle)
        with open(os.path.join(ui, "endernet_windows.json"), encoding="utf-8") as handle:
            shared = json.load(handle)["item_can_be_selected"]
        declared = set(key.split("|")[0] for key in shared if key.startswith("$"))
        #: `$item_*` 变量名与绑定名后缀**不是同一个拼法**（`$item_id_aux_binding`
        #: 对应的绑定名后缀是 `item_id_aux`，去掉了那个 `item_` 前缀的那份不
        #: 在变量名里），因此这里逐条写出来，不做机械拼接。
        fields = (
            ("$item_visible_binding", "visible"),
            ("$item_id_aux_binding", "item_id_aux"),
            ("$item_selected_binding", "selected"),
            ("$item_count_binding", "item_count"),
        )
        # 变量与注册用的字段必须是同一批：类体里少注册一个字段的表现就是
        # 那一格永远空着。
        self.assertEqual(set(suffix for _, suffix in fields),
                         set(self.module.SLOT_FIELDS))
        for prefix in ("backpack", "hotbar"):
            wrapper = self.slot(data, prefix)
            overridden = set(key.split("|")[0] for key in wrapper if key.startswith("$"))
            self.assertEqual(overridden, declared)
            self.assertEqual(wrapper["$item_collection_name"],
                             "endernet_terminal_" + prefix)
            for variable, suffix in fields:
                self.assertEqual(wrapper[variable],
                                 self.module.slot_binding(prefix, suffix))
            self.assertEqual(wrapper["$item_select_button"],
                             self.module.slot_binding(prefix, "click"))

    def test_the_count_label_reads_the_binding_property_it_is_written_to(self):
        """数量标签必须**引用**自己被写入的那个绑定属性。

        `binding_name_override` 只负责把绑定的值**写进**控件的 `#text`
        属性；标签真正渲染的是自己的 `text`。`text` 缺席时属性照样被写，
        但没有任何东西去读它——**界面上一片空白，而绑定其实一直在被调用**
        （2026-10-01 真机：Python 侧 `backpack_item_count(2)` 返回 `"64"`，
        运行时节点 `text` 却是空串）。vanilla 的 `anvil.cost_label` 是
        `"text": "#text"` + `binding_name_override: "#text"` 两条一起写，
        缺一不可。

        另：标签的 `layer` 要压在 `item_renderer` 之上。物品图标按整个
        16×16 铺满（整块类物品是不透明的），标签在它下面就被数字被图标
        盖掉——这条同样是真机上看出来的。
        """
        with open(os.path.join(ROOT, "endernet_rp", "ui", "endernet_windows.json"),
                  encoding="utf-8") as handle:
            shared = json.load(handle)["item_can_be_selected"]
        label = dict(entry for control in shared["controls"]
                     for entry in control.items())["item_count"]
        self.assertEqual(label["text"], "#text")
        binding = label["bindings"][0]
        self.assertEqual(binding["binding_name"], "$item_count_binding")
        self.assertEqual(binding["binding_name_override"], "#text")
        renderer = dict(entry for control in shared["controls"]
                        for entry in control.items())["item_renderer"]
        self.assertGreater(label["layer"], renderer["layer"])

    def test_clicks_select_the_slot_their_collection_index_names(self):
        """界面槽号 → 引擎槽号：点击路径上真的接上了，而且四个边界都对。

        这是一条**会静默出错**的换算。客户端用它同时算出 slot 与 key，服务端
        的锚只回答「这个槽里是不是这件东西」——自己和自己必然一致，于是映射
        错了也不会有人报：表现是**搬走玩家没点的那一格**。因此这里逐条钉住
        上下两排的两端，游戏内逐格实点是唯一的最终确认。

        引擎槽序是**快捷栏在前**（0–8 快捷栏、9–35 背包），与界面正好相反。
        """
        with self.engine_backpack((35, "stone", 2), (8, "apple", 7)):
            screen = self.module.CommonTerminalPanel("test", None, None)
            # 上方 9x3 的最后一格（界面 26）→ 引擎 35。
            screen.OnBackpackClick({"#collection_index": 26})
            self.assertEqual(screen.backpack.selected, 26)
            self.assertEqual(screen.GetBackpackItems()[26], ("minecraft:stone", 2))
            # **先取消选中再点下一格**：2026-09-30 起，「有选中时点另一个槽」
            # 是**背包内部的动作**（合并／交换），不再改选中——不取消就直接
            # 点下一格，量到的会是上一格还没变的选中。
            screen.OnBackpackClick({"#collection_index": 26})   # 同格再点 = 取消
            self.assertIsNone(screen.backpack.selected)
            # 下方那一排的最后一格（界面 35）→ 引擎 8，**不是引擎 36**。
            screen.OnHotbarClick({"#collection_index": 8})
            self.assertEqual(screen.backpack.selected, 35)
            self.assertEqual(screen.GetBackpackItems()[35], ("minecraft:apple", 7))
            # 越界、缺项与非法类型一律不动选中，也不把缺项当成第 0 格。
            for bad in (9, 27, -1, None, True, "0"):
                screen.OnHotbarClick({"#collection_index": bad})
                self.assertEqual(screen.backpack.selected, 35)
            # 上排的越界下标不能落到下排去。
            screen.OnBackpackClick({"#collection_index": 27})
            self.assertEqual(screen.backpack.selected, 35)
            # **有选中时点空格**：那是「与空槽交换」（即移动），但这条裸面板
            # 没有会话，而背包动作必须由服务端执行——因此不发命令，也**不动
            # 选中**。界面 0（引擎 9）是空的。
            screen.OnBackpackClick({"#collection_index": 0})
            self.assertEqual(screen.backpack.selected, 35)
            # **没有选中时点空格**：选中不会被挪到空格上（选中一个空格意味着
            # 下一次点终端行会去搬一格空气）。
            screen.OnHotbarClick({"#collection_index": 8})    # 同格再点 = 取消
            self.assertIsNone(screen.backpack.selected)
            screen.OnBackpackClick({"#collection_index": 0})
            self.assertIsNone(screen.backpack.selected)
            screen.OnDestroy()
            self.assertIsNone(screen.backpack.selected)


if __name__ == "__main__":
    unittest.main()
