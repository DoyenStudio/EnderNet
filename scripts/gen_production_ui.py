# coding=utf-8
"""Generate native inventory screens for the three independent production machines."""
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RP = ROOT / "endernet_rp"

# Preserve the title/title_text artwork authored for resonance_charger.
MACHINE_TITLE = {'type': 'image',
 'anchor_from': 'top_left',
 'anchor_to': 'top_left',
 'size': [64, 16],
 'layer': 1,
 'texture': 'textures/endernet/ui/device_title_background',
 'controls': [{'title_text': {'type': 'label',
                              'anchor_from': 'top_left',
                              'anchor_to': 'top_left',
                              'offset': [3.0, 3.0],
                              'color': [0.3058823529411765, 0.2549019607843137, 0.3137254901960784],
                              'font_scale_factor': 1.2,
                              'font_type': 'default',
                              'text': 'tile.endernet:resonance_charger.name',
                              'text_alignment': 'center'}}]}


# Adapted from SkyblueTech SkybluePanelLib/ThermalGeneratorUI/ChargerUI.
# Copyright (C) 2025 SuperScript@Doyen-Studio; MIT license preserved in
# third_party/skybluetech-ui-LICENSE.txt. These definitions are kept locally so
# regeneration does not require access to the upstream project.
SKYBLUE_COMMON = {'namespace': 'endernet_production_common',
 'machine_slot_background@endernet_container_common.container_item_slot_background_img': {
     'size': '$cell_image_size'},
 'hover_info@common.button': {'type': 'button',
                              '$control_alpha': 1.0,
                              '$default_texture': '',
                              '$hover_texture': '',
                              '$pressed_texture': '',
                              '$is_new_nine_slice': False,
                              '$label_color': [1, 1, 1],
                              '$label_font_scale_factor': 1.0,
                              '$label_font_size': 'normal',
                              '$label_layer': 3,
                              '$label_offset': [0, 0],
                              '$label_text': '',
                              '$nine_slice_buttom': 0,
                              '$nine_slice_left': 0,
                              '$nine_slice_right': 0,
                              '$nine_slice_top': 0,
                              '$nineslice_size': [0, 0, 0, 0],
                              '$pressed_button_name': 'button.no_action',
                              '$texture_layer': 2,
                              'size': ['100%', '100%'],
                              'layer': 2,
                              'button_mappings': [],
                              'default_control': 'default',
                              'hover_control': 'hover',
                              'pressed_control': 'pressed',
                              'is_handle_button_move_event': True,
                              'bindings': [{'binding_type': 'collection_details',
                                            'binding_collection_name': '',
                                            'binding_condition': 'always_when_visible'}],
                              'controls': [{'default': {'type': 'panel'}},
                                           {'hover': {'type': 'image',
                                                      'texture': 'textures/ui/white_background',
                                                      'alpha': 0.25,
                                                      'fill': True,
                                                      'layer': 1,
                                                      'controls': [{'hover_text@common.hover_text': {'layer': 29,
                                                                                                     'property_bag': {'#hover_text': ''}}}],
                                                      'keep_ratio': False}},
                                           {'pressed': {'type': 'image',
                                                        'texture': 'textures/ui/white_background',
                                                        'alpha': 0.25,
                                                        'fill': True,
                                                        'layer': 1,
                                                        'controls': [{'hover_text@common.hover_text': {'layer': 29,
                                                                                                       'property_bag': {'#hover_text': ''}}}],
                                                        'keep_ratio': False}}],
                              'sound_volume': 0.0},
 'status_ring': {'type': 'image',
                 'texture': 'textures/endernet/ui/production/skybluetech/status_ring',
                 'nineslice_size': 1,
                 'visible': True,
                 'color': [1, 1, 1],
                 'alpha': 1,
                 'layer': 10,
                 'keep_ratio': False,
                 'fill': True},
 'burn_flame': {'type': 'panel',
                'size': [13, 13],
                'layer': 1,
                '$mask_img|default': 'textures/endernet/ui/production/skybluetech/flame_mask',
                'controls': [{'bg': {'type': 'image',
                                     'texture': 'textures/endernet/ui/production/skybluetech/flame_img',
                                     'layer': 1,
                                     'keep_ratio': False,
                                     'fill': True}},
                             {'mask': {'type': 'image',
                                       'texture': '$mask_img',
                                       'clip_direction': 'down',
                                       'clip_ratio': 1.0,
                                       'clip_pixelperfect': True,
                                       'layer': 2,
                                       'keep_ratio': False,
                                       'fill': True}},
                             {'fill_up': {'type': 'panel', 'size': [0, 0], 'visible': False}}]},
 'energy_meter_classic': {'type': 'panel',
                          'size': [12, 54],
                          'layer': 1,
                          'controls': [{'bar': {'type': 'panel',
                                                'size': ['100% - 2px', '100% - 2px'],
                                                'layer': 1,
                                                'controls': [{'bg': {'type': 'image',
                                                                     'texture': 'textures/endernet/ui/production/skybluetech/energy/classic_track',
                                                                     'tiled': 'y',
                                                                     'layer': 1,
                                                                     'keep_ratio': False}},
                                                             {'mask': {'type': 'image',
                                                                       'texture': 'textures/endernet/ui/production/skybluetech/energy/orig_fill_rose',
                                                                       'color': [0.52, 0.68, 1.0],
                                                                       'tiled': 'y',
                                                                       'anchor_from': 'bottom_middle',
                                                                       'anchor_to': 'bottom_middle',
                                                                       'size': ['100%', '0%'],
                                                                       'layer': 2,
                                                                       'keep_ratio': False}}]}},
                                       {'frame': {'type': 'image',
                                                  'texture': 'textures/endernet/ui/production/skybluetech/frame_empty_slim',
                                                  'nineslice_size': 1,
                                                  'layer': 3,
                                                  'keep_ratio': False,
                                                  'fill': True}},
                                       {'classic_style': {'type': 'panel',
                                                          'size': [0, 0],
                                                          'visible': False}},
                                       {'label': {'type': 'label',
                                                  'text': '',
                                                  'visible': False,
                                                  'layer': 5}},
                                       {'info@endernet_production_common.hover_info': {'layer': 6}}]},
 'energy_meter@endernet_production_common.energy_meter_classic': {},
 'progress_arrow': {'type': 'panel', 'size': [22, 15], 'layer': 1,
                    'controls': [
                        {'bg': {'type': 'image', 'layer': 1, 'keep_ratio': False, 'fill': True,
                                'texture': 'textures/endernet/ui/production/skybluetech/progress/arrow_bg'}},
                        {'mask': {'type': 'image', 'layer': 2, 'keep_ratio': False, 'fill': True,
                                  'texture': 'textures/endernet/ui/production/skybluetech/progress/arrow_fill',
                                  'clip_direction': 'left', 'clip_ratio': 1.0, 'clip_pixelperfect': True}}]},
 'progress_icon_bolt': {'type': 'panel',
                        'size': [9, 14],
                        'layer': 1,
                        'controls': [{'bg': {'type': 'image',
                                             'texture': 'textures/endernet/ui/production/skybluetech/progress/bolt_bg',
                                             'size': [9, 14],
                                             'layer': 1,
                                             'keep_ratio': False,
                                             'fill': True}},
                                     {'mask': {'type': 'image',
                                               'texture': 'textures/endernet/ui/production/skybluetech/progress/bolt_fill',
                                               'size': [9, 14],
                                               'layer': 2,
                                               'clip_direction': 'up',
                                               'clip_ratio': 1.0,
                                               'clip_pixelperfect': True,
                                               'keep_ratio': False,
                                               'fill': True}},
                                     {'fill_up': {'type': 'panel', 'size': [0, 0], 'visible': False}}]}}


