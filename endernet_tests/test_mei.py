# coding=utf-8
"""MEI public-component contract, source data fidelity and unload regression."""
import unittest
import json
import os
import re
from types import SimpleNamespace as NS
from unittest.mock import Mock

import testsupport  # noqa: F401
from endernet.client import mei
from endernet.common.materials import MACHINE_BLOCKS, MACHINE_RECIPES
from endernet.server.production.machine import fuel_item, FUEL_TICKS, GENERATOR_RATE


class Ingredient(object):
    def __init__(self, stacks, amount=1):
        self.stacks, self.amount = stacks, amount

    def get_stacks(self):
        return self.stacks

    def get_amount(self):
        return self.amount

    def get_chance(self):
        return 1


class Recipe(object):
    def __init__(self, recipe_type, inputs, outputs, recipe_id=None, data=None):
        self.Type, self.Inputs, self.Outputs = recipe_type, inputs, outputs
        self.Id, self.Data = recipe_id, data

    def GetInputs(self):
        return self.Inputs

    def GetPrimaryOutput(self):
        return self.Outputs[0] if self.Outputs else None

    def GetOutputs(self):
        return self.Outputs

    def GetCatalysts(self):
        return []


class Source(object):
    def __init__(self, category_id):
        self.CategoryId, self.recipes = category_id, []

    def AddAll(self, recipes):
        self.recipes.extend(recipes)


class Factory(object):
    def __init__(self):
        self.missing = set()

    def GetItem(self, ItemId, AuxValue=0):
        if (ItemId, AuxValue) in self.missing:
            raise ValueError(ItemId)
        return NS(stack=lambda count=1: (ItemId, AuxValue, count))

    def GetStacksFromIds(self, ids):
        return [self.GetItem(name).stack() for name in ids]


class Registry(object):
    def __init__(self):
        self.categories, self.sources, self.scripts = {}, {}, {}

    def RegisterCategory(self, category):
        if category.category_id in self.categories:
            return False
        self.categories[category.category_id] = category
        return True

    def RegisterSource(self, source):
        self.sources[source.CategoryId] = source
        return True

    def RegisterScript(self, category_id, script):
        self.scripts[category_id] = script
        return True

    def UnRegisterScript(self, category_id):
        self.scripts.pop(category_id, None)

    def UnRegisterCategory(self, category_id):
        self.categories.pop(category_id, None)
        self.sources.pop(category_id, None)


def context():
    registry = Registry()
    return NS(
        Types=NS(Ingredient=Ingredient, Recipe=Recipe, RecipeCategory=NS, StaticRecipeSource=Source),
        Registry=NS(ItemFactory=Factory(), SourceRegistry=registry, RecipeUIManager=registry),
        UI=NS(Binding=NS(RecipeUIScript=object, RecipeBinder=NS(Group=lambda prefixes: lambda fn: fn)),
              Slots=NS(render_slot=lambda control, ingredient: (control, ingredient))),
        Log=Mock(),
    )


def registered_ui_documents():
    root = os.path.join(testsupport.REPO_ROOT, 'endernet_rp')
    with open(os.path.join(root, 'ui', '_ui_defs.json'), encoding='utf-8') as handle:
        paths = json.load(handle)['ui_defs']
    documents = {}
    for relative in paths:
        with open(os.path.join(root, relative), encoding='utf-8') as handle:
            document = json.load(handle)
        documents[document['namespace']] = document
    return documents


def ui_control_references(value):
    """Find inherited controls and qualified control templates, excluding inputs."""
    if isinstance(value, dict):
        for key, child in value.items():
            if '@' in key:
                reference = key.split('@', 1)[1]
                if not reference.startswith('$') and '.' in reference:
                    yield reference
            if ((key.startswith('$') or key.endswith('_template')) and isinstance(child, str)
                    and re.match(r'^[A-Za-z_]\w*\.[A-Za-z_]\w*$', child)
                    and not child.startswith('button.')):
                yield child
            for reference in ui_control_references(child):
                yield reference
    elif isinstance(value, list):
        for child in value:
            for reference in ui_control_references(child):
                yield reference


