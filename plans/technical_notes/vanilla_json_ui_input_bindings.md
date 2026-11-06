# 原版 JSON UI 的手势、按钮映射与数据绑定

样本来自 2026-11-06 本机 MC Studio 的 `D:/MCStudioDownload/MCX64Editor/Game/data/resource_packs/vanilla/ui/`。下文原版文件名与行号均相对于这个目录。递归读取了 219 个 JSON 文件，其中 209 个列在 `_ui_defs.json` 中。统计时去除了注释，计入模板参数数组中的声明，按源码定义计数，不展开继承。以下结论来自静态源码。

## 双击、长按与拖动

### 双击可以直接写进按钮映射

箱子的小网格把 `container_items` 交给 `chest.chest_grid_item`。格子继承 `common.container_item`，后者用 `$button_ref` 挂载 `common.container_slot_button_prototype`。下面节选同一个按钮接收 `button.menu_select` 的两条原版映射。箱子自身的文件里搜不到 `double_pressed`，要顺着这条继承链找。出处为 `chest_screen.json:26–37`、`ui_common.json:4604–4605, 4674–4684, 4721–4729, 4905–4912, 4996`。

```json
{
  "button_mappings": [
    {
      "from_button_id": "button.menu_select",
      "to_button_id": "button.container_take_all_place_all",
      "mapping_type": "pressed"
    },
    {
      "from_button_id": "button.menu_select",
      "to_button_id": "button.coalesce_stack",
      "mapping_type": "double_pressed"
    }
  ]
}
```

同一模板给 `button.menu_ok` 也配了一对。全目录 18 条 `double_pressed` 分布在 9 个文件里，都是上述两个输入成对出现，目标都是合堆动作或默认指向合堆动作的变量。铁砧使用 `button.anvil_coalesce_stack`，Pocket 交易结果槽使用 `button.trade_coalesce_stack`。护甲格则显式选用 `no_coalesce_container_slot_button`。这些差异说明要查实际格子使用的按钮模板。出处为 `anvil_screen.json:194–202`、`trade_screen_pocket.json:106–115, 148–156`、`inventory_screen.json:222, 275–283, 300–362, 1095`。

JSON 能确认双按有独立动作。双按间隔、第一次单按是否也派发、真实触屏连续点两下的结果，以及自定义动作能否送达模组回调，都需要在目标屏幕验证。

### 原版容器把长按拆堆的进度交给 UI 显示

| 顺着源码查 | 可以看到的实现 |
| --- | --- |
| 箱子和背包挂载进度控件 | Pocket 容器挂 `hold_icon@common.inventory_take_progress_icon_button`。这个按钮启用 `follows_cursor`，它的 default、pressed、hover 三态都显示同一个 `inventory_hold_icon`。`pocket_containers.json:151`、`inventory_screen_pocket.json:1446`、`ui_common.json:5716–5732`。 |
| 按住时显示进度与数量 | `progress_bar_renderer` 读 `#progress_bar_visible`、`#progress_bar_total_amount`、`#progress_bar_current_amount`。容器屏幕还挂载 `stack_splitting_overlay`，分别控制 Classic/Pocket 浮层，把 `#progressive_select_bar_ratio` 映射到 `#clip_ratio`，用 `#progressive_select_text` 显示数量。`ui_common.json:3374–3396, 3406–3444, 5607–5635, 6776–6779`。 |
| 拆堆与拖动有各自的时间设置 | UI 调试区有 `#stack_splitting_trigger_time` 与 `#dwell_before_drag_time` 两个滑杆。这些声明引用引擎提供的值，源码中没有给出具体时长。`settings_sections/general_section.json:10045, 10579–10604`。 |

