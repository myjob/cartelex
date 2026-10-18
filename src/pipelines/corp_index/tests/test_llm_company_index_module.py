"""Unit tests for the LLM company-index DSPy module.

Mocks live only at the LLM boundary: the predictor is injected, so no request is
sent. The subject is what the module guarantees around the model's answer --
that the functional key and the model id come from the caller and never from the
model, that roster placeholders are not passed off as facts, and that every
spelling of "unknown" lands as ``NA``.
"""

import os
import unittest
from unittest import mock

# Provide a dummy token before importing settings-dependent modules.
os.environ.setdefault("opencorporates_api_token", "dummy")

import dspy  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from config.llm_factory import corp_index_profile  # noqa: E402
from pipelines.corp_index.corp_index_module import (  # noqa: E402
    LlmCompanyIndexModule,
)
from pipelines.corp_index.models import (  # noqa: E402
    LlmCompanyIndexProfile,
    LlmCompanyIndexRow,
    LlmCompanyIndexTarget,
)


def _profile(**overrides: str) -> LlmCompanyIndexProfile:
    """Build a company profile with sensible defaults.

    Args:
        **overrides: Field values replacing the defaults.

    Returns:
        The profile to hand back from the mocked predictor.
    """
    fields = {
        "canonical_name": "Brasserie de Wiltz S.E.C.S.",
        "abbreviation": "Wiltz",
        "legal_form": "S.E.C.S.",
        "established_since": "1824",
        "life_cycle_status": "dissolved",
        "succession": "brands acquired by Brasserie Nationale in 1999",
        "products_services": "Beer brewing",
        "geo_market": "Luxembourg",
        "headquarter_address": "14 rue Joseph Simon, L-9550 Wiltz",
        "country": "LU",
        "jurisdiction": "LU",
        "lei": "NA",
        "euid": "8434-4121-8815-0614",
        "bris": "NA",
        "handelsregister": "NA",
        "siren": "NA",
        "generic_corp_id": "NA",
        "generic_corp_registrar": "NA",
    }
    fields.update(overrides)
    return LlmCompanyIndexProfile(**fields)


class TestPostalAddressComposition(unittest.TestCase):
    """Building one postal address out of the roster's two columns.

    The roster splits them: ``address`` holds what the decision printed, which
    usually has no country, and ``iso_country_code`` holds the country. A model
    asked to place a company no register lists does better with one complete
    address than with a street and a two-letter code in separate fields.
    """

    def test_country_is_appended_when_the_address_lacks_one(self) -> None:
        target = LlmCompanyIndexTarget(
            company_name="Ets. Abel Falisse",
            address="Av. Emile Digneffe 26, 4000 Liege",
            iso_country_code="BE",
        )

        self.assertEqual(
            target.postal_address(), "Av. Emile Digneffe 26, 4000 Liege, Belgium"
        )

    def test_an_address_that_already_names_a_country_is_left_alone(self) -> None:
        target = LlmCompanyIndexTarget(
            company_name="Sofrimport",
            address="100, rue Chalant, 59790 Ronchin, FRANCE",
            iso_country_code="FR",
        )

        self.assertEqual(
            target.postal_address(), "100, rue Chalant, 59790 Ronchin, FRANCE"
        )

    def test_the_addresss_own_country_wins_over_a_disagreeing_code(self) -> None:
        """The roster holds rows whose country code contradicts the address.

        Appending the code's country would produce '..., France, Germany'. The
        address is the more specific claim, so it keeps the country it has.
        """
        target = LlmCompanyIndexTarget(
            company_name="Some Trading Co",
            address="111/113, Cours Berriat, F-38019 Grenoble, Cedex 1, France",
            iso_country_code="DE",
        )

        self.assertNotIn("Germany", target.postal_address())

    def test_an_unresolvable_country_leaves_the_address_unchanged(self) -> None:
        target = LlmCompanyIndexTarget(
            company_name="Acme", address="1 High Street", iso_country_code="ZZ"
        )

        self.assertEqual(target.postal_address(), "1 High Street")

    def test_no_address_composes_to_empty(self) -> None:
        """Empty is what the signature documents as 'unknown'."""
        target = LlmCompanyIndexTarget(company_name="Acme", iso_country_code="GB")

        self.assertEqual(target.postal_address(), "")

    def test_query_name_folds_the_address_into_the_question(self) -> None:
        target = LlmCompanyIndexTarget(
            company_name="Ets. Abel Falisse",
            address="Av. Emile Digneffe 26, 4000 Liege",
            iso_country_code="BE",
        )

        self.assertEqual(target.query_name(), "Ets. Abel Falisse")
        self.assertEqual(
            target.query_name(use_address=True),
            "Ets. Abel Falisse, Av. Emile Digneffe 26, 4000 Liege, Belgium",
        )

    def test_query_name_without_an_address_is_just_the_name(self) -> None:
        """A company with no address is selectable but unreachable by the switch."""
        target = LlmCompanyIndexTarget(company_name="Acme", iso_country_code="BE")

        self.assertEqual(target.query_name(use_address=True), "Acme")


