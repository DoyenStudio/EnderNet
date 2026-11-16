# coding=utf-8
"""Optional MEI v2 integration, accessed only through its public component."""

from ..common.materials import (
    MACHINE_BLOCKS, MACHINE_RECIPES, GENERATOR_FUELS, GENERATOR_RATE, FUEL_TICKS,
)
from ..common.resources import ResourceKey

_BUILT = {}
_SERVICE = None
_TEMPLATE_VIEW = None
TEMPLATE_SCREEN = "endernet_template_terminal_panel.main"
CRAFTING_CATEGORY = "crafting_table"
GENERATOR = "endernet:resonance_generator"
MODE_LABELS = {"charge": u"充能", "grind": u"粉碎", "press": u"压印",
               "assemble": u"封装", "generate": u"燃料发电"}

try:
    INTEGER_TYPES = (int, long)
    TEXT_TYPES = (str, unicode)
except NameError:
    INTEGER_TYPES = (int,)
    TEXT_TYPES = (str,)


def set_template_view(view):
    # type: (object) -> None
    """Bind the live editor; MEI's transfer request carries no screen instance."""
    global _TEMPLATE_VIEW
    _TEMPLATE_VIEW = view


def clear_template_view(view):
    # type: (object) -> None
    global _TEMPLATE_VIEW
    if _TEMPLATE_VIEW is view:
        _TEMPLATE_VIEW = None


def _positive_count(count):
    if isinstance(count, bool) or not isinstance(count, INTEGER_TYPES) or count <= 0:
        raise ValueError(u"配方数量必须是正整数")
    return count


def _stack_key(stack):
    """Only plain item identities can be represented by this import contract."""
    item = getattr(stack, "Item", None)
    if getattr(stack, "KIND", None) != "item" or item is None:
        raise ValueError(u"暂不支持导入流体或其他非物品配方")
    name, aux = getattr(item, "ItemId", None), getattr(item, "AuxValue", None)
    if (not isinstance(name, TEXT_TYPES) or not name or ":" not in name
            or isinstance(aux, bool) or not isinstance(aux, INTEGER_TYPES) or aux < 0):
        raise ValueError(u"配方物品标识或附加值无效")
    attr = getattr(item, "Attr", None)
    if getattr(item, "ExtraData", None) or any(
            getattr(attr, field, None) for field in (
                "UserData", "EnchantData", "ModEnchantData", "CustomTips", "ExtraId", "Durability")):
        raise ValueError(u"暂不支持导入带自定义数据、附魔或耐久属性的配方物品")
    return ResourceKey.item(name, aux).to_dict()


def _ingredient_stacks(ingredient):
    if ingredient is None:
        return []
    if ingredient.get_chance() != 1:
        raise ValueError(u"样板不支持概率原料或概率产物")
    stacks = [stack for stack in ingredient.get_stacks() or () if stack and not stack.is_empty()]
    for stack in stacks:
        _stack_key(stack)
    if stacks:
        _positive_count(ingredient.get_amount())
    return stacks


def template_config(request, crafting):
    # type: (object, bool) -> dict
    """Translate public MEI data without mutating shared recipes or moving items."""
    recipe = request.Recipe
    if (request.CategoryId == CRAFTING_CATEGORY) != crafting:
        raise ValueError(u"配方类型与样板模式不匹配")
    if not getattr(request, "Fits", True):
        raise ValueError(u"配方放不进样板的九宫格")
    if request.Catalysts or recipe.GetCatalysts():
        raise ValueError(u"样板暂不支持需要催化剂或工具的配方")
    # MEI's transfer matcher omits non-item inputs. Check the recipe itself too,
    # so a fluid or an unsupported candidate cannot silently disappear.
    expected = 0
    for ingredient in recipe.GetInputs():
        if _ingredient_stacks(ingredient):
            expected += ingredient.get_amount()
    inputs = [None] * 9
    slots = request.Slots
    if len(slots) > 9:
        raise ValueError(u"样板最多支持九个输入格")
    actual = 0
    for slot in slots:
        index, count = slot.Index, _positive_count(slot.Need)
        if (isinstance(index, bool) or not isinstance(index, INTEGER_TYPES)
                or not 0 <= index < 9 or inputs[index] is not None):
            raise ValueError(u"配方输入格超出样板范围或重复")
        if crafting and count != 1:
            raise ValueError(u"工作台配方的每个输入格必须是一个物品")
        inputs[index] = {"key": _stack_key(slot.Stack), "count": count}
        actual += count
    if not actual or actual != expected:
        raise ValueError(u"配方原料无法完整导入样板")
    outputs = []
    for ingredient in recipe.GetOutputs():
        stacks = _ingredient_stacks(ingredient)
        if not stacks:
            continue
        if len(stacks) != 1:
            raise ValueError(u"样板不支持含多个候选产物的配方")
        outputs.append({"key": _stack_key(stacks[0]), "count": ingredient.get_amount()})
    if not outputs or len(outputs) > 3:
        raise ValueError(u"样板需要一至三个确定的物品产物")
    return {"mode": "crafting" if crafting else "factory", "inputs": inputs, "outputs": outputs}


