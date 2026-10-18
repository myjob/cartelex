"""Integration tests for the DeepSeek-backed corporate identifier lookup."""

import unittest
import uuid
from typing import Any

from sqlalchemy import text

from config.llm_factory import corp_index_profile
from config.settings import settings
from pipelines.corp_index.corp_index_module import (
    CorpIndexModule,
    LlmCompanyIndexModule,
)
from pipelines.corp_index.llm_index_repository import LlmCompanyIndexRepository
from pipelines.corp_index.models import LlmCompanyIndexRow

# The one stored profile the refresh test drives. Pinned by id as well as by
# name so a failure names a row a human can open, and so the test can show that
# the upsert updated that row rather than inserting a second one beside it.
REFRESH_COMPANY_NAME = "Ets. Abel Falisse"
REFRESH_ROW_ID = uuid.UUID("5a6d6a22-bdbb-45a2-bd39-cb452320a622")
# The hints as celex_facts.company_investigated2lei2corpidx2hr_vw holds them for
# that company: an address, a country, no jurisdiction, no Handelsregister, and
# fk_lei = 'NA' (GLEIF was asked and said no), which the batch folds to unknown.
REFRESH_ADDRESS_HINT = "Av. Emile Digneffe 26, 4000 Liège"
REFRESH_COUNTRY_HINT = "BE"


@unittest.skip("Live DeepSeek + database integration; disabled by filename until 2026-09-17, now an explicit skip. Remove the decorator to run it deliberately (real spend, tens of seconds per test).")
class TestCorpIndexModuleIntegration(unittest.TestCase):
    """Live integration tests for corporate identifier lookup."""

    def setUp(self) -> None:
        """Skip live cloud tests unless explicitly enabled.

        Raises:
            unittest.SkipTest: If cloud tests are not enabled or configured.
        """

        if not settings.LLM_MODEL_CLOUD_API_KEY.strip():
            self.skipTest("settings.LLM_MODEL_CLOUD_API_KEY is empty.")
        if settings.LLM_MODEL_CLOUD_API_KEY == "deepseek":
            self.skipTest(
                "settings.LLM_MODEL_CLOUD_API_KEY still has placeholder value."
            )

  
    def test_Maersk_Air_A_S_identifier_lookup(self) -> None:
        """Resolve Maersk Air A/S to the expected identifier row."""
        module = CorpIndexModule()

        prediction = module(
            corp_canoncical="Maersk Air A/S",
        )

        _record = prediction.output.records[0]
        #!! unrealiable return from opencorp
        # self.assertEqual(record.BRIS, "DK-CVR-14752972")
        
        # self.assertEqual(record.LEI, "213800X6FZ3V5K1TOQ87")  # parent Maersk A/S
        # A/S MAERSK AVIATION HOLDING	CVR (Danish)	22765310

    @unittest.expectedFailure
    def test_scottish_newcastle_limited_identifier_lookup(self) -> None:
        """Resolve SCOTTISH & NEWCASTLE LIMITED to the expected identifier row."""
        module = CorpIndexModule()

        prediction = module(
            corp_canoncical="SCOTTISH & NEWCASTLE LIMITED",
        )

        record = prediction.output.records[0]
        self.assertEqual(record.corp_canoncical, "SCOTTISH & NEWCASTLE LIMITED")
        self.assertEqual(record.BRIS, "UKSC051104")
        self.assertEqual(record.EUID, "GBR_SC030556")
        self.assertEqual(record.LEI, "213800X6FZ3V5K1TOQ87")
        #self.assertEqual(record.state, "active")

    @unittest.expectedFailure
    def test_United_International_Pictures_BV_identifier_lookup(self) -> None:
        """Resolve United International Pictures BV to the expected identifier row.
        fails with deepseek

Based on the available search results, it was only possible to confidently identify the SIREN and SIRET numbers for the specific entity you asked about, United International Pictures BV.

Here is a summary of the information found:

SIREN: 434 425 021 

SIRET: 434 425 021 00012 

BRIS, EUID, SRIEN, and LEI: No information was found in the search results for these specific identifiers for this company.

It is important to note that this information corresponds to a French establishment of the company and may represent one part of a larger, multinational organization .

📝 A Note on the Company's Structure
The search results show that "United International Pictures BV" is a Dutch-registered company (besloten vennootschap) and is part of a larger group with entities in many countries . For instance, another entity identified in the results is a separate company registered in the UK with the number 01683912 . This means the identifiers for the French branch will not be the same as those for the parent company in the Netherlands or in other countries.

If you need a specific international identifier like an LEI (Legal Entity Identifier), the company's unique identifiers will depend on the exact legal entity and the country where it is registered.

I hope this information is helpful for your needs. If you require more details about a specific international branch of United International Pictures, providing the country might help me find more specific information.

        """
        module = CorpIndexModule()

        prediction = module(
            corp_canoncical="United International Pictures BV",
        )

        record = prediction.output.records[0]
        self.assertEqual(record.corp_canoncical, "United International Pictures BV")
        # self.assertEqual(record.description, "'Film distribution joint venture'" ) 
        self.assertEqual(record.BRIS, "NA")
        self.assertEqual(record.EUID, "PLKRS.0000050620")
        self.assertEqual(record.SIREN, "434425021")
        self.assertEqual(record.LEI, "213800PC85O8BJ7DU398")
        # https://www.google.com/search?q=BRIS%2C+EUID%2C+LEI%2C+SIREN+of+%22United+International+Pictures+BV%22&sca_esv=f27198b4d2287e35&sxsrf=ANbL-n44lCZGxp7_SSVXzp8YS9Dn8ojvtw%3A1777447949555&ei=DbTxaY7FIZ6Yi-gPzb-2-AU&biw=1825&bih=1135&ved=0ahUKEwjO_47NxZKUAxUezAIHHc2fDV8Q4dUDCBE&uact=5&oq=BRIS%2C+EUID%2C+LEI%2C+SIREN+of+%22United+International+Pictures+BV%22&gs_lp=Egxnd3Mtd2l6LXNlcnAiPEJSSVMsIEVVSUQsIExFSSwgU0lSRU4gb2YgIlVuaXRlZCBJbnRlcm5hdGlvbmFsIFBpY3R1cmVzIEJWIjIEECMYJ0ilG1C_Elj0F3ABeACQAQCYAU2gAZEBqgEBMrgBA8gBAPgBAZgCA6ACpwHCAg4QABiABBiKBRiGAxiwA5gDAIgGAZAGApIHATOgB8gHsgcBMrgHnwHCBwMyLTPIBw2ACAE&sclient=gws-wiz-serp


    @unittest.expectedFailure
    def test_aeg_telefunken_limited_identifier_lookup(self) -> None:
        """Resolve AEG-Telefunken to the expected identifier row.
        fails with deepseek
        """
        module = CorpIndexModule()

        prediction = module(
            corp_canoncical="AEG-Telefunken",
        )

        record = prediction.output.records[0]
        self.assertEqual(record.corp_canoncical, "AEG-Telefunken")
        self.assertEqual(record.BRIS, "HRB6750")
        self.assertEqual(record.EUID, "DEK1101R.HRB6750")
        self.assertEqual(record.LEI, "NA")
        self.assertEqual(record.state, "retired")