全目录的 `mapping_type` 未出现 `long_pressed`、`held` 或 `released`，也没找到给任意按钮填一个时长就派发动作的原版例子。这里的 `pressed_control` 指定按住时显示的子控件。EnderNet 的样板面板已有“按住够久就打开数量窗”的实现。它记录格子与按下时刻，逐帧检查，达到阈值时触发一次，抬起时补判并避免再执行短按。这个阈值由项目自己设置。见项目 `endernet_bp/endernet_scripts/endernet/client/ui/template_panel.py:464–518`。

**拖动有另一套原版例子。** 结构编辑器的 `input_panel` 将 `menu_select` 映射到 `button.turn_structure`，同时用它作 `gesture_tracking_button`。子控件 `3d_structure_renderer` 通过 `view` 从面板读 `#gesture_mouse_delta_x`、`#gesture_mouse_delta_y` 及来源。需要跟踪按住后的位移时，这个例子比拆堆进度条更直接。见 `structure_editor_screen.json:1875–1898, 1942–1957`。

## button_mappings 有哪几类

一条常规映射用 `from_button_id` 指定收到的逻辑输入，用 `mapping_type` 指定响应方式，用 `to_button_id` 指定转发的动作。菜单选择、手柄确认和次要选择都是逻辑输入，物理按键由输入配置决定。

| 本版出现的 mapping_type | 条数 | 典型用途 |
| --- | ---: | --- |
| `pressed` | 586 | 控件接收按压。普通按钮把 `button.menu_select` 转给 `$pressed_button_name`。容器格还分别处理次要选择（取半或放一）、自动放置、丢一个和丢整叠。`ui_common.json:86–91, 4691–4720`。 |
| `focused` | 175 | 控件取得焦点后响应指定输入。普通按钮用 `button.menu_ok` 执行同一动作，滑杆用确认和取消选择或退出滑杆。`ui_common.json:92–96, 999–1008`。 |
| `global` | 569 | 在当前输入处理范围内统一响应。书本屏幕把退出和取消汇入 `button.book_exit`，左右翻页映射到 `prev_page` 和 `next_page`。`book_screen.json:1095–1115`。 |
| `double_pressed` | 18 | 同一逻辑输入的双按映射，原版样本用于槽位合堆。`ui_common.json:4721–4729`。 |

共 1,348 条映射，含 176 条写在模板参数数组里的定义。源动作还包括 `button.controller_*`、`button.slot0` 至 `button.slot9`、方向/翻页和聊天输入，以及 `key.emote`。目标也可以是变量。公共滚动面板的 `button_mappings` 整个取自 `$scrolling_panel_base_button_mappings`，默认空数组，容器模板再填入自己的映射。见 `emote_wheel_screen.json:4–20`、`ui_common.json:4493–4494, 5184–5215`。

| 映射中的附加字段 | 原版用途与位置 |
| --- | --- |
| `scope`、`ignore_input_scope` | `scope` 出现 `view`、`controller`、`global` 三个值，可以与 `mapping_type` 分开写。暂停屏有 `mapping_type: global` 配 `scope: controller`。滑杆的方向输入用 `ignore_input_scope: true`。`pause_screen.json:484–489`、`ui_common.json:1009–1035`。 |
| `ignored`、`input_mode_condition` | 前者按表达式跳过某条映射，例如只启用与滑杆方向相符的输入。后者出现 `gamepad`、`not_gamepad` 两个值，可按输入模式选择路由。`ui_common.json:479–496, 1009–1035`、`emote_wheel_screen.json:4–10`。 |
| `handle_select`、`handle_deselect`、`consume_event` | 编辑框分开处理选中和取消选中。取消选中的映射写 `consume_event: false`，原注释说明这样同一次鼠标点击还能选中另一个控件。这里的选中变化不能直接当作通用的手指按下/抬起回调。`ui_common.json:1635–1651`。 |
| `button_up_right_of_first_refusal` | 声明优先处理对应的抬起事件。滑杆、滚动视图和 HUD 快捷栏都与 `pressed` 配合使用。`ui_common.json:992–998, 4302–4308`、`hud_screen.json:821–836`。 |

