"""Unit tests for the OpenCorporates roster-resolution batch.

Mocks live only at the boundaries: the OpenCorporates client and the two
repositories. No HTTP or DB access occurs; ``time.sleep`` is patched so the
throttle does not slow the suite.

The subject is the resolution ladder loop. A single-shot search used to close a
roster row permanently on a clean zero-hit response, and these tests pin the
behaviours that replaced it: stop at the first rung that accepts something, keep
walking past a rung that came back too broad, and distinguish "no registry has
this company" from "hits came back but none were convincingly it".
"""

import os
import sys
import unittest
import uuid
from unittest import mock

# Provide a dummy token before importing settings-dependent modules so the suite
# runs even when ``opencorporates_api_token`` is absent from the env.
os.environ.setdefault("opencorporates_api_token", "dummy")

# Ensure src is on the path (src/batch/tests -> src).
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from batch.Main_ingest_opencorp_index_companies import (  # noqa: E402
    MARKER_AMBIGUOUS,
    MARKER_LINKED,
    MARKER_NO_HIT,
    OpenCorpIndexIngestor,
    _build_parser,
    _policy_from_args,
)
from config.settings import settings  # noqa: E402
from pipelines.corp_index.opencorp_client import (  # noqa: E402
    OpenCorpApiError,
    OpenCorpAuthError,
    OpenCorpRateLimitError,
)
from pipelines.corp_index.opencorp_models import (  # noqa: E402
    OpenCorpCompany,
    OpenCorpSearchResult,
)
from pipelines.corp_index.opencorp_resolution_ladder import (  # noqa: E402
    RUNG_VERBATIM,
    RUNG_VERBATIM_ANY,
    OpenCorpTarget,
    RelaxationPolicy,
)


def _target(
    name: str = "Acme Trading Ltd",
    country: str | None = "GB",
    jurisdiction: str | None = "GB",
    **overrides: object,
) -> OpenCorpTarget:
    """Build a roster target with sensible defaults.

    Args:
        name: Roster company name.
        country: ISO 3166-1 country code.
        jurisdiction: Canonical jurisdiction code, or None when unresolved.
        **overrides: Any further :class:`OpenCorpTarget` field.

    Returns:
        The target.
    """
    return OpenCorpTarget(
        company_name=name,
        iso_country_code=country,
        jurisdication_code=jurisdiction,
        **overrides,
    )


def _company(name: str, number: str = "1", jurisdiction: str = "gb") -> OpenCorpCompany:
    """Build a minimal OpenCorporates hit.

    Args:
        name: Registered company name.
        number: Registry company number.
        jurisdiction: OpenCorporates jurisdiction code.

    Returns:
        The hit.
    """
    return OpenCorpCompany(
        name=name, company_number=number, jurisdiction_code=jurisdiction
    )


def _search_result(
    query: str,
    *,
    from_cache: bool = False,
    fetched_pages: int = 1,
    total_count: int = 1,
    companies: list[OpenCorpCompany] | None = None,
    jurisdiction_code: str | None = "gb",
) -> OpenCorpSearchResult:
    """Build a search result with a known normalized_query.

    Args:
        query: Raw query text (also lower/collapsed for normalized_query).
        from_cache: Whether the result reports a cache hit.
        fetched_pages: Pages the search reports fetching.
        total_count: Hits the API reports in total.
        companies: The hit models; defaults to one whose name equals the query.
        jurisdiction_code: Jurisdiction the search was filtered by.

    Returns:
        A populated :class:`OpenCorpSearchResult`.
    """
    return OpenCorpSearchResult(
        query_text=query,
        normalized_query=" ".join(query.split()).casefold(),
        jurisdiction_code=jurisdiction_code,
        fetched_pages=fetched_pages,
        total_pages=fetched_pages,
        total_count=total_count,
        companies=[_company(query)] if companies is None else companies,
        from_cache=from_cache,
    )


