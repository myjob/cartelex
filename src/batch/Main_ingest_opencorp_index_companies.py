"""
Batch: resolve investigated companies against the OpenCorporates search API and
link the hits back to the roster.

By default this selects outstanding roster companies from
``celex_facts.company_investigated2lei2corpidx2hr_vw`` across every country;
pass ``--iso-country-code`` to restrict the run to one (GB was the pilot).
Rows are ones that failed LEI or Handelsregister resolution and have not yet
been looked up in OpenCorporates (``fk_opencorp_idx IS NULL``). Each name is
searched under the ``jurisdication_code`` the roster resolved for it (see
``Main_ingest_company_investigated2celex.py``), translated to the
OpenCorporates spelling -- ``AE-DU`` becomes ``ae_du``. It calls
:meth:`OpenCorpClient.search_companies`, which persists the hits into
``celex_facts.opencorp_index_companies`` and caches by ``(name, jurisdiction)``.
The returned OpenCorporates company rows are then linked to every roster row for
that name and country in the N:M table
``celex_facts.company_investigated_celex_id2opencorp_index_company``, and the
roster rows are marked (``fk_opencorp_idx = 'LINKED'`` on a hit, ``'NA'`` when
the search returned nothing).

It is a plain REST + DB ETL: no LLM or DSPy pipeline is involved, so the DSPy
signature rules do not apply.

Relaxation:
    A roster row is only as searchable as what the extractor left on it. Rows
    with no ``iso_country_code`` (829 of the 1,088 unmarked rows on 2026-08-01)
    were never even selected, a name under five characters produced no query at
    all, and a global search reporting more hits than its cap was discarded
    after its first page had already been fetched. ``--relax`` opens all three,
    and every knob behind it (``--min-query-length``, ``--unfiltered-hit-cap``,
    ``--min-match-score``, ``--score-over-cap``, ...) is separately settable.
    Relaxation widens what gets *asked*, never what gets accepted: the match
    threshold and the link ceiling are untouched by ``--relax``.

Quota safety:
    - The API is throttled to one request per ``--sleep-seconds`` (default 1s).
    - Every fetched page counts against a durable daily budget stored in
      ``celex_facts.opencorp_api_daily_calls``; when the day's count reaches
      ``--daily-call-limit`` (default 100,000) the run stops with a bold banner.
    - Because processed rows are marked, they leave the roster view, so a re-run
      the next day resumes with the remaining unprocessed companies.

Failure handling:
    - A failed search leaves its company unmarked, so it is retried by the next
      run; the error is recorded on the query row in
      ``celex_facts.opencorp_index_query`` (``error_class``, ``error_status_code``).
    - :data:`settings.OPENCORP_SKIP_RETRY` (default on) makes the run
      fail-fast-and-carry-on: no request is retried, and a failure specific to
      one name (the HTTP 400 the API answers for queries it will not accept) is
      logged and skipped however often it recurs, instead of ending a long
      roster run once three awkward names happen to land back-to-back. An IP
      throttle and a rejected token still stop the run -- neither is escaped by
      moving on.
    - ``OPENCORP_SKIP_RETRY=false`` restores the retry-with-backoff policy, under
      which any streak of ``--max-consecutive-api-errors`` failures stops the
      run.

This is a company-driven job, not a CELEX-driven one, so it does not use
``BatchJobRunner``; it has its own argparse CLI.
"""

import argparse
import logging
import time
import uuid
from collections import Counter
from datetime import date

from config.settings import settings
from pipelines.corp_index.jurisdiction_resolver import (
    country_part,
    normalize_jurisdiction_code,
    to_opencorp_code,
)
from pipelines.corp_index.opencorp_client import (
    OpenCorpApiError,
    OpenCorpAuthError,
    OpenCorpClient,
    OpenCorpRateLimitError,
)
from pipelines.corp_index.opencorp_repository import OpenCorpIndexRepository
from pipelines.corp_index.opencorp_resolution_ladder import (
    DEFAULT_POLICY,
    HitScore,
    OpenCorpTarget,
    RelaxationPolicy,
    ResolutionRung,
    build_ladder,
    rank_and_filter,
)
from pipelines.rag.pgvector.company_investigated_repository import (
    CompanyInvestigatedRepository,
    OpenCorpLink,
)

logger = logging.getLogger(__name__)

# ANSI bold for the quota-exhausted banner (no emoji / unicode-emoji per project
# style). Rendered bold on a terminal; harmless escape noise in a log file.
_BOLD = "\033[1m"
_RESET = "\033[0m"

# Roster marker values written to fk_opencorp_idx after a lookup, mirroring the
# 'NA' convention already used for fk_lei / fk_handelsregister.
MARKER_LINKED = "LINKED"
MARKER_NO_HIT = "NA"
# Hits came back but none of them identified this company. Distinct from 'NA'
# because the answer is "too many / none convincing", not "not in any registry":
# the first is a triage bucket a human can work through, the second is a fact.
MARKER_AMBIGUOUS = "AMBIGUOUS"

