import unittest
import rule_engine

from pipelines.rule_engine.condition_checker import ConditionValidator


class TestConditionValidator(unittest.TestCase):
    """Test suite for ConditionValidator."""

    def setUp(self):
        """Set up test fixtures."""
        self.validator = ConditionValidator()

    def test_check_substring_match(self):
        """Test that check() correctly identifies substring matches."""
        fact_context = {
            "law_article_all_relevant": "Article 101, Article 102, Article 103"
        }
        condition = "'Article 102' in law_article_all_relevant"

        result = self.validator.evaluate(condition, fact_context)

        self.assertTrue(result)

    def test_check_substring_no_match(self):
        """Test that check() returns False for non-matching substrings."""
        fact_context = {
            "law_article_all_relevant": "Article 101, Article 102, Article 103"
        }
        condition = "'Article 105' in law_article_all_relevant"

        result = self.validator.evaluate(condition, fact_context)

        self.assertFalse(result)

    def test_check_equality_match(self):
        """Test that check() correctly evaluates equality conditions."""
        fact_context = {"status": "active"}
        condition = "status == 'active'"

        result = self.validator.evaluate(condition, fact_context)

        self.assertTrue(result)

    def test_check_multiple_conditions(self):
        """Test that check() handles compound conditions."""
        fact_context = {"status": "active", "priority": 10}
        condition = "status == 'active' and priority > 5"

        result = self.validator.evaluate(condition, fact_context)

        self.assertTrue(result)

    def test_key_exists_conditions(self):

        facts_with_key = {"law_article_all_relevant": "Article 102"}
        facts_without_key = {"other_data": "N/A"}

        condition = 'exists("law_article_all_relevant")'

        result = self.validator.evaluate(condition, facts_with_key)
        self.assertTrue(result)

        result = self.validator.evaluate(condition, facts_without_key)
        self.assertFalse(result)

    def test_key_conditions_leniency(self):
        """Test exists() guard combined with boolean equality for leniency_case."""
        condition = 'exists("leniency_case") and leniency_case == true'

        facts_true = {"leniency_case": True}
        facts_absent = {"other_data": "N/A"}
        facts_false = {"leniency_case": False}

        # Key present and True -> condition satisfied
        result = self.validator.evaluate(condition, facts_true)
        self.assertTrue(result)

        # Key absent -> exists() is False, short-circuits to False
        result = self.validator.evaluate(condition, facts_absent)
        self.assertFalse(result)

        # Key present but False -> equality fails
        result = self.validator.evaluate(condition, facts_false)
        self.assertFalse(result)

        # Key absent again (unchanged)
        result = self.validator.evaluate(condition, facts_absent)
        self.assertFalse(result)

    def test_boolean_string_coercion_from_db(self):
        """Values from select_facts4celex are always strings.

        The resolver must coerce ``"True"`` / ``"False"`` strings to Python
        booleans so that ``== true`` / ``== false`` conditions work.
        """
        condition = "is_court_involved == true"

        # DB returns the string "True", not Python bool
        facts_str_true = {"is_court_involved": "True"}
        result = self.validator.evaluate(condition, facts_str_true)
        self.assertTrue(result, "String 'True' must match boolean true")

        facts_str_false = {"is_court_involved": "False"}
        result = self.validator.evaluate(condition, facts_str_false)
        self.assertFalse(result)

        # Also handle case-insensitive / whitespace variants
        facts_str_upper = {"is_court_involved": " TRUE "}
        result = self.validator.evaluate(condition, facts_str_upper)
        self.assertTrue(result, "Padded/upper-case 'TRUE' must also match")


