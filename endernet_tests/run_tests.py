# coding=utf-8
"""EnderNet 离线验证入口。

    python endernet_tests/run_tests.py            # 语法门禁 + 全部测试
    python endernet_tests/run_tests.py --no-lint  # 只跑测试
    python endernet_tests/run_tests.py -v         # 详细输出

不启动游戏。游戏内行为（tick 频率、事件语义、方块实体数据）无法在此验证，
见交付说明中的「未验证项」。
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

import py2check  # noqa: E402  (必须在 sys.path 设置之后导入)


def run_lint():
    # type: () -> bool
    paths = py2check.iter_source_files()
    if not paths:
        print("py2check: 源码目录尚未建立，跳过")
        return True
    violations = py2check.check_paths(paths)
    if not violations:
        print("py2check: %d 个文件全部通过 Python 2.7 语法门禁" % len(paths))
        return True
    for filename, line, code, message in sorted(violations):
        print("%s:%s: %s: %s" % (py2check._relative(filename), line, code, message))
    print("py2check: %d 个文件中有 %d 处违规" % (len(paths), len(violations)))
    return False


def run_unittests(verbosity):
    # type: (int) -> bool
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=TESTS_DIR, top_level_dir=TESTS_DIR)
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)
    return result.wasSuccessful()


def main(argv):
    # type: (list) -> int
    py2check.use_utf8_stdout()
    do_lint = "--no-lint" not in argv
    verbosity = 2 if ("-v" in argv or "--verbose" in argv) else 1

    ok = True
    if do_lint:
        ok = run_lint() and ok
    ok = run_unittests(verbosity) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
