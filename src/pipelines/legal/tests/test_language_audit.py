"""Unit tests for the chunk-language audit logic.

Pure: no database, and the detector is exercised only in the two tests that
explicitly pin its contract. Every other test constructs observations directly,
so the aggregation and verdict rules are tested independently of langdetect.
"""

import unittest

from config.settings import settings
from pipelines.legal.language_audit import (
    ChunkLanguageObservation,
    DescriptorAgreement,
    LanguageVerdict,
    ObservationStatus,
    aggregate_document_language,
    detect_chunk_language,
    expected_languages_from_descriptors,
)


def _detected(doc_pos: int, language: str, char_count: int) -> ChunkLanguageObservation:
    """Build a scorable observation in *language* of *char_count* characters."""
    return ChunkLanguageObservation(
        doc_pos=doc_pos,
        char_count=char_count,
        language=language,
        probability=0.99,
        status=ObservationStatus.DETECTED,
    )


def _unscorable(
    doc_pos: int, status: ObservationStatus, char_count: int = 50
) -> ChunkLanguageObservation:
    """Build an observation that contributes no language evidence."""
    return ChunkLanguageObservation(
        doc_pos=doc_pos, char_count=char_count, status=status
    )


class TestAggregateDocumentLanguage(unittest.TestCase):
    def test_all_english_document_is_english(self) -> None:
        observations = [_detected(i, "en", 1600) for i in range(20)]
        audit = aggregate_document_language("CELEX-TEST", observations)
        self.assertIs(audit.verdict, LanguageVerdict.ENGLISH)
        self.assertEqual(audit.non_english_char_share, 0.0)
        self.assertIsNone(audit.dominant_non_english)
        self.assertEqual(audit.worst_doc_positions, [])

    def test_wrong_language_load_is_flagged(self) -> None:
        """The 39452_3336_8 shape: a document ingested from its German original."""
        observations = [_detected(i, "de", 1650) for i in range(314)]
        observations += [_detected(400 + i, "en", 1650) for i in range(10)]
        audit = aggregate_document_language("39452_3336_8", observations)
        self.assertIs(audit.verdict, LanguageVerdict.NON_ENGLISH)
        self.assertEqual(audit.dominant_non_english, "de")
        self.assertGreater(audit.non_english_char_share, 0.75)
        self.assertEqual(audit.chunks_scored, 324)
        self.assertEqual(len(audit.worst_doc_positions), 3)

    def test_char_weighting_beats_chunk_counting(self) -> None:
        """Short foreign misfires must not outvote the document's real bulk.

        Six short foreign chunks against four long English ones is a 0.6 chunk
        share but only a ~0.07 char share -- the case char-weighting exists for.
        """
        observations = [_detected(i, "fr", 250) for i in range(6)]
        observations += [_detected(10 + i, "en", 5000) for i in range(4)]
        audit = aggregate_document_language("CELEX-SHORTNOISE", observations)
        self.assertAlmostEqual(audit.non_english_chunk_share, 0.6)
        self.assertLess(audit.non_english_char_share, 0.1)
        self.assertIs(audit.verdict, LanguageVerdict.ENGLISH)

    def test_quoting_document_lands_in_review_not_reload(self) -> None:
        """The FRUBO shape: an English decision dense with Dutch names."""
        observations = [_detected(i, "nl", 1000) for i in range(35)]
        observations += [_detected(100 + i, "en", 1000) for i in range(65)]
        audit = aggregate_document_language("CELEX-31974D0433", observations)
        self.assertIs(audit.verdict, LanguageVerdict.REVIEW)
        self.assertIsNot(audit.verdict, LanguageVerdict.NON_ENGLISH)

    def test_unscorable_chunks_are_excluded_from_both_shares(self) -> None:
        observations = [
            _detected(0, "en", 2000),
            _detected(1, "de", 2000),
            _unscorable(2, ObservationStatus.TOO_SHORT),
            _unscorable(3, ObservationStatus.LOW_CONFIDENCE, char_count=900),
            _unscorable(4, ObservationStatus.NO_FEATURES),
        ]
        audit = aggregate_document_language("CELEX-MIXED", observations)
        # Denominator is the two scored chunks only, not all five.
        self.assertEqual(audit.chunks_total, 5)
        self.assertEqual(audit.chunks_scored, 2)
        self.assertEqual(audit.chars_scored, 4000)
        self.assertAlmostEqual(audit.non_english_char_share, 0.5)
        self.assertAlmostEqual(audit.non_english_chunk_share, 0.5)
        self.assertEqual(audit.chunks_too_short, 1)
        self.assertEqual(audit.chunks_low_confidence, 1)
        self.assertEqual(audit.chunks_no_features, 1)

    def test_document_without_evidence_is_insufficient(self) -> None:
        observations = [
            _unscorable(i, ObservationStatus.TOO_SHORT) for i in range(4)
        ]
        audit = aggregate_document_language("CELEX-THIN", observations)
        self.assertIs(audit.verdict, LanguageVerdict.INSUFFICIENT_EVIDENCE)
        self.assertEqual(audit.non_english_char_share, 0.0)
        self.assertEqual(audit.non_english_chunk_share, 0.0)

    def test_no_observations_does_not_divide_by_zero(self) -> None:
        audit = aggregate_document_language("CELEX-EMPTY", [])
        self.assertIs(audit.verdict, LanguageVerdict.INSUFFICIENT_EVIDENCE)
        self.assertEqual(audit.chars_scored, 0)

    def test_threshold_lower_bounds_are_inclusive(self) -> None:
        total_chars = 100_000

        def _audit_at(share: float) -> LanguageVerdict:
            foreign = int(total_chars * share)
            observations = [_detected(0, "de", foreign)]
            observations.append(_detected(1, "en", total_chars - foreign))
            return aggregate_document_language("CELEX-BOUND", observations).verdict

        self.assertIs(
            _audit_at(settings.LANGUAGE_AUDIT_RELOAD_SHARE),
            LanguageVerdict.NON_ENGLISH,
        )
        self.assertIs(
            _audit_at(settings.LANGUAGE_AUDIT_REVIEW_SHARE), LanguageVerdict.REVIEW
        )

    def test_top_languages_ordered_by_chars_and_truncated(self) -> None:
        observations = [
            _detected(0, "en", 10_000),
            _detected(1, "de", 5_000),
            _detected(2, "fr", 3_000),
            _detected(3, "it", 1_000),
        ]
        audit = aggregate_document_language("CELEX-MANY", observations)
        self.assertEqual(
            [tally.language for tally in audit.top_languages], ["en", "de", "fr"]
        )
        self.assertEqual(audit.dominant_non_english, "de")

    def test_top_language_ties_broken_by_language_code(self) -> None:
        observations = [_detected(0, "nl", 4_000), _detected(1, "de", 4_000)]
        audit = aggregate_document_language("CELEX-TIE", observations)
        self.assertEqual(
            [tally.language for tally in audit.top_languages], ["de", "nl"]
        )