class TestSameConceptCondition(unittest.TestCase):
    """The ``same_concept(...)`` condition function (Component D).

    Canonicalizes the infringement concept across the EU treaty renumbering so a
    condition no longer couples a literal article number to a date window.
    """

    def setUp(self):
        """Set up test fixtures."""
        self.validator = ConditionValidator()
        self.condition = (
            "exists(\"law_article_all_relevant\") and "
            "same_concept('abuse_of_dominance')"
        )

    def test_modern_number_old_date_now_matches(self):
        """Decca-style: '102' text + 1988 date resolves to abuse of dominance.

        The legacy date-coupled condition silently skipped this (1988 fails the
        post-2009 branch for '102'); the canonical condition matches.
        """
        fact_context = {
            "law_article_all_relevant": "Article 101, Article 102",
            "eu_commisssion_decision_date": "1988-12-21",
        }
        self.assertTrue(self.validator.evaluate(self.condition, fact_context))

    def test_period_correct_number_matches(self):
        """An EEC-era '86' with a pre-Amsterdam date is abuse of dominance."""
        fact_context = {
            "law_article_all_relevant": "Article 86",
            "eu_commisssion_decision_date": "1988-12-21",
        }
        self.assertTrue(self.validator.evaluate(self.condition, fact_context))

    def test_ec_era_86_is_not_abuse(self):
        """An EC-era '86' is public undertakings, not abuse of dominance."""
        fact_context = {
            "law_article_all_relevant": "Article 86",
            "eu_commisssion_decision_date": "2003-05-01",
        }
        self.assertFalse(self.validator.evaluate(self.condition, fact_context))
        public_condition = (
            "exists(\"law_article_all_relevant\") and "
            "same_concept('public_undertaking')"
        )
        self.assertTrue(self.validator.evaluate(public_condition, fact_context))

    def test_na_date_uses_celex_year_fallback(self):
        """With an NA date, the CELEX year disambiguates the '86' token."""
        eec_context = {
            "law_article_all_relevant": "Article 86",
            "eu_commisssion_decision_date": "NA",
            "celex_id": "CELEX-31988D0000",
        }
        self.assertTrue(self.validator.evaluate(self.condition, eec_context))

        ec_context = {
            "law_article_all_relevant": "Article 86",
            "eu_commisssion_decision_date": "NA",
            "celex_id": "CELEX-32003D0000",
        }
        self.assertFalse(self.validator.evaluate(self.condition, ec_context))

    def test_missing_article_fact_short_circuits(self):
        """No article fact -> exists() guard fails, condition is False."""
        self.assertFalse(self.validator.evaluate(self.condition, {"other": "x"}))

    def test_unknown_concept_slug_is_false(self):
        """A typo'd concept slug yields False, never raises."""
        condition = (
            "exists(\"law_article_all_relevant\") and same_concept('not_a_concept')"
        )
        fact_context = {"law_article_all_relevant": "Article 102"}
        self.assertFalse(self.validator.evaluate(condition, fact_context))


