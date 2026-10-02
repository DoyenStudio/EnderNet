# coding=utf-8
"""槽位之间的飞行动画：把 `content.flying_item` 当一个会动的图标用。

**为什么是脚本逐帧推，而不是引擎的 UI 属性动画。** 引擎那套属性动画
（`RegisterUIAnimations` / `SetAnimation`）要在数据里写死 `from` / `to`，
而这里每一次搬运的起终点都不一样——源槽与目标槽都随玩家点的那一下变。
逐帧推才能每次现算，缓动也才能跟着由脚本给（取**先快后慢**）。

**时长按秒算，不按帧算。** 界面渲染帧率与脚本 tick、模拟 tick 都不是
一回事（CLAUDE.md 记了这条），按帧数计会让同一段时长在低帧率下拖长一截。
时钟是**注入的**：离线用例给一个假时钟就能精确推进，不必睡真实时间，
也不必假设帧率。

**它只是表现。** 飞行动画不参与任何判定，也不表示服务端接受了那条命令；
画出来的东西按**资源身份**取（`name` + `aux`），与槽位里画的是同一个图标。
"""

import time

from . import slot_layout

#: 物品图标画多大（像素）。**几何计算的单一事实来源。**
#:
#: 槽位面板是 18×18，而槽里那张图标是 16×16、**在槽位里居中**——两份槽位
#: 模板都是这么声明的：`endernet_container_common.custom_slot` 的
#: `item_renderer` 明写 `size: [16, 16]` 加 `anchor: center`；
#: `endernet_windows.item_can_be_selected` 写 `100%+-2px`（18 − 2 = 16），
#: 不写 anchor 即默认居中。两者都见 `plans/technical_notes/custom_slot.md`
#: 第 1 节「槽位大小为 18×18，物品渲染区域为 16×16」。
#:
#: 因此**四边各内缩 `(18 - 16) / 2 = 1` 像素**。飞行动画要让图标落在
#: 「槽里那一张本来在的位置」，就得算这个 1。
#:
#: **它是常量，不从引擎读。** 这个 `custom` 控件的 `GetSize()` 回的是 0
#: （2026-10-01 游戏内实测），拿它去算居中会让图标整整偏出半个槽位——
#: 见 `_corner` 的说明。
ICON_SIZE = 16.0

#: 一次飞行持续多久（秒）。**这是手感值，改它不影响任何判定。**
#:
#: 初值是 0.5，2026-10-01 游戏内实测后按 User 的要求**调快 4 倍**（0.5 / 4）。
#: 记下这两个数是因为它只由观感决定：快了显得仓促、慢了拖泥带水，而两者都
#: 不会让任何用例变红——只能看。
FLY_SECONDS = 0.125

#: 缓动的幂次：`eased = 1 - (1 - t) ** EASE_POWER`。
#:
#: 2 即**先快后慢**（二次缓出）：起步速度最大，临近终点速度趋近于零。
#: 用幂函数而不是线性，是因为线性位移看上去「两头都要停一下」，不像一件
#: 被扔过去的东西。调这个数就是调手感，不牵动任何判定。
EASE_POWER = 3


def _pair(value):
    # type: (object) -> object
    """把引擎给的坐标／尺寸收成两个浮点数；不是两元组就返回 `None`。

    **取值一律不假设。** 引擎在不同接口上给过整数、浮点、甚至长度不对的
    序列，而这里的每一处退化都只是「这一次不飞」，不该变成异常。
    """
    if value is None:
        return None
    try:
        first, second = value[0], value[1]
    except (TypeError, IndexError, KeyError):
        return None
    try:
        return (float(first), float(second))
    except (TypeError, ValueError):
        return None


def _rect(value):
    # type: (object) -> object
    """`(x, y, 宽, 高)`；形状不对返回 `None`。"""
    if value is None:
        return None
    try:
        return (float(value[0]), float(value[1]),
                float(value[2]), float(value[3]))
    except (TypeError, IndexError, KeyError, ValueError):
        return None