@unittest.skip("Live DeepSeek + database integration; disabled by filename until 2026-09-17, now an explicit skip. Remove the decorator to run it deliberately (real spend, tens of seconds per test).")
class TestLlmCompanyIndexModuleIntegration(unittest.TestCase):
    """Live tests for the company profiles written to llm_index_companies.

    These are the companies the deterministic pipeline gave up on -- every one
    of them carries ``fk_opencorp_idx = 'NA'`` on the roster. The contract tests
    below (shape, key, hint handling) must hold for every company; the recall
    tests after them pin specific answers and are expected to move as the model
    changes, which is the point of recording ``llm_model_id`` per row.
    """

    def setUp(self) -> None:
        """Skip live cloud tests unless explicitly enabled.

        Raises:
            unittest.SkipTest: If cloud tests are not enabled or configured.
        """
        if not settings.LLM_MODEL_CLOUD_API_KEY.strip():
            self.skipTest("settings.LLM_MODEL_CLOUD_API_KEY is empty.")
        if settings.LLM_MODEL_CLOUD_API_KEY == "deepseek":
            self.skipTest(
                "settings.LLM_MODEL_CLOUD_API_KEY still has placeholder value."
            )
        self.module = LlmCompanyIndexModule()

    def test_profile_is_complete_and_attributable(self) -> None:
        """Every field comes back populated, keyed and attributed.

        The batch upserts whatever this returns, so the row must be complete
        (``NA`` counts -- an empty string does not), keyed by the name that was
        asked about, and stamped with the model that answered.
        """
        prediction = self.module(
            company_name="Aviación y Comercio, SA (AVIACO)",
            iso_country_code="ES",
            jurisdication_code="ES",
        )

        row = prediction.output
        self.assertIsInstance(row, LlmCompanyIndexRow)
        self.assertEqual(row.company_name, "Aviación y Comercio, SA (AVIACO)")
        self.assertEqual(row.llm_model_id, corp_index_profile().model_id)
        for field_name, value in row.profile.model_dump().items():
            with self.subTest(field=field_name):
                self.assertTrue(value.strip(), f"{field_name} came back empty")

    def test_confirmed_lei_hint_is_reproduced_verbatim(self) -> None:
        """A GLEIF-confirmed LEI is authoritative and must survive the round trip.

        The hint is the one input the model is told not to second-guess: it was
        already matched against the register, so an answer that "corrects" it
        would replace a fact with a recollection.
        """
        prediction = self.module(
            company_name="Scottish & Newcastle Limited",
            iso_country_code="GB",
            lei="5493008JEWD6T3I5ZJ82",
        )

        self.assertEqual(prediction.output.profile.lei, "5493008JEWD6T3I5ZJ82")

    def test_address_hint_disambiguates_a_local_company(self) -> None:
        """The decision's address places a company the name alone does not.

        'Brasserie de Wiltz' is a small Luxembourg brewery that OpenCorporates
        never resolved; the address is what keeps the answer in Luxembourg.
        """
        prediction = self.module(
            company_name="Brasserie de Wiltz",
            address="14 rue Joseph Simon, L-9550 Wiltz, Grand Duchy of Luxembourg",
            iso_country_code="LU",
            jurisdication_code="LU",
        )

        profile = prediction.output.profile
        # with a custom message
        self.assertIn(profile.country, ("LU", "Luxembourg"),
              f"unexpected country {profile.country!r}")
        self.assertIn("wiltz", profile.canonical_name.lower())

    def test_defunct_airline_is_reported_as_defunct(self) -> None:
        """A company that ceased trading must not come back as active.

        AVIACO stopped flying in 1999. Life cycle status is the field the fact
        pipeline actually needs from this table -- no register in the pipeline
        supplies it -- so a wrong 'active' here is worse than an 'NA'.
        """
        prediction = self.module(
            company_name="Aviación y Comercio, SA (AVIACO)",
            iso_country_code="ES",
        )

        profile = prediction.output.profile
        self.assertIn("AVIACO", profile.abbreviation.upper())
        self.assertNotEqual(profile.life_cycle_status.lower(), "active")

    def test_unknown_identifiers_are_na_not_invented(self) -> None:
        """A company with no French registration must not acquire a SIREN.

        The failure mode this pins is the expensive one: a fabricated
        identifier is indistinguishable from a recalled one once it is in the
        table, whereas 'NA' is honest and cheap to re-ask.
        """
        prediction = self.module(
            company_name="Aannemersvereniging 'de Rijnstreek'",
            address="Thomas Mannplaats 307, NL-3069 NJ Rotterdam",
            iso_country_code="NL",
            jurisdication_code="NL",
        )

        profile = prediction.output.profile
        self.assertEqual(profile.siren, "NA")
        self.assertEqual(profile.handelsregister, "NA")

    @unittest.expectedFailure
    def test_brasserie_de_wiltz_euid_lookup(self) -> None:
        """Recall the EUID of Brasserie de Wiltz.

        Recorded in docs/Design Corp Index.md from a manual DeepSeek session;
        the model does not reproduce it through the structured signature (it
        answers NA, and names the legal form S.A. rather than S.E.C.S.). Kept as
        an expected failure so the day a model does recall it is visible.
        """
        prediction = self.module(
            company_name="Brasserie de Wiltz",
            address="14 rue Joseph Simon, L-9550 Wiltz, Grand Duchy of Luxembourg",
            iso_country_code="LU",
            jurisdication_code="LU",
        )

        profile = prediction.output.profile
        self.assertEqual(profile.euid, "8434-4121-8815-0614")
        self.assertEqual(profile.canonical_name, "Brasserie De Wiltz S.E.C.S.")

    @unittest.expectedFailure
    def test_ac_nielsen_italia_bris_lookup(self) -> None:
        """Recall the historical BRIS record of A. C. Nielsen Italia SpA.

        Also from the manual session recorded in the design doc (BRIS
        1513375/1996, a historical record). Expected to fail through the
        signature for the same reason as the EUID case above.
        """
        prediction = self.module(
            company_name="A. C. Nielsen Italia SpA",
            iso_country_code="IT",
            jurisdication_code="IT",
        )

        self.assertEqual(prediction.output.profile.bris, "1513375/1996")


