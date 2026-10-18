
import unittest
from unittest.mock import MagicMock, patch
import sys
import os

# Ensure src is in path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))

from batch.BatchJobRunner import BatchJobRunner

class TestBatchJobRunner(unittest.TestCase):
    
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

if __name__ == '__main__':
    unittest.main()
