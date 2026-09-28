# coding=utf-8
"""JSON UI 速读工具：只打印开发需要的那几行。

    python scripts/ui_dump.py tree endernet_rp/ui/assemble_chamber.json
    python scripts/ui_dump.py tree <文件> --root content/root_panel --layout
    python scripts/ui_dump.py tree <文件> --all --depth 2
    python scripts/ui_dump.py find <正则> [--file <文件>]
    python scripts/ui_dump.py refs <文件>

界面 JSON 极其啰嗦：一个 9 格网格铺开写要 300 行，其中大半是颜色、字体、
九宫格切图、手柄 focus 参数。这些对「控件路径是什么、它绑哪个集合、脚本该读
哪个绑定名」毫无帮助，却会把真正要看的那几行淹掉。

所以 `tree` 默认只打印结构、类型、集合、文本、绑定和 `$` 变量——判据是
**这一项能不能被脚本用上**；几何布局（size/offset/anchor/layer）要加
`--layout`，其余全部属性要加 `--all`。

`find` 跨全部界面文件查找控件或模板，回答「这个模板定义在哪个文件」——
`plans/ui/*.md` 第 3 节的「定义位置」一栏要的就是它。
`refs` 汇总一个界面对外的引用：继承、纹理、本地化键、按钮事件名、集合槽位。

原版 `common.*` 模板也会被解析：它们的定义在游戏目录的
`data/resource_packs/*/ui/` 下（路径取自 `.mcdev.json`），那些文件带
`//` 注释，严格 json 解析器读不了，因此这里自带一层容错解析。找不到游戏
目录时只跳过原版，本仓库的界面照常分析。

只读，不修改任何文件。本脚本是仓库工具（和 gen_blocks.py 一样跑在 Python 3
上），不是模组代码，不受 py2check 门禁约束。
"""

import argparse
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _console import use_utf8_stdout  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RP_UI = os.path.join(REPO, "endernet_rp", "ui")
MCDEV = os.path.join(REPO, ".mcdev.json")

#: `tree` 默认打印的属性，顺序即输出顺序。
#:
#: 入选标准是「脚本用得上」：控件身份（type）、数据来源（集合与绑定）、
#: 交互入口（按钮名）、显示内容（text）、模板参数（`$` 变量，见下）。
PRIMARY_KEYS = (
    "type",
    "collection_name",
    "collection_index",
    "grid_dimensions",
    "grid_item_template",
    "maximum_grid_items",
    "orientation",
    "text",
    "visible",
    "ignored",
    "enabled",
    "bindings",
)

#: 只在 `--layout` 下打印：几何与渲染层级。写布局或排查遮挡时才需要。
LAYOUT_KEYS = (
    "size",
    "offset",
    "anchor_from",
    "anchor_to",
    "min_size",
    "max_size",
    "layer",
    "clips_children",
)

#: 变量键前缀。`$foo` 与 `$foo|default` 是模板的对外参数，
#: `plans/ui/*.md` 第 4 节「模板参数」要填的就是它们，因此默认全打印。
VARIABLE_PREFIX = "$"

#: 绑定与按钮名的属性。
BINDING_KEYS = ("bindings",)
BUTTON_KEYS = ("$pressed_button_name", "$button_name")

TEXTURE_PREFIX = "textures/"

#: 本地化键的形状，例如 `tile.endernet:assemble_chamber.name`。
#: JSON UI 里没有独立标记，只能按前缀认；认错顶多多打一行，不会漏掉真的键。
LANG_KEY_RE = re.compile(r"^(tile|item|ui|action|gui|screen)\.[A-Za-z0-9_:.\-]+$")

#: 单行里属性值的最大长度，超出截断。长文本属性会把树挤到没法看。
MAX_VALUE_CHARS = 56

