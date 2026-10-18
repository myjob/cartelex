"""Test helpers shared by the config test modules."""

import logging
import unittest


def silence_logger(test_case: unittest.TestCase, name: str) -> None:
    """Keep one logger's records off the console for the duration of a test.

    The thermal modules log deliberately loud lines -- a CRITICAL "GATE
    TRIPPED" banner naming a temperature, a WARNING telling the operator to add
    a sudoers entry on the Spark. Printed by a test run they are
    indistinguishable from a real incident on the real box, and have twice been
    read as one. Tests assert on log content with ``assertLogs``, which
    overrides this for the logger it captures, so nothing is lost.

    Args:
        test_case: The running test; used to register the restore cleanup.
        name: Dotted logger name to quieten.
    """
    logger = logging.getLogger(name)
    previous_propagate = logger.propagate
    previous_handlers = list(logger.handlers)

    logger.propagate = False
    logger.handlers = [logging.NullHandler()]

    def restore() -> None:
        logger.propagate = previous_propagate
        logger.handlers = previous_handlers

    test_case.addCleanup(restore)
