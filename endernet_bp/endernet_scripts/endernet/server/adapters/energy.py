# coding=utf-8
"""Six-sided radium FE receiver. The block entity owns the committed balance."""

from ...common.definitions import ENERGY_INPUT_ID, FE_PER_EN, ENERGY_INPUT_CAPACITY_FE
from ...common.identity import NodeKey
from ...common.resource_key import _INT_TYPES
from ..devices.capabilities import EnergyPort
from .fluids import get_transfer_api


BALANCE_FIELD = "endernet_energy_fe"


class InputBuffer(EnergyPort):
    def __init__(self, api, entity):
        # type: (object, object) -> None
        self.entity = entity
        self.alive = True
        amount = entity[BALANCE_FIELD]
        if amount is None:
            amount = 0
        if isinstance(amount, bool) or not isinstance(amount, _INT_TYPES) or not 0 <= amount <= ENERGY_INPUT_CAPACITY_FE:
            raise ValueError("invalid saved FE balance")
        self.storage = api.create_energy_storage(
            ENERGY_INPUT_CAPACITY_FE, amount=amount, persist=self.save, owner=self)
        self.input = api.energy_view(self.storage, can_extract=False, valid=self.valid)

    def valid(self):
        return self.alive

    def save(self):
        if not self.alive:
            return
        amount = int(self.storage.get_amount())
        self.entity[BALANCE_FIELD] = amount
        if self.entity[BALANCE_FIELD] != amount:
            raise IOError("could not persist energy input balance")

    def balance(self):
        """Available EN and retained FE, from the same committed Cell."""
        return divmod(self.storage.get_amount(), FE_PER_EN)


class EnergyInputs(object):
    def __init__(self, world, api_getter=get_transfer_api):
        self.world = world
        self.api_getter = api_getter
        self.api = None
        self.buffers = {}
        self.provider = self._provide
        self.error = ""

    def discover(self):
        if self.api is not None:
            return
        try:
            api = self.api_getter()
            if getattr(api, "API_VERSION", ())[:1] != (1,):
                return
            api.register_provider(api.ENERGY, ENERGY_INPUT_ID, self.provider)
            self.api = api
            self.error = ""
        except Exception as exc:
            self.error = str(exc)

    def load(self, key):
        if self.api is None:
            return None
        buffer = self.buffers.get(key)
        if buffer is None:
            entity = self.world.energy_block_entity(key)
            if entity is not None:
                try:
                    buffer = InputBuffer(self.api, entity)
                except ValueError as exc:
                    self.error = str(exc)
                    return None
                self.buffers[key] = buffer
        return buffer

    def _provide(self, dimension, pos, face):
        key = NodeKey.from_tuple(dimension, pos)
        buffer = self.load(key)
        return buffer.input if buffer is not None else None

    def unload(self, key):
        buffer = self.buffers.pop(key, None)
        if buffer is not None:
            buffer.alive = False
            self.api.invalidate()

    def unload_chunk(self, chunk_key):
        for key in list(self.buffers):
            if key.chunk_key() == chunk_key:
                self.unload(key)

    def close(self):
        if self.api is not None:
            self.api.flush()
            self.api.unregister_provider(self.api.ENERGY, ENERGY_INPUT_ID, self.provider)
            for buffer in self.buffers.values():
                buffer.alive = False
            self.buffers.clear()
            self.api = None

    def spend(self, keys, demand):
        """Fund the whole EN deficit atomically, preserving each port's odd FE."""
        if self.api is None or not demand:
            return {}
        paid = {}
        remaining = demand
        try:
            with self.api.open_transaction() as tx:
                for key in keys:
                    buffer = self.buffers.get(key)
                    if buffer is None:
                        continue
                    amount = min(remaining, buffer.balance()[0])
                    if amount:
                        if buffer.storage.extract(amount * FE_PER_EN, tx) != amount * FE_PER_EN:
                            return {}
                        paid[key] = amount
                        remaining -= amount
                    if not remaining:
                        tx.commit()
                        return paid
        except self.api.TransactionError as exc:
            self.error = str(exc)
        return {}
