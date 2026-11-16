# coding=utf-8
"""Native containers and ticks; only standalone processing machines expose FE ports."""

import uuid

from ...common.channels.production import build_event_classes
from ...common.identity import NodeKey
from ...common.materials import MACHINE_RECIPES, MACHINE_BLOCKS
from ...common.rate_limit import PlayerRateLimiter
from ..production.machine import ProductionMachine, MACHINE_IDS, GENERATOR
from .compat.fluids import get_transfer_api
from .containers.engine import EngineItemPrimitives
from .engine_text import engine_item_data
from .events import ToolDeltaEventApi, engine_player_dimension
from .messages import engine_player_position
from .world import ToolDeltaWorldAdapter

_EVENT_CLASSES = {}
_SERVICE = None
FE_MACHINE_IDS = tuple(device_id for device_id in MACHINE_IDS if device_id != GENERATOR)
SCREENS = dict((machine.id, machine.screen) for machine in MACHINE_BLOCKS)


class NativeSlots(object):
    def __init__(self, key, primitives=None):
        self.key = key
        self.primitives = primitives if primitives is not None else EngineItemPrimitives()

    def read(self, slot):
        return self.primitives.container_item(self.key.dimension, self.key.as_tuple(), slot)

    def write(self, slot, item):
        # The machine already owns and journals this internal mutation. Do not
        # broadcast it as a foreign hopper insertion into a protected result slot.
        comp = self.primitives._container_comp()
        from ...common.resources.item_codec import with_engine_empty_values
        return bool(comp.SpawnItemToContainer(
            engine_item_data(with_engine_empty_values(item)), slot,
            self.key.as_tuple(), self.key.dimension))


def block_entity(key):
    from ....tooldelta.api.server.block import GetBlockEntityData
    return GetBlockEntityData(key.dimension, key.as_tuple())


def sneaking(player_id):
    from ....tooldelta.api.server.player import IsSneaking
    return bool(IsSneaking(player_id))


def event_item(event):
    value = getattr(event, "item", None)
    if isinstance(value, dict):
        return value
    return value.marshal() if value is not None else {}


def static_listener(callback):
    # ToolDelta marks static callables with _td_static. Python 2 bound methods
    # cannot carry that attribute, so keep a real function in the event bus.
    def receive(event):
        return callback(event)
    return receive