#: 原版资源包目录名。**顺序即优先级，靠后的覆盖靠前的**——这个次序是
#: 按目录名字典序排出来的，与游戏内的包叠加次序一致（vanilla 最低，
#: vanilla_netease 是网易的覆盖层）。只影响同名模板的归属，不影响分析。
VANILLA_PACKS = ("vanilla", "vanilla_base", "vanilla_netease")


# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------

def strip_json_comments(text):
    # type: (str) -> str
    """去掉 `//` 与 `/* */` 注释。原版界面文件带注释，严格解析器读不了。

    逐字符扫描而不是用正则：贴图路径里全是 `/`，只有跟踪「当前是否在字符串
    内」才能区分 `"textures/ui/x"` 和真正的注释开始。
    """
    out = []
    in_string = False
    escaped = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length:
            following = text[index + 1]
            if following == "/":
                while index < length and text[index] not in "\r\n":
                    index += 1
                continue
            if following == "*":
                index += 2
                while index + 1 < length and text[index:index + 2] != "*/":
                    index += 1
                index += 2
                continue
        out.append(char)
        index += 1
    return "".join(out)


def read_json(path):
    # type: (str) -> dict
    """读界面 JSON。先按标准 JSON 试，失败再剥注释重试。

    本仓库自己写的界面文件是严格 JSON，原版那些不是；两条路都留着，
    这样本仓库的文件一旦被误加注释也能立刻发现，而不是被容错吃掉。
    """
    with io.open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    try:
        return json.loads(text)
    except ValueError:
        return json.loads(strip_json_comments(text))


def relative(path):
    # type: (str) -> str
    """仓库内的文件显示相对路径；仓库外的（原版资源包）直接给绝对路径。

    对 D 盘另一棵树 `relpath` 会算出一串 `../../..`，既难读也没法直接点开。
    """
    inside = os.path.relpath(path, REPO)
    if inside.startswith(".."):
        return path.replace(os.sep, "/")
    return inside.replace(os.sep, "/")


def split_key(key):
    # type: (str) -> tuple
    """`name@namespace.template` -> `("name", "namespace.template"|None)`。"""
    name, sep, ref = key.partition("@")
    return name, (ref if sep else None)


def is_control_key(key):
    # type: (str) -> bool
    """顶层与 controls 里哪些键是控件。

    排除 `namespace` 与 `_` 开头的元数据；`$` 开头的顶层键是变量而非控件，
    由调用方的 `isinstance(props, dict)` 兜住。
    """
    return not key.startswith("_") and key != "namespace"


def child_entries(props):
    # type: (dict) -> list
    """`controls` 里的子控件，按书写顺序返回 `(键, 属性)`。

    controls 每一项是 `{控件名: 属性}`；规范上一次只写一个键，
    但这里遍历全部键，免得遇到多个键时静默丢控件。
    """
    out = []
    for entry in props.get("controls") or []:
        if not isinstance(entry, dict):
            continue
        for key, value in entry.items():
            out.append((key, value))
    return out


# --------------------------------------------------------------------------
# 索引：`namespace.控件名` -> 定义处
# --------------------------------------------------------------------------

