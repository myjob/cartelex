
import unittest
from unittest.mock import MagicMock, patch
import sys
import os

# Ensure src is in path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))

import httpx
import litellm

from config.settings import settings
from batch.BatchJobRunner import (
    EXIT_CODE_LLM_TIMEOUTS,
    BatchJobRunner,
    batch_exit_code,
    is_llm_timeout,
)

class TestBatchJobRunner(unittest.TestCase):

    def setUp(self) -> None:
        """Keep the real DGX thermal gate out of every test in this class.

        Without ``--dry-run`` ``run()`` builds a live ``OllamaHealthGate``,
        which SSHes to the Spark and, when the probe fails under the strict
        gate, sleeps ``THERMAL_COOLDOWN_SECONDS`` up to
        ``THERMAL_MAX_COOLDOWN_CYCLES`` times per test. These tests exercise
        scheduling and exit codes, not the gate (see
        ``test_BatchJobRunner_thermal``), so the gate is stubbed out.
        """
        gate_patch = patch.object(BatchJobRunner, '_build_gate', return_value=None)
        gate_patch.start()
        self.addCleanup(gate_patch.stop)

    @patch('batch.BatchJobRunner.BatchJobRunner.load_celex_ids')
    def test_run_serial(self, mock_load):
        mock_load.return_value = ['ID1', 'ID2']
        
        runner = BatchJobRunner()
        process_mock = MagicMock(return_value=True)
        
        # Simulate args
        with patch.object(sys, 'argv', ['prog', '--dry-run']):
             # We need to catch SystemExit because run() calls sys.exit(0)
             with self.assertRaises(SystemExit) as cm:
                 runner.run(process_mock, context="ctx")
             
             self.assertEqual(cm.exception.code, 0)
             
        self.assertEqual(process_mock.call_count, 2)
        # Check calls. Note: order is guaranteed in serial
        process_mock.assert_any_call('ID1', 'ctx', True)
        process_mock.assert_any_call('ID2', 'ctx', True)

    @patch('batch.BatchJobRunner.BatchJobRunner.load_celex_ids')
    def test_run_concurrent(self, mock_load):
        # We need enough items to likely trigger concurrency if we were checking threads, 
        # but here we just check that all are processed.
        mock_load.return_value = ['ID1', 'ID2', 'ID3']
        
        runner = BatchJobRunner()
        process_mock = MagicMock(return_value=True)
        
        with patch.object(sys, 'argv', ['prog', '--max-workers', '2']):
             with self.assertRaises(SystemExit) as cm:
                 runner.run(process_mock)
             self.assertEqual(cm.exception.code, 0)
        
        self.assertEqual(process_mock.call_count, 3)

    @patch('batch.BatchJobRunner.BatchJobRunner.load_celex_ids')
    def test_limit(self, mock_load):
        mock_load.return_value = ['ID1', 'ID2', 'ID3']
        
        runner = BatchJobRunner()
        process_mock = MagicMock(return_value=True)
        
        with patch.object(sys, 'argv', ['prog', '--limit', '1']):
             with self.assertRaises(SystemExit):
                 runner.run(process_mock)
        
        self.assertEqual(process_mock.call_count, 1)
        process_mock.assert_called_with('ID1', None, False)

    @patch('batch.BatchJobRunner.BatchJobRunner.load_celex_ids')
    def test_per_celex_failure_exits_non_zero(self, mock_load):
        """A run that lost CELEX rows must not look successful to its caller.

        The loop deliberately continues past a failure (so one bad decision does
        not abandon the rest of the list), which is exactly why the exit code
        has to carry the information instead.
        """
        mock_load.return_value = ['ID1', 'ID2']

        runner = BatchJobRunner()
        process_mock = MagicMock(
            side_effect=[RuntimeError('CUDA out of memory'), True]
        )

        with patch.object(sys, 'argv', ['prog']):
            with self.assertRaises(SystemExit) as cm:
                runner.run(process_mock)

            self.assertEqual(cm.exception.code, 1)

        # Both IDs still attempted -- the failure must not abort the run.
        self.assertEqual(process_mock.call_count, 2)

    @patch('batch.BatchJobRunner.BatchJobRunner.load_celex_ids')
    def test_timeout_only_failures_exit_tolerable(self, mock_load):
        """An LLM timeout among successes exits EXIT_CODE_LLM_TIMEOUTS, not 1."""
        mock_load.return_value = ['ID1', 'ID2']
        process_mock = MagicMock(side_effect=[_timeout(), True])
        with patch.object(sys, 'argv', ['prog']):
            with self.assertRaises(SystemExit) as cm:
                BatchJobRunner().run(process_mock)
        self.assertEqual(cm.exception.code, EXIT_CODE_LLM_TIMEOUTS)

    @patch('batch.BatchJobRunner.BatchJobRunner.load_celex_ids')
    def test_timeout_mixed_with_other_failure_exits_one(self, mock_load):
        mock_load.return_value = ['ID1', 'ID2', 'ID3']
        process_mock = MagicMock(side_effect=[_timeout(), RuntimeError('boom'), True])
        with patch.object(sys, 'argv', ['prog']):
            with self.assertRaises(SystemExit) as cm:
                BatchJobRunner().run(process_mock)
        self.assertEqual(cm.exception.code, 1)


class TestExitCodeHelpers(unittest.TestCase):

    def test_all_timed_out_without_success_exits_one(self):
        with patch.object(settings, 'LLM_HUNG_SERVER_MIN_BATCH', 2):
            self.assertEqual(batch_exit_code({'A', 'B'}, {'A', 'B'}, 0), 1)

    def test_small_batch_all_timed_out_is_tolerable(self):
        with patch.object(settings, 'LLM_HUNG_SERVER_MIN_BATCH', 3):
            self.assertEqual(batch_exit_code({'A'}, {'A'}, 0), EXIT_CODE_LLM_TIMEOUTS)

    def test_small_batch_with_other_failure_exits_one(self):
        with patch.object(settings, 'LLM_HUNG_SERVER_MIN_BATCH', 3):
            self.assertEqual(batch_exit_code({'A', 'B'}, {'A'}, 0), 1)

    def test_no_failures_exit_zero(self):
        self.assertEqual(batch_exit_code(set(), set(), 3), 0)

    def test_timeout_only_exit_code(self):
        self.assertEqual(batch_exit_code({'A'}, {'A'}, 24), EXIT_CODE_LLM_TIMEOUTS)

    def test_litellm_timeout_is_detected(self):
        self.assertTrue(is_llm_timeout(_timeout()))

    def test_wrapped_timeout_is_detected(self):
        try:
            try:
                raise httpx.ReadTimeout('timed out')
            except httpx.ReadTimeout as inner:
                raise RuntimeError('pipeline failed') from inner
        except RuntimeError as outer:
            self.assertTrue(is_llm_timeout(outer))

    def test_other_error_is_not_a_timeout(self):
        self.assertFalse(is_llm_timeout(RuntimeError('CUDA out of memory')))


def _timeout() -> Exception:
    """Build the exception DSPy surfaces for an Ollama request timeout."""
    return litellm.Timeout('Request timed out.', model='qwen3-coder:30b', llm_provider='ollama')


if __name__ == '__main__':
    unittest.main()
