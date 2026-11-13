# coding=utf-8
"""Project the authoritative bandwidth tree into a renderable graph (no engine API)."""
from ...common.identity import DeviceRef, PartKey, FACING_NAMES, FACING_OFFSETS
from .topology import TunnelSegment


def host(key):
    return key.node_key if isinstance(key, PartKey) else key


def point(key):
    p = [value + 0.5 for value in host(key).as_tuple()]
    if isinstance(key, PartKey) and key.slot in FACING_NAMES:
        offset = FACING_OFFSETS[FACING_NAMES.index(key.slot)]
        p = [p[i] + offset[i] * 0.46875 for i in range(3)]
    return p


def identity(key):
    return PartKey.of(key).to_text()


class ViewGraph(object):
    def __init__(self, definitions, registry, catalog, topology, admission, tunnels, status_of):
        self.definitions = definitions
        self.registry = registry
        self.catalog = catalog
        self.topology = topology
        self.admission = admission
        self.tunnels = tunnels
        self.status_of = status_of

    def exists(self, key):
        record = self.registry.get(host(key))
        if record is None:
            return False
        return (not isinstance(key, PartKey) or key.slot == "center"
                or key.slot in dict(self.catalog.parts_of(record)))

    def color(self, key):
        record = self.registry.get(host(key))
        if record is None or not record.loaded or not self.exists(key):
            return "gray"
        part = isinstance(key, PartKey) and key.slot != "center"
        ref = DeviceRef(record.node_key, record.generation, key.slot if part else "center")
        bandwidth = self.admission.status_of(ref)
        # Keep confirmed bandwidth failures distinct from other offline causes.
        if bandwidth.ok is False:
            return "red"
        if bandwidth.ok is None:
            return "gray"
        status = self.status_of(ref)
        if status.validity != "ok":
            return "gray"
        return "green" if status.is_online else "orange"

    def root_id(self, tree, key, face):
        if tree is None:
            return None
        root_id = tree.line_of(key)
        if root_id is None:
            root_id = next((r.root_id for r in tree.roots
                            if r.baseband == host(key) and r.face == face), None)
        return root_id

    def refresh_colors(self, key, face, nodes, keys):
        """Poll only visible markers; power changes need not invalidate the route."""
        snapshot = self.topology.snapshot()
        if snapshot.is_pending(key) or self.root_id(snapshot.line_tree_of(key), key, face) is None:
            return nodes
        by_id = dict((identity(k), k) for k in keys)
        refreshed = []
        for node in nodes:
            updated = dict(node)
            if node["marker"]:
                updated["color"] = self.color(by_id[node["id"]])
            refreshed.append(updated)
        return refreshed

    def node(self, key, connected=False, force_marker=False):
        record = self.registry.get(host(key))
        if record is None or not self.exists(key):
            return None
        part = isinstance(key, PartKey) and key.slot != "center"
        color = self.color(key) if connected else "gray"
        return {"id": identity(key), "pos": point(key), "color": color,
                "marker": force_marker or part or not self.definitions.is_cable(record.device_id)}

    def build(self, key, face, dimension, previous_keys=()):
        snapshot = self.topology.snapshot()
        tree = snapshot.line_tree_of(key)
        pending = snapshot.is_pending(key)
        root_id = self.root_id(tree, key, face)
        # A pending snapshot must never continue advertising the old route.
        if pending:
            keys = set(previous_keys) | set([key])
            nodes = [self.node(k, force_marker=(k == key)) for k in sorted(keys, key=identity)
                     if host(k).dimension == dimension]
            return [n for n in nodes if n is not None], [], "线路重建中", keys
        if root_id is None:
            node = self.node(key, force_marker=True)
            return [node] if node is not None else [], [], "未找到带宽线路", set([key])
        root = tree.root(root_id)
        keys = set()
        nodes = {}
        edges = set()
        cross_dimension = [False]

        def add_node(k):
            keys.add(k)
            if host(k).dimension != dimension:
                cross_dimension[0] = True
                return
            if identity(k) in nodes:
                return
            node = self.node(k, connected=True)
            if node is not None:
                nodes[node["id"]] = node

        def connect(a, b):
            add_node(a)
            add_node(b)
            if host(a).dimension != host(b).dimension:
                cross_dimension[0] = True
            if identity(a) in nodes and identity(b) in nodes and a != b:
                edges.add((identity(a), identity(b)))

        add_node(root.baseband)
        root_identity = identity(root.baseband)
        if root_identity in nodes:
            offset = FACING_OFFSETS[FACING_NAMES.index(root.face)]
            nodes[root_identity]["pos"] = [v + offset[i] * 0.5
                                             for i, v in enumerate(point(root.baseband))]
        for k in tree.keys_of(root_id):
            if isinstance(k, TunnelSegment):
                continue
            node = tree.node(k)
            parent = node.parent
            if tree.is_segment_root(parent):
                connect(root.baseband, k)
            elif isinstance(parent, TunnelSegment):
                entry = tree.node(parent).parent
                # Both faces are real tunnel endpoints; the logical segment has no world position.
                sources = self.tunnels.endpoints_at(parent.via, host(entry))
                targets = self.tunnels.endpoints_at(parent.via, host(k))
                for source in sources:
                    connect(entry, source)
                    for target in targets:
                        connect(source, target)
                        connect(target, k)
            else:
                connect(parent, k)
        message = "存在跨维度连接，仅显示当前维度" if cross_dimension[0] else ""
        return [nodes[n] for n in sorted(nodes)], [list(e) for e in sorted(edges)], message, keys
