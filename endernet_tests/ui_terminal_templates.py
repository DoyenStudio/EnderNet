# coding=utf-8
"""Inline extracted terminal templates for existing JSON contract assertions.

The legacy inline helper expands only endernet_ui_common.terminal_* controls.
UiControls additionally resolves this pack's templates and content parameters
for source-path assertions. Neither helper emulates the game's UI renderer.
"""

import copy
import json
import os


def inline_terminal_controls(document, common=None):
    if common is None:
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "endernet_rp", "ui", "endernet", "common", "ui_common.json")
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


UI_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "endernet_rp", "ui")


def read_ui_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def walk_ui_controls(node, path=()):
    """枚举源码中的控件声明，保留名字以检查被排除的点击层。"""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, dict):
                yield path + (key,), key, value
            yield from walk_ui_controls(value, path + (key,))
    elif isinstance(node, list):
        for value in node:
            yield from walk_ui_controls(value, path)


class UiControls(object):
    """只展开本包控件；原版状态图像与标签保留外部模板名。"""

    def __init__(self):
        registry = read_ui_json(os.path.join(UI_ROOT, "_ui_defs.json"))
        self.documents = {}
        self.definitions = {}
        for relative in registry["ui_defs"]:
            document = read_ui_json(os.path.join(UI_ROOT, *relative.split("/")[1:]))
            self.documents[relative] = document
            for key, value in document.items():
                if isinstance(value, dict):
                    reference = document["namespace"] + "." + key.split("@")[0]
                    self.definitions[reference] = (key, value)

    @staticmethod
    def variables(node, inherited):
        result = {key.split("|")[0]: value for key, value in node.items()
                  if key.startswith("$")}
        result.update(inherited)
        return result

    @staticmethod
    def substitute(value, variables):
        seen = set()
        while isinstance(value, str) and value.startswith("$") and value in variables:
            if value in seen:
                raise ValueError("Cyclic UI variable: " + value)
            seen.add(value)
            value = variables[value]
        return value

    def resolve(self, key, value, variables=None, chain=()):
        variables = self.variables(value, variables or {})
        base = self.substitute(key.partition("@")[2], variables)
        result = {}
        if base in self.definitions:
            if base in chain:
                raise ValueError("Cyclic UI control: " + base)
            parent_key, parent = self.definitions[base]
            result, inherited = self.resolve(parent_key, parent, variables, chain + (base,))
            inherited.update(variables)
            variables = inherited
        elif base:
            result["external_template"] = base
        result.update(copy.deepcopy(value))
        return result, variables

    def template(self, reference):
        return self.resolve(*self.definitions[reference])

    def children(self, resolved):
        node, variables = resolved
        result = {}
        for entry in node.get("controls", []):
            for key, value in entry.items():
                name = key.split("@")[0]
                if name in result:
                    raise ValueError("Duplicate UI child: " + name)
                result[name] = self.resolve(key, value, variables)
        return result

    def child(self, resolved, path):
        for name in path.split("/"):
            resolved = self.children(resolved)[name]
        return resolved

    def ancestry(self, key, value):
        bases = []
        while "@" in key:
            base = key.partition("@")[2]
            if base in bases:
                raise ValueError("Cyclic UI ancestry: " + base)
            bases.append(base)
            if base not in self.definitions:
                break
            key, value = self.definitions[base]
        return bases

    def paths(self, reference):
        result = set()

        def walk(resolved, parent):
            for name, child in self.children(resolved).items():
                path = parent + "/" + name if parent else name
                result.add(path)
                walk(child, path)

        walk(self.template(reference), "")
        return result

    def records(self):
        for filename, document in self.documents.items():
            for path, key, value in walk_ui_controls(document):
                yield filename, path, key, value