def machine_title(kind):
    result = copy.deepcopy(MACHINE_TITLE)
    if kind == "resonance_generator":
        result["size"] = [88, 16]
    result["controls"][0]["title_text"]["text"] = "tile.endernet:" + kind + ".name"
    return {"title": result}


def label(text, y, binding=None):
    result = {
        "type": "label", "anchor_from": "top_middle", "anchor_to": "top_middle",
        "offset": [0, y], "size": [162, 12], "text": text,
        "color": [0.24, 0.24, 0.30], "text_alignment": "center", "layer": 3,
    }
    if binding:
        result["text"] = "#text"
        result["bindings"] = [{"binding_type": "global", "binding_condition": "always",
                               "binding_name": binding, "binding_name_override": "#text"}]
    return result


def slot(index, x, y):
    return {"slot_%d@endernet_container_common.indexed_container_slot" % index: {
        "anchor_from": "top_left", "anchor_to": "top_left", "offset": [x, y],
        "$container_slot_index": index,
    }}


def button(mode, text, x):
    selected_binding = "#EnderNetProduction.%s_selected" % mode
    controls = []
    for state in ("default", "hover"):
        controls.append({state: {"type": "panel", "controls": [
            {"normal@endernet_ui_common.button_" + state: {"bindings": [
                {"binding_type": "global", "binding_condition": "always",
                 "binding_name": selected_binding, "binding_name_override": "#mode_selected"},
                {"binding_type": "view", "source_property_name": "(not #mode_selected)",
                 "target_property_name": "#visible"},
            ]}},
            {"selected@endernet_ui_common.button_pressed": {"bindings": [
                {"binding_type": "global", "binding_condition": "always",
                 "binding_name": selected_binding, "binding_name_override": "#visible"},
            ]}},
        ]}})
    controls.append({"pressed@endernet_ui_common.button_pressed": {}})
    return {"mode_" + mode + "@endernet_ui_common.endernet_button": {
        "anchor_from": "top_left", "anchor_to": "top_left", "offset": [x, 23],
        "size": [50, 18], "layer": 5, "$pressed_button_name": "#EnderNetProduction." + mode,
        "$default_texture": "textures/endernet/ui/button_light_default",
        "$label_text": text, "$label_color": [0.20, 0.20, 0.26], "$label_font_size": "normal",
        "$button_content_control": "endernet_ui_common.button_label_content",
        "button_mappings": [
            {"from_button_id": "button.menu_select", "to_button_id": "$pressed_button_name", "mapping_type": "pressed"},
            {"from_button_id": "button.menu_ok", "to_button_id": "$pressed_button_name", "mapping_type": "focused"},
        ],
        "controls": controls,
    }}


