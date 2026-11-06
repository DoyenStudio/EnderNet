# coding=utf-8
"""Inline extracted terminal templates for existing JSON contract assertions.

Only endernet_ui_common.terminal_* inheritance is expanded. Vanilla controls,
window templates and their variable scopes remain untouched. This is a source
contract helper, not an emulator of the game's JSON UI renderer.
"""

import copy
import json
import os


def inline_terminal_controls(document, common=None):
    if common is None:
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "endernet_rp", "ui", "endernet_ui_common.json")
        with open(path, encoding="utf-8") as handle:
            common = json.load(handle)
    definitions = {key.split("@")[0]: (key, value)
                   for key, value in common.items() if isinstance(value, dict)}

    def expand_key(key, value, chain=()):
        name, separator, base = key.partition("@")
        if not base.startswith("endernet_ui_common.terminal_"):
            return key, copy.deepcopy(value)
        template = base.split(".", 1)[1]
        if template in chain:
            raise ValueError("Cyclic terminal template: " + base)
        template_key, defaults = definitions[template]
        inherited_key, inherited = expand_key(template_key, defaults, chain + (template,))
        inherited.update(copy.deepcopy(value))
        inherited_base = inherited_key.partition("@")[2]
        return name + ("@" + inherited_base if inherited_base else ""), inherited

    def walk(node):
        if isinstance(node, list):
            return [walk(value) for value in node]
        if not isinstance(node, dict):
            return node
        result = {}
        for key, value in node.items():
            if isinstance(value, dict):
                key, value = expand_key(key, value)
            if key in result:
                raise ValueError("Duplicate expanded control: " + key)
            result[key] = walk(value)
        return result

    return walk(document)
