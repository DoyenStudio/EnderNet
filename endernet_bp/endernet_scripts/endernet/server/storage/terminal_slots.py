# coding=utf-8
"""Slot primitives for the physical crafting and pattern inventories."""
from ...common.item_codec import item_dict_from_key, item_key_from_dict


class DeviceSlots(object):
    def __init__(self, slots, stack_limit):
        self.slots = slots
        self.stack_limit_of = stack_limit

    def slot_count(self):
        return len(self.slots.slots)

    def is_valid(self):
        return True

    def read_slot(self, index):
        entry = self.slots.entry(index)
        return item_dict_from_key(*entry) if entry else None

    def write_slot(self, index, data):
        key = item_key_from_dict(data) if data else None
        self.slots.set_slot(index, key, data["count"] if key else 0)
        return True