class TestDescriptorAgreement(unittest.TestCase):
    def _verdict_with(
        self,
        share: float,
        language_init: str | None,
        language_translated: str | None,
    ) -> DescriptorAgreement:
        foreign = int(100_000 * share)
        observations = [
            _detected(0, "de", foreign),
            _detected(1, "en", 100_000 - foreign),
        ]
        return aggregate_document_language(
            "CELEX-DESC", observations, language_init, language_translated
        ).descriptor_agreement

    def test_no_metadata_when_descriptor_is_silent(self) -> None:
        self.assertIs(self._verdict_with(0.99, None, None), DescriptorAgreement.NO_METADATA)

    def test_clean_document_agrees(self) -> None:
        self.assertIs(self._verdict_with(0.0, "en", None), DescriptorAgreement.AGREES)

    def test_non_english_original_without_translation_is_expected(self) -> None:
        self.assertIs(
            self._verdict_with(0.99, "de", None),
            DescriptorAgreement.EXPECTED_NON_ENGLISH,
        )

    def test_english_original_flagged_is_unexpected(self) -> None:
        self.assertIs(
            self._verdict_with(0.99, "en", None),
            DescriptorAgreement.UNEXPECTED_NON_ENGLISH,
        )

    def test_translated_to_english_but_flagged_is_unexpected(self) -> None:
        """A recorded English translation that did not make it into the table."""
        self.assertIs(
            self._verdict_with(0.99, "de", "en"),
            DescriptorAgreement.UNEXPECTED_NON_ENGLISH,
        )


class TestExpectedLanguagesFromDescriptors(unittest.TestCase):
    _PAYLOAD = {
        "documents": {
            "CELEX-A": {"language_init": "EN", "language_translated": "NA"},
            "CELEX-B": {"language_init": "de", "language_translated": "en"},
            "CELEX-C": {"language_init": "", "language_translated": None},
            "CELEX-D": {},
            "CELEX-E": "not-a-dict",
        }
    }

    def test_na_and_empty_normalize_to_none(self) -> None:
        expected = expected_languages_from_descriptors(self._PAYLOAD)
        self.assertEqual(expected["CELEX-A"], ("en", None))
        self.assertEqual(expected["CELEX-C"], (None, None))
        self.assertEqual(expected["CELEX-D"], (None, None))

    def test_recorded_languages_are_lowercased(self) -> None:
        expected = expected_languages_from_descriptors(self._PAYLOAD)
        self.assertEqual(expected["CELEX-B"], ("de", "en"))

    def test_non_dict_descriptor_is_skipped(self) -> None:
        self.assertNotIn("CELEX-E", expected_languages_from_descriptors(self._PAYLOAD))

    def test_accepts_bare_document_map(self) -> None:
        expected = expected_languages_from_descriptors(self._PAYLOAD["documents"])
        self.assertEqual(expected["CELEX-B"], ("de", "en"))


class TestDetectChunkLanguage(unittest.TestCase):
    def test_short_chunk_short_circuits_without_a_verdict(self) -> None:
        observation = detect_chunk_language("Table 3", doc_pos=7)
        self.assertIs(observation.status, ObservationStatus.TOO_SHORT)
        self.assertIsNone(observation.language)
        self.assertEqual(observation.doc_pos, 7)

    def test_german_paragraph_is_detected(self) -> None:
        """Pins the seeded-detector contract; the only live detector call here."""
        german = (
            "Die Kommission stellte fest, dass die betroffenen Unternehmen an einer "
            "einheitlichen und fortgesetzten Zuwiderhandlung gegen Artikel 101 des "
            "Vertrages teilgenommen haben, die das gesamte Gebiet des Europaeischen "
            "Wirtschaftsraums umfasste und Preisabsprachen zum Gegenstand hatte."
        )
        observation = detect_chunk_language(german, doc_pos=1)
        self.assertIs(observation.status, ObservationStatus.DETECTED)
        self.assertEqual(observation.language, "de")

    def test_featureless_long_chunk_is_not_scored(self) -> None:
        observation = detect_chunk_language("1234567890 " * 40, doc_pos=2)
        self.assertIsNot(observation.status, ObservationStatus.DETECTED)


if __name__ == "__main__":
    unittest.main()
