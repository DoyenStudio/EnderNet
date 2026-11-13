# coding=utf-8
"""四槽向网络转储；一秒滚动计费，有限预算、公平轮转。"""

from collections import deque

from ...common.definitions import STORAGE_TRANSFER_ID
from ...common.resources.key import MICRO_UNITS_PER_UNIT


def _rotate(values, cursor):
    if not values:
        return values
    offset = cursor % len(values)
    return values[offset:] + values[:offset]


def format_units(amount):
    whole, remainder = divmod(amount, MICRO_UNITS_PER_UNIT)
    return str(whole) if not remainder else ("%d.%02d" % (
        whole, remainder * 100 // MICRO_UNITS_PER_UNIT)).rstrip("0")


class StorageTransferService(object):
    """生命周期独立于网络运行时，重建不会丢失最近一秒的已提交量。"""

    def __init__(self, bays):
        self.bays = bays
        self.views = {}
        self._samples = {}
        self._cursors = {}
        self._known = {}
        self._key_cursors = {}
        self._target_cursors = {}
        self._paused = set()
        self._last_tick = None
        self._executed_tick = None
        self._device_cursor = 0

    def _identity(self, ref):
        return (ref.node_key.as_key(), ref.generation)

    def prepare(self, tick, energy_of):
        if self._last_tick == tick:
            return
        self._last_tick = tick
        fps = self.bays.medium_policy.frames_per_second
        live = set()
        for state in self.bays.states():
            if state.device_id != STORAGE_TRANSFER_ID:
                continue
            identity = self._identity(state.ref)
            live.add(identity)
            samples = self._samples.setdefault(identity, deque())
            while samples and (samples[0][0] < tick - fps or samples[0][0] >= tick):
                samples.popleft()
            rate = sum(amount for _, amount in samples)
            view = self.views.setdefault(identity, {"hint": "等待介质", "slots": [0] * 4})
            view.update(rate_micro=rate, demand=rate // (4 * MICRO_UNITS_PER_UNIT))
            media = dict((a.slot, self.bays.medium(a.medium_id)) for a in state.occupied())
            view["slots"] = [media[slot].used_micro if media.get(slot) is not None else 0
                             for slot in range(4)]
            energy = energy_of(state.ref)
            if energy is not None:
                energy.set_dynamic_demand(state.ref, view["demand"])
        for identity in set(self.views).difference(live):
            self.views.pop(identity, None)
            self._samples.pop(identity, None)
            self._cursors.pop(identity, None)
            self._known.pop(identity, None)
            self._key_cursors.pop(identity, None)
            self._target_cursors.pop(identity, None)
            self._paused.discard(identity)

    def execute(self, tick, online, storage_of, budget=32, device_budget=4):
        if self._executed_tick == tick:
            return 0
        self._executed_tick = tick
        self.bays.tick = tick
        states = [state for state in self.bays.states() if state.device_id == STORAGE_TRANSFER_ID]
        candidates = []
        for state in states:
            identity = self._identity(state.ref)
            if identity not in self.views:
                continue
            if not state.loaded or not online(state.ref):
                self._paused.add(identity)
                self.views[identity]["hint"] = "网络离线，暂停转储"
            else:
                candidates.append(state)
        total, spent = 0, 0
        selected = _rotate(candidates, self._device_cursor)[:device_budget]
        self._device_cursor += 1
        for state in selected:
            if spent >= budget:
                break
            identity = self._identity(state.ref)
            view = self.views[identity]
            storage = storage_of(state.ref)
            if storage is None or not self.bays.refresh(state.ref):
                self._paused.add(identity)
                view["hint"] = "容器不可读，暂停"
                spent += 1
                continue
            cursor = self._cursors.get(identity, 0)
            assignments = _rotate(list(state.occupied()), cursor)
            media = dict((a.slot, self.bays.medium(a.medium_id)) for a in assignments)
            previous = self._known.get(identity, set())
            current = set(a.medium_id for a in assignments)
            # 新放入或停机恢复时从零积累源读取额度，不能补发离线时间。
            for medium in media.values():
                if medium is not None and (identity in self._paused or medium.medium_id not in previous):
                    medium.read_bucket.balance_micro = 0
                    medium.read_bucket.numerator = 0
                    medium.read_bucket.last_tick = tick
            self._known[identity] = current
            self._paused.discard(identity)
            view["hint"] = "介质不可用" if state.invalid else ("已完成" if assignments else "等待介质")
            moved_here = 0
            # 每台最多八次目标探测；多设备及各槽都能获得后续调度机会。
            allowance = min(8, budget - spent)
            local_spent = 0
            key_cursors = self._key_cursors.setdefault(identity, {})
            target_cursors = self._target_cursors.setdefault(identity, {})
            for medium_id in set(key_cursors).union(target_cursors).difference(current):
                key_cursors.pop(medium_id, None)
                target_cursors.pop(medium_id, None)
            for assignment in assignments:
                source = media.get(assignment.slot)
                if source is None or not source.is_tradeable():
                    view["hint"] = "介质不可用"
                    continue
                key_cursor = key_cursors.get(source.medium_id, 0)
                target_cursor = target_cursors.get(source.medium_id, 0)
                keys = _rotate(sorted(source.keys(), key=lambda key: key.canonical()), key_cursor)
                for key in keys:
                    if local_spent >= allowance:
                        break
                    amount, reason, cost = storage.import_medium(
                        state.ref, assignment.slot, key, source.count_of(key), tick,
                        budget=1, cursor=target_cursor)
                    target_cursors[source.medium_id] = target_cursor + 1
                    local_spent += cost
                    if amount:
                        moved_here += key.micro_units(amount)
                        key_cursors[source.medium_id] = key_cursor + 1
                    elif not moved_here:
                        view["hint"] = reason
                    if (not amount and reason == "网络空间不足"
                            and (target_cursor + 1) % max(1, len(storage.medium_ids())) == 0):
                        key_cursors[source.medium_id] = key_cursor + 1
                    # 每槽每轮只占一次目标探测；低速时保持当前资源直到额度可用。
                    break
                if local_spent >= allowance:
                    break
            spent += max(1, local_spent)
            self._cursors[identity] = cursor + 1 if moved_here else cursor
            view["slots"] = [media[slot].used_micro if media.get(slot) is not None else 0
                             for slot in range(4)]
            if moved_here:
                self._samples[identity].append((tick, moved_here))
                view["hint"] = "转储中"
                total += moved_here
            elif view["hint"] == "等待读写额度" and view["rate_micro"]:
                view["hint"] = "转储中"
        return total

    def view(self, ref):
        return self.views.get(self._identity(ref))

    def summary(self, ref):
        view = self.view(ref)
        if view is None:
            return "等待转储器确认"
        return "%s 单位物/s | %d EN/t\n%s\n剩余：%s" % (
            format_units(view["rate_micro"]), view["demand"], view["hint"],
            " / ".join(format_units(amount) for amount in view["slots"]))