# Score recorded for a link made on an unscored rung, where the jurisdiction
# filter rather than the scorer supplied the precision.
_UNSCORED_HIT = HitScore(
    score=1.0,
    name_score=1.0,
    town_match=False,
    industry_match=False,
    jurisdiction_match=True,
    temporally_plausible=True,
)

DEFAULT_DAILY_CALL_LIMIT = 100_000
DEFAULT_SLEEP_SECONDS = 1.0
# A single company whose search keeps failing is skipped rather than allowed to
# end the run; this many failures back-to-back means the API side is unhealthy
# (IP throttle) rather than one awkward name, so the run stops and resumes later.
# Under settings.OPENCORP_SKIP_RETRY only a throttle can reach this limit -- see
# OpenCorpIndexIngestor._ends_the_run.
DEFAULT_MAX_CONSECUTIVE_API_ERRORS = 3

# What OpenCorporates actually indexes. The roster resolves any ISO 3166-2
# subdivision, but OpenCorporates covers only some of them, and its jurisdiction
# vocabulary is its own -- neither fact is derivable from the ISO or GLEIF code
# lists. Both sets below are transcribed from ``GET /jurisdictions`` (351 codes at
# the time of writing); regenerate with:
#
#   curl -s "https://api.opencorporates.com/v0.4/jurisdictions?api_token=$TOKEN" \
#     | jq -r '.results.jurisdictions[].jurisdiction.code' | sort
#
# Deliberately static constants rather than a table: this batch must not depend on
# OpenCorporates data being loaded before it can decide what to ask for.
#
# Countries whose subdivisions OpenCorporates indexes. A resolved subdivision
# outside these countries (GB-SCT, GB-NIR, BE-VLG, PT-20, PT-30, MY-15, KN-N) is
# not a jurisdiction there, so it degrades to the country code.
OPENCORP_SUBDIVISION_COUNTRIES = frozenset({"AE", "CA", "US"})
# Countries with no country-level jurisdiction at all. A bare 'ae', 'us' or 'eu'
# search returns HTTP 200 with zero companies -- indistinguishable from a genuine
# miss -- so it must never be sent; a name with no usable subdivision is skipped
# instead.
#
# 'EU' is not a country and never was a jurisdiction; it reaches here because 26
# roster rows carry it as their iso_country_code (see the country-code hygiene
# entry in ISSUES.md). Confirmed absent from GET /jurisdictions by the 2026-07-31
# probe, alongside 'US' -- 357 rows had been marked 'NA' on a question that was
# never actually asked.
OPENCORP_NO_COUNTRY_LEVEL = frozenset({"AE", "US", "EU"})


