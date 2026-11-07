"""Pure fixture verdict regressions; no GUI, model, or desktop inputs."""

import unittest

from scripts.model_smoke import completion_passed


class FixtureVerdictTests(unittest.TestCase):
    def setUp(self):
        self.evidence = {"fixture_assertions_passed": True, "model_task_actions_observed": True}
        self.styles = [{"transparent": True, "noactivate": True, "layered": True, "toolwindow": True}] * 2

    def test_model_stop_after_proven_completion_passes(self):
        self.assertTrue(completion_passed({"result": "Stopped"}, self.evidence, 3, self.styles))

    def test_premature_model_stop_fails(self):
        self.assertFalse(completion_passed({"result": "Stopped"},
            {"fixture_assertions_passed": False, "model_task_actions_observed": False}, 1, self.styles))

    def test_error_or_local_cancellation_cannot_pass_matching_state(self):
        for failure in ({"error": "Input failed"}, {"cancelled": True}):
            with self.subTest(failure=failure):
                self.assertFalse(completion_passed({"result": "Stopped", **failure},
                                                   self.evidence, 3, self.styles))


if __name__ == "__main__":
    unittest.main()
