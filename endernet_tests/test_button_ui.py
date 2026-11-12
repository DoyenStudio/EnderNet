# coding=utf-8
"""按钮视觉合同：每个实体按钮都必须把内容放进引擎的三态分支。

这里只解析本包的控件继承、变量引用和显式 controls，不模拟引擎输入。
悬停、按压及移出后的实际视觉仍须在游戏里验收。
"""

import unittest

from ui_terminal_templates import UiControls


# 默认与悬停同位，仅按住时内容下沉 1 像素。
STATES = (("default", [0, -1]), ("hover", [0, -1]), ("pressed", [0, 0]))
CLOSE_STATES = ("default", "hover", "pressed")
BUTTON_TEXTURES = ("textures/endernet/ui/button_", "textures/endernet/ui/frame_rise_")


class ButtonUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ui = UiControls()

    def assert_moving_content(self, button):
        children = self.ui.children(button)
        self.assertNotIn("icon", children)
        self.assertNotIn("button_label", children)
        contents = []
        for state, offset in STATES:
            face = children[state]
            face_node = face[0]
            self.assertTrue(face_node.get("type") == "image"
                            or face_node.get("external_template")
                            == "netease_editor_template_namespace." + state)
            self.assertNotIn("offset", face_node, "按钮底板必须固定")
            wrapper = self.ui.child(face, "wrapper")
            self.assertEqual(self.ui.substitute(wrapper[0]["offset"], wrapper[1]), offset)
            content = self.ui.child(wrapper, "button_content")
            self.assertTrue(self.ui.children(content), "状态内容不能是空面板")
            contents.append(content[0])
        self.assertEqual(contents[0], contents[1])
        self.assertEqual(contents[1], contents[2])

    def test_all_conventional_textured_buttons_use_the_shared_states(self):
        covered = set()
        covered_sort_controls = set()
        for filename, path, key, value in self.ui.records():
            name = key.split("@")[0]
            if name in ("endernet_button", "endernet_badged_button", "endernet_static_button"):
                continue
            ancestry = self.ui.ancestry(key, value)
            if "endernet_ui_common.endernet_static_button" in ancestry:
                continue
            button = self.ui.resolve(key, value)
            if button[0].get("type") != "button":
                continue
            texture = self.ui.substitute(button[0].get("$default_texture", ""), button[1])
            if (not isinstance(texture, str)
                    or not (texture.startswith(BUTTON_TEXTURES)
                            or texture == "textures/endernet/ui/background_panel_opaque")):
                continue
            with self.subTest(file=filename, path="/".join(path)):
                self.assertIn("endernet_ui_common.endernet_button",
                              ancestry)
                self.assert_moving_content(button)
                covered.add(name)
                if name in ("sort_method_btn", "sort_order_btn"):
                    covered_sort_controls.add((filename, name))
        self.assertTrue({"settings_btn", "filter_mode_btn", "add_1x_btn", "sub_100x_btn",
                         "select_from_net_btn", "make_template_btn", "universal_import_btn",
                         "sample_btn", "mode_btn", "save_btn"}.issubset(covered))
        for panel in ("common_terminal_panel", "crafting_terminal_panel", "template_terminal_panel"):
            for name in ("sort_method_btn", "sort_order_btn"):
                self.assertIn(("ui/endernet/terminal/" + panel + ".json", name),
                              covered_sort_controls)

    def test_shared_button_does_not_move_the_hitbox(self):
        button, unused = self.ui.template("endernet_ui_common.endernet_button")
        self.assertNotIn("offset", button)
        for state, unused in STATES:
            self.assertEqual(button[state + "_control"], state)

    def test_plain_and_badged_buttons_reuse_the_same_state_faces(self):
        for template in ("endernet_button", "endernet_badged_button"):
            root, unused = self.ui.template("endernet_ui_common." + template)
            controls = {key.split("@")[0]: key
                        for entry in root["controls"] for key in entry}
            for state, offset in STATES:
                with self.subTest(template=template, state=state):
                    self.assertEqual(controls[state],
                                     state + "@endernet_ui_common.button_" + state)
                    face = self.ui.template("endernet_ui_common.button_" + state)
                    self.assertNotIn("offset", face[0])
                    wrapper = self.ui.child(face, "wrapper")
                    self.assertEqual(self.ui.substitute(wrapper[0]["offset"], wrapper[1]), offset)

    def test_runtime_controls_do_not_use_instance_modifications(self):
        for filename, path, key, value in self.ui.records():
            with self.subTest(file=filename, path="/".join(path)):
                self.assertNotIn("modifications", value,
                                 "Native controls require explicit children or a shared template")

    def test_dynamic_content_paths_exist_in_every_button_state(self):
        references = (
            ("endernet_common_terminal_panel.content", "sort_method_btn", "icon"),
            ("endernet_common_terminal_panel.content", "sort_order_btn", "icon"),
            ("endernet_crafting_terminal_panel.content", "sort_method_btn", "icon"),
            ("endernet_crafting_terminal_panel.content", "sort_order_btn", "icon"),
            ("endernet_template_terminal_panel.content", "sort_method_btn", "icon"),
            ("endernet_template_terminal_panel.content", "sort_order_btn", "icon"),
            ("endernet_bus_item_input_io.settings_window", "filter_mode_btn", "icon"),
            ("endernet_bus_fluid_input_io.settings_window", "filter_mode_btn", "icon"),
            ("endernet_universal_importer.overlay_panel", "control_board/sample_btn", "button_label"),
            ("endernet_universal_importer.overlay_panel", "control_board/mode_btn", "button_label"),
        )
        for reference, button_path, content_name in references:
            with self.subTest(reference=reference, button=button_path):
                button = self.ui.child(self.ui.template(reference), button_path)
                for state, unused in STATES:
                    child = self.ui.child(button, state + "/wrapper/button_content/" + content_name)
                    expected_type = "image" if content_name == "icon" else "label"
                    self.assertTrue(child[0].get("type") == expected_type
                                    or child[0].get("external_template")
                                    == "netease_editor_template_namespace.button_label")

    def test_import_ready_badge_stays_outside_the_moving_content(self):
        content = self.ui.template("endernet_template_terminal_panel.content")
        button = self.ui.child(content, "template_panel/universal_import_btn")
        self.assertEqual(button[0]["$button_badge_control"],
                         "endernet_template_terminal_panel.universal_import_ready_badge")
        self.assertNotIn("modifications", button[0])
        badge = self.ui.child(button, "ok_img")[0]
        self.assertEqual(badge["anchor_from"], "bottom_right")
        self.assertEqual(badge["anchor_to"], "bottom_right")
        self.assertEqual(badge["bindings"][0]["binding_name"],
                         "#EnderNetTemplate.universal_import_ready")
        self.assertNotIn("offset", badge)
        for state, unused in STATES:
            moving = self.ui.child(button, state + "/wrapper/button_content")
            self.assertNotIn("ok_img", self.ui.children(moving))

    def test_importer_drag_mapping_allows_child_buttons_to_receive_the_press(self):
        board = self.ui.child(self.ui.template("endernet_universal_importer.overlay_panel"),
                              "control_board")[0]
        self.assertEqual(board["draggable"], "both")
        mappings = [mapping for mapping in board["button_mappings"]
                    if mapping["from_button_id"] == "button.menu_select"]
        self.assertEqual(len(mappings), 1)
        self.assertIs(mappings[0]["consume_event"], False)

    def test_text_styles_and_button_enabled_binding_survive_content_extraction(self):
        number = self.ui.child(self.ui.template("endernet_windows.number_set_window"), "confirm_btn")
        importer = self.ui.child(self.ui.template("endernet_universal_importer.overlay_panel"),
                                  "control_board/exit_btn")
        preview = self.ui.child(self.ui.template("endernet_windows.order_preview_window"),
                                 "confirm_btn")
        self.assertIn("#EnderNetPreview.submit_enabled",
                      [binding.get("binding_name") for binding in preview[0]["bindings"]])
        unused_key, preview_window = self.ui.definitions["endernet_windows.order_preview_window"]
        confirm = next(value for entry in preview_window["controls"]
                       for key, value in entry.items() if key.split("@")[0] == "confirm_btn")
        self.assertNotIn("$label_offset", confirm)
        self.assertEqual(preview[1]["$label_offset"], [0, 0])
        for state, unused in STATES:
            shadow = self.ui.child(number, state + "/wrapper/button_content/button_label")
            unicode_label = self.ui.child(importer, state + "/wrapper/button_content/button_label")
            self.assertIs(self.ui.substitute(shadow[0]["shadow"], shadow[1]), True)
            self.assertEqual(self.ui.substitute(unicode_label[0]["font_type"], unicode_label[1]),
                             "unicode")

    def test_runtime_labels_do_not_repeat_the_wrapper_vertical_offset(self):
        for filename, path, unused_key, control in self.ui.records():
            for variable, value in control.items():
                if variable.split("|")[0] != "$label_offset":
                    continue
                with self.subTest(file=filename, path="/".join(path), variable=variable):
                    # 数值比较同时覆盖 [0, -1] 和 [0.0, -1.0]。
                    self.assertNotEqual(value, [0, -1])

    def test_source_selector_labels_use_zero_offset_inside_the_moving_wrapper(self):
        for window in ("select_item_window", "select_fluid_window"):
            root = self.ui.template("endernet_windows." + window)
            for name in ("select_from_backpack_btn", "select_from_all_btn", "select_from_net_btn"):
                with self.subTest(window=window, button=name):
                    button = self.ui.child(root, name)
                    self.assertEqual(button[1]["$label_offset"], [0, 0])
                    for state, offset in STATES:
                        wrapper = self.ui.child(button, state + "/wrapper")
                        self.assertEqual(self.ui.substitute(wrapper[0]["offset"], wrapper[1]), offset)
                        label = self.ui.child(wrapper, "button_content/button_label")
                        self.assertEqual(label[1]["$label_offset"], [0, 0])

    def test_icon_only_controls_and_the_cache_switch_are_excluded(self):
        panel = self.ui.child(self.ui.template("endernet_template_terminal_panel.content"),
                              "template_panel")
        for name, texture in (("clear_btn", "textures/endernet/ui/trash_bin"),
                              ("crafting_template_mode_radio_btn", "textures/blocks/crafting_table_top"),
                              ("factoric_template_mode_radio_btn", "textures/blocks/furnace_front_on")):
            button = self.ui.child(panel, name)
            self.assertEqual(button[0]["external_template"], "common.button")
            for texture_state in ("default", "hover", "pressed"):
                self.assertEqual(button[0]["$" + texture_state + "_texture"], texture)
                self.assertNotIn("wrapper", self.ui.children(
                    self.ui.child(button, texture_state)))
        switch = self.ui.child(self.ui.template("endernet_bus_item_output_io.settings_window"),
                               "cache_slots_enable_mode_switch")[0]
        self.assertEqual(switch["external_template"], "common_toggles.switch_toggle_collection")
        self.assertEqual(switch["$default_texture"], "textures/endernet/ui/switch_off_default")
        self.assertNotIn("modifications", switch)

    def test_transparent_and_background_click_layers_do_not_gain_moving_content(self):
        for filename, path, key, value in self.ui.records():
            if value.get("type") != "button":
                continue
            texture = value.get("$default_texture")
            is_click_layer = texture == "" or key.split("@")[0] == "container_slots_background_but"
            is_order_row = (filename.endswith("factoric_terminal_panel.json")
                            and key.split("@")[0] == "interact_btn")
            if is_click_layer or is_order_row:
                with self.subTest(file=filename, path="/".join(path)):
                    self.assertNotIn("endernet_ui_common.endernet_button",
                                     self.ui.ancestry(key, value))

    def test_vanilla_close_instances_use_local_visual_templates(self):
        found = set()
        for filename, path, key, value in self.ui.records():
            if key.split("@")[0] in ("vanilla_close_button", "vanilla_compact_close_button"):
                continue
            base = key.partition("@")[2]
            with self.subTest(file=filename, path="/".join(path)):
                self.assertNotIn(base, ("common.close_button", "common.compact_close_button"))
                self.assertFalse(base.startswith("endernet_close_buttons."))
            if base in ("endernet_ui_common.vanilla_close_button",
                        "endernet_ui_common.vanilla_compact_close_button"):
                found.add(base)
        self.assertIn("endernet_ui_common.vanilla_close_button", found)
        self.assertIn("endernet_ui_common.vanilla_compact_close_button", found)
        self.assertFalse(any(reference.startswith("endernet_close_buttons.")
                             for reference in self.ui.definitions))
        custom = self.ui.template("endernet_ui_common.close_btn")
        for state in CLOSE_STATES:
            wrapper = self.ui.child(custom, state + "/wrapper")
            self.assertEqual(self.ui.substitute(wrapper[0]["offset"], wrapper[1]), [0, 0])
            self.ui.child(wrapper, "button_content/icon")

    def test_close_buttons_in_every_window_keep_their_icon_position(self):
        covered = set()
        for filename, path, key, value in self.ui.records():
            if key.split("@")[0] not in ("close", "compact_close"):
                continue
            with self.subTest(file=filename, path="/".join(path)):
                ancestry = self.ui.ancestry(key, value)
                self.assertNotIn("endernet_ui_common.endernet_button", ancestry)
                button = self.ui.resolve(key, value)
                self.assertEqual(button[0]["type"], "button")
                for state in CLOSE_STATES:
                    wrapper = self.ui.child(button, state + "/wrapper")
                    self.assertEqual(self.ui.substitute(wrapper[0]["offset"], wrapper[1]),
                                     [0, 0], "关闭叉号不能随 hover/pressed 下沉")
                    self.ui.child(wrapper, "button_content/icon")
                covered.add(filename)
        # 两处用户抽检失败的界面必须经过实际实例的完整继承检查。
        self.assertIn("ui/endernet/bus/bus_template_io.json", covered)
        self.assertIn("ui/endernet/terminal/template_terminal_panel.json", covered)

    def test_importer_red_close_button_keeps_its_text_position(self):
        button = self.ui.child(self.ui.template("endernet_universal_importer.overlay_panel"),
                               "control_board/exit_btn")
        self.assertEqual(button[0]["type"], "button")
        self.assertEqual(button[0]["$label_text"], "x")
        self.assertEqual(button[0]["$label_offset"], [0, 0])
        self.assertEqual(button[0]["button_mappings"], [])
        for state, texture in (("default", "button_red_default"),
                               ("hover", "button_red_hovered"),
                               ("pressed", "button_red_pressed")):
            with self.subTest(state=state):
                self.assertEqual(button[0]["$" + state + "_texture"],
                                 "textures/endernet/ui/" + texture)
                wrapper = self.ui.child(button, state + "/wrapper")
                self.assertEqual(self.ui.substitute(wrapper[0]["offset"], wrapper[1]), [0, 0])
                label = self.ui.child(wrapper, "button_content/button_label")
                self.assertEqual(self.ui.substitute(label[0]["font_type"], label[1]), "unicode")

    def test_close_backgrounds_are_fixed_and_the_pressed_cross_has_no_baked_copy(self):
        for name, compact in (("vanilla_close_button", False),
                              ("vanilla_compact_close_button", True)):
            root = self.ui.template("endernet_ui_common." + name)
            self.assertEqual(root[0]["type"], "button")
            # 原版的点击区、声音、关闭映射和绑定继续由原版模板提供。
            for field in ("size", "offset", "button_mappings", "bindings", "sound_name"):
                self.assertNotIn(field, root[0])
            for state in CLOSE_STATES:
                with self.subTest(button=name, state=state):
                    face = self.ui.child(root, state)
                    background = self.ui.child(face, "background")
                    self.assertNotIn("offset", background[0])
                    self.assertEqual(background[0]["uv"], [0, 0])
                    self.assertEqual(background[0]["uv_size"], [1, 1])
                    self.assertEqual(self.ui.substitute(background[0]["visible"], background[1]),
                                     state != "default")
                    wrapper = self.ui.child(face, "wrapper")
                    self.assertEqual(self.ui.substitute(wrapper[0]["offset"], wrapper[1]), [0, 0])
                    icon = self.ui.child(wrapper, "button_content/icon")
                    self.assertEqual(self.ui.substitute(icon[0]["uv"], icon[1]),
                                     [0, 4] if compact else [0, 0])
                    self.assertEqual(self.ui.substitute(icon[0]["uv_size"], icon[1]),
                                     [15, 7] if compact else [15, 15])
                    if state == "pressed":
                        self.assertEqual(self.ui.substitute(icon[0]["texture"], icon[1]),
                                         "textures/ui/close_button_default_light")

    def test_close_template_entrypoints_declare_their_button_type(self):
        for name in ("custom_close_button", "vanilla_close_button",
                     "vanilla_compact_close_button"):
            with self.subTest(template=name):
                unused, definition = self.ui.definitions["endernet_ui_common." + name]
                self.assertEqual(definition["type"], "button")


if __name__ == "__main__":
    unittest.main()