class TestRuleEngineBasics(unittest.TestCase):
    """Test suite for raw rule_engine functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.validator = ConditionValidator()

    def test_rule_engine_substring_match(self):
        """Test rule_engine's 'in' operator for substring matching."""
        fact_context = {
            "law_article_all_relevant": "Article 101, Article 102, Article 103"
        }
        condition = "'Article 102' in law_article_all_relevant"

        context = rule_engine.Context(resolver=rule_engine.resolve_item)
        rule = rule_engine.Rule(condition, context=context)

        is_match = rule.matches(fact_context)

        self.assertTrue(is_match)

    def test_rule_engine_substring_no_match(self):
        """Test rule_engine's 'in' operator when substring is not present."""
        fact_context = {
            "law_article_all_relevant": "Article 101, Article 102, Article 103"
        }
        condition = "'Article 105' in law_article_all_relevant"

        context = rule_engine.Context(resolver=rule_engine.resolve_item)
        rule = rule_engine.Rule(condition, context=context)

        is_match = rule.matches(fact_context)

        self.assertFalse(is_match)

    def test_rule_engine_horizontal_vertical(self):

        fact_context = {
            "law_article_all_relevant": "Article 101, Article 102, Article 103",
            "eu_commisssion_decision_date": "2009-12-31",
        }

        condition = 'exists("law_article_all_relevant") and "Article 102" in law_article_all_relevant and eu_commisssion_decision_date < d"2010-01-01"'
        context = rule_engine.Context(resolver=self.validator.unified_custom_resolver)
        rule = rule_engine.Rule(condition, context=context)
        is_match = rule.matches(fact_context)
        self.assertTrue(is_match)

        condition = 'exists("law_article_all_relevant") and "Article 105" in law_article_all_relevant and eu_commisssion_decision_date < d"2010-01-01"'
        context = rule_engine.Context(resolver=self.validator.unified_custom_resolver)
        rule = rule_engine.Rule(condition, context=context)
        is_match = rule.matches(fact_context)
        self.assertFalse(is_match)

        condition = 'exists("law_article_all_relevant") and "Article 105" in law_article_all_relevant and (eu_commisssion_decision_date < d"2010-01-01" or eu_commisssion_decision_date == "NA")'
        context = rule_engine.Context(resolver=self.validator.unified_custom_resolver)
        rule = rule_engine.Rule(condition, context=context)
        is_match = rule.matches(fact_context)
        self.assertFalse(is_match)

        fact_context = {
            "law_article_all_relevant": "Article 101, Article 102, Article 103",
            "eu_commisssion_decision_date": "NA",
        }

        condition = 'exists("law_article_all_relevant") and "Article 102" in law_article_all_relevant and ( eu_commisssion_decision_date == "NA" or eu_commisssion_decision_date < d"2010-01-01")'
        context = rule_engine.Context(resolver=self.validator.unified_custom_resolver)
        rule = rule_engine.Rule(condition, context=context)
        is_match = rule.matches(fact_context)
        self.assertTrue(is_match)


        fact_context = {
            "law_article_all_relevant": "Article 85",
            "eu_commisssion_decision_date": "1999-04-29",
        }

        condition = 'exists("law_article_all_relevant") and "Article 85" in law_article_all_relevant and ( eu_commisssion_decision_date == "NA" or eu_commisssion_decision_date < d"1999-04-30")'
        context = rule_engine.Context(resolver=self.validator.unified_custom_resolver)
        rule = rule_engine.Rule(condition, context=context)
        is_match = rule.matches(fact_context)
        self.assertTrue(is_match)


        fact_context = {
            "law_article_all_relevant": "Article 85",
            "eu_commisssion_decision_date": "1999-04-29",
        }

        condition = """ exists("law_article_all_relevant") and 
                        ( ( "Article 85" in law_article_all_relevant and ( eu_commisssion_decision_date == "NA" or eu_commisssion_decision_date < d"1999-04-30") )
                          or  ( "Article 81" in law_article_all_relevant and ( eu_commisssion_decision_date == "NA" or (eu_commisssion_decision_date > d"1999-04-30" and eu_commisssion_decision_date < d"2009-11-30") ) )
                          or  ( "Article 101" in law_article_all_relevant and ( eu_commisssion_decision_date == "NA" or (eu_commisssion_decision_date > d"2009-11-30" )) )
                        )  """
        context = rule_engine.Context(resolver=self.validator.unified_custom_resolver)
        rule = rule_engine.Rule(condition, context=context)
        is_match = rule.matches(fact_context)
        self.assertTrue(is_match)

        # -- 81 ---------------------------------------
        fact_context = {
            "law_article_all_relevant": "Article 81",
            "eu_commisssion_decision_date": "1999-05-29",
        }
        condition = """ exists("law_article_all_relevant") and 
                        ( ( "Article 85" in law_article_all_relevant and ( eu_commisssion_decision_date == "NA" or eu_commisssion_decision_date < d"1999-04-30") )
                          or  ( "Article 81" in law_article_all_relevant and ( eu_commisssion_decision_date == "NA" or (eu_commisssion_decision_date > d"1999-04-30" and eu_commisssion_decision_date < d"2009-11-30") ) )
                          or  ( "Article 101" in law_article_all_relevant and ( eu_commisssion_decision_date == "NA" or (eu_commisssion_decision_date > d"2009-11-30" )) )
                        )  """
        context = rule_engine.Context(resolver=self.validator.unified_custom_resolver)
        rule = rule_engine.Rule(condition, context=context)
        is_match = rule.matches(fact_context)
        self.assertTrue(is_match)

        fact_context = {
            "law_article_all_relevant": "Article 85",
            "eu_commisssion_decision_date": "1999-05-29",
        }
        context = rule_engine.Context(resolver=self.validator.unified_custom_resolver)
        rule = rule_engine.Rule(condition, context=context)
        is_match = rule.matches(fact_context)
        self.assertFalse(is_match)

        # -- 101 ---------------------------------------
        fact_context = {
            "law_article_all_relevant": "Article 101",
            "eu_commisssion_decision_date": "2009-12-29",
        }
        condition = """ exists("law_article_all_relevant") and 
                        ( ( "Article 85" in law_article_all_relevant and ( eu_commisssion_decision_date == "NA" or eu_commisssion_decision_date < d"1999-04-30") )
                          or  ( "Article 81" in law_article_all_relevant and ( eu_commisssion_decision_date == "NA" or (eu_commisssion_decision_date > d"1999-04-30" and eu_commisssion_decision_date < d"2009-11-30") ) )
                          or  ( "Article 101" in law_article_all_relevant and ( eu_commisssion_decision_date == "NA" or (eu_commisssion_decision_date > d"2009-11-30" )) )
                        )  """
        context = rule_engine.Context(resolver=self.validator.unified_custom_resolver)
        rule = rule_engine.Rule(condition, context=context)
        is_match = rule.matches(fact_context)
        self.assertTrue(is_match)

        fact_context = {
            "law_article_all_relevant": "Article 102",
            "eu_commisssion_decision_date": "2009-12-29",
        }
        context = rule_engine.Context(resolver=self.validator.unified_custom_resolver)
        rule = rule_engine.Rule(condition, context=context)
        is_match = rule.matches(fact_context)
        self.assertFalse(is_match)


        fact_context = {
            "law_article_all_relevant": "Article 101",
            "eu_commisssion_decision_date": "2009-10-29",
        }
        context = rule_engine.Context(resolver=self.validator.unified_custom_resolver)
        rule = rule_engine.Rule(condition, context=context)
        is_match = rule.matches(fact_context)
        self.assertFalse(is_match)


