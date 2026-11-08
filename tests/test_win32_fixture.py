"""Pure Win32 fixture guard tests. No native window or input is created."""

from contextlib import redirect_stdout, redirect_stderr
import io
import json
import unittest
from unittest.mock import patch

from scripts import win32_semantic_fixture as fixture


class Win32SemanticFixtureTests(unittest.TestCase):
    def controls(self, with_password=False):
        return [{**control, "hwnd": index + 100} for index, control in
                enumerate(fixture.control_specs(with_password))]

    def test_no_arguments_and_help_never_construct_native_fixture(self):
        with patch.object(fixture, "Win32Fixture") as native, redirect_stdout(io.StringIO()):
            self.assertEqual(fixture.main([]), 0)
            with self.assertRaises(SystemExit) as help_exit:
                fixture.main(["--help"])
        self.assertEqual(help_exit.exception.code, 0)
        native.assert_not_called()

    def test_metadata_binds_identity_bounds_and_omits_edit_values(self):
        controls = self.controls(True)
        controls[-1]["value"] = fixture.PASSWORD_SENTINEL
        metadata = fixture.fixture_metadata(123, 456, controls)
        self.assertEqual(metadata["fixture_pid"], 123)
        self.assertEqual(metadata["window_hwnd"], 456)
        self.assertEqual(metadata["controls"][1]["bounds"], controls[1]["bounds"])
        self.assertNotIn(fixture.PASSWORD_SENTINEL, json.dumps(metadata))
        self.assertNotIn("label", metadata["controls"][-1])
        self.assertTrue(metadata["controls"][-1]["password"])
        self.assertTrue(metadata["live_identity_and_geometry_must_be_rechecked"])

    def test_invalid_identity_duplicate_ids_array_limit_and_outside_bounds_reject(self):
        with self.assertRaises(ValueError):
            fixture.fixture_metadata(0, 456, self.controls())
        with self.assertRaises(ValueError):
            fixture.fixture_metadata(123, 456, self.controls() + [self.controls()[0]])
        with self.assertRaises(ValueError):
            fixture.fixture_metadata(123, 456, self.controls() * fixture.MAX_CONTROLS)
        controls = self.controls()
        controls[0] = {**controls[0], "bounds": {"x": 719, "y": 0, "width": 2, "height": 1}}
        with self.assertRaises(ValueError):
            fixture.fixture_metadata(123, 456, controls)

    def test_unbounded_timeout_rejects_before_native_construction(self):
        with patch.object(fixture.sys, "platform", "win32"), patch.object(fixture, "Win32Fixture") as native, \
                redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                fixture.main(["--run", "--timeout", "601"])
        native.assert_not_called()


if __name__ == "__main__":
    unittest.main()