class TestBuildParser(unittest.TestCase):
    """CLI argument wiring."""

    def test_all_corps_defaults_to_false(self) -> None:
        """Omitting --all-corps leaves the namespace attribute set to False."""
        args = _build_parser().parse_args(["--iso-country-code", "GB"])

        self.assertFalse(args.all_corps)

    def test_all_corps_flag_sets_true(self) -> None:
        """--all-corps is a real flag that main() can read as args.all_corps."""
        args = _build_parser().parse_args(["--iso-country-code", "GB", "--all-corps"])

        self.assertTrue(args.all_corps)

    def test_retry_flags_default_to_false(self) -> None:
        """Reopening already-marked rows must be opt-in, never the default."""
        args = _build_parser().parse_args([])

        self.assertFalse(args.retry_na)
        self.assertFalse(args.retry_ambiguous)

    def test_retry_flags_set_true(self) -> None:
        args = _build_parser().parse_args(["--retry-na", "--retry-ambiguous"])

        self.assertTrue(args.retry_na)
        self.assertTrue(args.retry_ambiguous)


class TestTargetSelection(unittest.TestCase):
    """How run() asks the roster repository for work."""

    def setUp(self) -> None:
        """Build an ingestor over three mock collaborators."""
        self.client = mock.MagicMock()
        self.opencorp_repo = mock.MagicMock()
        self.company_repo = mock.MagicMock()
        self.company_repo.select_opencorp_targets.return_value = []
        self.ingestor = OpenCorpIndexIngestor(
            client=self.client,
            opencorp_repo=self.opencorp_repo,
            company_repo=self.company_repo,
        )

    def test_no_retry_flags_means_no_retry_markers(self) -> None:
        """Without a flag only never-looked-up rows are selected."""
        self.ingestor.run(iso_country_code="GB")

        self.company_repo.select_opencorp_targets.assert_called_once_with(
            iso_country_code="GB",
            all_corps=False,
            retry_markers=(),
            include_missing_country=False,
        )

    def test_retry_na_reopens_the_na_marker(self) -> None:
        """Nothing else in the codebase reopens a row closed as 'NA'."""
        self.ingestor.run(retry_na=True)

        self.company_repo.select_opencorp_targets.assert_called_once_with(
            iso_country_code=None,
            all_corps=False,
            retry_markers=(MARKER_NO_HIT,),
            include_missing_country=False,
        )

    def test_both_retry_flags_reopen_both_markers(self) -> None:
        self.ingestor.run(all_corps=True, retry_na=True, retry_ambiguous=True)

        self.company_repo.select_opencorp_targets.assert_called_once_with(
            iso_country_code=None,
            all_corps=True,
            retry_markers=(MARKER_NO_HIT, MARKER_AMBIGUOUS),
            include_missing_country=False,
        )

    def test_country_less_rows_are_excluded_by_default(self) -> None:
        """The 829 rows with no country stay out unless asked for explicitly."""
        self.ingestor.run()

        _, kwargs = self.company_repo.select_opencorp_targets.call_args
        self.assertFalse(kwargs["include_missing_country"])

    def test_country_less_rows_are_selected_when_requested(self) -> None:
        """A relaxed run reaches the roster's largest unresolved group."""
        self.ingestor.run(include_missing_country=True)

        _, kwargs = self.company_repo.select_opencorp_targets.call_args
        self.assertTrue(kwargs["include_missing_country"])

    def test_limit_truncates_the_target_list(self) -> None:
        self.company_repo.select_opencorp_targets.return_value = [
            _target("A Trading Ltd"),
            _target("B Trading Ltd"),
            _target("C Trading Ltd"),
        ]

        summary = self.ingestor.run(iso_country_code="GB", limit=2, dry_run=True)

        self.assertEqual(2, summary["targets"])


