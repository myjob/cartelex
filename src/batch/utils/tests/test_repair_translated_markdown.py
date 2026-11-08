"""Unit tests for the translated-markdown repair planner (no DB, no files).

Run with:
    PYTHONPATH=src .venv/bin/python -m unittest batch.utils.tests.test_repair_translated_markdown
"""

import unittest

from batch.utils.repair_translated_markdown import RepairRow, parse_args, plan_repair
from pipelines.download.descriptor_store import TranslationEntry

PDF = "/d/CELEX-31964D0233/CELEX-31964D0233.pdf"
EN_MD = "/d/CELEX-31964D0233/mineru/auto/CELEX-31964D0233_en.md"
DE_MD = "/d/CELEX-31964D0233/mineru/CELEX-31964D0233.md"


def _row(**kw: object) -> RepairRow:
    """Build a repair row for the DE example decision.

    Args:
        **kw: Field overrides.

    Returns:
        A RepairRow.
    """
    data: dict[str, object] = {
        "decision_id": "CELEX-31964D0233",
        "source_document_id": "CELEX-31964D0233_DE",
        "source_language": "DE",
        "source_pdf": PDF,
        "english_md": EN_MD,
        "phantom_document_id": "CELEX-31964D0233_EN",
    }
    data.update(kw)
    return RepairRow.model_validate(data)


class PlanRepairTests(unittest.TestCase):
    """Pure planning of the repair actions."""

    def test_phantom_decision(self) -> None:
        """German original on disk and a phantom candidate: full repair."""
        actions = plan_repair([_row()], file_exists=lambda p: p in {EN_MD, DE_MD})
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertEqual(action.source_document_id, "CELEX-31964D0233_DE")
        self.assertEqual(action.original_file_md, DE_MD)
        self.assertEqual(action.clear_document_id, "CELEX-31964D0233_EN")
        self.assertEqual(action.master, TranslationEntry(
            txt_md_file=EN_MD, original_file_md=DE_MD,
            language_init="de", language_translated="en"))

    def test_no_german_md_yet(self) -> None:
        """No MinerU original and no phantom: only the translated marker."""
        actions = plan_repair(
            [_row(source_language="FR", phantom_document_id=None)],
            file_exists=lambda p: p == EN_MD)
        action = actions[0]
        self.assertIsNone(action.original_file_md)
        self.assertIsNone(action.clear_document_id)
        self.assertEqual(action.master.language_init, "fr")
        self.assertIsNone(action.master.original_file_md)
        self.assertEqual(action.master.txt_md_file, EN_MD)

    def test_missing_english_md_is_dropped(self) -> None:
        """A row whose English file is gone is not repaired."""
        with self.assertLogs("batch.utils.repair_translated_markdown", "WARNING"):
            actions = plan_repair([_row()], file_exists=lambda p: p == DE_MD)
        self.assertEqual(actions, [])

    def test_dry_run_is_default(self) -> None:
        """Without --apply nothing is written."""
        self.assertFalse(parse_args([]).apply)
        self.assertTrue(parse_args(["--apply"]).apply)


if __name__ == "__main__":
    unittest.main()
