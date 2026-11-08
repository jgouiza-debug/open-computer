"""Pure generated-pin provenance checks; no binaries, builds, network, or UI."""

import importlib.util
from pathlib import Path
import unittest


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("native_build_support_tests", HERE / "build_support.py")
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


class GeneratedNativePinTests(unittest.TestCase):
    expected = "a" * 64

    @staticmethod
    def code(source):
        # Compile only. Neither the pin module nor any generated instruction runs.
        return compile(source, "opencomputer/_native_pin.py", "exec")

    def test_matching_single_literal_assignment_is_accepted(self):
        result = support.check_native_pin_code(
            self.code(f"NATIVE_SHA256 = {self.expected!r}\n"), self.expected)
        self.assertIsNone(result)

    def test_other_literal_digest_is_rejected(self):
        with self.assertRaises(ValueError):
            support.check_native_pin_code(self.code(f"NATIVE_SHA256 = {'b' * 64!r}\n"), self.expected)

    def test_non_sha_literals_and_missing_assignment_are_rejected(self):
        for source in ("pass\n", "NATIVE_SHA256 = None\n", "NATIVE_SHA256 = 123\n",
                       f"NATIVE_SHA256 = {'a' * 63!r}\n", f"NATIVE_SHA256 = {'A' * 64!r}\n"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                support.check_native_pin_code(self.code(source), self.expected)

    def test_dynamic_equivalent_expression_is_rejected_without_execution(self):
        for source in (f"NATIVE_SHA256 = str({self.expected!r})\n",
                       f"NATIVE_SHA256 = ''.join([{self.expected!r}])\n"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                support.check_native_pin_code(self.code(source), self.expected)

    def test_extra_import_assignment_or_function_is_rejected(self):
        literal = f"NATIVE_SHA256 = {self.expected!r}\n"
        for source in (literal + "OTHER = 'unreviewed'\n", literal + literal,
                       "import os\n" + literal, literal + "def unused():\n    return 1\n"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                support.check_native_pin_code(self.code(source), self.expected)


if __name__ == "__main__":
    unittest.main()