def placed_control(name, template, x, y, size=None):
    control = {"anchor_from": "top_left", "anchor_to": "top_left", "offset": [x, y]}
    if size is not None:
        control["size"] = size
    return {name + "@endernet_production_common." + template: control}


def skyblue_machine_layout(kind, root, bg, main):
    """Keep SkyblueTech gauges inside EnderNet's assembly-style container skin."""
    root["size"] = [176, 166]
    # Match assemble_chamber's panel and native slot artwork, without its
    # network stat_bar. The local background also fits the 26px result slot.
    bg["texture"] = "textures/endernet/ui/background_panel_opaque"
    bg["$background_images"] = "endernet_production_common.machine_slot_background"
    if kind == "resonance_generator":
        controls = [
            placed_control("flame", "burn_flame", 52, 20),
            slot(0, 50, 37),
            placed_control("ring_in_gen_slot", "status_ring", 50, 37, [18, 18]),
            placed_control("power_bar", "energy_meter", 108, 17, [18, 54]),
            placed_control("ring_energy", "status_ring", 108, 17, [18, 54]),
        ]
    else:
        output_slot = slot(1, 108, 31)
        output = next(iter(output_slot.values()))
        output["size"] = [26, 26]
        output["controls"] = [{"slot@endernet_container_common.container_grid_item": {
            "collection_index": "$container_slot_index",
            "size": [26, 26], "$cell_image_size": [26, 26],
        }}]
        controls = [
            placed_control("power_bar", "energy_meter", 7, 17, [12, 54]),
            placed_control("ring_energy", "status_ring", 7, 17, [12, 54]),
            slot(0, 50, 35),
            placed_control("ring_in_input_slot", "status_ring", 50, 35, [18, 18]),
            placed_control("progress", "progress_icon_bolt", 84, 38),
            output_slot,
            placed_control("ring_out_output_slot", "status_ring", 108, 31, [26, 26]),
        ]
    main["controls"] = [machine_title(kind)] + controls