class TemplateTransferHandler(object):
    CONSUMES = False
    HINT = u"§7把配方导入样板终端，自动选择样板模式"
    SCREEN_HINT = u"§7打开样板终端后可导入配方"

    def __init__(self, crafting):
        self.crafting = crafting
        # Machine quantities must stay in one slot; GRID expands shapeless
        # ingredient amounts into separate crafting cells in MEI.
        self.GRID = (3, 3) if crafting else None

    def Check(self, request):
        view = _TEMPLATE_VIEW
        if view is None:
            return u"请先打开样板终端"
        # MEI caches this decision for the open recipe page. Pending edits are
        # transient, so only Transfer may reject them at click time.
        error = view.mei_import_error(include_pending=False)
        if error:
            return error
        try:
            template_config(request, self.crafting)
        except (AttributeError, TypeError, ValueError) as error:
            return error.args[0] if error.args else u"配方无法导入样板终端"
        return None

    def Transfer(self, request):
        # MEI caches Check while the page is open. Recheck the live session and
        # pending edits at the moment of clicking, then submit one atomic edit.
        error = self.Check(request)
        if error:
            return error
        error = _TEMPLATE_VIEW.mei_import_error()
        if error:
            return error
        config = template_config(request, self.crafting)
        return bool(_TEMPLATE_VIEW.on_mei_import(config))


def _factory_recipe(recipe):
    return getattr(recipe, "Type", None) != CRAFTING_CATEGORY


def _ingredient(ctx, candidates, count=1):
    stacks = []
    for name, aux in candidates:
        try:
            item = ctx.Registry.ItemFactory.GetItem(ItemId=name, AuxValue=aux)
        except ValueError:
            item = None
        if item is not None:
            stacks.append(item.stack(count))
    return ctx.Types.Ingredient(stacks, count) if stacks else None


def build_recipes(ctx, machine_id):
    """Keep quantities, modes and energy tied to the actual machine rules."""
    recipes = []
    for spec in MACHINE_RECIPES:
        if spec.machine != machine_id:
            continue
        inputs = [_ingredient(ctx, [(name, 0)], count) for name, count in spec.inputs]
        output = _ingredient(ctx, [(spec.output, 0)], spec.count)
        if output is None or any(value is None for value in inputs):
            ctx.Log.warn("[EnderNet/MEI] Missing item for %s" % spec.id)
            continue
        recipes.append(ctx.Types.Recipe(
            machine_id, inputs, [output], recipe_id=spec.id,
            data={"mode": spec.mode, "ticks": spec.duration_ticks, "power": spec.power_fe},
        ))
    if machine_id == GENERATOR:
        fuel = _ingredient(ctx, GENERATOR_FUELS)
        if fuel is not None:
            # Energy is display metadata, never a fictitious item output.
            recipes.append(ctx.Types.Recipe(
                machine_id, [fuel], [], recipe_id="endernet:generator_fuel",
                data={"mode": "generate", "ticks": FUEL_TICKS, "power": GENERATOR_RATE},
            ))
    return recipes


class RecipeInfoPanel(object):
    """MEI overlay-only panel for energy and duration, without a fake item."""
    def __init__(self, text):
        self.HoverText = text.encode("utf-8")
        self.OverlayControlVisible = True

    def Destroy(self):
        pass


