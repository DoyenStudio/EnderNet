# coding=utf-8
"""验证 py2check 门禁本身有效。

门禁工具最大的风险是「永远不会报错」——那它就没有任何保护作用。
这里对每种被禁语法都喂一段反例，断言确实被拦下；再喂一段合规代码，断言放行。
"""

import unittest

import py2check


def codes(source):
    # type: (str) -> list
    return [v[2] for v in py2check.check_source(source, "<test>")]


class TestPy2CheckRejects(unittest.TestCase):
    """每种 py3 专有语法都必须被拦下。"""

    def assert_rejects(self, source, code):
        found = codes(source)
        self.assertIn(code, found, "未拦下: %r -> %r" % (source, found))

    def test_fstring(self):
        self.assert_rejects("x = f'v={1}'\n", "PY2001")

    def test_nonlocal(self):
        self.assert_rejects(
            "def a():\n    y = 1\n    def b():\n        nonlocal y\n        y = 2\n",
            "PY2002",
        )

    def test_async_def(self):
        self.assert_rejects("async def a():\n    pass\n", "PY2003")

    def test_variable_annotation(self):
        self.assert_rejects("x: int = 1\n", "PY2004")

    def test_yield_from(self):
        self.assert_rejects(
            "def a():\n    yield from range(3)\n",
            "PY2005",
        )

    def test_matmul(self):
        self.assert_rejects("x = a @ b\n", "PY2006")

    def test_walrus(self):
        self.assert_rejects("if (n := 10) > 5:\n    pass\n", "PY2007")

    def test_raise_from(self):
        self.assert_rejects(
            "try:\n    pass\nexcept Exception as e:\n    raise ValueError('x') from e\n",
            "PY2009",
        )

    def test_dict_unpacking(self):
        self.assert_rejects("x = {**a, 'b': 1}\n", "PY2010")

    def test_starred_assignment(self):
        self.assert_rejects("a, *b = [1, 2, 3]\n", "PY2011")

    def test_return_annotation(self):
        self.assert_rejects("def a() -> int:\n    return 1\n", "PY2012")

    def test_arg_annotation(self):
        self.assert_rejects("def a(x: int):\n    return x\n", "PY2013")

    def test_kwonly_args(self):
        self.assert_rejects("def a(*, x):\n    return x\n", "PY2014")

    def test_bare_super(self):
        self.assert_rejects(
            "class A(object):\n    def f(self):\n        super().f()\n", "PY2018"
        )

    def test_any_future_import_is_flagged(self):
        """项目约定不使用 __future__；三种写法都要被拦下。"""
        for source in (
            "from __future__ import division\n",
            "from __future__ import absolute_import\n",
            "from __future__ import print_function\n",
            "from __future__ import unicode_literals\n",
        ):
            self.assert_rejects(source, "PY2019")

    def test_multi_arg_print_is_flagged(self):
        """没有 print_function 时，py2 的 print(a, b) 打印的是元组。"""
        self.assert_rejects("print('failed', e)\n", "PY2021")

    def test_print_with_keywords_is_flagged(self):
        """py2 无 print_function 时，print 带关键字参数是语法错误。"""
        self.assert_rejects("print('x', file=f)\n", "PY2021")

    def test_unprotected_division_is_flagged(self):
        """没有 division 时，py2 的 a / b 对整数是整除。"""
        self.assert_rejects("x = a / b\n", "PY2022")
        self.assert_rejects("x = (a + 1) / (b - 1)\n", "PY2022")

    def test_syntax_error_is_reported(self):
        found = [v[2] for v in py2check.check_source("def (:\n", "<test>")]
        self.assertEqual(found, ["PY2999"])


class TestPy2CheckAccepts(unittest.TestCase):
    """合规的 py2.7 常见写法必须放行。"""

    def assert_clean(self, source):
        found = codes(source)
        self.assertEqual(found, [], "误报: %r" % (found,))

    def test_plain_module(self):
        self.assert_clean("x = 1\ny = x // 2\n")

    def test_type_comment_style(self):
        self.assert_clean(
            "def a(x):\n    # type: (int) -> int\n    return x\n"
        )

    def test_super_with_args(self):
        self.assert_clean(
            "class A(object):\n"
            "    def f(self):\n"
            "        pass\n"
            "class B(A):\n"
            "    def f(self):\n"
            "        super(B, self).f()\n"
        )

    def test_dict_and_set_comprehensions(self):
        self.assert_clean("d = {k: v for k, v in items}\ns = {i for i in items}\n")

    def test_single_star_and_double_star_call(self):
        self.assert_clean("f(*args)\nf(**kwargs)\n")

    def test_except_as_and_single_arg_print(self):
        self.assert_clean(
            "try:\n"
            "    pass\n"
            "except ValueError as e:\n"
            "    print('failed: %s' % e)\n"
        )

    def test_slots_and_class_attributes(self):
        self.assert_clean(
            "class A(object):\n"
            "    __slots__ = ('a', 'b')\n"
            "    name = 'x'\n"
        )

    def test_guarded_division_is_accepted(self):
        """让一边明显是浮点，或显式用 //，都必须放行。"""
        for source in (
            "x = a // b\n",
            "x = float(a) / b\n",
            "x = a / float(b)\n",
            "x = 1.0 * a / b\n",
            "x = Fraction(a, b)\n",
            "from fractions import Fraction\nx = Fraction(a) / b\n",
            "x = -1.0 / b\n",
        ):
            self.assert_clean(source)

    def test_no_future_import_is_accepted(self):
        """不写 __future__ 是现在的常规写法，必须放行。"""
        self.assert_clean("import os\n\n\ndef f():\n    return 1\n")


if __name__ == "__main__":
    unittest.main()
