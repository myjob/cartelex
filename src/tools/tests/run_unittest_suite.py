import inspect
import os
import sys
import unittest


def _extract_test_location(test: unittest.TestCase) -> tuple[str, int]:
    """Return the absolute file path and line number of a test method.

    Parameters
    ----------
    test:
        The test case instance to inspect.

    Returns
    -------
    tuple[str, int]
        Absolute path to the source file and the line number of the test
        method, or (empty-string, 0) if the location cannot be determined.
    """
    try:
        method = getattr(type(test), test._testMethodName, None)
        if method is None:
            return "", 0
        source_file = inspect.getfile(method)
        source_lines, start_line = inspect.getsourcelines(method)
        return os.path.abspath(source_file), start_line
    except (TypeError, OSError):
        return "", 0


def _vscode_link(file_path: str, line: int) -> str:
    """Format a VSCode-clickable terminal link.

    Parameters
    ----------
    file_path:
        Absolute path to the file.
    line:
        Line number (1-based).

    Returns
    -------
    str
        A string in the form ``file_path:line`` that VSCode renders as a
        clickable link in the integrated terminal.
    """
    if not file_path:
        return "<unknown location>"
    return f"{file_path}:{line}"


class _VsCodeReportResult(unittest.TextTestResult):
    """TextTestResult subclass that appends a VSCode-clickable failure report."""

    def _print_vscode_report(self) -> None:
        """Print a summary of failures and errors with clickable VSCode links."""
        problems: list[tuple[str, unittest.TestCase, str]] = [
            ("FAIL", test, tb) for test, tb in self.failures
        ] + [
            ("ERROR", test, tb) for test, tb in self.errors
        ]

        if not problems:
            return

        self.stream.writeln()
        self.stream.writeln("=" * 70)
        self.stream.writeln("FAILED TESTS — clickable locations")
        self.stream.writeln("=" * 70)

        for label, test, tb in problems:
            file_path, line = _extract_test_location(test)
            # Fall back to parsing the traceback when inspect fails.
            if not file_path:
                for tb_line in reversed(tb.splitlines()):
                    tb_line = tb_line.strip()
                    if tb_line.startswith('File "') and ', line ' in tb_line:
                        parts = tb_line.split('"')
                        if len(parts) >= 3:
                            file_path = parts[1]
                            try:
                                line = int(tb_line.split(", line ")[1].split(",")[0])
                            except (IndexError, ValueError):
                                line = 0
                        break

            link = _vscode_link(file_path, line)
            self.stream.writeln(f"  [{label}] {test.id()}")
            self.stream.writeln(f"         {link}")

        self.stream.writeln()


class _VsCodeReportRunner(unittest.TextTestRunner):
    """TextTestRunner that uses _VsCodeReportResult for enriched output."""

    resultclass = _VsCodeReportResult

    def run(self, test: unittest.TestSuite | unittest.TestCase) -> unittest.TestResult:
        """Run the suite and append the VSCode clickable report.

        Parameters
        ----------
        test:
            The test suite or case to run.

        Returns
        -------
        unittest.TestResult
            The result object after the run completes.
        """
        result = super().run(test)
        result._print_vscode_report()  # type: ignore[attr-defined]
        return result


def suite() -> unittest.TestSuite:
    """Build and return the master test suite.

    Discovers all test files matching ``test_*.py`` under ``src/`` and the
    optional top-level ``tests/`` directory.

    Returns
    -------
    unittest.TestSuite
        The aggregated suite ready to be passed to a test runner.
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
    master_suite.addTest(src_suite)

    root_tests_path = os.path.join(project_root, "tests")
    if os.path.exists(root_tests_path):
        root_tests_suite = loader.discover(
            start_dir=root_tests_path, pattern="test_*.py", top_level_dir=project_root
        )
        master_suite.addTest(root_tests_suite)

    return master_suite


if __name__ == "__main__":
    runner = _VsCodeReportRunner(verbosity=2)
    result = runner.run(suite())
    sys.exit(not result.wasSuccessful())


"""
----------------------------------------------------------------------
Ran 116 tests in 619.916s

FAILED (failures=12, errors=7)

======================================================================
FAILED TESTS — clickable locations
======================================================================
  [FAIL] src.agents.tests.test_fact_type_agent.TestFactTypeAgent.test_select_cartel_period_end
         ~/a/celex-facts/src/agents/tests/test_fact_type_agent.py:28
  [FAIL] src.agents.tests.test_supervisor_agent.TestSupervisorAgentUserQA.test_fact_retrieval_flow
         ~/a/celex-facts/src/agents/tests/test_supervisor_agent.py:43
  [FAIL] src.agents.tests.test_supervisor_agent.TestSupervisorAgentUserQA.test_no_context_available
         ~/a/celex-facts/src/agents/tests/test_supervisor_agent.py:103
  [FAIL] src.agents.tests.test_supervisor_agent.TestSupervisorAgentUserQA.test_no_context_available_empty_list
         ~/a/celex-facts/src/agents/tests/test_supervisor_agent.py:120
  [FAIL] src.agents.tests.test_supervisor_agent.TestSupervisorAgentUserQA.test_semantic_fallback_fact_na
         ~/a/celex-facts/src/agents/tests/test_supervisor_agent.py:83
  [FAIL] src.agents.tests.test_supervisor_agent.TestSupervisorAgentUserQA.test_semantic_fallback_rag_na
         ~/a/celex-facts/src/agents/tests/test_supervisor_agent.py:63
  [FAIL] src.agents.tests.test_supervisor_agent_integration.TestSupervisorAgentIntegration.test_celex_31975D0358_flow_2
         /usr/lib/python3.12/unittest/mock.py:52
  [FAIL] src.agents.tests.test_supervisor_agent_interactive.TestSupervisorAgentInteractive.test_interactive_input_flow
         /usr/lib/python3.12/unittest/mock.py:9
  [FAIL] src.agents.tests.test_supervisor_agent_interactive.TestSupervisorAgentInteractive.test_interactive_input_used
         /usr/lib/python3.12/unittest/mock.py:30


"""
