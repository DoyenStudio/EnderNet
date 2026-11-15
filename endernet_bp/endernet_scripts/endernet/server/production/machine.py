# coding=utf-8
"""Engine-independent production machines with recoverable native-slot commits.

Inputs stay in real container slots while processing. A saved before/after journal
rolls an interrupted final write forward before allowing any outside transfer.
The only inventory adapter operations are read(slot) and write(slot, item).
"""

from copy import deepcopy
import json

from ...common.resources import is_empty_item, normalize_engine_item, item_key_from_dict
from ...common.resources.key import _TEXT_TYPES
from ...common.materials import GENERATOR_RATE, FUEL_TICKS, GENERATOR_FUELS

CHARGER = "endernet:resonance_charger"
INSCRIBER = "endernet:resonance_inscriber"
GENERATOR = "endernet:resonance_generator"
MACHINE_IDS = (CHARGER, INSCRIBER, GENERATOR)
STATE_FIELD = "endernet:production"
CAPACITY = 32000
MODES = ("grind", "press", "assemble")
SLOT_COUNTS = {CHARGER: 2, INSCRIBER: 4, GENERATOR: 1}
EMPTY = {"newItemName": "minecraft:air", "newAuxValue": 0, "count": 0}


def item_name(item):
    return (item.get("newItemName") or item.get("itemName", "")) if item else ""


def item_count(item):
    return 0 if is_empty_item(item) else int(item.get("count", 0))


def same_item(first, second):
    if not item_count(first) or not item_count(second):
        return not item_count(first) and not item_count(second)
    # Native reads include incidental defaults (itemId, isDiggerItem, etc.).
    # Use the same canonical resource identity as storage, retaining userData.
    return (item_count(first) == item_count(second)
            and item_key_from_dict(normalize_engine_item(first))
            == item_key_from_dict(normalize_engine_item(second)))


def plain_item(name, count):
    return {"newItemName": name, "newAuxValue": 0, "count": count}


def fuel_item(item):
    name = item_name(item)
    aux = int(item.get("newAuxValue", item.get("auxValue", 0)) or 0) if item else 0
    return item_count(item) > 0 and (name, aux) in GENERATOR_FUELS


class BudgetedOutput(object):
    """A public radium energy endpoint sharing one transactional six-face budget."""

    def __init__(self, machine, budget):
        self.machine = machine
        self.budget = budget

    @property
    def manager(self):
        return self.machine.energy.manager

    @property
    def storage_ids(self):
        return self.machine.energy.storage_ids | self.budget.storage_ids

    def get_amount(self, tx=None):
        return self.machine.energy.get_amount(tx)

    def get_capacity(self):
        return CAPACITY

    def insert(self, amount, tx):
        return 0

    def extract(self, amount, tx):
        tx.guard(self.machine.valid)
        count = min(int(amount), self.budget.get_amount(tx), self.get_amount(tx))
        if count <= 0:
            return 0
        if self.budget.extract(count, tx) != count:
            raise self.machine.api.TransactionError("generator budget changed")
        actual = self.machine.energy.extract(count, tx)
        if actual != count:
            raise self.machine.api.TransactionError("generator balance changed")
        return actual


