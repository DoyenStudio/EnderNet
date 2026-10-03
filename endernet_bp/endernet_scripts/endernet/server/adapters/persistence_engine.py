# coding=utf-8
"""真实存档后端：世界 `ExtraData`（任务 12-02 选定的后端）。

本模块是**引擎边界**，唯一允许 import `mod.*` / `tooldelta` 的地方之一，
而引擎访问一律写在**方法体内**：模块级的 engine 导入会让
`ServerBootstrap(bind_engine=False)` 与整份离线测试链一起失败
（`test_import_boundaries.py` 静态拦着，`world.py` 的写法是同一个理由）。

## 为什么后端在这里，而 `ExtraDataRepository` 在 `persistence/`

`server/persistence/` 属 `DOMAIN_PREFIXES`，**全树禁止引擎导入**。所以
「怎么和引擎说话」只能放这里，「记录键怎么映射、索引怎么维护、版本过新
怎么办」留在 `persistence/repository.py` —— 那些是脱机能验完的纯逻辑，
不该被引擎依赖拖成只能在游戏里跑。

## 选型依据（12-02 实测，`persistence_backend.md`）

- 世界 `ExtraData` 会落盘：标记串在 leveldb WAL 里直接 `grep` 到过（§3.1）。
- `SetExtraData(k, v, auto_save=False)` 只有 `SaveExtraData()` 能提交，
  **且提交是异步的**——返回 `True` 不等于已落盘（§3.3）。因此「已提交」与
  「已持久」之间有一个**不承诺精确一次**的窗口，由 12-05/12-06 承接。
- 每次保存约 39.8 KB 固定开销、与载荷几乎无关，**按调用次数付**（§3.2）。
  因此提交必须合并，`ExtraDataRepository.commit()` 是唯一的调用点。
"""


class ExtraDataBackend(object):
    """存档后端的引擎口子：四个原语。

    刻意做成**最小契约**：仓库层只需要知道「拿一个键、放一个键、
    删一个键、提交一次」，别的一概不要。这样离线测试用一个字典替身
    就能把仓库层整条跑通（替身见 `endernet_tests/testsupport.py`）。

    进出都是**字符串**：键是纯 ASCII 文本，值是 `ExtraDataRepository`
    编好的 JSON 字符串。后端不解析内容，也不做类型转换。
    """

    def get(self, key):
        # type: (str) -> object
        """读一个键。不存在返回 None（不是空串——两者必须分得开）。"""
        raise NotImplementedError

    def set(self, key, value, auto_save=True):
        # type: (str, str, bool) -> bool
        """写一个键。`auto_save=False` 表示只改内存区，等 `save()` 提交。"""
        raise NotImplementedError

    def delete(self, key):
        # type: (str) -> bool
        raise NotImplementedError

    def save(self):
        # type: () -> bool
        """提交。**这是唯一的提交点**；返回 True 也不代表已经落盘。"""
        raise NotImplementedError


class EngineExtraDataBackend(ExtraDataBackend):
    """`ExDataCompServer` 的世界级实现（`levelId` 当实体 ID 用）。

    组件**每次调用现取**，不缓存：缓存的组件对象在退出世界再进来、
    或热重载之后可能指向上一份世界数据，而那种错误不会报任何异常——
    它只是安静地读写到一份没人再看的存储上。调用的频次不高
    （恢复期按记录数、运行期每帧至多一次提交），不值得为此冒险。
    """

    def __init__(self):
        self.calls = {"get": 0, "set": 0, "delete": 0, "save": 0}

    def _component(self):
        # type: () -> object
        from mod.server.extraServerApi import GetEngineCompFactory, GetLevelId

        return GetEngineCompFactory().CreateExtraData(GetLevelId())

    def get(self, key):
        # type: (str) -> object
        self.calls["get"] += 1
        return self._component().GetExtraData(key)

    def set(self, key, value, auto_save=True):
        # type: (str, str, bool) -> bool
        self.calls["set"] += 1
        return bool(self._component().SetExtraData(key, value, auto_save))

    def delete(self, key):
        # type: (str) -> bool
        self.calls["delete"] += 1
        return bool(self._component().CleanExtraData(key))

    def save(self):
        # type: () -> bool
        self.calls["save"] += 1
        return bool(self._component().SaveExtraData())

    def diagnostics(self):
        # type: () -> dict
        report = dict(self.calls)
        report["backend"] = "engine_extra_data"
        return report