class TestResolutionLadderLoop(unittest.TestCase):
    """The rung walk: when it stops, what it links, how it marks."""

    def setUp(self) -> None:
        """Build an ingestor over three mock collaborators and patch sleep."""
        self.client = mock.MagicMock()
        self.opencorp_repo = mock.MagicMock()
        self.company_repo = mock.MagicMock()
        self.roster_ids = [uuid.uuid4()]
        self.company_repo.roster_ids_for_company.return_value = self.roster_ids
        self.opencorp_repo.today_call_count.return_value = 0
        self.opencorp_repo.record_calls.return_value = 1
        self.ingestor = OpenCorpIndexIngestor(
            client=self.client,
            opencorp_repo=self.opencorp_repo,
            company_repo=self.company_repo,
        )
        sleep_patcher = mock.patch(
            "batch.Main_ingest_opencorp_index_companies.time.sleep"
        )
        self.mock_sleep = sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def _run_one(self, target: OpenCorpTarget, **kwargs) -> dict:
        """Run the batch over exactly one target.

        Args:
            target: The roster target to resolve.
            **kwargs: Extra keyword arguments for ``run``.

        Returns:
            The run summary.
        """
        self.company_repo.select_opencorp_targets.return_value = [target]
        return self.ingestor.run(iso_country_code=target.iso_country_code, **kwargs)

    def test_stops_at_the_first_accepting_rung(self) -> None:
        """A verbatim hit must not cost the rest of the ladder in API calls."""
        self.client.search_companies.return_value = _search_result("Acme Trading Ltd")
        self.opencorp_repo.company_row_ids.return_value = [uuid.uuid4()]

        summary = self._run_one(_target())

        self.assertEqual(1, self.client.search_companies.call_count)
        self.assertEqual(1, summary["linked"])

    def test_links_carry_the_rung_that_found_them(self) -> None:
        """A link is unauditable without the rung that produced it."""
        self.client.search_companies.return_value = _search_result("Acme Trading Ltd")
        self.opencorp_repo.company_row_ids.return_value = [uuid.uuid4()]

        self._run_one(_target())

        links = self.company_repo.insert_opencorp_links.call_args.args[0]
        self.assertEqual(1, len(links))
        self.assertEqual(RUNG_VERBATIM, links[0].resolution_rung)
        self.assertEqual("Acme Trading Ltd", links[0].resolution_query)
        self.assertEqual("gb", links[0].resolution_jurisdiction)
        self.assertEqual(self.roster_ids[0], links[0].fk_company_investigated)

    def test_an_offshore_registration_is_found_by_the_unfiltered_rung(self) -> None:
        """The Halkis case: stored GR, actually registered in Cyprus.

        The jurisdiction-filtered rung returns nothing because the company is
        not registered where its headquarters are. The unfiltered rung finds it
        under a whole-word prefix name match, which the scorer accepts.
        """
        opencorp_id = uuid.uuid4()
        halkis_hit = _company("HALKIS CEMENT TRANSPORTERS CO. LIMITED", "HE12345", "cy")
        self.client.search_companies.side_effect = [
            _search_result("Halkis", total_count=0, companies=[]),
            _search_result(
                "Halkis",
                total_count=1,
                companies=[halkis_hit],
                jurisdiction_code=None,
            ),
        ]
        self.opencorp_repo.company_row_ids_by_key.return_value = {
            ("cy", "HE12345"): opencorp_id
        }

        summary = self._run_one(
            _target("Halkis", country="GR", jurisdiction="GR", decision_year=1994)
        )

        self.assertEqual(1, summary["linked"])
        links = self.company_repo.insert_opencorp_links.call_args.args[0]
        self.assertEqual(RUNG_VERBATIM_ANY, links[0].resolution_rung)
        self.assertIsNone(links[0].resolution_jurisdiction)
        self.assertEqual(opencorp_id, links[0].fk_opencorp_index_company)
        self.company_repo.set_opencorp_idx.assert_called_once_with(
            [(self.roster_ids[0], MARKER_LINKED)]
        )

    def test_an_over_cap_rung_does_not_end_the_ladder(self) -> None:
        """Too broad says nothing about whether a narrower rung will resolve."""
        self.client.search_companies.side_effect = [
            _search_result(
                "Acme Trading Ltd",
                total_count=settings.OPENCORP_HIT_CAP_JURISDICTION + 1,
            ),
            _search_result(
                "Acme Trading Ltd", total_count=0, companies=[], jurisdiction_code=None
            ),
            _search_result("Acme Trading"),
        ]
        self.opencorp_repo.company_row_ids.return_value = [uuid.uuid4()]

        summary = self._run_one(_target())

        self.assertEqual(3, self.client.search_companies.call_count)
        self.assertEqual(1, summary["linked"])

    def test_hits_that_none_match_mark_ambiguous_and_link_nothing(self) -> None:
        """'Hits, none convincing' is not the same answer as 'not found'."""
        self.client.search_companies.side_effect = [
            _search_result("Halkis", total_count=0, companies=[]),
            _search_result(
                "Halkis",
                total_count=1,
                companies=[_company("GESTION HALKIS INC.", "9", "ca_qc")],
                jurisdiction_code=None,
            ),
        ]
        self.opencorp_repo.company_row_ids_by_key.return_value = {
            ("ca_qc", "9"): uuid.uuid4()
        }

        summary = self._run_one(_target("Halkis", country="GR", jurisdiction="GR"))

        self.assertEqual(1, summary["ambiguous"])
        self.assertEqual(0, summary["linked"])
        self.company_repo.insert_opencorp_links.assert_not_called()
        self.company_repo.set_opencorp_idx.assert_called_once_with(
            [(self.roster_ids[0], MARKER_AMBIGUOUS)]
        )

    def test_nothing_anywhere_marks_na(self) -> None:
        """'NA' is reserved for a company no rung found anything for."""
        self.client.search_companies.return_value = _search_result(
            "Acme Trading Ltd", total_count=0, companies=[]
        )

        summary = self._run_one(_target())

        self.assertEqual(1, summary["na"])
        self.company_repo.set_opencorp_idx.assert_called_once_with(
            [(self.roster_ids[0], MARKER_NO_HIT)]
        )

    def test_a_name_too_short_to_search_is_left_unmarked(self) -> None:
        """Declining to ask must not be recorded as having asked and missed."""
        summary = self._run_one(_target("RS", jurisdiction="GB"))

        self.client.search_companies.assert_not_called()
        self.company_repo.set_opencorp_idx.assert_not_called()
        self.assertEqual(1, summary["skipped_no_ladder"])

    def test_a_cached_rung_does_not_increment_the_daily_counter(self) -> None:
        """Retrying the 3,548 closed rows must not re-charge for rung 1."""
        self.client.search_companies.return_value = _search_result(
            "Acme Trading Ltd", from_cache=True
        )
        self.opencorp_repo.company_row_ids.return_value = [uuid.uuid4()]

        self._run_one(_target())

        self.opencorp_repo.record_calls.assert_not_called()

    def test_an_http_error_advances_to_the_next_rung(self) -> None:
        """A 400 is a verdict on one query text, not on the company."""
        self.client.search_companies.side_effect = [
            OpenCorpApiError("HTTP 400", status_code=400),
            _search_result(
                "Acme Trading Ltd",
                jurisdiction_code=None,
                companies=[_company("Acme Trading Ltd")],
            ),
        ]
        self.opencorp_repo.company_row_ids_by_key.return_value = {
            ("gb", "1"): uuid.uuid4()
        }

        summary = self._run_one(_target())

        self.assertEqual(2, self.client.search_companies.call_count)
        self.assertEqual(1, summary["linked"])

    def test_a_target_whose_every_rung_failed_is_left_unmarked(self) -> None:
        """'NA' would permanently close a row on a question never actually asked.

        A single failed search always left its company unmarked; the ladder loop
        must keep that contract when every rung fails, not fall through to the
        no-hits marker.
        """
        self.client.search_companies.side_effect = OpenCorpApiError(
            "HTTP 400", status_code=400
        )

        summary = self._run_one(_target())

        self.company_repo.set_opencorp_idx.assert_not_called()
        self.assertEqual(1, summary["skipped"])
        self.assertEqual(0, summary["na"])
        self.assertEqual(0, summary["processed"])

    def test_a_target_answered_by_one_rung_is_still_marked(self) -> None:
        """One clean zero-hit answer is a real 'NA', even after earlier errors."""
        self.client.search_companies.side_effect = [
            OpenCorpApiError("HTTP 400", status_code=400),
            _search_result(
                "Acme Trading Ltd",
                total_count=0,
                companies=[],
                jurisdiction_code=None,
            ),
            _search_result("Acme Trading", total_count=0, companies=[]),
            _search_result(
                "Acme Trading", total_count=0, companies=[], jurisdiction_code=None
            ),
        ]

        summary = self._run_one(_target())

        self.assertEqual(0, summary["skipped"])
        self.assertEqual(1, summary["na"])
        self.company_repo.set_opencorp_idx.assert_called_once_with(
            [(self.roster_ids[0], MARKER_NO_HIT)]
        )

    def test_a_rejected_token_stops_the_run(self) -> None:
        """Every remaining search would fail identically."""
        self.client.search_companies.side_effect = OpenCorpAuthError(
            "bad token", status_code=401
        )

        summary = self._run_one(_target())

        self.assertTrue(summary["stopped_on_api_error"])
        self.company_repo.set_opencorp_idx.assert_not_called()

    def test_a_throttle_streak_stops_the_run(self) -> None:
        """A throttle applies to every later request, so moving on cannot
        escape it."""
        self.client.search_companies.side_effect = OpenCorpRateLimitError(
            "throttled", status_code=403
        )

        summary = self._run_one(_target(), max_consecutive_api_errors=1)

        self.assertTrue(summary["stopped_on_api_error"])

    def test_quota_exhaustion_leaves_the_target_unmarked(self) -> None:
        """The resume contract: an unmarked row is retried by the next run."""
        self.opencorp_repo.today_call_count.return_value = 10

        summary = self._run_one(_target(), daily_call_limit=10)

        self.assertTrue(summary["stopped_on_quota"])
        self.company_repo.set_opencorp_idx.assert_not_called()

    def test_dry_run_makes_no_calls_and_no_writes(self) -> None:
        summary = self._run_one(_target(), dry_run=True)

        self.client.search_companies.assert_not_called()
        self.company_repo.insert_opencorp_links.assert_not_called()
        self.company_repo.set_opencorp_idx.assert_not_called()
        self.assertEqual(1, summary["targets"])


