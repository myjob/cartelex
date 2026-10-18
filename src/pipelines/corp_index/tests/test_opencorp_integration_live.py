"""Live integration test for the OpenCorporates lookup.

Hits the real OpenCorporates API and the live ``celex_facts`` database. It
self-skips unless a real ``opencorporates_api_token`` is present in the
environment and the database is reachable, mirroring the skip style of
``test_corp_index_integration.py``.
"""

import os
import unittest
from typing import Any

from sqlalchemy import text

# The real token must come from the environment. Capture it before providing a
# dummy so the settings import below never hard-fails during discovery; the
# dummy value is treated as "no token" and triggers a skip.
_ENV_TOKEN = os.environ.get("opencorporates_api_token", "").strip()
os.environ.setdefault("opencorporates_api_token", "dummy")

from pipelines.corp_index.opencorp_client import OpenCorpClient  # noqa: E402
from pipelines.corp_index.opencorp_repository import (  # noqa: E402
    OpenCorpIndexRepository,
)

_SEARCH_QUERY = "Swiss Re"
_SEARCH_JURISDICTION = "ch"

# OpenCorporates has no unified "ae" jurisdiction for the UAE; it indexes
# emirate-level codes (e.g. "ae_du" for Dubai, "ae_az" for Abu Dhabi) --
# "ae" alone returns zero hits.
_EMIRATES_QUERY = "Emirates"
_EMIRATES_JURISDICTION = "ae_du"

# The OpenCorporates jurisdiction_code is lowercase, so the ISO country code
# 'NL' is passed to the API as 'nl'.
_UIP_QUERY = "United International Pictures BV"
_UIP_JURISDICTION = "nl"
_UIP_COMPANY_NUMBER = "33165864"
_UIP_EXPECTED_NAME = "United International Pictures B.V."
_UIP_EXPECTED_COMPANY_TYPE = "Besloten Vennootschap"
_UIP_EXPECTED_ADDRESS_PARTS = (
    "Koningin Wilhelminaplein 2",
    "Amsterdam",
    "1062HK",
    "Netherlands",
)
_UIP_EXPECTED_ALTERNATIVE_NAMES = (
    "UIP (Netherlands)",
    "United International Pictures (Netherlands)",
)


def _collect_strings(payload: Any) -> list[str]:
    """Flatten every string leaf of a nested JSON-like structure.

    The OpenCorporates detail sections (``registered_address``,
    ``alternative_names``) nest their values differently per jurisdiction, so
    assertions are made against the flattened string leaves rather than an
    assumed key layout.

    Args:
        payload: A ``dict``, ``list``, scalar, or ``None`` from a detail record.

    Returns:
        Every string leaf found, in traversal order.
    """
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, dict):
        return [leaf for value in payload.values() for leaf in _collect_strings(value)]
    if isinstance(payload, list):
        return [leaf for entry in payload for leaf in _collect_strings(entry)]
    return []


@unittest.skip("Live OpenCorporates API + database integration; disabled by filename until 2026-09-17, now an explicit skip. Remove the decorator to run it deliberately.")
class LiveOpenCorpTestCase(unittest.TestCase):
    """Base case that provisions a live client or skips when unavailable."""

    def setUp(self) -> None:
        """Skip unless a real token is set and the database is reachable.

        Raises:
            unittest.SkipTest: If the token is absent/placeholder or the
                database cannot be reached.
        """
        if not _ENV_TOKEN or _ENV_TOKEN == "dummy":
            self.skipTest("opencorporates_api_token env var is not set.")

        self.repository = OpenCorpIndexRepository()
        try:
            with self.repository.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as connection_error:  # pragma: no cover - env dependent
            self.skipTest(f"Database is not reachable: {connection_error}")

        self.repository.ensure_tables()
        self.client = OpenCorpClient(repository=self.repository)

    def tearDown(self) -> None:
        """Dispose the engine so its pooled connections are closed here.

        ``setUp`` builds a repository -- and therefore an engine and its
        connection pool -- per test. Without an explicit dispose the pooled
        psycopg connections stay open until the interpreter finalizes them,
        which surfaces as ``ResourceWarning: connection was deleted while still
        open`` once ``unittest`` re-enables default warning filters. Guarded
        with ``getattr`` because the token check in ``setUp`` can skip before
        the repository is assigned.
        """
        repository = getattr(self, "repository", None)
        if repository is not None:
            repository.engine.dispose()


@unittest.skip("Live OpenCorporates API + database integration; disabled by filename until 2026-09-17, now an explicit skip. Remove the decorator to run it deliberately.")
class TestOpenCorpIntegration(LiveOpenCorpTestCase):
    """Live search-and-cache round trip against the real API and DB."""

    def test_swiss_re_search_caches_on_second_call(self) -> None:
        """First search fetches and persists; the second is served from cache."""
        first = self.client.search_companies(
            _SEARCH_QUERY,
            jurisdiction_code=_SEARCH_JURISDICTION,
            force_refresh=True,
        )
        self.assertFalse(first.from_cache)
        self.assertGreater(len(first.companies), 0)

        second = self.client.search_companies(
            _SEARCH_QUERY,
            jurisdiction_code=_SEARCH_JURISDICTION,
        )
        self.assertTrue(second.from_cache)
        self.assertEqual(len(second.companies), len(first.companies))