class ProductionService(object):
    def __init__(self):
        self.world = ToolDeltaWorldAdapter()
        self.api = None
        self.event_api = None
        self.registrations = []
        self.machines = {}
        self.watchers = {}
        self.errors = {}
        self.ticks = {}
        self.provider = self.provide_energy
        self.open_limiter = PlayerRateLimiter(0.25)
        self.request_limiter = PlayerRateLimiter(0.1)
        self.events = None

    def discover(self):
        if self.api is not None:
            return True
        api = get_transfer_api()
        if api is None or getattr(api, "API_VERSION", ())[:1] != (1,):
            return False
        for device_id in FE_MACHINE_IDS:
            api.register_provider(api.ENERGY, device_id, self.provider)
        self.api = api
        return True

    def load(self, dimension, pos):
        key = NodeKey.from_tuple(dimension, pos)
        name = self.world.block_name_at(key)
        if name not in MACHINE_IDS:
            self.unload(key)
            return None
        machine = self.machines.get(key)
        if machine is not None and machine.device_id == name:
            return machine
        if not self.discover():
            return None
        entity = block_entity(key)
        if entity is None:
            return None
        machine = ProductionMachine(name, entity, NativeSlots(key), self.api, MACHINE_RECIPES)
        self.machines[key] = machine
        return machine

    def unload(self, key):
        machine = self.machines.pop(key, None)
        if machine is not None:
            machine.alive = False
            self.api.invalidate()
        self.ticks.pop(key, None)
        self.errors.pop(key, None)
        for player, watch in list(self.watchers.items()):
            if watch[0] == key:
                del self.watchers[player]

    def provide_energy(self, dimension, pos, face):
        machine = self.load(dimension, pos)
        if machine is None or machine.device_id == GENERATOR:
            return None
        return machine.energy_input

    def snapshot(self, dimension, pos):
        machine = self.load(dimension, pos)
        if machine is None:
            return None
        result = machine.snapshot()
        result.update({"dimension": int(dimension), "pos": list(pos),
                       "key": SCREENS[machine.device_id]})
        return result

    def set_mode(self, dimension, pos, mode):
        machine = self.load(dimension, pos)
        return bool(machine is not None and machine.set_mode(mode))

    def _near(self, player, key):
        if engine_player_dimension(player) != key.dimension:
            return False
        pos = engine_player_position(player)
        return pos is not None and sum((pos[i] - (key.as_tuple()[i] + 0.5)) ** 2
                                       for i in range(3)) <= 64

    def _send(self, player, key, session, opened=False, message=""):
        from ....tooldelta.events.notify import NotifyToClient
        data = self.snapshot(key.dimension, key.as_tuple())
        if data is None:
            return
        data.update({"session": session, "opened": bool(opened)})
        if message:
            data["message"] = message
        NotifyToClient(player, self.events[1](data))

    def _block_use(self, event):
        if event.blockName not in MACHINE_IDS or sneaking(event.playerId):
            return
        if not self.open_limiter.allow(event.playerId):
            return
        key = NodeKey(event.dimensionId, event.x, event.y, event.z)
        if not self._near(event.playerId, key):
            return
        previous = self.watchers.get(event.playerId)
        session = previous[1] if previous is not None and previous[0] == key else uuid.uuid4().hex
        self.watchers[event.playerId] = (key, session, event.blockName)
        self._send(event.playerId, key, session, opened=True)

    def _item_use(self, event):
        if event.blockName in MACHINE_IDS and not sneaking(event.entityId):
            event.cancel()

    def _request(self, event):
        player = event.player_id
        data = event.payload
        watch = self.watchers.get(player)
        if watch is None:
            return
        key, session, device_id = watch
        if (data.get("session") != session or data.get("dimension") != key.dimension
                or data.get("pos") != list(key.as_tuple()) or data.get("device_id") != device_id):
            return
        if data.get("action") == "close":
            del self.watchers[player]
            return
        if not self.request_limiter.allow(player):
            return
        if not self._near(player, key) or self.world.block_name_at(key) != device_id:
            del self.watchers[player]
            return
        action = data.get("action")
        message = ""
        if action == "mode":
            if not self.set_mode(key.dimension, key.as_tuple(), data.get("mode")):
                message = "加工中不能切换模式"
        elif action != "poll":
            return
        self._send(player, key, session, message=message)

    def _tick(self, event):
        if event.blockName not in MACHINE_IDS:
            return
        key = NodeKey(event.dimension, event.posX, event.posY, event.posZ)
        machine = self.load(key.dimension, key.as_tuple())
        if machine is None:
            return
        try:
            machine.tick()
        except (ValueError, IOError, self.api.TransactionError) as exc:
            error = str(exc)
            if self.errors.get(key) != error:
                print("[EnderNet production] %s: %s" % (key, error))
            self.errors[key] = error
            machine.status = "error"
            machine.message = error
        else:
            self.errors.pop(key, None)
        self.ticks[key] = self.ticks.get(key, 0) + 1
        if self.ticks[key] % 5:
            return
        for player, watch in list(self.watchers.items()):
            if watch[0] != key:
                continue
            if not self._near(player, key):
                del self.watchers[player]
                continue
            self._send(player, key, watch[1])

    def _container(self, event, inserting, automatic):
        dimension = event.dimension if automatic else engine_player_dimension(event.playerId)
        machine = self.load(dimension, (event.x, event.y, event.z))
        if machine is None or machine.writing:
            return
        if not machine.recover():
            if inserting or automatic:
                event.cancel()
            else:
                machine.fence_manual_extraction()
            return
        slot = event.collectionIndex
        allowed = machine.accepts(slot, event_item(event)) if inserting else machine.removable(slot, automatic)
        if not allowed:
            event.cancel()

    def _remove(self, event):
        self.unload(NodeKey(event.dimension, event.x, event.y, event.z))

    def _try_destroy(self, event):
        if event.fullName not in MACHINE_IDS:
            return
        machine = self.load(event.dimensionId, (event.x, event.y, event.z))
        if machine is not None and not machine.recover():
            event.cancel()

    def _chunk_unload(self, event):
        for key in list(self.machines):
            if (key.dimension == event.dimension and key.x // 16 == event.chunkPosX
                    and key.z // 16 == event.chunkPosZ):
                self.unload(key)

    def start(self):
        from ....tooldelta import events
        self.event_api = ToolDeltaEventApi()
        if events not in _EVENT_CLASSES:
            _EVENT_CLASSES[events] = build_event_classes(events)
        self.events = _EVENT_CLASSES[events]
        bindings = (
            (events.ServerBlockEntityTickEvent, self._tick),
            (events.ServerBlockUseEvent, self._block_use),
            (events.ServerItemUseOnEvent, self._item_use),
            (events.BlockRemoveServerEvent, self._remove),
            (events.ServerPlayerTryDestroyBlockEvent, self._try_destroy),
            (events.ChunkAcquireDiscardedServerEvent, self._chunk_unload),
            (events.PlayerTryPutCustomContainerItemServerEvent,
             lambda event: self._container(event, True, False)),
            (events.PlayerTryRemoveCustomContainerItemServerEvent,
             lambda event: self._container(event, False, False)),
            (events.ItemPushInCustomContainerServerEvent,
             lambda event: self._container(event, True, True)),
            (events.ItemPullOutCustomContainerServerEvent,
             lambda event: self._container(event, False, True)),
            (self.events[0], self._request),
        )
        for cls, handler in bindings:
            handler = static_listener(handler)
            self.event_api.listen(cls)(handler)
            self.registrations.append((cls, handler))
        self.world.register_block_remove_listener(MACHINE_IDS)
        self.discover()

    def close(self):
        for cls, handler in self.registrations:
            self.event_api.remove(cls, handler)
        self.registrations = []
        if self.api is not None:
            self.api.flush()
            for device_id in FE_MACHINE_IDS:
                self.api.unregister_provider(self.api.ENERGY, device_id, self.provider)
            for key in list(self.machines):
                self.unload(key)
        self.watchers.clear()


def start_production():
    global _SERVICE
    if _SERVICE is None:
        service = ProductionService()
        service.start()
        _SERVICE = service
    return _SERVICE


def get_production():
    return _SERVICE


def stop_production():
    global _SERVICE
    if _SERVICE is not None:
        _SERVICE.close()
        _SERVICE = None