class TestSearchJurisdiction(unittest.TestCase):
    """Bridging the roster's ISO vocabulary to what OpenCorporates indexes."""

    def setUp(self) -> None:
        """Bind the static resolver under test."""
        self.resolve = OpenCorpIndexIngestor._search_jurisdiction

    def test_a_bare_country_is_used_as_is(self) -> None:
        self.assertEqual("gb", self.resolve("GB", "GB"))

    def test_an_indexed_subdivision_is_used_as_is(self) -> None:
        self.assertEqual("ae_du", self.resolve("AE", "AE-DU"))

    def test_an_unindexed_subdivision_degrades_to_its_country(self) -> None:
        """Without this, resolving a Scottish address would break GB searches."""
        self.assertEqual("gb", self.resolve("GB", "GB-SCT"))

    def test_a_country_with_no_country_level_jurisdiction_is_declined(self) -> None:
        """A bare 'us' search returns zero hits and would write a false 'NA'."""
        self.assertIsNone(self.resolve("US", None))

    def test_eu_is_declined(self) -> None:
        """'EU' is not a jurisdiction; confirmed absent from GET /jurisdictions."""
        self.assertIsNone(self.resolve("EU", "EU"))

    def test_a_row_with_no_country_at_all_is_declined(self) -> None:
        """Not a failure: the ladder reads None as 'search every jurisdiction'."""
        self.assertIsNone(self.resolve(None, None))


