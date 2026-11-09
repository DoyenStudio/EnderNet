# coding=utf-8
"""Bridge radium's public fluid transactions to integer-mB container receipts.

Only this boundary discovers the engine component. No machine implementation or
radium script package is imported. A query proxy lives for one operation only.
"""

import math

from ....common.protocol import ContainerCapabilities, Reason, TransferReceipt, TransferStatus
from ....common.resources.key import ResourceKey, _INT_TYPES, _require_count
from ..containers import ContainerAdapter, ContainerEntry


# Largest consecutive integer exactly representable by the public float API.
# Also bounds the supply hint for explicit creative infinite sources.
MAX_FLUID_COUNT = 2 ** 53 - 1


def get_transfer_api():
    # type: () -> object
    from mod.server.extraServerApi import GetComponent, GetLevelId
    return GetComponent(GetLevelId(), "radium", "transfer")


def compatible_api(api):
    # type: (object) -> bool
    version = tuple(getattr(api, "API_VERSION", ()))
    return bool(version and version[0] == 1 and version >= (1, 1, 0))


def resource_key_of(api, resource):
    # type: (object, object) -> ResourceKey
    return ResourceKey.fluid(api.get_fluid_id(resource),
                             user_data=api.get_fluid_properties(resource) or None)


def public_resource(api, key):
    # type: (object, ResourceKey) -> object
    if not key.is_fluid():
        return None
    resource = api.fluid_resource(key.name, key.raw_identity.get("user_data"))
    # radium canonicalizes {} to no properties. EnderNet distinguishes them;
    # reject any lossy identity conversion, including negative-zero properties.
    return resource if resource_key_of(api, resource) == key else None