@unittest.skip("Live DeepSeek + database integration; disabled by filename until 2026-09-17, now an explicit skip. Remove the decorator to run it deliberately (real spend, tens of seconds per test).")
class TestLlmCompanyIndexRefreshIntegration(unittest.TestCase):
    """Live refresh of one stored profile, end to end against the database.

    Every test above stops at the model's answer. This one runs the path
    ``Main_LLM_corp_index_searcher.py --refresh`` runs -- ask, then upsert into
    ``celex_facts.llm_index_companies`` -- because what it asks about is the
    write rather than the answer: does a second pass update the stored row in
    place, does it reach columns added after that row was first written, and
    does it leave the two columns the batch does not own alone.

    ``Ets. Abel Falisse`` is the case that motivated the change. Profiled on
    2026-08-01 by ``deepseek-chat`` under the old single blanket "NA is a
    correct answer" rule, it stored ``NA`` for legal form, life cycle, products
    and geo market while a DeepSeek chat session returns a Belgian CBE
    enterprise number, a 1948 founding date and an SA/NV legal form for the same
    company.

    One live call serves the whole class, made once in ``setUpClass``: it costs
    tens of seconds against the reasoning model and every assertion below reads
    the same answer.

    This test WRITES to ``settings.DATABASE_URL``. It overwrites exactly one
    row -- the one the batch overwrites on every ``--refresh`` anyway -- and
    nothing else.
    """

    repository: LlmCompanyIndexRepository
    row: LlmCompanyIndexRow
    before: dict[str, Any]
    after: dict[str, Any]
    stored_row_count: int

    @classmethod
    def setUpClass(cls) -> None:
        """Refresh the stored profile once, keeping the row before and after.

        Raises:
            unittest.SkipTest: If the cloud key is absent or a placeholder, or
                if the company has no stored row to refresh.
        """
        if not settings.LLM_MODEL_CLOUD_API_KEY.strip():
            raise unittest.SkipTest("settings.LLM_MODEL_CLOUD_API_KEY is empty.")
        if settings.LLM_MODEL_CLOUD_API_KEY == "deepseek":
            raise unittest.SkipTest(
                "settings.LLM_MODEL_CLOUD_API_KEY still has placeholder value."
            )

        cls.repository = LlmCompanyIndexRepository()
        # The stored row predates established_since, succession and the generic
        # id pair, so the columns have to exist before the upsert names them.
        # This is the same call the batch makes at startup.
        cls.repository.ensure_tables()

        stored_before = cls._stored_row()
        if stored_before is None:
            raise unittest.SkipTest(
                f"{REFRESH_COMPANY_NAME!r} has no row in llm_index_companies; "
                "this test refreshes an existing profile rather than creating "
                "one."
            )
        cls.before = stored_before

        prediction = LlmCompanyIndexModule()(
            company_name=REFRESH_COMPANY_NAME,
            address=REFRESH_ADDRESS_HINT,
            iso_country_code=REFRESH_COUNTRY_HINT,
        )
        cls.row = prediction.output
        cls.repository.upsert_company(cls.row)

        stored_after = cls._stored_row()
        assert stored_after is not None, "the upsert wrote no row"
        cls.after = stored_after

    @classmethod
    def _stored_row(cls) -> dict[str, Any] | None:
        """Return the stored profile for the refreshed company.

        Also records how many rows carry the name, which is what distinguishes
        an update in place from an insert beside the old row.

        Returns:
            The row as a column mapping, or None when the company is absent.
        """
        with cls.repository.engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        f"SELECT * FROM {settings.DB_SCHEMA}.llm_index_companies "
                        "WHERE company_name = :company_name"
                    ),
                    {"company_name": REFRESH_COMPANY_NAME},
                )
                .mappings()
                .all()
            )
        cls.stored_row_count = len(rows)
        return dict(rows[0]) if rows else None

    def test_the_row_is_updated_in_place(self) -> None:
        """A refresh overwrites the stored profile; it does not accumulate rows.

        ``company_name`` is the functional key, so the surrogate id and the
        creation timestamp must survive the write and only ``updated_at`` may
        move. A second row under the same name would break every join that
        assumes one profile per company.
        """
        self.assertEqual(self.stored_row_count, 1)
        self.assertEqual(self.after["id"], REFRESH_ROW_ID)
        self.assertEqual(self.after["created_at"], self.before["created_at"])
        self.assertGreater(self.after["updated_at"], self.before["updated_at"])

    def test_columns_added_after_the_row_was_written_are_filled(self) -> None:
        """``ensure_tables`` adds the column; only the re-ask fills it.

        These six columns did not exist when this row was first written, so on
        the first run of this test they were NULL going in. NULL coming out
        would mean the upsert never named them -- the silent failure
        ``_PROFILE_COLUMNS`` exists to prevent, where the row is written, the
        batch reports success, and one column simply never changes.
        """
        added_columns = (
            "established_since",
            "succession",
            "generic_corp_id",
            "generic_corp_registrar",
        )

        for column_name in added_columns:
            with self.subTest(column=column_name):
                self.assertIsNotNone(
                    self.after[column_name],
                    f"{column_name} is still NULL after the refresh",
                )

    def test_every_non_key_column_is_refreshed(self) -> None:
        """The whole answer is restated, and its author is restamped.

        Every field the model just answered must be what the table now holds,
        and the row must be attributed to the model that answered. Deliberately
        *not* asserted: that ``llm_model_id`` changed. It did on the first run
        of this test, because the stored row had been written by
        ``deepseek-chat`` -- but that is a one-off observation about a
        migration, not a contract, and asserting it made the test pass once and
        fail on every re-run.
        """
        self.assertEqual(self.after["llm_model_id"], corp_index_profile().model_id)
        for column_name, value in self.row.profile.model_dump().items():
            with self.subTest(column=column_name):
                self.assertEqual(self.after[column_name], value)

    def test_the_country_hint_is_respected(self) -> None:
        """The roster places this company in Belgium and the answer must agree.

        Deliberately accepts both spellings. The stored row held the ISO code
        ``BE``; the reasoning model answers ``Belgium``, and nothing in the
        field description asks for one form over the other -- see the note in
        ISSUES.md, because a consumer joining this column on ISO codes silently
        loses the row.
        """
        self.assertIn(self.after["country"].strip().upper(), {"BE", "BELGIUM"})

    def test_the_canonical_name_is_expanded(self) -> None:
        """The abbreviated roster name is resolved to the full legal name.

        ``Ets.`` is an abbreviation of ``Établissements``. Expanding it is the
        one thing this company's profile reliably gains from the model, and it
        is what makes the row searchable in a register that spells names out.
        """
        self.assertIn('Falisse', self.after["canonical_name"])

    @unittest.expectedFailure
    def test_belgian_enterprise_number_is_recalled(self) -> None:
        """Recall the CBE enterprise number of Ets. Abel Falisse.

        A DeepSeek chat session returns ``BE 0402.398.659``, registered with the
        Crossroads Bank for Enterprises. Through the structured signature the
        model answers ``NA`` for both halves of the pair, on the reasoning model
        with the split NA rule in place -- which is the evidence that the chat
        session's answer came from its web search rather than from recall, since
        that session says so itself ("The search results do not provide...").

        Kept as an expected failure so the day a model, or a search leg, does
        produce it is visible rather than silent.
        """
        self.assertIn("0402.398.659", self.after["generic_corp_id"])
        self.assertNotEqual(self.after["generic_corp_registrar"], "NA")

    @unittest.expectedFailure
    def test_founding_year_is_recalled(self) -> None:
        """Recall that the company was founded in 1948.

        The same chat session dates it to 23 October 1948;
        ``established_since`` comes back ``NA``. Pinned for the same reason as
        the enterprise number above.

        ``legal_form`` is deliberately not asserted here. Two calls twenty
        minutes apart answered it ``NA`` and ``SA/NV (probable; the decision
        uses the trading style 'Ets.')`` -- so it is not stably absent, but the
        answer that does arrive is an inference from the name rather than
        recall, which :meth:`test_descriptive_answers_are_hedged_not_recalled`
        is about.
        """
        self.assertIn("1948", self.after["established_since"])

    def test_descriptive_answers_are_hedged_not_recalled(self) -> None:
        """Record what the permissive descriptive rule actually buys here.

        This is a characterization test, not a contract: it pins the behaviour
        so a change in it is visible. Under the split NA rule the descriptive
        fields stop answering ``NA`` and start answering things like ``not
        established; likely a Belgian trading or manufacturing concern in the
        Liège area`` -- which is an inference from the country and the name, not
        knowledge of this company. A fill-rate rise measured over the table is
        therefore not by itself evidence of better answers, which is the whole
        reason ISSUES.md asks for a gold set before any of this is believed.

        The assertion is only that the fields are populated at all. Whether a
        hedge is worth more than ``NA`` for the downstream fact pipeline is the
        open question; recording it here is what makes it answerable.
        """
        for column_name in ("legal_form", "life_cycle_status", "products_services"):
            with self.subTest(column=column_name):
                self.assertTrue(self.after[column_name].strip())


if __name__ == "__main__":
    unittest.main()