class TestPolicyFromArgs(unittest.TestCase):
    """How the CLI composes a relaxation policy."""

    def _policy(self, argv: list[str]) -> RelaxationPolicy:
        """Parse argv and build the policy it describes.

        Args:
            argv: Command-line tokens, without the program name.

        Returns:
            The resulting policy.
        """
        return _policy_from_args(_build_parser().parse_args(argv))

    def test_no_flags_is_the_default_policy(self) -> None:
        self.assertEqual(RelaxationPolicy(), self._policy([]))

    def test_relax_is_the_relaxed_policy(self) -> None:
        self.assertEqual(RelaxationPolicy.relaxed(), self._policy(["--relax"]))

    def test_an_individual_flag_overrides_relax(self) -> None:
        """'--relax --min-match-score 0.9' is relaxed asking, stricter accepting."""
        policy = self._policy(["--relax", "--min-match-score", "0.9"])

        self.assertEqual(0.9, policy.min_match_score)
        self.assertEqual(
            RelaxationPolicy.relaxed().min_query_length, policy.min_query_length
        )

    def test_a_boolean_flag_can_be_switched_off_under_relax(self) -> None:
        policy = self._policy(["--relax", "--no-score-over-cap"])

        self.assertFalse(policy.score_over_cap)
        self.assertTrue(policy.strip_leading_legal_form)

    def test_a_knob_applies_without_relax(self) -> None:
        """Every knob is settable on its own, not only as part of the profile."""
        policy = self._policy(["--min-query-length", "3", "--max-links", "2"])

        self.assertEqual(3, policy.min_query_length)
        self.assertEqual(2, policy.max_links)
        self.assertFalse(policy.score_over_cap)

    def test_include_missing_country_defaults_off_and_follows_relax(self) -> None:
        """It is a selection flag, not a policy field, so it is checked here."""
        self.assertIsNone(_build_parser().parse_args([]).include_missing_country)
        self.assertTrue(
            _build_parser()
            .parse_args(["--include-missing-country"])
            .include_missing_country
        )
        self.assertFalse(
            _build_parser()
            .parse_args(["--relax", "--no-include-missing-country"])
            .include_missing_country
        )


