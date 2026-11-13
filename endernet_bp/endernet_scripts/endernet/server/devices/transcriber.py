# coding=utf-8
"""八槽本机转录：先报价、再能源准入、最后成对搬运。引擎无关。"""

from ...common.definitions import TRANSCRIBER_ID
from ...common.resources.key import MICRO_UNITS_PER_UNIT
from ..storage.medium import DIRECTION_READ, DIRECTION_WRITE, Medium, transfer_between


def _rotate(values, offset):
    if not values:
        return values
    offset %= len(values)
    return values[offset:] + values[:offset]


def simulate_second(medium):
    """复制内容并只授予一秒额定额度；不读取或消耗真实余额。"""
    clone = Medium(medium.medium_id, medium.model_id, medium.spec,
                   medium.policy, entries=medium.entries)
    for bucket in (clone.read_bucket, clone.write_bucket):
        bucket.balance_micro = min(bucket.capacity_micro, bucket.rate_micro_per_second)
    return clone


class TranscriberService(object):
    def __init__(self, bays):
        self.bays = bays
        self.views = {}
        self._prepared = {}
        self._cursors = {}
        self._last_tick = None

    def _identity(self, state):
        return (state.node_key.as_key(), state.ref.generation)

    def _media(self, state):
        if not state.loaded or state.unreadable or not state.has_reading():
            return {}
        result = {}
        for assignment in state.occupied():
            medium = self.bays.medium(assignment.medium_id)
            owner = self.bays.medium_slot_owner(assignment.medium_id)
            if (medium is not None and medium.is_tradeable() and owner is not None
                    and owner[0] is state and owner[1].slot == assignment.slot):
                result[assignment.slot] = medium
        return result

    def _move(self, media, tick, cursor, changed=False):
        total = 0
        reserved_targets = set()
        for left in _rotate(list(range(4)), cursor):
            source = media.get(left)
            if source is None:
                continue
            keys = _rotate(sorted(source.entries, key=lambda key: key.canonical()), cursor)
            for key in keys:
                unit = key.micro_units(1)
                candidates = []
                for right in _rotate(list(range(4, 8)), cursor):
                    target = media.get(right)
                    if target is None or right in reserved_targets or target.free_micro() < unit:
                        continue
                    if (not target.count_of(key) and target._counts_as_type(key)
                            and target.type_count >= target.max_types()):
                        continue
                    candidates.append((right, target))
                if not candidates:
                    continue
                # 不能让可逐 mB 写入的流体每帧吃掉不足一件物品的积累额度。
                # 无容量/类型不兼容仍跳过；仅额度不足时等待当前轮到的资源。
                if source._available_items(DIRECTION_READ, unit, tick) <= 0:
                    break
                key_moved = 0
                for right, target in candidates:
                    if target._available_items(DIRECTION_WRITE, unit, tick) <= 0:
                        reserved_targets.add(right)
                        continue
                    moved = transfer_between(source, target, key, source.count_of(key), tick,
                                             (source.revision, target.revision))
                    if moved:
                        key_moved += moved
                        total += key.micro_units(moved)
                        if changed:
                            self.bays.note_transfer_changed(source.medium_id, target.medium_id)
                if not key_moved:
                    break
        return total

    def prepare(self, tick, energy_of):
        """与电力是否满足无关地计算报价，故停机不会抹掉需求。"""
        if tick == self._last_tick:
            return False
        self._last_tick = tick
        self.bays.tick = tick
        self._prepared = {}
        live = set()
        for state in self.bays.states():
            if state.device_id != TRANSCRIBER_ID:
                continue
            identity = self._identity(state)
            live.add(identity)
            cursor = self._cursors.get(identity, 0)
            # 真实槽位复核先于任何介质操作；读失败时不使用旧的挂载读数。
            readable = state.loaded and self.bays.refresh(state.ref)
            media = self._media(state) if readable else {}
            clones = dict((slot, simulate_second(medium)) for slot, medium in media.items())
            rate = self._move(clones, 0, cursor)
            demand = rate // (4 * MICRO_UNITS_PER_UNIT) if readable else None
            energy = energy_of(state.ref)
            if energy is not None:
                energy.set_dynamic_demand(state.ref, demand)
            hint = "等待介质"
            if not readable:
                hint = "容器不可读，暂停"
            elif state.invalid or any(self.bays.medium(a.medium_id) is None
                                      or not self.bays.medium(a.medium_id).is_tradeable()
                                      for a in state.occupied()):
                hint = "部分介质不可用"
            elif any(slot < 4 for slot in media) and any(slot >= 4 for slot in media):
                hint = "等待转写" if rate else "来源已空或目标无法容纳"
            self.views[identity] = {"rate_micro": rate, "demand": demand, "hint": hint}
            self._prepared[identity] = (state, state.content_fingerprint(), media, cursor)
        for identity in list(self.views):
            if identity not in live:
                self.views.pop(identity, None)
                self._cursors.pop(identity, None)
        return True

    def execute(self, tick, online):
        prepared, self._prepared = self._prepared, {}
        total = 0
        for identity, (state, fingerprint, media, cursor) in sorted(prepared.items()):
            view = self.views[identity]
            if not media or not view["rate_micro"]:
                continue
            if not online(state.ref):
                view["hint"] = "网络离线，暂停转写"
                continue
            # 能源结算/其他服务推进后再确认真实槽位；变化则等下一轮重新报价。
            if (self.bays.get_by_ref(state.ref) is not state or not state.loaded
                    or not self.bays.refresh(state.ref)
                    or state.content_fingerprint() != fingerprint):
                view["hint"] = "介质变化，等待确认"
                continue
            moved = self._move(media, tick, cursor, changed=True)
            total += moved
            view["hint"] = "正在转写"
            # 按成功调度轮换，低速介质不会因 tick 周期锁定而饿死某个资源。
            if moved:
                self._cursors[identity] = cursor + 1
        return total

    def view(self, ref):
        return self.views.get((ref.node_key.as_key(), ref.generation))

    def summary(self, ref):
        view = self.view(ref)
        if view is None or view["demand"] is None:
            return "等待转录器确认"
        rate = view["rate_micro"]
        whole, remainder = divmod(rate, MICRO_UNITS_PER_UNIT)
        text = str(whole) if not remainder else "%d.%02d" % (whole, remainder * 2)
        return "%s 单位物/s  |  %d EN/t\n%s" % (text, view["demand"], view["hint"])
