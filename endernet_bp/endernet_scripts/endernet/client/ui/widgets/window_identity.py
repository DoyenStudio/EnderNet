# coding=utf-8
"""辅助窗口的身份常量：`_vars` 键、collection 名、绑定名。

**为什么单独一个模块、而且一个引擎 import 都不许有。**

这些常量有两个读者：窗口对象自己按它们开窗口，而**宿主屏的类体绑定**按
它们声明绑定名与 collection。窗口模块会 import 引擎
（`tooldelta.api.client.player` 等），如果宿主那侧要从窗口模块取常量，
就会把「导入终端屏」变成「导入整条 tooldelta 初始化链」——在离线测试里
那一步表现为一个与本次改动毫不相干的循环导入
（`mod.client.ui.controls.baseUIControl` 自己绕回自己）。

因此身份常量住在这一层：两边都从这里取，谁也不用 import 谁。
本模块**只放常量与字符串拼接**，不放逻辑。
"""

#: 物品名单编辑器的 `screen._vars` 键、collection 与绑定名前缀。
#: 三者一起改：`_vars` 键是「窗口开没开」的唯一判据（`show_*` 拒绝第二次
#: 打开就查它），collection 与前缀必须与 `endernet_windows.json` 逐字相同。
ITEM_LIST_WINDOW_ATTRIBUTE = "_endernet_item_list_window"
ITEM_LIST_COLLECTION = "endernet_item_list"
ITEM_LIST_BINDING_PREFIX = "#EnderNetItemList."

FLUID_LIST_WINDOW_ATTRIBUTE = "_endernet_fluid_list_window"
FLUID_LIST_COLLECTION = "endernet_fluid_list"
FLUID_LIST_BINDING_PREFIX = "#EnderNetFluidList."

SELECT_ITEM_WINDOW_ATTRIBUTE = "_endernet_select_item_window"
SELECT_ITEM_SEARCH_BINDING = "#EnderNetSelectItem.search"

SELECT_FLUID_WINDOW_ATTRIBUTE = "_endernet_select_fluid_window"
SELECT_FLUID_SEARCH_BINDING = "#EnderNetSelectFluid.search"

NUMBER_SET_WINDOW_ATTRIBUTE = "_endernet_number_set_window"
NUMBER_SET_EDIT_BINDING = "#EnderNetNumberSet.number"