class OpenCorpIndexIngestor:
    """Resolves roster companies against OpenCorporates and links the hits.

    Follows the Provisioner pattern: the OpenCorporates client and both
    repositories are injected in ``__init__`` so the ingestor is testable with
    mocks at the DB / HTTP boundary.

    Parameters
    ----------
    client:
        DB-cached OpenCorporates search client.
    opencorp_repo:
        DAO for the OpenCorporates cache tables and the daily-call counter.
    company_repo:
        DAO for the investigated-company roster, its N:M link table, and the
        ``fk_opencorp_idx`` marker.
    """

    def __init__(
        self,
        client: OpenCorpClient,
        opencorp_repo: OpenCorpIndexRepository,
        company_repo: CompanyInvestigatedRepository,
    ) -> None:
        self.client = client
        self.opencorp_repo = opencorp_repo
        self.company_repo = company_repo

    def run(
        self,
        *,
        iso_country_code: str | None = None,
        jurisdiction_code: str | None = None,
        all_corps: bool = False,
        daily_call_limit: int = DEFAULT_DAILY_CALL_LIMIT,
        sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
        limit: int = 0,
        dry_run: bool = False,
        max_consecutive_api_errors: int = DEFAULT_MAX_CONSECUTIVE_API_ERRORS,
        retry_na: bool = False,
        retry_ambiguous: bool = False,
        include_missing_country: bool = False,
        policy: RelaxationPolicy = DEFAULT_POLICY,
    ) -> dict:
        """Resolve and link the roster's outstanding OpenCorporates targets.

        Args:
            iso_country_code: ISO 3166-1 country code to restrict the run to
                (e.g. ``GB``). When ``None`` (the default), every outstanding
                company across all countries is processed, each searched under
                its own country's jurisdiction.
            jurisdiction_code: Overrides the roster's resolved jurisdiction for
                every target. Accepted in any casing or separator (``AE-DU``,
                ``ae_du``, ``AE_DU``). Only meaningful together with
                ``iso_country_code``, since it would otherwise force one
                jurisdiction onto every country's companies.
            all_corps: When True, select every outstanding company regardless
                of ``fk_lei`` / ``fk_handelsregister`` resolution status,
                instead of only ones that failed LEI or Handelsregister
                resolution. Applies in both the single-country and
                all-countries selection paths.
            daily_call_limit: Maximum API page fetches allowed for the current
                day across all runs. The pre-search check can overshoot by at
                most one search's pages, negligible against the default.
            sleep_seconds: Throttle applied before each (non-cached) search.
            limit: Process at most this many distinct company names; ``0`` for
                all.
            dry_run: When True, no API calls and no writes; logs the targets.
            max_consecutive_api_errors: Stop the run once this many rung
                searches fail back-to-back with a run-ending error. Isolated
                failures skip only their own rung, so a single awkward query
                cannot end the run. Under ``settings.OPENCORP_SKIP_RETRY`` only
                an IP throttle counts as run-ending; every other failure is
                logged, recorded, and skipped however often it recurs.
            retry_na: Reopen rows already marked ``'NA'``.
            retry_ambiguous: Reopen rows already marked ``'AMBIGUOUS'``.
            include_missing_country: Also select roster rows with no
                ``iso_country_code``. Every rung for such a target searches
                unfiltered, which is the only way these rows can be resolved at
                all -- and they are the largest unresolved group on the roster.
            policy: Relaxation knobs for the ladder: minimum query length, hit
                caps, match threshold, whether leading legal forms are stripped,
                and whether an over-cap rung's first page is still scored.

        Returns:
            A summary dict with ``iso_country_code`` (``None`` when the run
            covered all countries), ``targets``, ``processed``, ``linked``
            (names resolved to at least one hit), ``na`` (no rung returned
            anything), ``ambiguous`` (hits came back but none matched
            confidently), ``skipped`` (every rung failed at the API, so the row
            is left unmarked for the next run), ``skipped_no_jurisdiction``,
            ``skipped_no_ladder`` (name too short for any rung to be sent),
            ``calls_today``, ``stopped_on_quota`` and ``stopped_on_api_error``.
        """
        override_jurisdiction = normalize_jurisdiction_code(jurisdiction_code)
        if jurisdiction_code and override_jurisdiction is None:
            raise ValueError(
                f"Not a well-formed jurisdiction code: {jurisdiction_code!r}"
            )

        retry_markers: tuple[str, ...] = ()
        if retry_na:
            retry_markers += (MARKER_NO_HIT,)
        if retry_ambiguous:
            retry_markers += (MARKER_AMBIGUOUS,)

        targets = self.company_repo.select_opencorp_targets(
            iso_country_code=iso_country_code,
            all_corps=all_corps,
            retry_markers=retry_markers,
            include_missing_country=include_missing_country,
        )
        if limit > 0:
            targets = targets[:limit]

        summary = {
            "iso_country_code": iso_country_code,
            "targets": len(targets),
            "processed": 0,
            "linked": 0,
            "na": 0,
            "ambiguous": 0,
            "skipped": 0,
            "skipped_no_jurisdiction": 0,
            "skipped_no_ladder": 0,
            "calls_today": 0,
            "stopped_on_quota": False,
            "stopped_on_api_error": False,
        }

        if not targets:
            logger.info(
                "No outstanding OpenCorporates targets for %s.",
                iso_country_code or "ALL",
            )
            return summary

        if dry_run:
            rung_counts: Counter[str] = Counter()
            no_ladder = 0
            for target in targets:
                ladder = self._ladder_for(target, override_jurisdiction, policy)
                if not ladder:
                    no_ladder += 1
                    logger.info(
                        "[DRY RUN] %r (%s): no searchable rung -- name is "
                        "shorter than %d characters.",
                        target.company_name,
                        target.iso_country_code,
                        policy.min_query_length,
                    )
                    continue
                rung_counts.update(rung.rung for rung in ladder)
                logger.info(
                    "[DRY RUN] %r (%s): %s",
                    target.company_name,
                    target.iso_country_code,
                    [
                        (rung.rung, rung.query_text, rung.jurisdiction_code)
                        for rung in ladder
                    ],
                )
            summary["skipped_no_ladder"] = no_ladder
            logger.info(
                "[DRY RUN] %d target(s) across %s; %d unsearchable. Worst-case "
                "rung usage if no rung ever resolves: %s",
                len(targets),
                iso_country_code or "ALL countries",
                no_ladder,
                dict(rung_counts.most_common()),
            )
            return summary

        today = date.today()
        calls_today = self.opencorp_repo.today_call_count(today)
        summary["calls_today"] = calls_today
        if calls_today >= daily_call_limit:
            self._log_quota_banner(today, calls_today, daily_call_limit)
            summary["stopped_on_quota"] = True
            return summary

        consecutive_api_errors = 0
        total_targets = len(targets)
        for target_index, target in enumerate(targets, start=1):
            ladder = self._ladder_for(target, override_jurisdiction, policy)
            if not ladder:
                # Every rung was rejected before it was sent: the name is too
                # short to search for. Leave the row unmarked rather than write
                # a false 'NA' -- the answer is "we declined to ask", and a
                # better name upstream would change it.
                summary["skipped_no_ladder"] += 1
                logger.warning(
                    "Skipping %r (%s): no rung clears the %d-character "
                    "minimum query length.",
                    target.company_name,
                    target.iso_country_code,
                    policy.min_query_length,
                )
                continue
            if all(rung.jurisdiction_code is None for rung in ladder) and (
                self._search_jurisdiction(
                    target.iso_country_code,
                    override_jurisdiction or target.jurisdication_code,
                )
                is None
            ):
                summary["skipped_no_jurisdiction"] += 1

            roster_ids = self.company_repo.roster_ids_for_company(
                target.company_name, target.iso_country_code
            )
            accepted: list[tuple[uuid.UUID, HitScore, ResolutionRung]] = []
            saw_any_hits = False
            answered_rungs = 0
            stop_run = False

            for rung in ladder:
                if calls_today >= daily_call_limit:
                    self._log_quota_banner(today, calls_today, daily_call_limit)
                    summary["stopped_on_quota"] = True
                    stop_run = True
                    break

                time.sleep(sleep_seconds)
                try:
                    result = self.client.search_companies(
                        rung.query_text,
                        jurisdiction_code=rung.jurisdiction_code,
                        max_hits=rung.hit_cap,
                    )
                except OpenCorpAuthError as auth_error:
                    # The token itself was rejected; every remaining search would
                    # fail the same way. Stop cleanly: processed rows are already
                    # marked, and this target resumes on the next run.
                    self._record_query_error(
                        rung.query_text, rung.jurisdiction_code, auth_error
                    )
                    self._log_api_error_banner(today, calls_today, auth_error)
                    summary["stopped_on_api_error"] = True
                    stop_run = True
                    break
                except OpenCorpApiError as api_error:
                    # A verdict on one query text, not on the target: an HTTP 400
                    # for a stripped name says nothing about the next rung. Skip
                    # the rung, not the company -- unless failures keep coming
                    # back-to-back *and* moving on cannot escape them.
                    self._record_query_error(
                        rung.query_text, rung.jurisdiction_code, api_error
                    )
                    consecutive_api_errors += 1
                    logger.warning(
                        "Rung %s failed for %r (%d consecutive), trying the "
                        "next rung: %s",
                        rung.rung,
                        rung.query_text,
                        consecutive_api_errors,
                        api_error,
                    )
                    if consecutive_api_errors >= max_consecutive_api_errors and (
                        self._ends_the_run(api_error)
                    ):
                        self._log_api_error_banner(today, calls_today, api_error)
                        summary["stopped_on_api_error"] = True
                        stop_run = True
                        break
                    continue

                consecutive_api_errors = 0
                answered_rungs += 1
                if not result.from_cache:
                    calls_today = self.opencorp_repo.record_calls(
                        today, result.fetched_pages
                    )
                summary["calls_today"] = calls_today

                total_count = result.total_count or 0
                if total_count:
                    saw_any_hits = True
                over_cap = total_count > rung.hit_cap
                if over_cap and not (policy.score_over_cap and result.companies):
                    # Too broad to be evidence about this company. Not a stop:
                    # a later rung searches a *narrower* name and may still
                    # resolve cleanly.
                    logger.info(
                        "Rung %s for %r returned %d hit(s), over the %d cap; "
                        "not linking, continuing the ladder.",
                        rung.rung,
                        rung.query_text,
                        total_count,
                        rung.hit_cap,
                    )
                    continue
                if over_cap:
                    # The cap stopped the paging, not the search: page 1 was
                    # fetched and paid for either way. Scoring it costs no call
                    # and is what reaches 'Wavin', whose unfiltered search
                    # reports 166 hits and whose real registrations are on it.
                    # Scoring is forced even on a rung that would otherwise link
                    # blind -- over the cap is exactly where the jurisdiction
                    # filter has stopped supplying precision.
                    logger.info(
                        "Rung %s for %r returned %d hit(s), over the %d cap; "
                        "scoring the %d already-fetched hit(s).",
                        rung.rung,
                        rung.query_text,
                        total_count,
                        rung.hit_cap,
                        len(result.companies),
                    )

                if rung.requires_scoring or over_cap:
                    scored = rank_and_filter(
                        result.companies,
                        target,
                        query_text=rung.query_text,
                        policy=policy,
                        # One over the ceiling, which is all it takes to tell
                        # "a handful of hits identified this company" from
                        # "hundreds did" -- see the over-cap check below.
                        max_links=policy.max_links + 1,
                    )
                    if not scored:
                        continue
                    if over_cap and len(scored) > policy.max_links:
                        # An over-cap rung is only evidence if the scorer
                        # *narrows* it. 'Emirates' returns 1,447 hits of which
                        # 189 clear the threshold on the prefix tier; truncating
                        # those to five would link five arbitrary companies and
                        # record it as a resolution. Too many confident matches
                        # is the definition of ambiguous, so leave it to a
                        # narrower rung.
                        logger.info(
                            "Rung %s for %r: more than %d hit(s) cleared the "
                            "match threshold on an over-cap rung; not linking, "
                            "continuing the ladder.",
                            rung.rung,
                            rung.query_text,
                            policy.max_links,
                        )
                        continue
                    scored = scored[: policy.max_links]
                    ids_by_key = self.opencorp_repo.company_row_ids_by_key(
                        result.normalized_query, rung.jurisdiction_code
                    )
                    accepted = [
                        (row_id, score, rung)
                        for company, score in scored
                        if (
                            row_id := ids_by_key.get(
                                (company.jurisdiction_code, company.company_number)
                            )
                        )
                        is not None
                    ]
                else:
                    opencorp_ids = self.opencorp_repo.company_row_ids(
                        result.normalized_query, rung.jurisdiction_code
                    )
                    if not opencorp_ids:
                        continue
                    # Inside the target's own jurisdiction the filter supplies
                    # the precision, so every hit is linked -- but still capped,
                    # so a generic name cannot write thousands of rows.
                    accepted = [
                        (opencorp_id, _UNSCORED_HIT, rung)
                        for opencorp_id in opencorp_ids[: policy.max_links]
                    ]

                if accepted:
                    break

            if stop_run:
                break

            if not answered_rungs:
                # Every rung raised: the API never actually answered a question
                # about this company. Marking that 'NA' would record "processed,
                # not in any registry" for a question that was never asked, and
                # 'NA' is what removes the row from the next run's selection --
                # a permanent close on an API-side failure. Leave it unmarked,
                # exactly as a single failed search used to.
                summary["skipped"] += 1
                logger.warning(
                    "Skipping %r (%s): all %d rung(s) failed at the API; the "
                    "row stays unmarked and is retried by the next run.",
                    target.company_name,
                    target.iso_country_code,
                    len(ladder),
                )
                continue

            if accepted and roster_ids:
                self.company_repo.insert_opencorp_links([
                    OpenCorpLink(
                        fk_company_investigated=roster_id,
                        fk_opencorp_index_company=opencorp_id,
                        resolution_rung=rung.rung,
                        resolution_query=rung.query_text,
                        resolution_jurisdiction=rung.jurisdiction_code,
                        match_score=score.score,
                    )
                    for roster_id in roster_ids
                    for opencorp_id, score, rung in accepted
                ])

            if accepted:
                marker = MARKER_LINKED
                summary["linked"] += 1
            elif saw_any_hits:
                # Hits came back but none identified this company. That is a
                # different answer from "not in any registry" and belongs in its
                # own bucket, not silently in 'NA'.
                marker = MARKER_AMBIGUOUS
                summary["ambiguous"] += 1
            else:
                marker = MARKER_NO_HIT
                summary["na"] += 1

            self.company_repo.set_opencorp_idx(
                [(roster_id, marker) for roster_id in roster_ids]
            )
            summary["processed"] += 1
            logger.info(
                "Resolved %r (%s): %d link(s) via %s, %d roster row(s) marked "
                "%s. [%d/%d, %.1f%%]",
                target.company_name,
                target.iso_country_code,
                len(accepted),
                accepted[0][2].rung if accepted else "-",
                len(roster_ids),
                marker,
                target_index,
                total_targets,
                target_index / total_targets * 100,
            )

        return summary

    def _ladder_for(
        self,
        target: OpenCorpTarget,
        override_jurisdiction: str | None,
        policy: RelaxationPolicy = DEFAULT_POLICY,
    ) -> list[ResolutionRung]:
        """Build the rung sequence for one target.

        Args:
            target: The roster company to resolve.
            override_jurisdiction: Canonical jurisdiction forced by the CLI, or
                None to use the roster's own.
            policy: Relaxation knobs handed to :func:`build_ladder`.

        Returns:
            The rungs to attempt, or an empty list when no query survived the
            minimum-length guard. A target with neither country nor jurisdiction
            gets the unfiltered rungs only.
        """
        canonical = override_jurisdiction or target.jurisdication_code
        search_jurisdiction = self._search_jurisdiction(
            target.iso_country_code, canonical
        )
        country_jurisdiction = None
        if canonical and "-" in canonical:
            country_jurisdiction = self._search_jurisdiction(
                target.iso_country_code, country_part(canonical)
            )
        return build_ladder(
            target,
            jurisdiction_code=search_jurisdiction,
            country_jurisdiction_code=country_jurisdiction,
            policy=policy,
        )

    @staticmethod
    def _search_jurisdiction(
        row_country: str | None, canonical_code: str | None
    ) -> str | None:
        """Pick the OpenCorporates jurisdiction to search a target under.

        Bridges the roster's ISO 3166-2 vocabulary to what OpenCorporates
        actually indexes:

        - A subdivision in a country OpenCorporates covers by subdivision is used
          as-is (``AE-DU`` -> ``ae_du``).
        - A subdivision elsewhere is not a jurisdiction there, so it degrades to
          the country (``GB-SCT`` -> ``gb``). Without this, resolving a Scottish
          address would break searches that work today.
        - A bare country is used when OpenCorporates has a country-level
          jurisdiction for it.
        - Otherwise there is nothing safe to search.

        Args:
            row_country: The target's ISO 3166-1 alpha-2 country code, or None
                when the roster row names no country.
            canonical_code: The canonical jurisdiction resolved for the target
                (or the CLI override), or None when the roster resolved none.

        Returns:
            The OpenCorporates jurisdiction code to search, or None when no
            searchable jurisdiction exists for this target. None is not a
            failure: the ladder reads it as "search every jurisdiction".
        """
        country = normalize_jurisdiction_code(row_country)
        if canonical_code is not None:
            if "-" in canonical_code:
                if country_part(canonical_code) in OPENCORP_SUBDIVISION_COUNTRIES:
                    return to_opencorp_code(canonical_code)
                logger.debug(
                    "OpenCorporates has no subdivision jurisdictions for %s; "
                    "degrading %s to the country code.",
                    country_part(canonical_code),
                    canonical_code,
                )
                country = country_part(canonical_code)
            else:
                country = canonical_code

        if country is None or country in OPENCORP_NO_COUNTRY_LEVEL:
            return None
        return to_opencorp_code(country)

    @staticmethod
    def _ends_the_run(error: OpenCorpApiError) -> bool:
        """Report whether a streak of this error should stop the whole run.

        With retries enabled (the default) any error that survives the client's
        backoff is treated as evidence the API side is unhealthy, so a streak
        ends the run.

        Under :data:`settings.OPENCORP_SKIP_RETRY` the client no longer rides
        anything out, so most errors say nothing about the API's health: an
        HTTP 400 is a verdict on one company name, and the next name is very
        likely fine. Those are skipped indefinitely rather than allowed to end
        the batch. An IP-level throttle is the exception -- it applies to every
        subsequent request, so continuing would burn the remaining roster on
        failures and hammer an API that is already asking the caller to slow
        down.

        Args:
            error: The API error raised for the company just skipped.

        Returns:
            True when a streak of this error class should stop the run.
        """
        if not settings.OPENCORP_SKIP_RETRY:
            return True
        return isinstance(error, OpenCorpRateLimitError)

    def _record_query_error(
        self, company_name: str, jurisdiction_code: str, error: Exception
    ) -> None:
        """Persist a failed search so its error code is visible in the DB.

        Args:
            company_name: The raw company name that was searched.
            jurisdiction_code: The jurisdiction filter used for the search.
            error: The exception raised by the failed search; ``status_code``
                is read off it when present (:class:`OpenCorpAuthError` and
                :class:`OpenCorpApiError` set it, ``None`` for a transport
                error with no HTTP response).
        """
        self.opencorp_repo.save_query_error(
            query_text=company_name,
            normalized_query=OpenCorpClient.normalize_query(company_name),
            jurisdiction_code=jurisdiction_code,
            error_class=type(error).__name__,
            error_status_code=getattr(error, "status_code", None),
            error_message=str(error),
        )

    @staticmethod
    def _log_api_error_banner(
        call_date: date, calls_today: int, error: Exception
    ) -> None:
        """Log a bold banner for a mid-run OpenCorporates API failure.

        Args:
            call_date: The day the run stopped on.
            calls_today: API page fetches recorded for the day so far.
            error: The error that stopped the run -- a rejected token, an IP
                throttle, or repeated transport failures.
        """
        if isinstance(error, OpenCorpRateLimitError):
            cause = (
                "OpenCorporates is throttling this IP address. The token is "
                "valid; wait, or raise --sleep-seconds to slow the run down."
            )
        elif isinstance(error, OpenCorpAuthError):
            cause = (
                "OpenCorporates rejected the API token: check "
                "opencorporates_api_token, or the account's credit allowance."
            )
        elif settings.OPENCORP_SKIP_RETRY:
            cause = "The API kept failing (retries are off: OPENCORP_SKIP_RETRY)."
        else:
            cause = "The API kept failing after retries."
        logger.warning(
            "%s%s\nOPENCORPORATES API STOPPED the run on %s after %d recorded "
            "call(s): %s\n%s This is an API-side stop, not a local budget stop. "
            "Processed rows are marked; unprocessed rows resume on the next "
            "run.\n%s%s",
            _BOLD,
            "=" * 72,
            call_date.isoformat(),
            calls_today,
            error,
            cause,
            "=" * 72,
            _RESET,
        )

    @staticmethod
    def _log_quota_banner(call_date: date, calls_today: int, limit: int) -> None:
        """Log a bold banner announcing the daily OpenCorporates quota is spent.

        Args:
            call_date: The day whose budget is exhausted.
            calls_today: API page fetches recorded for the day.
            limit: The daily budget that was reached.
        """
        logger.warning(
            "%s%s\nOPENCORPORATES DAILY QUOTA REACHED for %s: %d/%d API calls "
            "used. Stopping; unprocessed rows resume on the next run.\n%s%s",
            _BOLD,
            "=" * 72,
            call_date.isoformat(),
            calls_today,
            limit,
            "=" * 72,
            _RESET,
        )


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for the OpenCorporates resolution batch.

    Returns:
        The configured :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Resolve investigated companies against OpenCorporates and link the "
            "hits back to the roster (quota-safe, resumable)."
        )
    )
    parser.add_argument(
        "--iso-country-code",
        default=None,
        help=(
            "ISO 3166-1 country code to restrict the run to (e.g. GB). Omit to "
            "process every outstanding company across all countries, each "
            "searched under its own country's jurisdiction."
        ),
    )
    parser.add_argument(
        "--all-corps",
        action="store_true",
        help=(
            "Select every outstanding company regardless of fk_lei / "
            "fk_handelsregister resolution status, instead of only ones that "
            "failed LEI or Handelsregister resolution."
        ),
    )
    parser.add_argument(
        "--retry-na",
        action="store_true",
        help=(
            "Reopen rows already marked fk_opencorp_idx = 'NA' and put them "
            "back through the resolution ladder. A single-shot zero-hit search "
            "closed those rows permanently, and most of them are recoverable: "
            "the name carried extraction junk, or the jurisdiction was the "
            "company's headquarters rather than where it is registered."
        ),
    )
    parser.add_argument(
        "--retry-ambiguous",
        action="store_true",
        help=(
            "Reopen rows marked 'AMBIGUOUS' (hits came back but none matched "
            "confidently). Useful after tuning OPENCORP_MIN_MATCH_SCORE or the "
            "hit caps."
        ),
    )
    parser.add_argument(
        "--jurisdiction-code",
        default=None,
        help=(
            "Override the jurisdiction resolved on the roster for every target. "
            "Any casing or separator is accepted (AE-DU, ae_du, AE_DU). Only "
            "meaningful together with --iso-country-code."
        ),
    )
    relaxation = parser.add_argument_group(
        "relaxation",
        "Knobs that decide how hard the ladder tries. --relax sets the whole "
        "relaxed profile; the individual flags below override it either way, so "
        "'--relax --min-match-score 0.9' is a relaxed run with a stricter "
        "scorer.",
    )
    relaxation.add_argument(
        "--relax",
        action="store_true",
        help=(
            "Run the relaxed profile: select rows with no iso_country_code "
            "(searched unfiltered), lower the minimum query length to "
            "OPENCORP_RELAXED_MIN_QUERY_LENGTH so short names like 'STET' are "
            "searchable at all, strip leading legal forms, and score an "
            "over-cap rung's already-fetched first page instead of discarding "
            "it. The match threshold and link ceiling are unchanged: a relaxed "
            "run asks more questions, it does not accept weaker answers."
        ),
    )
    relaxation.add_argument(
        "--include-missing-country",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Select roster rows with no iso_country_code. Every rung for such a "
            "target searches every jurisdiction, which is the only way to "
            "resolve them -- and they are the largest unresolved group on the "
            "roster. On under --relax."
        ),
    )
    relaxation.add_argument(
        "--min-query-length",
        type=int,
        default=None,
        help=(
            "Shortest relaxed query still worth sending (default: "
            "OPENCORP_MIN_RELAXED_QUERY_LENGTH, or "
            "OPENCORP_RELAXED_MIN_QUERY_LENGTH under --relax)."
        ),
    )
    relaxation.add_argument(
        "--jurisdiction-hit-cap",
        type=int,
        default=None,
        help=(
            "Largest linkable result set for a jurisdiction-filtered rung; also "
            "its page budget (default: OPENCORP_HIT_CAP_JURISDICTION)."
        ),
    )
    relaxation.add_argument(
        "--unfiltered-hit-cap",
        type=int,
        default=None,
        help=(
            "The same for a rung that searches every jurisdiction (default: "
            "OPENCORP_HIT_CAP_UNFILTERED). Raising it buys hits with API calls; "
            "--score-over-cap buys some of them for free."
        ),
    )
    relaxation.add_argument(
        "--min-match-score",
        type=float,
        default=None,
        help=(
            "Acceptance threshold on a scored rung (default: "
            "OPENCORP_MIN_MATCH_SCORE). The default admits an exact folded name "
            "or a whole-word folded prefix, nothing weaker."
        ),
    )
    relaxation.add_argument(
        "--max-links",
        type=int,
        default=None,
        help=(
            "Ceiling on links written for one roster row (default: "
            "OPENCORP_MAX_LINKS_PER_TARGET)."
        ),
    )
    relaxation.add_argument(
        "--strip-leading-legal-form",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Let the 'core' rung also drop leading legal-form tokens, so "
            "'S.A. Cimenteries CBR' is searched as 'Cimenteries CBR'. On under "
            "--relax."
        ),
    )
    relaxation.add_argument(
        "--score-over-cap",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Score the first page of a rung whose total hit count is over its "
            "cap instead of discarding it. Costs no extra API call -- that page "
            "was fetched either way. On under --relax."
        ),
    )
    parser.add_argument(
        "--daily-call-limit",
        type=int,
        default=DEFAULT_DAILY_CALL_LIMIT,
        help="Maximum API page fetches per day (default: 100000).",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=DEFAULT_SLEEP_SECONDS,
        help="Throttle before each non-cached search (default: 1.0).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most this many distinct company names. 0 for all.",
    )
    parser.add_argument(
        "--max-consecutive-api-errors",
        type=int,
        default=DEFAULT_MAX_CONSECUTIVE_API_ERRORS,
        help=(
            "Stop once this many company searches fail back-to-back; isolated "
            "failures are skipped and resume next run (default: 3)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the target companies but make no API calls and no writes.",
    )
    return parser


def _policy_from_args(args: argparse.Namespace) -> RelaxationPolicy:
    """Build the ladder's relaxation policy from parsed CLI arguments.

    ``--relax`` chooses the base profile; every individual flag then overrides
    that base, so the two compose in either direction. A flag left unset is
    ``None`` rather than its default value, which is what keeps "not given" from
    silently undoing ``--relax``.

    Args:
        args: The parsed CLI namespace.

    Returns:
        The policy for this run.
    """
    base = RelaxationPolicy.relaxed() if args.relax else RelaxationPolicy()
    overrides = {
        "min_query_length": args.min_query_length,
        "jurisdiction_hit_cap": args.jurisdiction_hit_cap,
        "unfiltered_hit_cap": args.unfiltered_hit_cap,
        "min_match_score": args.min_match_score,
        "max_links": args.max_links,
        "strip_leading_legal_form": args.strip_leading_legal_form,
        "score_over_cap": args.score_over_cap,
    }
    return base.model_copy(
        update={key: value for key, value in overrides.items() if value is not None}
    )


def main() -> None:
    """Entry point: resolve outstanding roster companies against OpenCorporates."""
    logging.basicConfig(level=logging.INFO)
    args = _build_parser().parse_args()
    policy = _policy_from_args(args)
    include_missing_country = (
        args.relax
        if args.include_missing_country is None
        else args.include_missing_country
    )
    logger.info(
        "OpenCorporates resolution policy: %s (include_missing_country=%s)",
        policy.model_dump(),
        include_missing_country,
    )

    ingestor = OpenCorpIndexIngestor(
        client=OpenCorpClient(),
        opencorp_repo=OpenCorpIndexRepository(),
        company_repo=CompanyInvestigatedRepository(),
    )
    result = ingestor.run(
        iso_country_code=args.iso_country_code,
        jurisdiction_code=args.jurisdiction_code,
        all_corps=args.all_corps,
        daily_call_limit=args.daily_call_limit,
        sleep_seconds=args.sleep_seconds,
        limit=args.limit,
        dry_run=args.dry_run,
        max_consecutive_api_errors=args.max_consecutive_api_errors,
        retry_na=args.retry_na,
        retry_ambiguous=args.retry_ambiguous,
        include_missing_country=include_missing_country,
        policy=policy,
    )
    logger.info("OpenCorporates resolution result: %s", result)


if __name__ == "__main__":
    main()


"""
# Dry run: list the GB companies that would be resolved (no API calls, no writes):
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_ingest_opencorp_index_companies.py \
    --iso-country-code GB --limit 3 --dry-run

# Small real run (throttled, quota-counted, resumable):
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_ingest_opencorp_index_companies.py \
    --iso-country-code GB --limit 20

# Full GB run:
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_ingest_opencorp_index_companies.py --iso-country-code GB

# Full run across every outstanding country (each searched under its own
# jurisdiction):

PYTHONPATH=src .venv/bin/python \
    src/batch/Main_ingest_opencorp_index_companies.py

# Full run across every outstanding country, including companies that already
# resolved fk_lei / fk_handelsregister:

PYTHONPATH=src .venv/bin/python \
    src/batch/Main_ingest_opencorp_index_companies.py --all-corps

# Restore the retry-with-backoff policy for one run (OPENCORP_SKIP_RETRY is on
# by default, which never retries a request and never lets names the API rejects
# with HTTP 400 end the batch -- each is logged, recorded on its
# opencorp_index_query row, left unmarked for a later run, and the loop moves
# on; an IP throttle or a rejected token still stops the run):
OPENCORP_SKIP_RETRY=false PYTHONPATH=src .venv/bin/python \
    src/batch/Main_ingest_opencorp_index_companies.py --all-corps

# Each name is searched under the jurisdication_code the roster resolved for it,
# so run the roster's jurisdiction stage first if it has not been run:
#
#   PYTHONPATH=src .venv/bin/python \
#       src/batch/Main_ingest_company_investigated2celex.py
#
# A country whose companies only exist under subdivision jurisdictions (AE, US)
# reports skipped_no_jurisdiction for every name the roster could not place; the
# dry run lists the jurisdiction planned per name:
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_ingest_opencorp_index_companies.py \
    --iso-country-code AE --all-corps --dry-run

# Force one jurisdiction for the whole run (any casing/separator accepted):
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_ingest_opencorp_index_companies.py \
    --iso-country-code AE --jurisdiction-code AE_DU --all-corps

# RELAXED RUNS ---------------------------------------------------------------
#
# Dry run of the relaxed profile: shows the rungs planned for every unmarked
# row, including the ~789 targets with no country that the default run never
# selects. No API calls, no writes:
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_ingest_opencorp_index_companies.py \
    --relax --all-corps --dry-run

# The relaxed run itself over every unmarked row:
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_ingest_opencorp_index_companies.py --relax --all-corps

# Relaxed, and also reopening the rows a single-shot search closed as 'NA'
# before the ladder existed. This is what recovers 'NYK Container Line, Ltd'
# (zero hits verbatim, one exact hit once the legal form is cut) and
# 'PRINTEOS, S.A.' (zero hits under es, three unfiltered):
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_ingest_opencorp_index_companies.py \
    --relax --all-corps --retry-na --retry-ambiguous

# Relaxed but stricter about what it accepts, and willing to page further on a
# global search (every knob is settable on its own, with or without --relax):
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_ingest_opencorp_index_companies.py \
    --relax --all-corps --min-match-score 0.9 --unfiltered-hit-cap 200

# Just the country-less rows, at otherwise default settings:
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_ingest_opencorp_index_companies.py \
    --include-missing-country --all-corps

"""