class RadiumFluidContainer(ContainerAdapter):
    """A side-specific public port. Each write is one root transaction.

    Override the base delta check: creative sources and voiding receivers have
    legitimate transfers without a storage delta. radium's committed actual
    amount is authoritative. The EnderNet coordinator still owns transit stock,
    returns and uncertain receipts across network/external operations.
    """

    adapter_id = "radium_fluid"

    def __init__(self, api_getter, node_key, face):
        # type: (object, object, int) -> None
        self.api_getter = api_getter
        self.node_key = node_key
        self.face = face
        self.valid = True
        self.last_error = ""

    def _resolve(self):
        if not self.valid:
            return None, None
        api = self.api_getter()
        if not compatible_api(api):
            return None, None
        handler = api.query(api.FLUID, self.node_key.dimension,
                            self.node_key.as_tuple(), face=self.face)
        return api, handler

    def is_valid(self):
        try:
            return self._resolve()[1] is not None
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def capabilities(self):
        return ContainerCapabilities(can_read=True, can_insert=True,
                                     can_simulate=True, supports_partial=True,
                                     is_atomic=True, notify_token=None,
                                     note="radium >= 1.1; integer mB; side-specific")

    def _operate(self, api, handler, resource, count, extracting, simulate=False, slot=None):
        """Abort fractional attempts before retrying a whole-mB amount once."""
        candidate = min(count, MAX_FLUID_COUNT)
        method = handler.extract if extracting else handler.insert
        committing = False
        try:
            for unused in range(2):
                if candidate <= 0:
                    return 0
                with api.open_transaction() as tx:
                    actual = method(resource, candidate, tx, slot=slot)
                    if (isinstance(actual, bool)
                            or not isinstance(actual, _INT_TYPES + (float,))
                            or math.isnan(actual) or math.isinf(actual)
                            or actual < 0 or actual > candidate):
                        raise ValueError("Invalid radium transfer amount: %r" % (actual,))
                    whole = int(actual)
                    if actual == whole:
                        if whole and not simulate:
                            committing = True
                            tx.commit()
                        return whole
                    # No commit: the fractional change is discarded on exit.
                    candidate = whole
            return 0
        except api.TransactionError as exc:
            # Public contract: stale guards and failed apply roll back the root.
            self.last_error = str(exc)
            return 0
        except Exception as exc:
            self.last_error = str(exc)
            # An unexpected failure after entering commit must not be replayed.
            return TransferStatus.UNCERTAIN if committing else 0

    def entries(self):
        # type: () -> list
        try:
            api, handler = self._resolve()
            if handler is None:
                return []
            entries = []
            for slot in handler.get_slots():
                resource = handler.get_resource(slot)
                if resource is None:
                    continue
                amount = handler.get_amount(slot)
                if amount <= 0 or math.isnan(amount):
                    continue
                count = int(min(amount, MAX_FLUID_COUNT))
                # Visible input-only tanks must not starve a later output tank.
                available = self._operate(api, handler, resource, count, True,
                                          simulate=True, slot=slot)
                if available:
                    entries.append((slot, ContainerEntry(resource_key_of(api, resource), available)))
            return entries
        except Exception as exc:
            self.last_error = str(exc)
            return []

    def _count_of(self, resource_key):
        return min(MAX_FLUID_COUNT, sum(entry.count for _, entry in self.entries()
                                       if entry.resource_key == resource_key))

    def can_accept(self, resource_key, count):
        _require_count(count, "fluid mB")
        try:
            api, handler = self._resolve()
            if handler is None:
                return 0
            resource = public_resource(api, resource_key)
            if resource is None:
                return 0
            return self._operate(api, handler, resource, count, False, simulate=True)
        except Exception as exc:
            self.last_error = str(exc)
            return 0

    def _transfer(self, op_id, resource_key, count, extracting):
        _require_count(count, "fluid mB")
        moved = 0
        reason = Reason.INSUFFICIENT_STOCK if extracting else Reason.CONTAINER_REFUSED
        try:
            api, handler = self._resolve()
            if handler is None:
                reason = Reason.NO_ADAPTER
            else:
                resource = public_resource(api, resource_key)
                if resource is not None:
                    moved = self._operate(api, handler, resource, count, extracting)
        except Exception as exc:
            self.last_error = str(exc)
        if moved == TransferStatus.UNCERTAIN:
            return TransferReceipt(op_id, count, 0, TransferStatus.UNCERTAIN,
                                   Reason.CONTAINER_UNCERTAIN, resource_key)
        if moved == count:
            status, reason = TransferStatus.COMPLETE, ""
        elif moved:
            status, reason = TransferStatus.PARTIAL, Reason.CONTAINER_PARTIAL
        else:
            status = TransferStatus.REFUSED
        return TransferReceipt(op_id, count, moved, status, reason, resource_key)

    def insert(self, op_id, resource_key, count):
        return self._transfer(op_id, resource_key, count, False)

    def extract(self, op_id, resource_key, count):
        return self._transfer(op_id, resource_key, count, True)


class RadiumFluidProvider(object):
    """Cache addresses, never public query proxies or a missing component."""

    def __init__(self, api_getter=None):
        self.api_getter = api_getter if api_getter is not None else get_transfer_api
        self._cache = {}
        self.stats = {"probes": 0, "misses": 0, "invalidations": 0}

    def container_at(self, node_key, face):
        key = (node_key.as_key(), face)
        self.stats["probes"] += 1
        adapter = self._cache.get(key)
        if adapter is None:
            adapter = RadiumFluidContainer(self.api_getter, node_key, face)
        if not adapter.is_valid():
            self.stats["misses"] += 1
            return None
        self._cache[key] = adapter
        return adapter

    def invalidate(self, node_key):
        found = False
        for key in list(self._cache):
            if key[0] == node_key.as_key():
                self._cache.pop(key).invalidate()
                self.stats["invalidations"] += 1
                found = True
        return found

    def invalidate_all(self):
        count = len(self._cache)
        for adapter in self._cache.values():
            adapter.invalidate()
        self._cache.clear()
        self.stats["invalidations"] += count
        return count

    def diagnostics(self):
        report = dict(self.stats, cached=len(self._cache))
        try:
            api = self.api_getter()
            report["available"] = compatible_api(api)
            report["api_version"] = list(getattr(api, "API_VERSION", ()))
        except Exception as exc:
            report["available"] = False
            report["error"] = str(exc)
        return report
