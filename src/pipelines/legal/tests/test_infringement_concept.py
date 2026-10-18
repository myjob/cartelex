"""Unit tests for the canonical infringement-concept normalizer.

Pure functions only — no DB, no LLM — so every case runs in isolation.
"""

import unittest
from datetime import date

from pipelines.legal.infringement_concept import (
    DEFAULT_PROVISION_MAP,
    InfringementConcept,
    ProvisionMap,
    ProvisionRow,
    TreatyEra,
    articles_to_concepts,
    celex_year,
    concepts_for,
    parse_article_tokens,
    parse_decision_date,
    treaty_era,
)


class TestParseArticleTokens(unittest.TestCase):
    """``parse_article_tokens`` extracts the distinct article numbers."""

    def test_comma_joined_value(self):
        """The stored 'Article N, Article M' surface yields its numbers."""
        self.assertEqual(
            parse_article_tokens("Article 101, Article 102, Article 102"),
            {101, 102},
        )

    def test_case_insensitive_and_spacing(self):
        """Matching ignores case and tolerates extra whitespace."""
        self.assertEqual(parse_article_tokens("article  86"), {86})

    def test_empty_and_none(self):
        """Empty or None inputs yield an empty set, never raise."""
        self.assertEqual(parse_article_tokens(""), set())
        self.assertEqual(parse_article_tokens(None), set())


class TestCelexYear(unittest.TestCase):
    """``celex_year`` recovers the year embedded in a CELEX id."""

    def test_standard_celex_id(self):
        """A well-formed CELEX id yields its four-digit year."""
        self.assertEqual(celex_year("CELEX-31990D0645"), 1990)

    def test_non_celex_surface(self):
        """Non-CELEX ids (AT_/numeric) are not parseable -> None."""
        self.assertIsNone(celex_year("AT_39610"))
        self.assertIsNone(celex_year(""))
        self.assertIsNone(celex_year(None))


class TestParseDecisionDate(unittest.TestCase):
    """``parse_decision_date`` tolerates dates, ISO strings, placeholders."""

    def test_iso_string(self):
        """An ISO 'YYYY-MM-DD' string parses to a date."""
        self.assertEqual(parse_decision_date("1988-12-21"), date(1988, 12, 21))

    def test_placeholders(self):
        """'NA' / '' / None yield None rather than raising."""
        self.assertIsNone(parse_decision_date("NA"))
        self.assertIsNone(parse_decision_date(""))
        self.assertIsNone(parse_decision_date(None))

    def test_passthrough_date(self):
        """An existing date is returned unchanged."""
        self.assertEqual(parse_decision_date(date(2001, 1, 1)), date(2001, 1, 1))


class TestTreatyEra(unittest.TestCase):
    """``treaty_era`` resolves from the date, then the CELEX year."""

    def test_date_boundaries(self):
        """The three eras split at the Amsterdam and Lisbon cut-offs."""
        self.assertIs(treaty_era(date(1999, 4, 29), None), TreatyEra.EEC)
        self.assertIs(treaty_era(date(1999, 5, 29), None), TreatyEra.EC)
        self.assertIs(treaty_era(date(2009, 12, 29), None), TreatyEra.TFEU)

    def test_year_fallback_when_date_missing(self):
        """With no date, the CELEX year selects the era."""
        self.assertIs(treaty_era(None, 1995), TreatyEra.EEC)
        self.assertIs(treaty_era(None, 2003), TreatyEra.EC)
        self.assertIs(treaty_era(None, 2015), TreatyEra.TFEU)

    def test_both_unknown(self):
        """No date and no year leaves the era undetermined."""
        self.assertIsNone(treaty_era(None, None))