def build_ui_script(ctx, generating=False):
    binding = ctx.UI.Binding
    render_slot = ctx.UI.Slots.render_slot
    input_count = 1 if generating else 3

    class MachineRecipeUI(binding.RecipeUIScript):
        def OnCreate(self):
            root = self.RecipeControl
            base = "/wrapper/content/"
            if generating:
                self.inputs = [root.GetChildByPath(base + "body/input/0")]
            else:
                self.inputs = [root.GetChildByPath(base + "body/input%d/0" % i)
                               for i in range(input_count)]
                self.output = root.GetChildByPath(base + "body/output/0")
                # Slot bindings control their own visibility, so hide the parent.
                for i in range(input_count):
                    root.GetChildByPath(base + "body/input%d" % i).SetVisible(i < len(self.Recipe.GetInputs()))
                root.GetChildByPath(base + "body/mode").asLabel().SetText(
                    MODE_LABELS[self.Recipe.Data["mode"]].encode("utf-8"))
            self.power_mask = root.GetChildByPath(base + "power/bar/mask")
            progress = "body/flame/mask" if generating else "body/progress/mask"
            self.progress_mask = root.GetChildByPath(base + progress).asImage()
            self.progress_mask.SetClipDirection("fromTopToBottom" if generating else "fromRightToLeft")
            # MEI ticks every 0.2 s; cap long fuel previews at the same 20 s as SkyBlueTech.
            self.period = max(1, min(100, int(self.Recipe.Data["ticks"]) // 4))
            self.energy_shown = None
            self.OnAnimTick(0)

        @binding.RecipeBinder.Group(["mei_ui.recipe.input%d" % i for i in range(input_count)])
        def InputPanel(self, index):
            inputs = self.Recipe.GetInputs()
            return render_slot(self.inputs[index], inputs[index] if index < len(inputs) else None)

        if not generating:
            @binding.RecipeBinder.Group(["mei_ui.recipe.output0"])
            def OutputPanel(self, index):
                return render_slot(self.output, self.Recipe.GetPrimaryOutput())

        @binding.RecipeBinder.Group(["mei_ui.recipe.power"])
        def PowerPanel(self, index):
            data = self.Recipe.Data
            label = u"产能" if generating else u"耗能"
            unit = "EN" if generating else "FE"
            return RecipeInfoPanel(u"§7%s：§f%d %s/t\n§7合计：§f%d %s" % (
                label, data["power"], unit, data["ticks"] * data["power"], unit))

        @binding.RecipeBinder.Group(["mei_ui.recipe.progress"])
        def ProgressPanel(self, index):
            data = self.Recipe.Data
            return RecipeInfoPanel(u"§7%s\n§7耗时：§f%g 秒" % (
                MODE_LABELS[data["mode"]], data["ticks"] / 20.0))

        def OnAnimTick(self, ticks):
            ratio = float(ticks % (self.period + 1)) / self.period
            self.progress_mask.SetSpriteClipRatio(ratio if generating else 1.0 - ratio)
            level = ratio if generating else 1.0 - ratio
            # Match SkyBlueTech's 17 segments in a 52 px inner track.
            segments = max(1 if level > 0 else 0, int(level * 17 + 0.5))
            shown = 3.0 * segments / 52.0
            if shown != self.energy_shown:
                self.power_mask.SetFullSize("y", {"followType": "parent", "relativeValue": shown})
                self.energy_shown = shown

        def OnDestroy(self):
            self.DestroyBindings()

    return MachineRecipeUI


def build_extension(base):
    """Cache the class across worlds so MEI's duplicate-ID guard accepts it."""
    if base in _BUILT:
        return _BUILT[base]

    class EnderNetMachines(base):
        ID = "endernet:production"
        NAME = "endernet.mei.name"
        AUTHOR = "EnderNet"
        VERSION = "1.0.0"
        MIN_API_VERSION = 2

        def __init__(self):
            self.registered = []
            self.transfer_tokens = []
            self.registered_screen = False

        def OnRegister(self, ctx):
            screens = getattr(ctx.UI, "Screens", None)
            transfers = getattr(ctx.UI, "Transfers", None)
            if screens is not None and transfers is not None:
                is_registered = getattr(screens, "IsRegistered", lambda _name: False)
                attached = is_registered(TEMPLATE_SCREEN)
                if not attached:
                    # screen_root owns a centered /window; avoid and hide only
                    # that panel, never the full-screen overlay root.
                    self.registered_screen = screens.Register(
                        TEMPLATE_SCREEN, {"ContentPaths": ["/window"]})
                    attached = self.registered_screen
                if attached:
                    for handler, options in (
                            (TemplateTransferHandler(False), {"accepts": _factory_recipe}),
                            (TemplateTransferHandler(True), {"categories": [CRAFTING_CATEGORY]})):
                        token = transfers.Register(TEMPLATE_SCREEN, handler, **options)
                        if token:
                            self.transfer_tokens.append(token)
            registry = ctx.Registry.SourceRegistry
            scripts = {False: build_ui_script(ctx), True: build_ui_script(ctx, True)}
            for index, machine in enumerate(MACHINE_BLOCKS):
                layout = "EnderNetMeiFuel" if machine.id == GENERATOR else "EnderNetMei"
                if machine.id == "endernet:resonance_charger":
                    layout = "EnderNetMeiCharge"
                category = ctx.Types.RecipeCategory(
                    category_id=machine.id, name_key="tile.%s.name" % machine.id,
                    icon=(machine.id, 0), control_def=layout + ".RECIPES_GRID",
                    stations=ctx.Registry.ItemFactory.GetStacksFromIds([machine.id]),
                    sort_index=100 + index,
                )
                source = ctx.Types.StaticRecipeSource(machine.id)
                source.AddAll(build_recipes(ctx, machine.id))
                if not registry.RegisterCategory(category):
                    continue
                self.registered.append(machine.id)
                if not registry.RegisterSource(source):
                    raise ValueError("Cannot register MEI source: %s" % machine.id)
                if not ctx.Registry.RecipeUIManager.RegisterScript(machine.id, scripts[machine.id == GENERATOR]):
                    raise ValueError("Cannot register MEI UI: %s" % machine.id)

        def OnUnload(self, ctx):
            transfers = getattr(ctx.UI, "Transfers", None)
            if transfers is not None:
                for token in self.transfer_tokens:
                    transfers.Unregister(token)
            self.transfer_tokens = []
            screens = getattr(ctx.UI, "Screens", None)
            if self.registered_screen and screens is not None:
                screens.Unregister(TEMPLATE_SCREEN)
            self.registered_screen = False
            for category_id in self.registered:
                ctx.Registry.RecipeUIManager.UnRegisterScript(category_id)
                ctx.Registry.SourceRegistry.UnRegisterCategory(category_id)
            self.registered = []

    _BUILT[base] = EnderNetMachines
    return EnderNetMachines


def setup(client_api):
    """Called after UiInitFinished; absent MEI is a normal soft dependency."""
    try:
        player = client_api.GetLocalPlayerId()
        mei = client_api.CreateComponent(player, "MoreEnoughItems", "MeiApi") if player else None
    except Exception:
        return False
    if not mei or mei.GetApiVersion() < 2:
        return False
    return mei.RegisterExtension(build_extension(mei.GetExtensionCls()))


class MeiClient(object):
    def __init__(self, bus, ready_event, client_api):
        self.bus = bus
        self.ready_event = ready_event
        self.client_api = client_api

    def start(self):
        self.bus.ListenEvent(self.ready_event, 0)(self.on_ready)

    def on_ready(self, event):
        return setup(self.client_api)

    def stop(self):
        # MEI owns extension unloading when leaving the world.
        self.bus.RemoveEventListener(self.ready_event, self.on_ready, 0)


def start_mei():
    global _SERVICE
    import mod.client.extraClientApi as client_api
    from ...tooldelta.events.client import UiInitFinishedEvent
    from ...tooldelta.events.event_bus import GetMCClientEventBus

    stop_mei()
    _SERVICE = MeiClient(GetMCClientEventBus(), UiInitFinishedEvent, client_api)
    _SERVICE.start()


def stop_mei():
    global _SERVICE
    if _SERVICE is not None:
        _SERVICE.stop()
        _SERVICE = None