class ProductionMachine(object):
    def __init__(self, device_id, entity, inventory, api, recipes):
        if device_id not in MACHINE_IDS:
            raise ValueError("unknown production device")
        self.device_id = device_id
        self.entity = entity
        self.inventory = inventory
        self.api = api
        self.recipes = tuple(r for r in recipes if r.machine == device_id)
        self.alive = True
        self.writing = False
        self.status = "idle"
        self.message = ""
        saved = entity[STATE_FIELD]
        saved = {} if saved is None else saved
        if not isinstance(saved, dict):
            raise ValueError("invalid production save")
        self.mode = saved.get("mode", "grind" if device_id == INSCRIBER else "charge")
        if device_id == INSCRIBER and self.mode not in MODES:
            raise ValueError("invalid saved inscriber mode")
        self.progress = int(saved.get("progress", 0))
        self.recipe_id = saved.get("recipe", "")
        self.burn = int(saved.get("burn", 0))
        self.cursor = int(saved.get("cursor", 0)) % 6
        journal = saved.get("journal")
        # Version 1 stored the journal as a nested dictionary. Native block data
        # silently rejects mixed [item-dict, None] lists, so version 2 uses JSON.
        self.journal = json.loads(journal) if isinstance(journal, _TEXT_TYPES) else deepcopy(journal)
        if self.journal is not None and not isinstance(self.journal, dict):
            raise ValueError("invalid production journal")
        if not 0 <= self.progress <= 80 or not 0 <= self.burn <= FUEL_TICKS:
            raise ValueError("invalid production progress")
        amount = int(saved.get("energy", 0))
        if not 0 <= amount <= CAPACITY:
            raise ValueError("invalid production energy")
        self.energy = api.create_energy_storage(CAPACITY, amount=amount,
                                                persist=self.save, owner=self)
        self.energy_input = api.energy_view(self.energy, can_extract=False, valid=self.valid)
        self.budget = api.create_energy_storage(GENERATOR_RATE)
        self.energy_output = BudgetedOutput(self, self.budget)
        self.save()

    def valid(self):
        return self.alive

    def save(self):
        if not self.alive:
            return
        journal = (json.dumps(self.journal, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
                   if self.journal is not None else None)
        value = {"version": 2, "mode": self.mode, "progress": self.progress,
                 "recipe": self.recipe_id, "burn": self.burn, "cursor": self.cursor,
                 "energy": int(self.energy.get_amount()), "journal": journal}
        self.entity[STATE_FIELD] = value
        if self.entity[STATE_FIELD] != value:
            raise IOError("production state did not persist")

    def snapshot(self):
        return {"device_id": self.device_id, "energy": self.energy.get_amount(),
                "capacity": CAPACITY, "progress": self.progress, "duration": 80,
                "mode": self.mode, "status": self.status, "burn": self.burn,
                "burn_duration": FUEL_TICKS,
                "message": self.message}

    def set_mode(self, mode):
        if self.device_id != INSCRIBER or mode not in MODES:
            return False
        if self.progress or self.journal:
            return False
        self.mode = mode
        self.recipe_id = ""
        self.save()
        return True

    def accepts(self, slot, item):
        if self.journal:
            return False
        if self.device_id == GENERATOR:
            return slot == 0 and fuel_item(item)
        if slot < 0 or slot >= SLOT_COUNTS[self.device_id] - 1:
            return False
        if int((item or {}).get("newAuxValue", (item or {}).get("auxValue", 0)) or 0) != 0:
            return False
        return any(r.mode == self.mode and slot < len(r.inputs)
                   and r.inputs[slot][0] == item_name(item) for r in self.recipes)

    def removable(self, slot, automatic=False):
        if self.journal:
            return False
        if automatic:
            return self.device_id != GENERATOR and slot == SLOT_COUNTS[self.device_id] - 1
        return True

    def _items(self):
        return [deepcopy(self.inventory.read(slot)) for slot in range(SLOT_COUNTS[self.device_id])]

    def _recipe(self, items):
        for recipe in self.recipes:
            if recipe.mode != self.mode:
                continue
            matched = True
            for slot in range(len(items) - 1):
                item = items[slot]
                if slot >= len(recipe.inputs):
                    if item_count(item):
                        matched = False
                    continue
                name, count = recipe.inputs[slot]
                if item_name(item) != name or item_count(item) < count:
                    matched = False
                if int((item or {}).get("newAuxValue", (item or {}).get("auxValue", 0)) or 0):
                    matched = False
            if matched:
                return recipe
        return None

    def _has_room(self, recipe, output):
        if not item_count(output):
            return True
        key = item_key_from_dict(normalize_engine_item(output))
        expected = item_key_from_dict(normalize_engine_item(plain_item(recipe.output, 1)))
        return key == expected and item_count(output) + recipe.count <= 64

    def _commit_slots(self, before, after, post):
        self.journal = {"before": deepcopy(before), "after": deepcopy(after), "post": dict(post)}
        self.save()
        return self.recover()

    def recover(self):
        """Finish only a journal whose every slot still equals its before/after image."""
        if not self.journal:
            return True
        journal = self.journal
        if journal.get("blocked"):
            self.status = "recovery_error"
            self.message = "Manual extraction interrupted a failed inventory commit"
            return False
        before, after = journal["before"], journal["after"]
        if len(before) != SLOT_COUNTS[self.device_id] or len(after) != len(before):
            raise ValueError("invalid production journal size")
        current = self._items()
        if any(not same_item(current[i], before[i]) and not same_item(current[i], after[i])
               for i in range(len(current))):
            self.status = "recovery_error"
            self.message = "Container changed during a saved production commit"
            return False
        # A previous save may have failed before any slot was touched. Keep the
        # journal durable before every retry, including the in-memory retry path.
        self.save()
        self.writing = True
        try:
            for slot, target in enumerate(after):
                if same_item(current[slot], target):
                    continue
                if not self.inventory.write(slot, target or EMPTY):
                    self.status = "write_failed"
                    return False
                if not same_item(self.inventory.read(slot), target):
                    self.status = "write_failed"
                    return False
            post = journal["post"]
            self.progress = int(post.get("progress", self.progress))
            self.burn = int(post.get("burn", self.burn))
            self.recipe_id = post.get("recipe", self.recipe_id)
            self.journal = None
            self.save()
            self.message = ""
            return True
        finally:
            self.writing = False

    def fence_manual_extraction(self):
        # NetEase's PlayerTryRemove event has no cancel field. If recovery cannot
        # complete before that extraction, fence the journal instead of replaying
        # an output that the player may now be holding.
        if self.journal:
            self.journal["blocked"] = True
            self.save()

    def tick(self):
        if not self.alive or not self.recover():
            return
        self.message = ""
        if self.device_id == GENERATOR:
            self._generate()
        else:
            self._process()

    def _process(self):
        items = self._items()
        recipe = self._recipe(items)
        if recipe is None:
            # Taking an input is permitted. It cancels that batch, never refunds FE.
            if self.progress or self.recipe_id:
                self.progress = 0
                self.recipe_id = ""
                self.save()
            self.status = "waiting_input"
            return
        if self.recipe_id != recipe.id:
            self.progress = 0
            self.recipe_id = recipe.id
            self.save()
        if not self._has_room(recipe, items[-1]):
            self.status = "output_full"
            return
        if self.progress < recipe.duration_ticks:
            with self.api.open_transaction() as tx:
                if self.energy.extract(recipe.power_fe, tx) != recipe.power_fe:
                    self.status = "no_power"
                    return
                tx.commit()
            self.progress += 1
            self.save()
        self.status = "working"
        if self.progress < recipe.duration_ticks:
            return
        after = deepcopy(items)
        for slot, (_name, count) in enumerate(recipe.inputs):
            after[slot]["count"] -= count
            if not after[slot]["count"]:
                after[slot] = None
        after[-1] = plain_item(recipe.output, item_count(items[-1]) + recipe.count)
        if self._commit_slots(items, after, {"progress": 0, "recipe": ""}):
            self.status = "complete"

    def _generate(self):
        # Reset a shared transactional allowance: all pulls and pushes use this endpoint.
        with self.api.open_transaction() as tx:
            self.budget.insert(GENERATOR_RATE, tx)
            tx.commit()
        if self.energy.get_amount() + GENERATOR_RATE > CAPACITY:
            self.status = "buffer_full"
            return
        if not self.burn:
            items = self._items()
            if not fuel_item(items[0]):
                self.status = "waiting_fuel"
                return
            after = deepcopy(items)
            after[0]["count"] -= 1
            if not after[0]["count"]:
                after[0] = None
            if not self._commit_slots(items, after, {"burn": FUEL_TICKS}):
                return
        old_burn = self.burn
        self.burn -= 1
        try:
            with self.api.open_transaction() as tx:
                if self.energy.insert(GENERATOR_RATE, tx) != GENERATOR_RATE:
                    self.burn = old_burn
                    self.status = "buffer_full"
                    return
                tx.commit()
        except self.api.TransactionError:
            self.burn = old_burn
            raise
        self.save()
        self.status = "generating"

    def push_energy(self, neighbors):
        """Round robin in small quanta; source budget caps all six faces together."""
        moved = 0
        start = self.cursor
        targets = list(neighbors)
        if len(targets) != 6:
            raise ValueError("six energy neighbors required")
        # At most 64 FE / 8 FE slices = 8 passes; failed/full neighbors do not burn budget.
        for _ in range(8):
            changed = False
            for offset in range(6):
                face = (start + offset) % 6
                target = targets[face]
                if target is None:
                    continue
                with self.api.open_transaction() as tx:
                    amount = self.api.move_energy(self.energy_output, target, 8, tx)
                    tx.commit()
                if amount:
                    moved += amount
                    changed = True
                    self.cursor = (face + 1) % 6
                if not self.budget.get_amount():
                    self.save()
                    return moved
            if not changed:
                break
        self.save()
        return moved