class TestArticlesToConcepts(unittest.TestCase):
    """``articles_to_concepts`` maps numbers to concepts; 86 needs the era."""

    def test_unambiguous_tokens_ignore_era(self):
        """Every token except 86 resolves without consulting the era."""
        for article in (85, 81, 101):
            self.assertEqual(
                articles_to_concepts({article}, None),
                {InfringementConcept.ANTICOMPETITIVE_AGREEMENT},
            )
        for article in (82, 102):
            self.assertEqual(
                articles_to_concepts({article}, None),
                {InfringementConcept.ABUSE_OF_DOMINANCE},
            )
        for article in (90, 106):
            self.assertEqual(
                articles_to_concepts({article}, None),
                {InfringementConcept.PUBLIC_UNDERTAKING},
            )

    def test_article_86_resolved_by_era(self):
        """86 is abuse pre-Amsterdam, public undertakings under the EC Treaty."""
        self.assertEqual(
            articles_to_concepts({86}, TreatyEra.EEC),
            {InfringementConcept.ABUSE_OF_DOMINANCE},
        )
        self.assertEqual(
            articles_to_concepts({86}, TreatyEra.EC),
            {InfringementConcept.PUBLIC_UNDERTAKING},
        )

    def test_article_86_unknown_era_emits_both(self):
        """86 with an undetermined era emits both candidate concepts."""
        self.assertEqual(
            articles_to_concepts({86}, None),
            {
                InfringementConcept.ABUSE_OF_DOMINANCE,
                InfringementConcept.PUBLIC_UNDERTAKING,
            },
        )

    def test_non_competition_article_dropped(self):
        """An article outside the renumbering map contributes nothing."""
        self.assertEqual(articles_to_concepts({3, 15}, TreatyEra.TFEU), set())


class TestConceptsFor(unittest.TestCase):
    """``concepts_for`` is the end-to-end composition used by call sites."""

    def test_decca_modern_numbering_old_date(self):
        """The Decca case: '102' text + 1988 date -> abuse of dominance.

        The literal token is the modern '102', but the period-correct concept is
        recovered from the decision date — the bug the canonicalization fixes.
        """
        self.assertEqual(
            concepts_for("Article 101, Article 102", "1988-12-21", "CELEX-31988D0000"),
            {
                InfringementConcept.ANTICOMPETITIVE_AGREEMENT,
                InfringementConcept.ABUSE_OF_DOMINANCE,
            },
        )

    def test_na_date_uses_celex_year(self):
        """An NA date with an EEC-era CELEX id resolves 86 to abuse."""
        self.assertEqual(
            concepts_for("Article 86", "NA", "CELEX-31988D0000"),
            {InfringementConcept.ABUSE_OF_DOMINANCE},
        )

    def test_na_date_ec_era_celex_year(self):
        """An NA date with an EC-era CELEX id resolves 86 to public undertaking."""
        self.assertEqual(
            concepts_for("Article 86", "NA", "CELEX-32003D0000"),
            {InfringementConcept.PUBLIC_UNDERTAKING},
        )

    def test_na_date_and_unparseable_id_emits_both_for_86(self):
        """No date and no CELEX year leaves 86 ambiguous (both concepts)."""
        self.assertEqual(
            concepts_for("Article 86", "NA", "AT_39610"),
            {
                InfringementConcept.ABUSE_OF_DOMINANCE,
                InfringementConcept.PUBLIC_UNDERTAKING,
            },
        )

    def test_no_articles(self):
        """Text without any competition article yields no concepts."""
        self.assertEqual(concepts_for("", "1988-12-21", "CELEX-31988D0000"), set())


