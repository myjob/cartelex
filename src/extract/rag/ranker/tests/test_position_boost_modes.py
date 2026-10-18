"""Unit tests for the structural-boost dispatch (position_boost.py).

Covers the three RANKER_BOOST_MODE values, the section-type weight table and
the legacy Legal Climax formula the section mode replaces.
"""

import unittest
from unittest.mock import patch

from extract.rag.models.chunk_vo import ChunkVo, SectionType
from extract.rag.ranker.position_boost import (
    LEGAL_CLIMAX_DEPTH_CAP,
    LEGAL_CLIMAX_POWER,
    LEGAL_CLIMAX_WEIGHT,
    SECTION_TYPE_BOOSTS,
    legal_climax_boost,
    section_type_boost,
    structural_boost,
)


def _chunk(section_type: SectionType | None = None, doc_pos: int = 0) -> ChunkVo:
    return ChunkVo(
        id="c1",
        celex_id="TEST",
        chunk="text",
        origin="pgvector_fts",
        similarity=0.5,
        doc_pos=doc_pos,
        section_type=section_type,
    )


class TestSectionTypeBoost(unittest.TestCase):
    def test_weighted_sections(self) -> None:
        self.assertEqual(section_type_boost(SectionType.OPERATIVE), 0.30)
        self.assertEqual(section_type_boost(SectionType.FINES), 0.30)
        self.assertEqual(section_type_boost(SectionType.LEGAL_ASSESSMENT), 0.10)

    def test_unweighted_sections_get_zero(self) -> None:
        for section in (
            SectionType.PARTIES,
            SectionType.RECITALS_FACTS,
            SectionType.ANNEX,
            SectionType.OTHER,
        ):
            with self.subTest(section=section):
                self.assertEqual(section_type_boost(section), 0.0)

    def test_unclassified_gets_zero(self) -> None:
        self.assertEqual(section_type_boost(None), 0.0)

    def test_raw_string_value_accepted(self) -> None:
        """The DB hands back varchar; the boost accepts the raw value."""
        self.assertEqual(section_type_boost("fines"), 0.30)
        self.assertEqual(section_type_boost("garbage-value"), 0.0)

    def test_weights_stay_below_climax_maximum(self) -> None:
        """Section weights are tie-breakers, weaker than the old climax max."""
        climax_max = (LEGAL_CLIMAX_DEPTH_CAP**LEGAL_CLIMAX_POWER) * LEGAL_CLIMAX_WEIGHT
        self.assertTrue(all(w < climax_max for w in SECTION_TYPE_BOOSTS.values()))


class TestStructuralBoostDispatch(unittest.TestCase):
    def test_section_type_mode(self) -> None:
        boost = structural_boost(
            _chunk(SectionType.FINES, doc_pos=1), local_max=100, mode="section_type"
        )
        self.assertEqual(boost, 0.30)

    def test_legal_climax_mode(self) -> None:
        chunk = _chunk(SectionType.PARTIES, doc_pos=90)
        boost = structural_boost(chunk, local_max=100, mode="legal_climax")
        self.assertEqual(boost, legal_climax_boost(90, 100))

    def test_none_mode(self) -> None:
        boost = structural_boost(
            _chunk(SectionType.OPERATIVE, doc_pos=99), local_max=100, mode="none"
        )
        self.assertEqual(boost, 0.0)

    def test_unknown_mode_raises(self) -> None:
        with self.assertRaises(ValueError):
            structural_boost(_chunk(), local_max=1, mode="bogus")

    def test_default_mode_comes_from_settings(self) -> None:
        with patch(
            "extract.rag.ranker.position_boost.settings.RANKER_BOOST_MODE",
            "legal_climax",
        ):
            chunk = _chunk(SectionType.FINES, doc_pos=50)
            self.assertEqual(
                structural_boost(chunk, local_max=100),
                legal_climax_boost(50, 100),
            )


class TestLegalClimaxBoost(unittest.TestCase):
    """The legacy formula, unchanged, kept for the A/B comparison."""

    def test_start_of_document_gets_zero(self) -> None:
        self.assertEqual(legal_climax_boost(0, 100), 0.0)

    def test_depth_is_capped(self) -> None:
        expected = (LEGAL_CLIMAX_DEPTH_CAP**LEGAL_CLIMAX_POWER) * LEGAL_CLIMAX_WEIGHT
        self.assertEqual(legal_climax_boost(100, 100), expected)


if __name__ == "__main__":
    unittest.main()
