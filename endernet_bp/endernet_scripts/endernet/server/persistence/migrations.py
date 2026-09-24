# coding=utf-8
"""存档版本迁移。

对应任务 12-01/12-02。三条纪律：

1. **每份权威数据都有自己的 `schema_version`**，不共用一个大版本号。
2. **老版本按登记过的迁移步骤逐级升上来**；没有登记步骤的版本差直接报错，
   不做「尽力而为」的猜测。
3. **未知的新版本必须保留而不是当作空数据。** 这是验收清单里单列的一条：
   玩家用新版本存过档之后回退到旧版本，旧版本不得把看不懂的存档
   当成「没有库存」并覆盖掉——那会直接销毁玩家的物品。
"""

#: 本构建能写出的最高版本。写入时使用它。
CURRENT_SCHEMA_VERSION = 1


class UnsupportedVersionError(Exception):
    """版本高于本构建所能理解的范围。

    调用方收到它必须**保留原数据并暂停该记录的写入**，
    绝不允许把它当作空值继续。
    """

    def __init__(self, kind, version, supported):
        self.kind = kind
        self.version = version
        self.supported = supported
        Exception.__init__(
            self,
            "记录类型 %s 的存档版本为 %s，高于本构建支持的 %s；"
            "保留原数据并停止写入，不得当作空值覆盖" % (kind, version, supported),
        )


class MissingMigrationError(Exception):
    """缺少从某版本到下一版本的迁移步骤。"""

    def __init__(self, kind, from_version, to_version):
        self.kind = kind
        self.from_version = from_version
        self.to_version = to_version
        Exception.__init__(
            self,
            "记录类型 %s 缺少 %s -> %s 的迁移步骤" % (kind, from_version, to_version),
        )


class Migration(object):
    """一步迁移：把某个记录类型的载荷从 `from_version` 升到 `to_version`。

    `handler` 接收并返回纯数据（dict），不得触碰存档后端。
    """

    __slots__ = ("kind", "from_version", "to_version", "handler", "note")

    def __init__(self, kind, from_version, to_version, handler, note=""):
        # type: (str, int, int, object, str) -> None
        if to_version != from_version + 1:
            raise ValueError(
                "迁移步骤必须逐级递增: %s -> %s" % (from_version, to_version)
            )
        self.kind = kind
        self.from_version = from_version
        self.to_version = to_version
        self.handler = handler
        self.note = note


class Migrator(object):
    """按记录类型登记并执行迁移。"""

    def __init__(self, target_version=CURRENT_SCHEMA_VERSION):
        # type: (int) -> None
        self.target_version = int(target_version)
        self._steps = {}
        #: 迁移执行次数，供诊断使用。
        self.migrated_count = 0

    def register(self, kind, from_version, handler, note=""):
        # type: (str, int, object, str) -> Migration
        """登记一步迁移。`from_version` 升到 `from_version + 1`。"""
        step = Migration(kind, from_version, from_version + 1, handler, note)
        bucket = self._steps.setdefault(kind, {})
        if from_version in bucket:
            raise ValueError(
                "记录类型 %s 的 %s 版本已登记过迁移步骤" % (kind, from_version)
            )
        bucket[from_version] = step
        return step

    def supported_versions(self, kind):
        # type: (str) -> tuple
        """该记录类型在本构建下可直接读取的版本集合。"""
        versions = [self.target_version]
        version = self.target_version
        bucket = self._steps.get(kind, {})
        while (version - 1) in bucket:
            version -= 1
            versions.append(version)
        return tuple(sorted(versions))

    def needs_migration(self, kind, version):
        # type: (str, int) -> bool
        return int(version) != self.target_version

    def migrate(self, kind, version, payload):
        # type: (str, int, dict) -> tuple
        """把载荷升到目标版本。

        返回 `(new_version, payload)`；记录本身与其版本一并由调用方写回。

        - 版本更高：抛 `UnsupportedVersionError`（保留，不覆盖）。
        - 缺少中间步骤：抛 `MissingMigrationError`。
        """
        version = int(version)
        if version > self.target_version:
            raise UnsupportedVersionError(kind, version, self.target_version)
        if version == self.target_version:
            return (version, payload)

        bucket = self._steps.get(kind, {})
        current = payload
        while version < self.target_version:
            step = bucket.get(version)
            if step is None:
                raise MissingMigrationError(kind, version, self.target_version)
            current = step.handler(current)
            if current is None:
                raise ValueError(
                    "记录类型 %s 的 %s 迁移步骤返回了 None；"
                    "迁移必须返回载荷，不能悄悄丢数据" % (kind, version)
                )
            version = step.to_version
            self.migrated_count += 1
        return (version, current)

    def diagnostics(self):
        # type: () -> dict
        return {
            "target_version": self.target_version,
            "kinds": sorted(self._steps.keys()),
            "migrated_count": self.migrated_count,
        }