class Registry(object):
    """界面模板的索引，用来解析 `@ns.template` 到底落在哪个文件。

    本仓库的文件在初始化时全部读入；原版资源包按需读——先把每个包的
    namespace 扫出来（只做正则，不解析），真正查到某个 namespace 时才解析
    那个文件。原版有几百个界面文件，全量解析太慢，而实际用到的只有
    `common` 等少数几个。
    """

    def __init__(self):
        #: 本仓库：`namespace.控件名` -> `(文件, 原始键, 属性)`
        self.controls = {}
        #: 原版（按需解析）：同一形状，但**永远让位给本仓库的定义**
        self.vanilla_controls = {}
        #: 文件 -> `namespace`，解析继承链时用来确定「下一跳在哪个域里找」
        self.file_namespaces = {}
        #: 原版：`namespace` -> 文件列表，按包叠加次序
        self.vanilla_files = {}
        self._vanilla_loaded = set()
        self._vanilla_ready = False

    def load_repo(self):
        # type: () -> None
        for path in sorted(os.listdir(RP_UI)):
            if not path.endswith(".json"):
                continue
            full = os.path.join(RP_UI, path)
            try:
                payload = read_json(full)
            except ValueError as error:
                print("!! %s 解析失败，已跳过：%s" % (relative(full), error))
                continue
            self.add_payload(full, payload)

    def add_payload(self, path, payload, into=None):
        # type: (str, dict, dict) -> None
        if into is None:
            into = self.controls
        namespace = payload.get("namespace")
        if not namespace:
            return
        self.file_namespaces[path] = namespace
        for key, props in payload.items():
            if not is_control_key(key) or not isinstance(props, dict):
                continue
            name, _ref = split_key(key)
            # 后读到的覆盖先读到的。这与游戏一致（同名控件由后加载的文件
            # 覆盖），也让「原版 → 各覆盖包」的次序自然生效。
            into["%s.%s" % (namespace, name)] = (path, key, props)

    def _index_vanilla(self):
        # type: () -> None
        """扫出各资源包界面文件的 namespace，只做正则，不解析。

        一个 namespace 可能横跨多个文件（`common` 就在 `ui_common.json`
        等若干文件里都有），所以这里记的是列表而不是单个文件——
        只留最后一个的话，`common.inventory_screen_common` 会因为不在
        「最后那个文件」里而解析不出来。
        """
        if self._vanilla_ready:
            return
        self._vanilla_ready = True
        root = vanilla_ui_root()
        if root is None:
            return
        for pack in VANILLA_PACKS:
            directory = os.path.join(root, pack, "ui")
            if not os.path.isdir(directory):
                continue
            for name in sorted(os.listdir(directory)):
                if not name.endswith(".json"):
                    continue
                path = os.path.join(directory, name)
                try:
                    with io.open(path, "r", encoding="utf-8") as handle:
                        head = handle.read(4096)
                except (IOError, OSError, UnicodeDecodeError):
                    continue
                found = re.search(r'"namespace"\s*:\s*"([^"]+)"', head)
                if found:
                    # 靠后的包覆盖靠前的，与游戏内叠加次序一致。
                    self.vanilla_files.setdefault(found.group(1), []).append(path)

    def _load_vanilla_namespace(self, namespace):
        # type: (str) -> None
        if namespace in self._vanilla_loaded:
            return
        self._vanilla_loaded.add(namespace)
        self._index_vanilla()
        for path in self.vanilla_files.get(namespace, ()):
            try:
                payload = read_json(path)
            except ValueError:
                continue
            self.add_payload(path, payload, into=self.vanilla_controls)

    def namespace_of(self, path):
        # type: (str) -> str
        return self.file_namespaces.get(path)

    def lookup(self, ref, namespace=None):
        # type: (str, str) -> tuple
        """继承目标 -> `(文件, 原始键, 属性)`；查不到返回 None。

        继承引用有两种写法，**两种都真实存在**：

        - 带域名的 `common.root_panel`；
        - 不带域名的 `base_screen`，在**当前控件所在的那个域**里解析。
          `common.inventory_screen_common@base_screen` 就是后一种，
          按带域名去查会一路查到 `base_screen` 这个域上，然后落空。

        **先查本仓库**：资源包叠在原版之上，同名 namespace 以本仓库为准，
        所以本仓库的定义必须能压住原版。
        """
        candidates = [ref]
        if "." not in ref and namespace:
            candidates.append("%s.%s" % (namespace, ref))
        for candidate in candidates:
            found = self.controls.get(candidate)
            if found is not None:
                return found
            self._load_vanilla_namespace(candidate.rsplit(".", 1)[0])
            found = self.vanilla_controls.get(candidate)
            if found is not None:
                return found
        return None

    def is_vanilla(self, path):
        # type: (str) -> bool
        return not path.startswith(REPO)


