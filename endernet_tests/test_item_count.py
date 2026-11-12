# coding=utf-8
"""Terminal quantity boundaries and integer precision."""

import unittest

import testsupport  # noqa: F401
from endernet.client.ui.shared.item_count import format_item_count


class ItemCountTests(unittest.TestCase):
    def test_small_counts_and_single_item_exception(self):
        for count in range(1000):
            self.assertEqual(format_item_count(count), "" if count == 1 else str(count))
        self.assertEqual(format_item_count(1, hide_one=False), "1")

    def test_unit_boundaries_truncate_without_rounding(self):
        for power, suffix in enumerate(("k", "M", "G", "T", "P", "E"), 1):
            scale = 1000 ** power
            self.assertEqual(format_item_count(scale), "1.0" + suffix)
            self.assertEqual(format_item_count(scale + scale // 5), "1.2" + suffix)
            self.assertEqual(format_item_count(scale + 3 * scale // 10 - 1), "1.2" + suffix)
            self.assertEqual(format_item_count(2 * scale - 1), "1.9" + suffix)
            self.assertEqual(format_item_count(1000 * scale - 1), "999.9" + suffix)
        self.assertEqual(format_item_count(2 ** 63 - 1), "9.2E")