def build(kind):
    # Preserve the established native container helpers, cursor and inventory wiring.
    source = json.loads((RP / "ui/endernet/storage/transcriber.json").read_text(encoding="utf-8"))
    result = copy.deepcopy(source)
    namespace = "endernet_" + kind
    result["namespace"] = namespace
    result["main@common.inventory_screen_common"]["$window_content"] = namespace + ".content"
    content = result["content"]["controls"]
    content[:] = [entry for entry in content if not any(
        key.startswith(("device_status_popup@", "endernet_importer_overlay@")) for key in entry)]
    root = next(entry["root_panel@common.root_panel"] for entry in content if "root_panel@common.root_panel" in entry)
    root["size"] = [174, 216]
    bg = root["controls"][0]["bg@common.dialog_background_opaque"]
    main = next(entry["main"] for entry in bg["controls"] if "main" in entry)
    if kind in ("resonance_charger", "resonance_generator"):
        skyblue_machine_layout(kind, root, bg, main)
        return result
    root["size"] = [176, 196]
    controls = [machine_title(kind)]
    controls += [button("grind", "粉碎", 7), button("press", "压印", 62), button("assemble", "封装", 117)]
    controls += [placed_control("power_bar", "energy_meter", 7, 45, [12, 54]),
                 placed_control("ring_energy", "status_ring", 7, 45, [12, 54])]
    # Adjacent 18px input slots form one compact group on the left of the arrow.
    controls += [slot(index, x, 63) for index, x in enumerate((31, 49, 67, 136))]
    controls += [placed_control("ring_slot_%d" % index, "status_ring", x, 63, [18, 18])
                 for index, x in enumerate((31, 49, 67, 136))]
    controls += [placed_control("progress", "progress_arrow", 99, 65)]
    status = label("等待机器", 87, "#EnderNetProduction.status")
    status["offset"] = [12, 87]
    status["size"] = [132, 12]
    controls += [{"status": status}]
    main["controls"] = controls
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    failures = []
    files = []
    outputs = [("common", SKYBLUE_COMMON)] + [
        (kind, build(kind)) for kind in
        ("resonance_charger", "resonance_inscriber", "resonance_generator")
    ]
    for kind, document in outputs:
        relative = "ui/endernet/production/%s.json" % kind
        files.append(relative)
        path = RP / relative
        expected = json.dumps(document, ensure_ascii=False, indent=4) + "\n"
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != expected:
                failures.append(relative)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(expected, encoding="utf-8")
    defs_path = RP / "ui/_ui_defs.json"
    defs = json.loads(defs_path.read_text(encoding="utf-8"))
    for relative in files:
        if relative not in defs["ui_defs"]:
            if args.check:
                failures.append(relative + " missing registration")
            else:
                defs["ui_defs"].append(relative)
    if not args.check:
        defs_path.write_text(json.dumps(defs, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
    if failures:
        raise SystemExit("\n".join(failures))
    print("Production UI: three screens and shared SkyblueTech controls %s" %
          ("checked" if args.check else "generated"))


if __name__ == "__main__":
    main()
