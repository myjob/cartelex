"""Test suite that discovers and runs only the UI (Playwright/browser) tests.

Companion to ``run_unittest_suite_wo_usr.py``: that runner excludes
``UI_TEST_MODULES``, this one runs exclusively those modules.
"""

import os
import sys
import unittest

from run_unittest_suite_wo_usr import _VsCodeReportRunner, _filter_ui_tests


def suite() -> unittest.TestSuite:
    """Build and return the UI-only test suite.

    Discovers all test files matching ``test_*.py`` under ``src/`` and the
    optional top-level ``tests/`` directory, then keeps only tests whose
    module is listed in ``run_unittest_suite_wo_usr.UI_TEST_MODULES``.

    Returns
    -------
    unittest.TestSuite
        The filtered suite ready to be passed to a test runner.
    """
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, "../../../"))
    src_path = os.path.join(project_root, "src")

    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    if src_path not in sys.path:
        sys.path.insert(0, src_path)

    loader = unittest.TestLoader()
    master_suite = unittest.TestSuite()

    src_suite = loader.discover(start_dir=src_path, pattern="test_*.py", top_level_dir=project_root)
    master_suite.addTest(_filter_ui_tests(src_suite, keep_ui=True))

    root_tests_path = os.path.join(project_root, "tests")
    if os.path.exists(root_tests_path):
        root_tests_suite = loader.discover(
            start_dir=root_tests_path, pattern="test_*.py", top_level_dir=project_root
        )
        master_suite.addTest(_filter_ui_tests(root_tests_suite, keep_ui=True))

    return master_suite


if __name__ == "__main__":
    runner = _VsCodeReportRunner(verbosity=2)
    result = runner.run(suite())
    # sys.exit(not result.wasSuccessful())