class TestSameConceptSource(unittest.TestCase):
    """``same_concept(slug, source)`` reads the operative articles first."""

    def setUp(self):
        self.validator = ConditionValidator()

    def test_operative_source_overrides_narrative_mentions(self):
        """An Art. 86 abuse case that merely mentions Art. 85 is not an agreement case."""
        context = {
            "celex_id": "CELEX-31976D0353",
            "eu_commisssion_decision_date": "1975-12-17",
            "law_article_all_relevant": "Article 85, Article 86",
            "law_article_operative": "Article 86",
        }
        self.assertFalse(
            self.validator.evaluate(
                "same_concept('anticompetitive_agreement', 'law_article_operative')",
                context,
            )
        )
        self.assertTrue(
            self.validator.evaluate(
                "same_concept('abuse_of_dominance', 'law_article_operative')", context
            )
        )

    def test_missing_source_falls_back_to_all_relevant(self):
        context = {
            "celex_id": "CELEX-31976D0353",
            "eu_commisssion_decision_date": "1975-12-17",
            "law_article_all_relevant": "Article 85, Article 86",
        }
        self.assertTrue(
            self.validator.evaluate(
                "same_concept('anticompetitive_agreement', 'law_article_operative')",
                context,
            )
        )

    def test_placeholder_source_falls_back_to_all_relevant(self):
        context = {
            "celex_id": "CELEX-31976D0353",
            "eu_commisssion_decision_date": "1975-12-17",
            "law_article_all_relevant": "Article 85",
            "law_article_operative": "NA",
        }
        self.assertTrue(
            self.validator.evaluate(
                "same_concept('anticompetitive_agreement', 'law_article_operative')",
                context,
            )
        )

    def test_single_argument_form_still_reads_all_relevant(self):
        context = {
            "celex_id": "CELEX-31976D0353",
            "eu_commisssion_decision_date": "1975-12-17",
            "law_article_all_relevant": "Article 85",
            "law_article_operative": "Article 86",
        }
        self.assertTrue(
            self.validator.evaluate("same_concept('anticompetitive_agreement')", context)
        )


if __name__ == "__main__":
    unittest.main()