def local_ui_instance(documents, key, value):
    """Resolve the local inheritance needed by the recipe script's control paths."""
    result = {}
    if '@' in key:
        reference = key.split('@', 1)[1]
        if reference.split('.', 1)[0] in documents:
            result.update(local_ui_control(documents, reference))
    result.update(value)
    return result


def local_ui_control(documents, reference):
    namespace, name = reference.split('.', 1)
    document = documents[namespace]
    key = next(key for key in document if key.split('@', 1)[0] == name)
    return local_ui_instance(documents, key, document[key])


def local_ui_path(documents, reference, path):
    control = local_ui_control(documents, reference)
    for segment in path.split('/'):
        children = [(key, child) for entry in control.get('controls', [])
                    for key, child in entry.items() if key.split('@', 1)[0] == segment]
        if len(children) != 1:
            raise AssertionError('%s/%s: missing or ambiguous %s' % (reference, path, segment))
        control = local_ui_instance(documents, *children[0])
    return control


class MeiUiDependencyTests(unittest.TestCase):
    def test_registered_ui_uses_only_bundled_vanilla_and_mei_namespaces(self):
        documents = registered_ui_documents()
        allowed = set(documents) | {
            'common', 'common_toggles', 'container', 'gameplay',
            'netease_container_pocket', 'netease_editor_template_namespace',
            'mei_common', 'mei_recipe_common',
        }
        missing = sorted((namespace, reference)
                         for namespace, document in documents.items()
                         for reference in ui_control_references(document)
                         if reference.split('.', 1)[0] not in allowed)
        self.assertEqual(missing, [], 'UI controls require an unbundled optional mod')

    def test_recipe_pages_do_not_use_skybluetech_resource_paths(self):
        documents = registered_ui_documents()
        for namespace, document in documents.items():
            with self.subTest(namespace=namespace):
                self.assertFalse('textures/ui/skybluetech/' in json.dumps(document),
                                 '%s requires an unbundled SkyBlueTech texture' % namespace)

    def test_three_recipe_layouts_keep_real_animation_masks_and_mei_hover(self):
        documents = registered_ui_documents()
        for namespace in ('EnderNetMei', 'EnderNetMeiCharge', 'EnderNetMeiFuel'):
            reference = namespace + '.content_panel'
            generating = namespace == 'EnderNetMeiFuel'
            with self.subTest(namespace=namespace):
                power_mask = local_ui_path(documents, reference, 'power/bar/mask')
                self.assertEqual(power_mask['type'], 'image')
                texture = power_mask['texture']
                self.assertTrue(texture.startswith('textures/endernet/'), texture)
                self.assertTrue(os.path.isfile(os.path.join(
                    testsupport.REPO_ROOT, 'endernet_rp', texture + '.png')), texture)
                info = local_ui_path(documents, reference, 'power/info')
                overlay = next(key for entry in info['controls'] for key in entry
                               if key.split('@', 1)[0] == 'overlay')
                self.assertEqual(overlay, 'overlay@mei_common.slot_overlay')
                progress = 'body/flame/mask' if generating else 'body/progress/mask'
                self.assertEqual(local_ui_path(documents, reference, progress)['type'], 'image')
                if generating:
                    self.assertEqual(local_ui_path(documents, reference, 'body/input')['type'], 'panel')
                    slot = local_ui_path(documents, reference, 'body/input')
                    self.assertIn('0@mei_common.item_panel', slot['controls'][0])