class FlyingItemAnimator(object):
    """把一件物品从 A 矩形飞到 B 矩形。

    **同一时刻只有一次飞行**：新的一次直接顶掉旧的那次。玩家连点两下时
    看到的是最后那一下的方向，而不是两次叠加——叠加需要同时持有多个控件，
    而 `content` 下只有一个 `flying_item`。

    **取不到控件就永久退化成「什么也不做」。** 本类是给整个
    `TerminalPanelBase` 家族用的，而 `content.flying_item` 只长在其中几个
    界面上；缺控件时 `fly()` 返回 `False` 且**不抛**——表现层的缺项不该
    让搬运本身失败。解析结果（成功或失败）都只算一次，之后不再重试，
    免得每个界面上都留一条每帧都要炸一遍的路径。
    """

    def __init__(self, screen, control_path, origin_path, now=None):
        # type: (object, str, str, object) -> None
        #: `screen` 是持有界面的那一侧（`TerminalPanelBase`），只要它能
        #: `GetBaseUIControl`。这里**不导入 tooldelta 的控件包装**：整条
        #: 动画路径都只用到 `BaseUIControl` 自己的方法。
        self.screen = screen
        self.control_path = control_path
        #: 坐标系原点控件。`SetPosition` 收的是**相对父节点**的坐标，而
        #: 起终点是全局矩形，因此要先减去这个控件的全局原点。
        self.origin_path = origin_path
        #: 时钟。注入是为了让离线用例能精确推进——游戏里就是 `time.time`。
        self._now = now or time.time
        self._resolved = False
        self._control = None
        self._origin = None
        self._active = False
        self._started = 0.0
        self._source = None
        self._target = None
        #: 计数只用于诊断：动画失败不该被吞掉不报，但也不该影响搬运。
        self.flights = 0
        self.errors = 0

    # -- 控件 ----------------------------------------------------------

    def _lookup(self):
        # type: () -> object
        """解析一次控件，成败都记下（见类文档）。

        **两条路径都交给 `slot_layout.control` 解析**，不自己拼字符串：四台
        终端面板有两种屏幕挂法（见 `slot_layout._candidates`），同一个控件
        在两个命名空间里。各解各的就会出现「图标找对了、原点按另一条候选算」，
        而那个错的症状是**图标整块偏移**，看起来像缓动写错了。
        """
        if self._resolved:
            return self._control
        self._resolved = True
        try:
            control = slot_layout.control(self.screen, self.control_path)
            origin = slot_layout.control(self.screen, self.origin_path)
        except Exception:
            self.errors += 1
            return None
        if control is None or origin is None:
            return None
        self._control = control
        self._origin = origin
        return control

    # -- 发起 ----------------------------------------------------------

    def fly(self, key, source, target):
        # type: (object, object, object) -> bool
        """从 `source` 飞到 `target`，两者都是**全局矩形** `(x, y, 宽, 高)`。

        `key` 是 `ResourceKey`——画的是**资源身份**，不是那一堆本身。附魔、
        耐久与 `userData` 带不过去：`SetUiItem` 只收名称与附加值，而槽位里
        那套 `#item_id_aux` 绑定的粒度与它相同（`plans/ui/
        common_terminal_panel.md` 记着「带损耗的工具会画成完好」是既有缺项）。
        这里沿用同一条口径，不新开一个更细的通道。

        任何一项取不到（控件缺席、矩形不是四元组、引擎拒绝）都返回 `False`
        且不抛：**宁可不动，也不要动错地方。**
        """
        rect_source = _rect(source)
        rect_target = _rect(target)
        if key is None or rect_source is None or rect_target is None:
            return False
        control = self._lookup()
        if control is None:
            return False
        try:
            origin = _pair(self._origin.GetGlobalPosition())
        except Exception:
            self.errors += 1
            return False
        if origin is None:
            return False
        self._source = self._corner(rect_source, origin)
        self._target = self._corner(rect_target, origin)
        try:
            control.asItemRenderer().SetUiItem(key.name, key.aux)
            control.SetPosition(self._source)
            control.SetVisible(True)
        except Exception:
            self.errors += 1
            return False
        # **位置在起点上先摆一次**，再开始计时：不摆的话第一帧渲染出来的是
        # 上一次飞行的终点（或 JSON 里的初始位置），看起来像闪了一下。
        self._started = self._now()
        self._active = True
        self.flights += 1
        return True

    @staticmethod
    def _corner(rect, origin):
        # type: (tuple, tuple) -> tuple
        """图标该摆在父节点坐标系里的哪儿：**让 `ICON_SIZE` 见方的图标落在
        这个矩形的正中**。

        这条规则把三种落点收成一句：

        - **槽位**（18×18）⇒ `+1`，正好是槽里那张 16×16 图标居中的位置。
          图标在槽里是居中的（见 `ICON_SIZE`），所以「槽位左上角」与
          「图标左上角」差 `(18 - 16) / 2 = 1` 像素。**2026-10-01 之前这里
          差的是 0**，理由是「两边同尺寸」——而两边从来不同尺寸（16 与 18），
          那句话是错的；用户当日定「按 16×16 权威算」之后改成现在这条。
        - **整块区域**（背包那一半、列表区）⇒ 落在区域中心，与文档说的
          「落到整块区域／列表区中心」一致。此前也是左上角，那是同一句话
          的另一种错。
        - **退化矩形**（点击处是 `(x, y, 0, 0)`）⇒ **原样用那个点**。它比的
          不是图标而是「玩家点的那一下在哪儿」；减去一半会把它挪到一个谁也
          没点过的位置上。比图标还小的矩形同理。

        **仍然不读 `GetSize()` 去算居中。** 早先这里是
        `rect - origin + (槽位尺寸 - 图标尺寸) / 2`，而图标尺寸取自这个
        `custom` 控件的 `GetSize()`——它回的是 **0**，于是那一项变成「加半个
        槽位」，游戏内实测**图标整整偏出半个物品（两轴各约 9px）**。现在用的
        是常量 `ICON_SIZE`，那个读数是 0 也不影响任何东西。
        """
        # 比图标小（含退化为点）就不动它——见上面第三条。
        dx = rect[2] - ICON_SIZE
        dy = rect[3] - ICON_SIZE
        if dx < 0.0:
            dx = 0.0
        if dy < 0.0:
            dy = 0.0
        return (rect[0] + dx / 2.0 - origin[0], rect[1] + dy / 2.0 - origin[1])

    # -- 推进 ----------------------------------------------------------

    def tick(self):
        # type: () -> None
        """推进一帧。没在飞时**第一次判断就返回**（每渲染帧都会被调用）。"""
        if not self._active:
            return
        control = self._control
        if control is None:
            self._active = False
            return
        elapsed = self._now() - self._started
        if elapsed < 0.0:
            elapsed = 0.0
        # `float(...)` 不是装饰：py2 下 `int / int` 是整除，而注入的时钟完全
        # 可能给整数（`FLY_SECONDS` 就是浮点，但那是巧合，不是保证）。
        progress = float(elapsed) / FLY_SECONDS
        if progress >= 1.0:
            self._finish()
            return
        eased = 1.0 - (1.0 - progress) ** EASE_POWER
        position = (
            self._source[0] + (self._target[0] - self._source[0]) * eased,
            self._source[1] + (self._target[1] - self._source[1]) * eased,
        )
        try:
            control.SetPosition(position)
        except Exception:
            self.errors += 1
            self._finish()

    def _finish(self):
        # type: () -> None
        """飞到终点：收起来。**不回到起点**——那会看起来像又飞了一次。"""
        self._active = False
        control = self._control
        if control is None:
            return
        try:
            control.SetVisible(False)
        except Exception:
            self.errors += 1

    def clear(self):
        # type: () -> None
        """收起正在飞的那一个。关界面／失活时调——下一次打开时它不该
        还挂在半空中。没在飞时什么也不做。"""
        if self._active:
            self._finish()

    def is_active(self):
        # type: () -> bool
        return self._active

    def diagnostics(self):
        # type: () -> dict
        return {
            "active": self._active,
            "flights": self.flights,
            "errors": self.errors,
        }