class TestRelaxedResolution(unittest.TestCase):
    """Names the default policy silences and a relaxed run reaches.

    Every case is a real roster row measured on 2026-08-01, with the hit counts
    the live API returned for it.
    """

    def setUp(self) -> None:
        """Build an ingestor over three mock collaborators and patch sleep."""
        self.client = mock.MagicMock()
        self.opencorp_repo = mock.MagicMock()
        self.company_repo = mock.MagicMock()
        self.roster_ids = [uuid.uuid4()]
        self.company_repo.roster_ids_for_company.return_value = self.roster_ids
        self.opencorp_repo.today_call_count.return_value = 0
        self.opencorp_repo.record_calls.return_value = 1
        self.ingestor = OpenCorpIndexIngestor(
            client=self.client,
            opencorp_repo=self.opencorp_repo,
            company_repo=self.company_repo,
        )
        sleep_patcher = mock.patch(
            "batch.Main_ingest_opencorp_index_companies.time.sleep"
        )
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)
        self.relaxed = RelaxationPolicy.relaxed()

    def _run_one(self, target: OpenCorpTarget, **kwargs) -> dict:
        """Run the batch over exactly one target.

        Args:
            target: The roster target to resolve.
            **kwargs: Extra keyword arguments for ``run``.

        Returns:
            The run summary.
        """
        self.company_repo.select_opencorp_targets.return_value = [target]
        return self.ingestor.run(**kwargs)

    def test_an_over_cap_rung_is_scored_instead_of_discarded(self) -> None:
        """The 'Wavin' case: 166 unfiltered hits, the real ones on page 1.

        The cap stopped the paging, not the search -- page 1 was fetched and
        charged for either way, so scoring it costs nothing.
        """
        opencorp_id = uuid.uuid4()
        self.client.search_companies.return_value = _search_result(
            "Wavin",
            total_count=166,
            companies=[_company("Wavin Overseas", "3", "nl")],
            jurisdiction_code=None,
        )
        self.opencorp_repo.company_row_ids_by_key.return_value = {
            ("nl", "3"): opencorp_id
        }

        summary = self._run_one(
            _target("Wavin", country=None, jurisdiction=None), policy=self.relaxed
        )

        self.assertEqual(1, summary["linked"])
        links = self.company_repo.insert_opencorp_links.call_args.args[0]
        self.assertEqual(opencorp_id, links[0].fk_opencorp_index_company)

    def test_an_over_cap_rung_that_narrows_to_nothing_is_ambiguous(self) -> None:
        """The 'Emirates' case: 1,447 hits, 189 of them clearing the threshold.

        An over-cap rung is evidence only if the scorer narrows it. Truncating
        189 confident matches to five would link five arbitrary companies and
        record it as a resolution.
        """
        hits = [
            _company(f"EMIRATES TRADING {index}", str(index), "ae_du")
            for index in range(20)
        ]
        self.client.search_companies.return_value = _search_result(
            "Emirates", total_count=1447, companies=hits, jurisdiction_code=None
        )
        self.opencorp_repo.company_row_ids_by_key.return_value = {
            ("ae_du", str(index)): uuid.uuid4() for index in range(20)
        }

        summary = self._run_one(
            _target("Emirates", country="AE", jurisdiction=None), policy=self.relaxed
        )

        self.company_repo.insert_opencorp_links.assert_not_called()
        self.assertEqual(1, summary["ambiguous"])

    def test_an_over_cap_rung_is_still_discarded_by_default(self) -> None:
        """Scoring an over-cap page is opt-in; the default keeps its precision."""
        self.client.search_companies.return_value = _search_result(
            "Wavin",
            total_count=166,
            companies=[_company("Wavin Overseas", "3", "nl")],
            jurisdiction_code=None,
        )

        summary = self._run_one(_target("Wavin", country=None, jurisdiction=None))

        self.company_repo.insert_opencorp_links.assert_not_called()
        self.assertEqual(1, summary["ambiguous"])

    def test_a_country_less_target_is_searched_unfiltered(self) -> None:
        """'CEJI-Interlude' names no country and returns exactly one hit (fr)."""
        opencorp_id = uuid.uuid4()
        self.client.search_companies.return_value = _search_result(
            "CEJI-Interlude",
            total_count=1,
            companies=[_company("CEJI INTERLUDE", "702037045", "fr")],
            jurisdiction_code=None,
        )
        self.opencorp_repo.company_row_ids_by_key.return_value = {
            ("fr", "702037045"): opencorp_id
        }

        summary = self._run_one(
            _target("CEJI-Interlude", country=None, jurisdiction=None),
            include_missing_country=True,
            policy=self.relaxed,
        )

        self.assertIsNone(
            self.client.search_companies.call_args.kwargs["jurisdiction_code"]
        )
        self.assertEqual(1, summary["linked"])
        self.company_repo.roster_ids_for_company.assert_called_once_with(
            "CEJI-Interlude", None
        )
        self.company_repo.set_opencorp_idx.assert_called_once_with(
            [(self.roster_ids[0], MARKER_LINKED)]
        )

    def test_a_short_name_is_unsearchable_by_default(self) -> None:
        """'STET' is four characters: no rung clears the default minimum."""
        summary = self._run_one(_target("STET", country=None, jurisdiction=None))

        self.client.search_companies.assert_not_called()
        self.assertEqual(1, summary["skipped_no_ladder"])

    def test_a_short_name_is_searched_under_the_relaxed_policy(self) -> None:
        self.client.search_companies.return_value = _search_result(
            "STET", total_count=0, companies=[], jurisdiction_code=None
        )

        summary = self._run_one(
            _target("STET", country=None, jurisdiction=None), policy=self.relaxed
        )

        self.assertEqual(1, self.client.search_companies.call_count)
        self.assertEqual(0, summary["skipped_no_ladder"])
        self.assertEqual(1, summary["na"])

    def test_a_leading_legal_form_is_cut_on_the_core_rung(self) -> None:
        """'S.A. Cimenteries CBR': the form leads the name, so no rung cut it."""
        self.client.search_companies.return_value = _search_result(
            "x", total_count=0, companies=[], jurisdiction_code=None
        )

        self._run_one(
            _target("S.A. Cimenteries CBR", country=None, jurisdiction=None),
            policy=self.relaxed,
        )

        queries = [
            call.args[0] for call in self.client.search_companies.call_args_list
        ]
        self.assertIn("Cimenteries CBR", queries)

    def test_the_policy_hit_cap_is_the_client_budget(self) -> None:
        """A CLI-set cap must reach the request, not only the linking decision."""
        self.client.search_companies.return_value = _search_result(
            "Wavin", total_count=0, companies=[], jurisdiction_code=None
        )

        self._run_one(
            _target("Wavin", country=None, jurisdiction=None),
            policy=RelaxationPolicy(unfiltered_hit_cap=17),
        )

        self.assertEqual(
            17, self.client.search_companies.call_args.kwargs["max_hits"]
        )


if __name__ == "__main__":
    unittest.main()