class MeiRecipesTests(unittest.TestCase):
    def test_optional_slots_hide_parent_outside_mei_visibility_binding(self):
        path = os.path.join(testsupport.REPO_ROOT, 'endernet_rp', 'ui', 'endernet',
                            'mei_recipes', 'processing.json')
        with open(path, encoding='utf-8') as handle:
            page = json.load(handle)
        content = dict(next(iter(control.items())) for control in page['content_panel']['controls'])
        controls = dict(next(iter(control.items())) for control in content['body']['controls'])
        for name in ('input0', 'input1', 'input2', 'output'):
            holder = controls[name]
            self.assertEqual(holder['type'], 'panel')
            self.assertNotIn('bindings', holder)
            self.assertIn('0@mei_common.item_panel', holder['controls'][0])

    def test_all_machine_recipes_keep_identity_counts_and_modes(self):
        ctx = context()
        actual = {}
        for machine in MACHINE_BLOCKS:
            actual.update((r.Id, r) for r in mei.build_recipes(ctx, machine.id))
        self.assertEqual(len(actual), len(MACHINE_RECIPES) + 1)
        for spec in MACHINE_RECIPES:
            recipe = actual[spec.id]
            self.assertEqual(recipe.Type, spec.machine)
            self.assertEqual([(v.stacks[0][0], v.amount) for v in recipe.Inputs], list(spec.inputs))
            self.assertEqual(recipe.Outputs[0].stacks, [(spec.output, 0, spec.count)])
            self.assertEqual(recipe.Outputs[0].amount, spec.count)
            self.assertEqual(recipe.Data, dict(mode=spec.mode, ticks=spec.duration_ticks, power=spec.power_fe))

    def test_fuel_is_queryable_with_exact_aux_and_no_fake_output(self):
        recipe = mei.build_recipes(context(), mei.GENERATOR)[0]
        self.assertEqual(recipe.Outputs, [])
        self.assertEqual(recipe.Data['ticks'] * recipe.Data['power'], FUEL_TICKS * GENERATOR_RATE)
        self.assertEqual(recipe.Inputs[0].amount, 1)
        self.assertEqual(recipe.Inputs[0].stacks,
                         [('minecraft:coal', 0, 1), ('minecraft:coal', 1, 1), ('minecraft:charcoal', 0, 1)])
        for name, aux, count in recipe.Inputs[0].stacks:
            self.assertTrue(fuel_item(dict(newItemName=name, newAuxValue=aux, count=count)))
        self.assertFalse(fuel_item(dict(newItemName='minecraft:coal', newAuxValue=2, count=1)))

    def test_missing_input_skips_whole_recipe_not_a_free_recipe(self):
        ctx = context()
        ctx.Registry.ItemFactory.missing.add(('endernet:silicon', 0))
        recipes = mei.build_recipes(ctx, 'endernet:resonance_inscriber')
        self.assertEqual(len(recipes), 4)
        self.assertNotIn('assemble', [r.Data['mode'] for r in recipes])
        self.assertEqual(ctx.Log.warn.call_count, 3)

    def test_unavailable_charcoal_alias_preserves_coal_aux_one(self):
        ctx = context()
        ctx.Registry.ItemFactory.missing.add(('minecraft:charcoal', 0))
        recipe = mei.build_recipes(ctx, mei.GENERATOR)[0]
        self.assertEqual(recipe.Inputs[0].stacks, [('minecraft:coal', 0, 1), ('minecraft:coal', 1, 1)])

    def test_registration_unload_and_world_reentry(self):
        ctx = context()
        cls = mei.build_extension(object)
        self.assertIs(cls, mei.build_extension(object))
        registry = ctx.Registry.SourceRegistry
        for _ in range(2):
            extension = cls()
            extension.OnRegister(ctx)
            self.assertEqual(len(registry.categories), 3)
            self.assertEqual(sorted(len(s.recipes) for s in registry.sources.values()), [1, 1, 7])
            for name, category in registry.categories.items():
                self.assertEqual(category.stations, [(name, 0, 1)])
                self.assertIn(name, registry.scripts)
            extension.OnUnload(ctx)
            extension.OnUnload(ctx)
            self.assertEqual((registry.categories, registry.sources, registry.scripts), ({}, {}, {}))

    def test_category_conflict_does_not_remove_foreign_registration(self):
        ctx = context()
        registry = ctx.Registry.SourceRegistry
        foreign = NS(category_id=MACHINE_BLOCKS[0].id)
        registry.RegisterCategory(foreign)
        extension = mei.build_extension(object)()
        extension.OnRegister(ctx)
        extension.OnUnload(ctx)
        self.assertEqual(registry.categories, {foreign.category_id: foreign})

    def test_no_mei_no_player_old_api_and_component_exception(self):
        api = Mock()
        api.GetLocalPlayerId.return_value = None
        self.assertFalse(mei.setup(api))
        api.CreateComponent.assert_not_called()
        api.GetLocalPlayerId.return_value = 'player'
        api.CreateComponent.return_value = None
        self.assertFalse(mei.setup(api))
        api.CreateComponent.return_value = NS(GetApiVersion=lambda: 1)
        self.assertFalse(mei.setup(api))
        api.CreateComponent.side_effect = RuntimeError('MEI absent')
        self.assertFalse(mei.setup(api))

    def test_setup_passes_same_class_and_waits_for_ui_ready(self):
        component = NS(GetApiVersion=lambda: 2, GetExtensionCls=lambda: object, RegisterExtension=Mock(return_value=True))
        api = Mock()
        api.GetLocalPlayerId.return_value = 'player'
        api.CreateComponent.return_value = component
        bus = Mock()
        ready = object()
        service = mei.MeiClient(bus, ready, api)
        service.start()
        api.CreateComponent.assert_not_called()
        self.assertTrue(service.on_ready(None))
        self.assertTrue(service.on_ready(None))
        self.assertIs(component.RegisterExtension.call_args_list[0].args[0],
                      component.RegisterExtension.call_args_list[1].args[0])
        service.stop()
        bus.RemoveEventListener.assert_called_once_with(ready, service.on_ready, 0)

    def test_ui_slots_energy_tooltips_and_animation(self):
        ctx = context()
        for machine in MACHINE_BLOCKS:
            generating = machine.id == mei.GENERATOR
            script_cls = mei.build_ui_script(ctx, generating)
            for recipe in mei.build_recipes(ctx, machine.id):
                controls = {}
                def get_control(path):
                    return controls.setdefault(path.replace('/wrapper/content/', ''), Mock())
                script = script_cls()
                script.Recipe = recipe
                script.RecipeControl = NS(GetChildByPath=get_control)
                script.OnCreate()
                for i in range(1 if generating else 3):
                    if not generating:
                        controls['body/input%d' % i].SetVisible.assert_called_with(i < len(recipe.Inputs))
                    self.assertIs(script.InputPanel(i)[1], recipe.Inputs[i] if i < len(recipe.Inputs) else None)
                if not generating:
                    controls['body/mode'].asLabel().SetText.assert_called_with(mei.MODE_LABELS[recipe.Data['mode']].encode('utf-8'))
                    self.assertIs(script.OutputPanel(0)[1], recipe.GetPrimaryOutput())
                else:
                    self.assertFalse(hasattr(script, 'OutputPanel'))
                power = script.PowerPanel(0)
                self.assertTrue(power.OverlayControlVisible)
                self.assertIn(('%d FE/t' % recipe.Data['power']).encode('utf-8'), power.HoverText)
                self.assertIn(('%d FE' % (recipe.Data['power'] * recipe.Data['ticks'])).encode('utf-8'), power.HoverText)
                self.assertIn(mei.MODE_LABELS[recipe.Data['mode']].encode('utf-8'), script.ProgressPanel(0).HoverText)
                mask = controls['power/bar/mask']
                initial = mask.SetFullSize.call_args.args[1]['relativeValue']
                self.assertAlmostEqual(initial, 0 if generating else 51.0 / 52)
                script.OnAnimTick(script.period)
                final = mask.SetFullSize.call_args.args[1]['relativeValue']
                self.assertAlmostEqual(final, 51.0 / 52 if generating else 0)
                progress = controls['body/flame/mask' if generating else 'body/progress/mask'].asImage()
                progress.SetSpriteClipRatio.assert_called_with(1 if generating else 0)
                script.OnAnimTick(script.period + 1)
                self.assertEqual(mask.SetFullSize.call_args.args[1]['relativeValue'], initial)
                self.assertEqual(script.period, 100 if generating else 20)
                power.Destroy()