class TestLlmCompanyIndexModule(unittest.TestCase):
    """Deterministic behaviour of the company-profile lookup module."""

    def setUp(self) -> None:
        """Create a module with mocked DSPy dependencies."""
        self.mock_lm = mock.MagicMock()
        self.mock_predictor = mock.MagicMock()
        self.module = LlmCompanyIndexModule(
            lm=self.mock_lm, predictor=self.mock_predictor
        )

    def test_forward_returns_persistable_row(self) -> None:
        """Forward wraps the validated profile in a row ready to persist."""
        self.mock_predictor.return_value = dspy.Prediction(output=_profile())

        prediction = self.module.forward(
            company_name="Brasserie de Wiltz",
            address="14 rue Joseph Simon, L-9550 Wiltz",
            iso_country_code="LU",
            jurisdication_code="LU",
        )

        row = prediction.output
        self.assertIsInstance(row, LlmCompanyIndexRow)
        self.assertEqual(row.company_name, "Brasserie de Wiltz")
        self.assertEqual(row.llm_model_id, corp_index_profile().model_id)
        self.assertEqual(row.profile.canonical_name, "Brasserie de Wiltz S.E.C.S.")
        self.assertEqual(row.profile.euid, "8434-4121-8815-0614")

    def test_functional_key_comes_from_the_request(self) -> None:
        """The stored name is the roster name, not the canonical one found."""
        self.mock_predictor.return_value = dspy.Prediction(
            output=_profile(canonical_name="Totally Different Holding AG")
        )

        row = self.module.forward(company_name="Brasserie de Wiltz").output

        self.assertEqual(row.company_name, "Brasserie de Wiltz")
        self.assertEqual(row.profile.canonical_name, "Totally Different Holding AG")

    def test_known_attributes_reach_the_predictor_as_hints(self) -> None:
        """Every known roster attribute is offered to the model."""
        self.mock_predictor.return_value = dspy.Prediction(output=_profile())

        self.module.forward(
            company_name="Acme Trading Ltd",
            address="1 High Street, London",
            iso_country_code="GB",
            jurisdication_code="GB-SCT",
            lei="5493008JEWD6T3I5ZJ82",
            handelsregister="HRB6750",
        )

        self.mock_predictor.assert_called_once_with(
            company_name="Acme Trading Ltd",
            address_hint="1 High Street, London",
            country_hint="GB",
            jurisdiction_hint="GB-SCT",
            lei_hint="5493008JEWD6T3I5ZJ82",
            handelsregister_hint="HRB6750",
        )

    def test_use_address_reaches_both_the_question_and_the_hint(self) -> None:
        """The composed address goes to the question AND the hint, not one.

        The question is what a person would ask; the hint is what the signature
        documents. Sending it in only one place would leave the other stating
        something narrower than the truth.
        """
        self.mock_predictor.return_value = dspy.Prediction(output=_profile())

        self.module.forward(
            company_name="Ets. Abel Falisse",
            address="Av. Emile Digneffe 26, 4000 Liege",
            iso_country_code="BE",
            use_address=True,
        )

        self.mock_predictor.assert_called_once_with(
            company_name="Ets. Abel Falisse, Av. Emile Digneffe 26, 4000 Liege, Belgium",
            address_hint="Av. Emile Digneffe 26, 4000 Liege, Belgium",
            country_hint="BE",
            jurisdiction_hint="",
            lei_hint="",
            handelsregister_hint="",
        )

    def test_the_composed_question_never_reaches_the_stored_key(self) -> None:
        """The row stays keyed by the roster name, or the table forks.

        ``company_name`` is the functional key and the join back to the roster.
        A key carrying the address would insert a second row for a company that
        already has one, under a name nothing else in the schema uses.
        """
        self.mock_predictor.return_value = dspy.Prediction(output=_profile())

        row = self.module.forward(
            company_name="Ets. Abel Falisse",
            address="Av. Emile Digneffe 26, 4000 Liege",
            iso_country_code="BE",
            use_address=True,
        ).output

        self.assertEqual(row.company_name, "Ets. Abel Falisse")

    def test_use_address_defaults_off(self) -> None:
        """Without the switch the call is byte-for-byte what it was before."""
        self.mock_predictor.return_value = dspy.Prediction(output=_profile())

        self.module.forward(
            company_name="Ets. Abel Falisse",
            address="Av. Emile Digneffe 26, 4000 Liege",
            iso_country_code="BE",
        )

        self.mock_predictor.assert_called_once_with(
            company_name="Ets. Abel Falisse",
            address_hint="Av. Emile Digneffe 26, 4000 Liege",
            country_hint="BE",
            jurisdiction_hint="",
            lei_hint="",
            handelsregister_hint="",
        )

    def test_roster_na_markers_are_sent_as_unknown(self) -> None:
        """'NA' means "asked, no match" on the roster -- never a claim to relay."""
        self.mock_predictor.return_value = dspy.Prediction(output=_profile())

        self.module.forward(
            company_name="Acme Trading Ltd",
            address=None,
            iso_country_code="NA",
            jurisdication_code=None,
            lei="NA",
            handelsregister="NA",
        )

        self.mock_predictor.assert_called_once_with(
            company_name="Acme Trading Ltd",
            address_hint="",
            country_hint="",
            jurisdiction_hint="",
            lei_hint="",
            handelsregister_hint="",
        )

    def test_missing_profile_values_normalize_to_na(self) -> None:
        """Every spelling of "the model does not know" lands as NA."""
        profile = LlmCompanyIndexProfile(
            canonical_name="Acme Trading Ltd",
            abbreviation="",
            legal_form=None,
            life_cycle_status="unknown",
            lei="not found",
            euid="n/a",
            bris="null",
        )

        self.assertEqual(profile.abbreviation, "NA")
        self.assertEqual(profile.legal_form, "NA")
        self.assertEqual(profile.life_cycle_status, "NA")
        self.assertEqual(profile.lei, "NA")
        self.assertEqual(profile.euid, "NA")
        self.assertEqual(profile.bris, "NA")
        # A field the model omitted entirely is indistinguishable from one it
        # answered "unknown" to.
        self.assertEqual(profile.siren, "NA")
        self.assertEqual(profile.succession, "NA")
        self.assertEqual(profile.generic_corp_id, "NA")

    def test_hedged_descriptive_answers_survive_the_na_fold(self) -> None:
        """The fold catches "I do not know", not "I am not certain".

        The whole point of the descriptive rule is that an approximate or
        successor-based answer beats NA, so the normalizer must not swallow one.
        """
        profile = LlmCompanyIndexProfile(
            canonical_name="Cooperatieve Zuivelfabriek Zuidelijk Westerkwartier",
            established_since="circa 1918",
            life_cycle_status="merged",
            succession="absorbed into DOMO, part of FrieslandCampina since 2008",
        )

        self.assertEqual(profile.established_since, "circa 1918")
        self.assertEqual(profile.life_cycle_status, "merged")
        self.assertIn("FrieslandCampina", profile.succession)

    def test_forward_validates_raw_prediction_dictionary(self) -> None:
        """Raw DSPy output dictionaries are validated into Pydantic models."""
        self.mock_predictor.return_value = dspy.Prediction(
            output={
                "canonical_name": "Aviación y Comercio, S.A.",
                "abbreviation": "AVIACO",
                "legal_form": "S.A.",
                "life_cycle_status": "dissolved",
                "country": "ES",
            }
        )

        row = self.module.forward(company_name="Aviación y Comercio, SA").output

        self.assertIsInstance(row.profile, LlmCompanyIndexProfile)
        self.assertEqual(row.profile.abbreviation, "AVIACO")
        self.assertEqual(row.profile.lei, "NA")

    def test_forward_rejects_empty_company_name(self) -> None:
        """A blank company name fails before the LLM is called."""
        with self.assertRaises(ValidationError):
            self.module.forward(company_name="   ")

        self.mock_predictor.assert_not_called()

    def test_model_id_can_be_pinned_alongside_an_injected_lm(self) -> None:
        """An injected LM is not described by the profile settings.

        Whatever answered is what the row must be labelled with, so a caller
        supplying its own LM supplies the label too rather than inheriting a
        model id that never saw the question.
        """
        module = LlmCompanyIndexModule(
            lm=mock.MagicMock(),
            predictor=self.mock_predictor,
            llm_model_id="some-other-model",
        )
        self.mock_predictor.return_value = dspy.Prediction(output=_profile())

        row = module.forward(company_name="Brasserie de Wiltz").output

        self.assertEqual(row.llm_model_id, "some-other-model")

    def test_row_flattens_to_database_columns(self) -> None:
        """The persistence VO exposes exactly the table's columns."""
        row = LlmCompanyIndexRow(
            company_name="Brasserie de Wiltz",
            llm_model_id="deepseek-v4-pro",
            profile=_profile(),
        )

        columns = row.to_columns()

        self.assertEqual(columns["company_name"], "Brasserie de Wiltz")
        self.assertEqual(columns["llm_model_id"], "deepseek-v4-pro")
        self.assertEqual(columns["euid"], "8434-4121-8815-0614")
        self.assertEqual(columns["established_since"], "1824")
        self.assertEqual(columns["succession"], "brands acquired by Brasserie Nationale in 1999")
        self.assertNotIn("profile", columns)


if __name__ == "__main__":
    unittest.main()