class TestProvisionMap(unittest.TestCase):
    """The ontology lookup that replaced the boundary-date arithmetic."""

    def test_exclusive_valid_to_boundaries(self):
        """valid_to is exclusive: the boundary date belongs to the next era."""
        era_of = DEFAULT_PROVISION_MAP.era_for_date
        self.assertIs(era_of(date(1999, 4, 29)), TreatyEra.EEC)
        self.assertIs(era_of(date(1999, 4, 30)), TreatyEra.EC)
        self.assertIs(era_of(date(2009, 11, 29)), TreatyEra.EC)
        self.assertIs(era_of(date(2009, 11, 30)), TreatyEra.TFEU)

    def test_pre_window_date_resolves_to_earliest_era(self):
        """Dates before the EEC window still resolve to EEC (old numbering)."""
        self.assertIs(DEFAULT_PROVISION_MAP.era_for_date(date(1950, 1, 1)), TreatyEra.EEC)

    def test_year_fallback_boundary_years(self):
        """Boundary years go to the era covering the year's majority."""
        year_of = DEFAULT_PROVISION_MAP.era_for_year
        self.assertIs(year_of(1998), TreatyEra.EEC)
        self.assertIs(year_of(1999), TreatyEra.EC)  # cut-off in April
        self.assertIs(year_of(2009), TreatyEra.EC)  # cut-off in November
        self.assertIs(year_of(2010), TreatyEra.TFEU)

    def test_single_concept_article_is_era_independent(self):
        for era in (TreatyEra.EEC, TreatyEra.EC, TreatyEra.TFEU, None):
            with self.subTest(era=era):
                self.assertEqual(
                    DEFAULT_PROVISION_MAP.concepts_for_article(101, era),
                    {InfringementConcept.ANTICOMPETITIVE_AGREEMENT},
                )

    def test_article_86_resolves_by_era(self):
        concepts = DEFAULT_PROVISION_MAP.concepts_for_article
        self.assertEqual(
            concepts(86, TreatyEra.EEC), {InfringementConcept.ABUSE_OF_DOMINANCE}
        )
        self.assertEqual(
            concepts(86, TreatyEra.EC), {InfringementConcept.PUBLIC_UNDERTAKING}
        )

    def test_article_86_without_era_emits_both(self):
        """Unknown era and the anachronistic TFEU era both stay ambiguous."""
        both = {
            InfringementConcept.ABUSE_OF_DOMINANCE,
            InfringementConcept.PUBLIC_UNDERTAKING,
        }
        concepts = DEFAULT_PROVISION_MAP.concepts_for_article
        self.assertEqual(concepts(86, None), both)
        self.assertEqual(concepts(86, TreatyEra.TFEU), both)

    def test_unlisted_article_yields_nothing(self):
        self.assertEqual(DEFAULT_PROVISION_MAP.concepts_for_article(7, None), set())

    def test_from_rows_equality(self):
        """Row order does not affect map equality (drift-guard contract)."""
        rows = [
            ProvisionRow(85, TreatyEra.EEC, date(1958, 1, 1), date(1999, 4, 30),
                         InfringementConcept.ANTICOMPETITIVE_AGREEMENT),
            ProvisionRow(81, TreatyEra.EC, date(1999, 4, 30), date(2009, 11, 30),
                         InfringementConcept.ANTICOMPETITIVE_AGREEMENT),
            ProvisionRow(101, TreatyEra.TFEU, date(2009, 11, 30), None,
                         InfringementConcept.ANTICOMPETITIVE_AGREEMENT),
        ]
        self.assertEqual(
            ProvisionMap.from_rows(rows), ProvisionMap.from_rows(reversed(rows))
        )

    def test_empty_rows_rejected(self):
        with self.assertRaises(ValueError):
            ProvisionMap.from_rows([])

    def test_injected_map_is_used_by_concepts_for(self):
        """The provision_map parameter overrides the static default."""
        agreement_only = ProvisionMap.from_rows([
            ProvisionRow(999, TreatyEra.TFEU, date(2009, 11, 30), None,
                         InfringementConcept.ANTICOMPETITIVE_AGREEMENT),
        ])
        self.assertEqual(
            concepts_for("Article 999", "2015-01-01", None, provision_map=agreement_only),
            {InfringementConcept.ANTICOMPETITIVE_AGREEMENT},
        )
        self.assertEqual(
            concepts_for("Article 101", "2015-01-01", None, provision_map=agreement_only),
            set(),
        )


if __name__ == "__main__":
    unittest.main()