def stack(name='minecraft:stone', aux=0, **item_fields):
    item = NS(ItemId=name, AuxValue=aux, **item_fields)
    return NS(Item=item, KIND='item', Count=1, is_empty=lambda: False)


def transfer_request(category='crafting_table', counts=(1,), indices=None, outputs=None):
    inputs = [Ingredient([stack('minecraft:stone', i)], count) for i, count in enumerate(counts)]
    recipe = Recipe(category, inputs, outputs if outputs is not None else [Ingredient([stack()], 4)])
    cells = [NS(Index=i if indices is None else indices[i], Ingredient=value,
                Need=value.amount, Have=0, Stack=value.stacks[0]) for i, value in enumerate(inputs)]
    return NS(Recipe=recipe, CategoryId=category, Slots=cells, Catalysts=[], Fits=True,
              Max=True, MaxTimes=Mock(return_value=99))


class MeiTemplateTransferTests(unittest.TestCase):
    def setUp(self):
        self.view = NS(mei_import_error=Mock(return_value=None), on_mei_import=Mock(return_value='request-1'))
        mei.set_template_view(self.view)

    def tearDown(self):
        mei.clear_template_view(self.view)

    def test_crafting_preserves_grid_holes_aux_and_output_amount_without_items(self):
        request = transfer_request(counts=(1, 1, 1, 1), indices=(0, 1, 3, 4))
        handler = mei.TemplateTransferHandler(True)
        self.assertIsNone(handler.Check(request))
        self.assertTrue(handler.Transfer(request))
        config = self.view.on_mei_import.call_args.args[0]
        self.assertEqual(config['mode'], 'crafting')
        self.assertEqual([i for i, value in enumerate(config['inputs']) if value], [0, 1, 3, 4])
        self.assertEqual(config['inputs'][4]['key']['aux'], 3)
        self.assertEqual(config['outputs'][0]['count'], 4)
        self.assertEqual(len(config['inputs']), 9)
        request.MaxTimes.assert_not_called()

    def test_factory_keeps_large_quantities_in_one_slot_and_all_outputs(self):
        outputs = [Ingredient([stack('minecraft:iron_ingot')], 8),
                   Ingredient([stack('minecraft:gold_ingot')], 2)]
        request = transfer_request('furnace', counts=(64, 2), outputs=outputs)
        handler = mei.TemplateTransferHandler(False)
        self.assertIsNone(handler.GRID)
        self.assertTrue(handler.Transfer(request))
        config = self.view.on_mei_import.call_args.args[0]
        self.assertEqual(config['mode'], 'factory')
        self.assertEqual([v['count'] for v in config['inputs'] if v], [64, 2])
        self.assertEqual([v['count'] for v in config['outputs']], [8, 2])
        self.assertEqual(outputs[0].stacks[0].Count, 1)

    def test_invalid_recipes_never_submit_a_partial_import(self):
        cases = []
        oversized = transfer_request('machine', counts=(1,) * 10)
        cases.append(oversized)
        cases.append(transfer_request('machine', outputs=[Ingredient([stack()])] * 4))
        cases.append(transfer_request('machine', outputs=[]))
        cases.append(transfer_request('machine', counts=()))
        catalysts = transfer_request('machine')
        catalysts.Catalysts = [NS()]
        cases.append(catalysts)
        for field in ('UserData', 'EnchantData', 'ModEnchantData', 'CustomTips', 'ExtraId', 'Durability'):
            attr = NS(**{field: {'x': 1} if field == 'UserData' else 1})
            cases.append(transfer_request('machine', outputs=[Ingredient([stack(Attr=attr)])]))
        cases.append(transfer_request('machine', outputs=[Ingredient([stack(ExtraData={'x': 1})])]))
        fluid = transfer_request('machine')
        fluid.Recipe.Inputs.append(Ingredient([NS(KIND='fluid', is_empty=lambda: False)], 1000))
        cases.append(fluid)  # omitted by MEI's item matcher, still rejected
        probability = Ingredient([stack()])
        probability.get_chance = lambda: 0.5
        cases.append(transfer_request('machine', outputs=[probability]))
        cases.append(transfer_request('machine', outputs=[Ingredient([stack(), stack('minecraft:dirt')])]))
        cases.append(transfer_request('machine', counts=(0,)))
        for request in cases:
            with self.subTest(request=request):
                handler = mei.TemplateTransferHandler(False)
                self.assertIsInstance(handler.Check(request), str)
                self.assertIsInstance(handler.Transfer(request), str)
        self.view.on_mei_import.assert_not_called()

    def test_stale_check_closed_editor_and_failed_submission_do_not_report_success(self):
        handler = mei.TemplateTransferHandler(True)
        request = transfer_request()
        self.assertIsNone(handler.Check(request))
        self.view.mei_import_error.return_value = '等待配置更新'
        self.assertEqual(handler.Transfer(request), '等待配置更新')
        self.view.on_mei_import.assert_not_called()
        self.view.mei_import_error.return_value = None
        self.view.on_mei_import.return_value = None
        self.assertFalse(handler.Transfer(request))
        mei.clear_template_view(self.view)
        self.assertEqual(handler.Transfer(request), '请先打开样板终端')

    def test_old_screen_destroy_does_not_unbind_the_new_editor(self):
        old = NS()
        mei.clear_template_view(old)
        self.assertTrue(mei.TemplateTransferHandler(True).Transfer(transfer_request()))

    def test_pending_edit_does_not_cache_a_disabled_button_but_blocks_transfer(self):
        self.view.mei_import_error.side_effect = lambda include_pending=True: (
            '等待配置更新' if include_pending else None)
        handler = mei.TemplateTransferHandler(True)
        request = transfer_request()
        self.assertIsNone(handler.Check(request))
        self.assertEqual(handler.Transfer(request), '等待配置更新')
        self.view.on_mei_import.assert_not_called()

    def test_worktable_rejects_oversized_shapes_and_stack_per_cell(self):
        request = transfer_request()
        request.Fits = False
        self.assertIsInstance(mei.TemplateTransferHandler(True).Check(request), str)
        request = transfer_request(counts=(2,))
        self.assertIsInstance(mei.TemplateTransferHandler(True).Check(request), str)
        self.view.on_mei_import.assert_not_called()

    def test_registration_uses_public_facades_two_layouts_and_owned_cleanup(self):
        ctx = context()
        screens = NS(IsRegistered=Mock(return_value=False), Register=Mock(return_value=True), Unregister=Mock())
        transfers = NS(Register=Mock(side_effect=['factory', 'crafting']), Unregister=Mock())
        ctx.UI.Screens, ctx.UI.Transfers = screens, transfers
        extension = mei.build_extension(object)()
        extension.OnRegister(ctx)
        screens.Register.assert_called_once_with(mei.TEMPLATE_SCREEN, {'ContentPaths': ['/window']})
        factory, crafting = transfers.Register.call_args_list
        self.assertEqual(factory.args[0], mei.TEMPLATE_SCREEN)
        self.assertIsNone(factory.args[1].GRID)
        self.assertFalse(factory.args[1].CONSUMES)
        self.assertFalse(factory.kwargs['accepts'](Recipe('crafting_table', [], [])))
        self.assertTrue(factory.kwargs['accepts'](Recipe('furnace', [], [])))
        self.assertEqual(crafting.args[1].GRID, (3, 3))
        self.assertEqual(crafting.kwargs['categories'], ['crafting_table'])
        extension.OnUnload(ctx)
        extension.OnUnload(ctx)
        self.assertEqual([c.args[0] for c in transfers.Unregister.call_args_list], ['factory', 'crafting'])
        screens.Unregister.assert_called_once_with(mei.TEMPLATE_SCREEN)

    def test_existing_screen_proxy_is_not_removed_and_missing_facades_are_optional(self):
        ctx = context()
        screens = NS(IsRegistered=Mock(return_value=True), Register=Mock(), Unregister=Mock())
        ctx.UI.Screens, ctx.UI.Transfers = screens, NS(Register=Mock(return_value='token'), Unregister=Mock())
        extension = mei.build_extension(object)()
        extension.OnRegister(ctx)
        extension.OnUnload(ctx)
        screens.Register.assert_not_called()
        screens.Unregister.assert_not_called()
        ctx = context()
        extension = mei.build_extension(object)()
        extension.OnRegister(ctx)
        self.assertEqual(extension.transfer_tokens, [])
        extension.OnUnload(ctx)

    def test_machine_pages_enable_import_but_fuel_without_output_stays_disabled(self):
        for name, ignored in [('processing', False), ('charger', False), ('fuel', True)]:
            path = os.path.join(testsupport.REPO_ROOT, 'endernet_rp', 'ui', 'endernet',
                                'mei_recipes', name + '.json')
            with open(path, encoding='utf-8') as handle:
                page = json.load(handle)
            wrapper = page['recipe@mei_recipe_common.recipe_panel']['controls'][0]['wrapper']
            buttons = wrapper['controls'][1]['buttons@mei_recipe_common.buttons_panel_vertical']
            self.assertEqual(buttons['$plus_button_ignored'], ignored)


if __name__ == '__main__':
    unittest.main()