源码中有 88 条省略 `from_button_id`，例如槽位的 `button.shape_drawing` 和 `button.container_slot_hovered`。另有一条 `any/global` 省略目标，位于 `gamepad_layout_screen.json:590–601` 的 `ghost_binding` 中，该文件未列入这份 `_ui_defs.json`。见 `ui_common.json:4731–4737`。

## bindings 有哪些类型，什么时候更新

`binding_type` 说明取哪一类数据，`binding_condition` 说明何时更新，`binding_name` 则是要读的具体数据。比如 `#form_button_texture` 是表单给出的源名，`#texture` 是图片控件接收的属性，`$button_text_binding_type` 是实例可以覆盖的模板参数。

| 本版出现的 binding_type | 用法 | 可查的原版实例 |
| --- | --- | --- |
| `global` | 从当前屏幕可取得的数据源取值，通常无需集合行。 | HUD 的 `#exp_progress → #clip_ratio` 更新经验条。`hud_screen.json:510–520`。 |
| `collection` | 取命名集合中当前项的数据。 | 箱子槽位从 `container_items` 取本格物品，通用物品渲染器读 `#item_id_aux`，数量标签默认读 `#inventory_stack_count`。`chest_screen.json:26–37`、`ui_common.json:3627–3643, 3892–3908`。 |
| `collection_details` | 给控件附上集合项的上下文。二维列表还可以分别附行、列上下文。 | 书本照片按钮给行、列各写一条，用不同 `binding_collection_prefix` 区分，点击动作另写 `button.picked`。教育版公共模板的注释也说明这样能取得网格位置。`book_screen.json:130–150`、`ui_edu_common.json:1331–1336`。 |
| `view` | 从本控件或指定控件读取属性，可以用表达式派生目标属性。 | 表单图片以自己的 `#texture` 决定显隐，旁边的加载指示器用 `source_control_name: image` 和 `resolve_sibling_scope: true` 读同一纹理。下拉面板则从切换控件读 `#toggle_state → #visible`。`server_form.json:172–204`、`ui_common.json:1183–1190`。 |
| `none` | 模板中用于不启用某项可选数据绑定。 | 滑杆可用性与超时绑定类型默认取 `none`，表单实例可改为 `collection`。编辑框也有直接写 `binding_type: none` 的条目。`ui_common.json:931–942, 1594–1597`、`server_form.json:387–395`。 |

另外有大量条目直接省略 `binding_type`，例如熔炉把 `#furnace_arrow_ratio` 送到 `#clip_ratio`。模板也能把 `binding_type` 写成 `$变量`，或把整个 `bindings` 写成 `$button_bindings` 让实例传入数组。查某个控件时，要一并看 `@继承`、`$变量|default` 和挂载处的覆盖值。见 `furnace_screen.json:32–46`、`ui_common.json:86–100, 1075–1087`。

**绑定条目里的常用字段**按职责可分为四组。

- `binding_name` 取源值，`binding_name_override` 把它交给另一个名字的属性。目标常见 `#visible`、`#enabled`、`#text`、`#texture`、`#clip_ratio`、`#collection_length`。表达式也能直接写在 `binding_name`，如 `(not #on_new_death_screen) → #visible`。见 `hud_screen.json:976–982, 993–997`。
- `binding_collection_name` 选集合，`binding_collection_prefix` 区分同一控件接入的不同集合上下文。父列表的 `collection_name`、生成行的 `factory` 和行内的 `bindings` 要连起来读。见 `book_screen.json:130–149`、`server_form.json:119–143`。
- `source_control_name`、`source_property_name`、`target_property_name` 描述 `view` 的来源和目标，`resolve_sibling_scope` 可用于解析同级控件。见 `server_form.json:172–204`。
- `binding_type`、`binding_condition` 分别选类型和更新条件，`ignored` 可跳过单条绑定。例如商店模板按变量跳过图片路径与文件系统两条绑定。见 `store_common.json:117–129`。