@unittest.skip("Live OpenCorporates API + database integration; disabled by filename until 2026-09-17, now an explicit skip. Remove the decorator to run it deliberately.")
class TestEmiratesLookup(LiveOpenCorpTestCase):
    """Live search-and-cache round trip for a UAE ('ae') jurisdiction query."""

    def test_emirates_search_caches_on_second_call(self) -> None:
        """First search fetches and persists; the second is served from cache."""
        first = self.client.search_companies(
            _EMIRATES_QUERY,
            jurisdiction_code=_EMIRATES_JURISDICTION,
            force_refresh=True,
        )
        self.assertFalse(first.from_cache)
        self.assertGreater(len(first.companies), 0)
        for company in first.companies:
            with self.subTest(company_number=company.company_number):
                self.assertEqual(company.jurisdiction_code, _EMIRATES_JURISDICTION)

        second = self.client.search_companies(
            _EMIRATES_QUERY,
            jurisdiction_code=_EMIRATES_JURISDICTION,
        )
        self.assertTrue(second.from_cache)
        self.assertEqual(len(second.companies), len(first.companies))


@unittest.skip("Live OpenCorporates API + database integration; disabled by filename until 2026-09-17, now an explicit skip. Remove the decorator to run it deliberately.")
class TestUnitedInternationalPicturesLookup(LiveOpenCorpTestCase):
    """Live lookup of United International Pictures B.V. in the Netherlands."""

    def test_search_returns_uip_netherlands_record(self) -> None:
        """The 'nl' search surfaces the company under its registered number."""
        search_result = self.client.search_companies(
            _UIP_QUERY,
            jurisdiction_code=_UIP_JURISDICTION,
            force_refresh=True,
        )

        self.assertFalse(search_result.from_cache)
        self.assertGreater(len(search_result.companies), 0)

        matches = [
            company
            for company in search_result.companies
            if company.company_number == _UIP_COMPANY_NUMBER
        ]
        self.assertEqual(
            len(matches),
            1,
            f"Expected exactly one hit for company number {_UIP_COMPANY_NUMBER}; "
            f"got {[company.company_number for company in search_result.companies]}",
        )

        company = matches[0]
        self.assertEqual(company.name, _UIP_EXPECTED_NAME)
        self.assertEqual(company.jurisdiction_code, _UIP_JURISDICTION)
        self.assertTrue(company.inactive)

    def test_company_details_match_registry_record(self) -> None:
        """The detail record carries the type, status, address, and aliases."""
        detail = self.client.fetch_company_details(
            _UIP_JURISDICTION,
            _UIP_COMPANY_NUMBER,
            force_refresh=True,
        )

        self.assertIsNotNone(detail, "OpenCorporates returned no detail record.")
        self.assertEqual(detail.name, _UIP_EXPECTED_NAME)
        self.assertEqual(detail.company_number, _UIP_COMPANY_NUMBER)
        self.assertEqual(detail.jurisdiction_code, _UIP_JURISDICTION)
        self.assertEqual(detail.company_type, _UIP_EXPECTED_COMPANY_TYPE)
        self.assertTrue(detail.inactive, "Expected an inactive company.")
        self.assertFalse(detail.from_cache)

        address_text = " ".join(
            [detail.registered_address_in_full or ""]
            + _collect_strings(detail.registered_address)
        ).casefold()
        for address_part in _UIP_EXPECTED_ADDRESS_PARTS:
            with self.subTest(address_part=address_part):
                self.assertIn(address_part.casefold(), address_text)

        alternative_names = {
            name.casefold() for name in _collect_strings(detail.alternative_names)
        }
        for alternative_name in _UIP_EXPECTED_ALTERNATIVE_NAMES:
            with self.subTest(alternative_name=alternative_name):
                self.assertIn(alternative_name.casefold(), alternative_names)

    def test_company_details_second_call_is_cached(self) -> None:
        """The detail record persists, so the follow-up call hits the cache."""
        fresh = self.client.fetch_company_details(
            _UIP_JURISDICTION,
            _UIP_COMPANY_NUMBER,
            force_refresh=True,
        )
        self.assertIsNotNone(fresh)

        cached = self.client.fetch_company_details(
            _UIP_JURISDICTION,
            _UIP_COMPANY_NUMBER,
        )
        self.assertIsNotNone(cached)
        self.assertTrue(cached.from_cache)
        self.assertEqual(cached.name, _UIP_EXPECTED_NAME)
        self.assertEqual(cached.company_type, _UIP_EXPECTED_COMPANY_TYPE)


if __name__ == "__main__":
    unittest.main()
