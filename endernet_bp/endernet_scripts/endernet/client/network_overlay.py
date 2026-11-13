# coding=utf-8
"""Diffed free-model renderer. Shader turns quads into camera-facing markers/ribbons."""
from collections import OrderedDict

from ..common.channels.network_view import COLORS, LINE_WIDTH, MARKER_SIZE


class OverlayRenderer(object):
    def __init__(self, port, budget=32):
        self.port = port
        self.budget = budget
        self.models = {}
        self.pending = OrderedDict()
        self.desired = {}
        self.dimension = None
        self.failures = 0

    def apply(self, payload):
        dimension = payload["dimension"]
        if self.dimension != dimension:
            self.clear()
        self.dimension = dimension
        nodes = dict((n["id"], n) for n in payload["nodes"])
        desired = {}
        for a, b in payload["edges"]:
            desired[("line", a, b)] = (nodes[a]["pos"], nodes[b]["pos"], "line")
        for node in payload["nodes"]:
            if node["marker"]:
                desired[("node", node["id"])] = (node["pos"], node["pos"], node["color"])
        for key in list(self.models):
            if key not in desired:
                self.port.remove(self.models.pop(key))
        self.pending = OrderedDict((key, value) for key, value in sorted(desired.items())
                                   if self.desired.get(key) != value or key not in self.models)
        self.desired = desired

    def advance(self):
        for unused in range(min(self.budget, len(self.pending))):
            key, value = self.pending.popitem(last=False)
            model = self.models.get(key)
            if model is None:
                model = self.port.create(self.dimension)
            if not model:
                self.failures += 1
                continue
            if not self.port.update(model, value):
                self.port.remove(model)
                self.models.pop(key, None)
                self.failures += 1
                continue
            self.models[key] = model

    def clear(self):
        for model in self.models.values():
            self.port.remove(model)
        self.models.clear()
        self.pending.clear()
        self.desired.clear()
        self.dimension = None

    def diagnostics(self):
        return {"models": len(self.models), "pending": len(self.pending), "failures": self.failures}


class ModelPort(object):
    def __init__(self):
        from mod.client.extraClientApi import GetEngineCompFactory, GetLevelId
        self.comp = GetEngineCompFactory().CreateModel(GetLevelId())

    def create(self, dimension):
        return self.comp.CreateFreeModel("endernet_network_overlay", dimension)

    def update(self, model, value):
        a, b, color = value
        middle = tuple((a[i] + b[i]) * 0.5 for i in range(3))
        start = tuple(a[i] - middle[i] for i in range(3))
        end = tuple(b[i] - middle[i] for i in range(3))
        size = LINE_WIDTH if color == "line" else MARKER_SIZE
        extent = tuple(abs(end[i]) + size for i in range(3))
        results = [self.comp.SetFreeModelPos(model, *middle),
                   self.comp.SetFreeModelBoundingBox(model, tuple(-v for v in extent), extent),
                   self.comp.SetExtraUniformValue(model, 1, COLORS[color]),
                   self.comp.SetExtraUniformValue(model, 2, start + (0.0,)),
                   self.comp.SetExtraUniformValue(model, 3, end + (0.0,)),
                   self.comp.SetExtraUniformValue(model, 4, (size, 0 if color == "line" else 1, 0, 0))]
        return all(results)

    def remove(self, model):
        return self.comp.RemoveFreeModel(model)