| 本版出现的 binding_condition | 原版怎么用 |
| --- | --- |
| `once` | 取一次。例如添加服务器界面的标题，或容器格初次取得集合上下文。`add_external_server_screen.json:108–118`、`ui_common.json:4632–4637`。 |
| `always` | 持续更新。例如熔炉箭头和火焰的裁切比例。`furnace_screen.json:39–45, 59–65`。 |
| `visible` | 按可见条件更新。例如可见网格的尺寸或物品上限。`create_world_upsell_screen.json:398–406`、`ui_common.json:5111–5117`。 |
| `always_when_visible` | 可见期间持续更新。例如取物进度条的当前量和总量。`ui_common.json:5623–5635`。 |
| `visibility_changed` | 按可见性变化更新。例如二维码图片重新绑定 `#qr_texture`。`gathering_info_screen.json:279–291`。 |
| `none` | 显式使用无附加更新条件的形式。命令方块的下拉文字直接这样写，堆叠数量标签也用它作默认条件。注意与上表的 `binding_type: none` 区分。`command_block_screen.json:101–111`、`ui_common.json:3634–3642`。 |

HUD 提示文字展示了为何要区分“刚显示”和“显示中变化”。同一 `#helper_description` 同时绑定 `visible` 和 `always_when_visible`，旁边的原版注释解释了初次显示的延迟与后续文字变化两个问题。该处注释用 “No condition” 描述前一种行为，实际第二条写的是 `visible`。引擎的具体更新时序仍应按控件验证。见 `hud_screen.json:2316–2338`。

### 把一次表单点击从数据追到动作

1. **有几行。** 父面板声明 `collection_name: form_buttons`，把 `#form_button_contents` 绑定到 `#collection_length`。同一面板的 factory 按项创建按钮、标签、标题或分隔线。见 `server_form.json:119–143`。
2. **这行画什么。** 图片从本行拿 `#form_button_texture → #texture`。值为空时图片隐藏，值为 `'loading'` 时隐藏图片并显示加载指示器。按钮文字用本行的 `#form_button_text`。见 `server_form.json:172–204, 212–225`。
3. **点了哪行，做什么。** 按钮附上 `form_buttons` 的 `collection_details`，并把 `$pressed_button_name` 设成 `button.form_button_click`。`light_text_button` 经 `light_button_assets` 继承 `common.button`，在公共按钮里才看到实际的 `pressed` 与 `focused` 两条映射。这样每行能共用一个动作并保留自己的行上下文。见 `server_form.json:212–225`、`ui_template_buttons.json:39, 319`、`ui_common.json:86–100`。

同文件的自定义表单又提供一组对照。开关、滑杆和输入框都取 `custom_form` 当前项，分别绑定 `#custom_toggle_state`、`#custom_slider_value`、`#custom_input_text`。这三个值是不同业务字段，底层都使用同一种 `collection` 绑定。见 `server_form.json:363–370, 383–394, 503–517`。

### 查一个具体的数据名或动作

全目录有 5,151 个绑定条目，其中 4,667 个直接在 `bindings` 数组内，484 个在模板参数数组内。单看 `binding_name` 中直接写成 `#名字` 的值，去重后有 2,078 个，该数字排除了表达式、模板变量和目标属性。移植到自定义屏幕时，还要接入能提供相应数据、处理相应动作的宿主。例如 `#form_button_texture` 取自表单的 `form_buttons`，`#furnace_arrow_ratio` 则由熔炉屏幕提供。

以下命令从 PowerShell 定位源文件和行号，结果也会匹配注释，适合查实例。

```powershell
$ui = 'D:\MCStudioDownload\MCX64Editor\Game\data\resource_packs\vanilla\ui'
rg -n '"mapping_type"\s*:\s*"double_pressed"' $ui
rg -n 'stack_splitting_trigger_time|progressive_select_bar_ratio|inventory_take_progress_icon_button' $ui
rg -n '#form_button_texture|button\.form_button_click' "$ui\server_form.json"
```