def vanilla_ui_root():
    # type: () -> str
    """游戏目录下的 `data/resource_packs`，取自 `.mcdev.json`；找不到返回 None。

    路径是本机相关的，因此只当作可选增强：读不到就退化成「原版模板不解析」，
    本仓库的界面分析不受影响。
    """
    if not os.path.isfile(MCDEV):
        return None
    try:
        with io.open(MCDEV, "r", encoding="utf-8") as handle:
            config = json.load(handle)
    except (ValueError, IOError, OSError):
        return None
    executable = config.get("game_executable_path")
    if not executable:
        return None
    game_dir = os.path.dirname(executable.replace("/", os.sep))
    root = os.path.join(game_dir, "data", "resource_packs")
    return root if os.path.isdir(root) else None


# --------------------------------------------------------------------------
# 属性渲染
# --------------------------------------------------------------------------

def format_value(value):
    # type: (object) -> str
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(format_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = value if isinstance(value, str) else repr(value)
    if len(text) > MAX_VALUE_CHARS:
        text = text[:MAX_VALUE_CHARS - 3] + "..."
    return '"%s"' % text


def format_bindings(bindings):
    # type: (list) -> str
    """绑定数组压成一行：`#a->#b(always)`。

    `bindings` 是列表套字典，原样打印会占十几行，而真正要看的信息只有
    绑定名和触发条件。
    """
    parts = []
    for entry in bindings:
        if not isinstance(entry, dict):
            parts.append(format_value(entry))
            continue
        source = entry.get("binding_name", "?")
        target = entry.get("binding_name_override", source)
        text = source if source == target else "%s->%s" % (source, target)
        condition = entry.get("binding_condition")
        if condition and condition != "once":
            text += "(%s)" % condition
        binding_type = entry.get("binding_type")
        if binding_type:
            text += "{%s}" % binding_type
        parts.append(text)
    return "[" + " ".join(parts) + "]"


def control_type(props, ref, registry, namespace=None, depth=0):
    # type: (dict, str, Registry, str, int) -> str
    """控件的最终类型。自身没写 `type` 时顺着 `@` 继承链往上找。

    找不到就返回 `"?"`——**如实写问号，不猜**。原版模板在游戏目录不可读时
    会大量落到这个分支，这是正常结果而不是错误。

    `namespace` 是**当前控件所在文件**的域：继承链每往上一跳，下一跳要在
    定义处的域里解析（`@base_screen` 这种不带域名的写法靠它）。
    """
    own = props.get("type")
    if own:
        return own
    if not ref or depth >= 8:
        return "?"
    found = registry.lookup(ref, namespace)
    if found is None:
        return "?"
    path, key, target = found
    _name, target_ref = split_key(key)
    return control_type(target, target_ref, registry,
                        registry.namespace_of(path), depth + 1)


def select_keys(props, options):
    # type: (dict, object) -> list
    """按展示档位挑出要打印的属性键，顺序固定。

    `--layout` 是**追加**而不是替换：布局属性是补充信息，不是全部信息。
    """
    # `type` 不在这里：它已经在方括号里了，再打一遍只是噪声。
    keys = [key for key in PRIMARY_KEYS if key in props and key != "type"]
    if options.all:
        extra = sorted(
            key for key in props
            if key not in ("controls", "type") and key not in keys
            and not key.startswith(VARIABLE_PREFIX))
    elif options.layout:
        extra = [key for key in LAYOUT_KEYS if key in props and key not in keys]
    else:
        extra = []
    keys.extend(extra)
    # 变量放最后：它们是模板参数，读的时候是「这个控件能怎么配」，
    # 与前面的运行时属性不是一类。
    keys.extend(sorted(key for key in props if key.startswith(VARIABLE_PREFIX)))
    return keys


def render_props(props, options):
    # type: (dict, object) -> str
    chunks = []
    for key in select_keys(props, options):
        value = props[key]
        if key in BINDING_KEYS and isinstance(value, list):
            chunks.append("%s=%s" % (key, format_bindings(value)))
        else:
            chunks.append("%s=%s" % (key, format_value(value)))
    return " ".join(chunks)


# --------------------------------------------------------------------------
# 遍历
# --------------------------------------------------------------------------

def top_entries(payload):
    # type: (dict) -> list
    """顶层控件，按书写顺序。

    顶层比 `controls` 多出 `namespace` 之类的元数据，以及可能存在的
    `$` 顶层变量，因此不能直接复用 `child_entries`。
    """
    out = []
    for key, props in payload.items():
        if not is_control_key(key) or not isinstance(props, dict):
            continue
        out.append((key, props))
    return out


def walk(entries, path="", depth=0):
    # type: (list, str, int) -> list
    """把 `(键, 属性)` 递归摊平成 `(路径, 键, 属性, 深度)`，按书写顺序。

    **先摊平再打印**，而不是一边递归一边打印：`find` / `refs` 要的是平铺
    结果，只有 `tree` 需要缩进，两条路共用这一处遍历。递归走的是
    `child_entries` 而不是 `items()`——`controls` 是个列表，按 `items()`
    遍历会被当成非字典属性跳过，整棵树只剩顶层（这个 bug 真实发生过）。
    """
    out = []
    for key, props in entries:
        name, _ref = split_key(key)
        here = "%s/%s" % (path, name) if path else name
        out.append((here, key, props, depth))
        out.extend(walk(child_entries(props), here, depth + 1))
    return out


def find_control(payload, root_path):
    # type: (dict, str) -> tuple
    """按 `a/b/c` 路径找控件，返回 `(路径, 键, 属性)`；找不到返回 None。"""
    segments = [segment for segment in root_path.split("/") if segment]
    entries = top_entries(payload)
    path = ""
    found = None
    for segment in segments:
        found = None
        for key, props in entries:
            if split_key(key)[0] == segment:
                found = (key, props)
                break
        if found is None:
            return None
        path = "%s/%s" % (path, segment) if path else segment
        entries = child_entries(found[1])
    if found is None:
        return None
    return (path, found[0], found[1])


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------

def load_target(path):
    # type: (str) -> tuple
    """读目标文件，返回 `(绝对路径, payload)`。"""
    full = path if os.path.isabs(path) else os.path.join(REPO, path)
    if not os.path.isfile(full):
        print("找不到文件：%s" % path)
        return None, None
    try:
        return full, read_json(full)
    except ValueError as error:
        print("%s 不是合法 JSON：%s" % (relative(full), error))
        return None, None


def print_header(full, payload, registry, count):
    # type: (str, dict, Registry, int) -> None
    print("# %s  namespace=%s  控件 %d 个"
          % (relative(full), payload.get("namespace", "?"), count))
    tops = []
    for key, props in payload.items():
        if not is_control_key(key):
            continue
        if not isinstance(props, dict):
            print("# 顶层非控件项 %s = %s" % (key, format_value(props)))
            continue
        _name, ref = split_key(key)
        tops.append("%s[%s]" % (
            key, control_type(props, ref, registry, payload.get("namespace"))))
    if tops:
        print("# 顶层：%s" % "、".join(tops))
    print("")


def command_tree(args):
    # type: (object) -> int
    registry = Registry()
    registry.load_repo()
    full, payload = load_target(args.file)
    if payload is None:
        return 1

    flat = walk(top_entries(payload))
    print_header(full, payload, registry, len(flat))

    if args.root:
        target = find_control(payload, args.root)
        if target is None:
            print("找不到控件路径 %s" % args.root)
            print("顶层可选：%s" % "、".join(
                split_key(k)[0] for k in payload if is_control_key(k)))
            return 1
        entries = [(target[0], target[1], target[2], 0)]
        entries.extend(walk(child_entries(target[2]), target[0], 1))
    else:
        entries = flat

    for _path, key, props, depth in entries:
        if args.depth and depth > args.depth:
            continue
        name, ref = split_key(key)
        label = key if ref is None else "%s@%s" % (name, ref)
        rendered = render_props(props, args)
        line = "%s%s [%s]" % (
            "  " * depth, label,
            control_type(props, ref, registry, payload.get("namespace")))
        if rendered:
            line += "  " + rendered
        # 深度截断是被藏起来的信息，标一下，免得看的人以为这控件没有子控件。
        if args.depth and depth == args.depth and child_entries(props):
            line += "  …(还有子控件，用 --depth 展开)"
        print(line)
    return 0


def iter_all_files():
    # type: () -> list
    return [os.path.join(RP_UI, name) for name in sorted(os.listdir(RP_UI))
            if name.endswith(".json")]


def matched_props(props, pattern):
    # type: (dict, object) -> list
    """控件里命中正则的属性，返回 `["键=值"]`。

    **属性名也要参与匹配**：`find collection_index` 想找的是「哪些控件带这个
    属性」，只看属性值的话一个都命中不了——而属性名往往正是要找的东西。
    """
    hits = []
    for key, value in props.items():
        if key == "controls":
            continue
        text = value if isinstance(value, str) else json.dumps(
            value, ensure_ascii=False)
        if pattern.search(key) or (isinstance(text, str) and pattern.search(text)):
            hits.append("%s=%s" % (key, format_value(value)))
    return hits


def command_find(args):
    # type: (object) -> int
    pattern = re.compile(args.pattern)
    registry = Registry()
    registry.load_repo()
    files = iter_all_files()
    if args.file:
        full, _payload = load_target(args.file)
        if full is None:
            return 1
        files = [full]

    total = 0
    for path in files:
        try:
            payload = read_json(path)
        except ValueError:
            continue
        for control_path, key, props, _depth in walk(top_entries(payload)):
            name, ref = split_key(key)
            ctype = control_type(props, ref, registry, payload.get("namespace"))
            # 名字、继承目标、类型命中就算；再退一步看属性值。
            by_identity = (pattern.search(name) or (ref and pattern.search(ref))
                           or pattern.search(ctype))
            hits = [] if by_identity else matched_props(props, pattern)
            if not by_identity and not hits:
                continue
            total += 1
            if args.file:
                where = ""
            else:
                where = relative(path) + ":"
            print("%s%s  [%s]" % (where, control_path, ctype))
            if hits:
                for hit in hits[:4]:
                    print("      %s" % hit)
    print("")
    print("命中 %d 个控件。" % total)
    return 0


def collect_strings(payload):
    # type: (dict) -> list
    """全文件扫字符串属性，返回 `(路径, 键, 值)`。"""
    out = []
    for path, key, props, _depth in walk(top_entries(payload)):
        for prop_key, value in props.items():
            if prop_key == "controls" or not isinstance(value, str):
                continue
            out.append((path, prop_key, value))
    return out


def command_refs(args):
    # type: (object) -> int
    registry = Registry()
    registry.load_repo()
    full, payload = load_target(args.file)
    if payload is None:
        return 1

    entries = walk(top_entries(payload))
    print("# %s  namespace=%s" % (relative(full), payload.get("namespace", "?")))
    print("")

    print("== 继承引用（定义位置）==")
    namespace = payload.get("namespace")
    seen = {}
    for path, key, props, _depth in entries:
        _name, ref = split_key(key)
        if ref and ref not in seen:
            seen[ref] = path
    def ref_order(ref):
        # 本仓库的定义排前面——那是要对接的东西；原版模板只是背景知识。
        found = registry.lookup(ref, namespace)
        return (1 if (found and registry.is_vanilla(found[0])) else 0, ref)

    for ref in sorted(seen, key=ref_order):
        found = registry.lookup(ref, namespace)
        if found is None:
            location = "未找到定义（原版模板且游戏目录不可读？）"
        else:
            location = relative(found[0])
            if registry.is_vanilla(found[0]):
                location += "（原版）"
        found_props = found[2] if found else {}
        found_key = found[1] if found else ref
        _fname, found_ref = split_key(found_key)
        print("  %-52s %s  [%s]" % (
            ref, location,
            control_type(found_props, found_ref, registry,
                         registry.namespace_of(found[0]) if found else namespace)))
    print("")

    print("== 纹理 ==")
    textures = sorted(set(v for _p, _k, v in collect_strings(payload)
                          if v.startswith(TEXTURE_PREFIX)))
    for texture in textures:
        print("  %s" % texture)
    if not textures:
        print("  （无）")
    print("")

    print("== 本地化键 ==")
    lang = sorted(set(v for _p, _k, v in collect_strings(payload)
                      if LANG_KEY_RE.match(v)))
    for key in lang:
        print("  %s" % key)
    if not lang:
        print("  （无）")
    print("")

    print("== 按钮／事件名 ==")
    buttons = []
    for path, _key, props, _depth in entries:
        for button_key in BUTTON_KEYS:
            if button_key in props:
                buttons.append((path, button_key, props[button_key]))
    for path, button_key, value in buttons:
        print("  %-40s %s = %s" % (path, button_key, format_value(value)))
    if not buttons:
        print("  （无）")
    print("")

    print("== 绑定名 ==")
    bindings = []
    for path, _key, props, _depth in entries:
        for entry in props.get("bindings") or []:
            if isinstance(entry, dict) and entry.get("binding_name"):
                bindings.append((path, entry.get("binding_name"),
                                 entry.get("binding_type", "property")))
    for path, name, kind in bindings:
        print("  %-40s %s  {%s}" % (path, name, kind))
    if not bindings:
        print("  （无）")
    print("")

    print("== 集合与槽位 ==")
    slots = []
    for path, _key, props, _depth in entries:
        if any(k in props for k in ("collection_name", "collection_index",
                                    "grid_dimensions", "grid_item_template")):
            slots.append((path, props))
    for path, props in slots:
        detail = []
        for probe in ("collection_name", "collection_index", "grid_dimensions",
                      "grid_item_template", "maximum_grid_items"):
            if probe in props:
                detail.append("%s=%s" % (probe, format_value(props[probe])))
        print("  %-40s %s" % (path, " ".join(detail)))
    if not slots:
        print("  （无）")
    return 0


def main(argv):
    # type: (list) -> int
    use_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="JSON UI 速读工具：打印控件树、查找模板定义、汇总对外引用。")
    sub = parser.add_subparsers(dest="command")

    tree = sub.add_parser("tree", help="打印控件树（默认已过滤次要属性）")
    tree.add_argument("file", help="界面 JSON 路径，相对于仓库根")
    tree.add_argument("--root", help="从该控件开始，形如 content/root_panel")
    tree.add_argument("--depth", type=int, default=0, help="展开层数，0 为不限")
    tree.add_argument("--layout", action="store_true",
                      help="附带 size/offset/anchor/layer 等布局属性")
    tree.add_argument("--all", action="store_true", help="打印全部属性")
    tree.set_defaults(func=command_tree)

    find = sub.add_parser("find", help="跨全部界面文件按正则查找控件或模板")
    find.add_argument("pattern", help="正则；匹配控件名、继承目标、类型或属性值")
    find.add_argument("--file", help="只查这个文件")
    find.set_defaults(func=command_find)

    refs = sub.add_parser("refs", help="汇总一个界面对外的引用")
    refs.add_argument("file", help="界面 JSON 路径，相对于仓库根")
    refs.set_defaults(func=command_refs)

    if not argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
