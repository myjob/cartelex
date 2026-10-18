"""Unit tests for the OpenCorporates repository DAO.

The engine is never connected: the repository is built with a mock engine and
its session factory is replaced with a mock session, so these tests exercise
the transaction shape and the row/model mapping without a database.
"""

import os
import unittest
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

# Provide a dummy token before importing settings-dependent modules.
os.environ.setdefault("opencorporates_api_token", "dummy")

from pipelines.corp_index.opencorp_models import (  # noqa: E402
    OpenCorpCompany,
    OpenCorpCompanyDetail,
    OpenCorpSearchResult,
)
from pipelines.corp_index.opencorp_repository import (  # noqa: E402
    OpenCorpIndexRepository,
)


def _build_repository_with_mock_session() -> tuple[OpenCorpIndexRepository, mock.MagicMock]:
    """Construct a repository whose session factory returns a mock session.

    Returns:
        A tuple of the repository and the mock session it will hand out.
    """
    repository = OpenCorpIndexRepository(engine=mock.MagicMock())
    session = mock.MagicMock()
    repository._session_factory = mock.MagicMock(return_value=session)
    return repository, session


class TestOpenCorpRepositoryMapping(unittest.TestCase):
    """Row/model round-trip mapping tests using attribute-compatible stand-ins."""

    def test_company_columns_round_trip(self) -> None:
        """A company maps to columns and back to an equal model."""
        company = OpenCorpCompany(
            name="Swiss Re Ltd",
            company_number="CHE-1",
            jurisdiction_code="ch",
            incorporation_date="1863-12-19",
            dissolution_date=None,
            company_type="Aktiengesellschaft",
            registry_url="https://example.test/1",
            inactive=False,
            current_status="Aktive",
            opencorporates_url="https://opencorporates.com/companies/ch/1",
            previous_names=[{"company_name": "Old Re"}],
        )

        columns = OpenCorpIndexRepository._company_to_columns(company)
        row = SimpleNamespace(**columns)
        restored = OpenCorpIndexRepository._company_from_row(row)

        self.assertEqual(restored, company)

    def test_detail_columns_round_trip(self) -> None:
        """A detail record maps to columns and back to an equal model."""
        detail = OpenCorpCompanyDetail(
            name="OPENCORPORATES LTD",
            company_number="07444723",
            jurisdiction_code="gb",
            incorporation_date="2010-11-18",
            number_of_employees="42",
            officers=[{"officer": {"name": "A Person"}}],
            registered_address={"locality": "London"},
            source={"publisher": "UK Companies House"},
        )

        columns = OpenCorpIndexRepository._detail_to_columns(detail)
        row = SimpleNamespace(**columns)
        restored = OpenCorpIndexRepository._detail_from_row(row)

        self.assertEqual(restored.company_number, "07444723")
        self.assertEqual(restored.number_of_employees, "42")
        self.assertEqual(restored.officers, [{"officer": {"name": "A Person"}}])
        self.assertEqual(restored.registered_address, {"locality": "London"})
        self.assertTrue(restored.from_cache)

    def test_number_of_employees_stored_as_text(self) -> None:
        """Integer employee counts are stringified for the text column."""
        detail = OpenCorpCompanyDetail(
            name="X",
            company_number="1",
            jurisdiction_code="gb",
            number_of_employees=100,
        )

        columns = OpenCorpIndexRepository._detail_to_columns(detail)

        self.assertEqual(columns["number_of_employees"], "100")


class TestOpenCorpRepositorySaveSearch(unittest.TestCase):
    """Transaction-shape tests for save_search."""

    def test_save_search_deletes_then_inserts_in_one_transaction(self) -> None:
        """An existing query row is deleted before new rows are inserted."""
        repository, session = _build_repository_with_mock_session()
        existing = mock.sentinel.existing_query
        session.execute.return_value.scalar_one_or_none.return_value = existing

        result = OpenCorpSearchResult(
            query_text="Swiss Re",
            normalized_query="swiss re",
            jurisdiction_code="ch",
            fetched_pages=1,
            total_pages=1,
            total_count=2,
            companies=[
                OpenCorpCompany(
                    name="Swiss Re Ltd", company_number="1", jurisdiction_code="ch"
                ),
                OpenCorpCompany(
                    name="Swiss Re AG", company_number="2", jurisdiction_code="ch"
                ),
            ],
        )

        returned_id = repository.save_search(result)

        session.delete.assert_called_once_with(existing)
        # One query row plus one row per company.
        self.assertEqual(session.add.call_count, 3)
        session.commit.assert_called_once()
        session.close.assert_called_once()
        self.assertEqual(returned_id, session.add.call_args_list[0].args[0].id)

    def test_save_search_without_existing_skips_delete(self) -> None:
        """With no cached row, no delete is issued but inserts still happen."""
        repository, session = _build_repository_with_mock_session()
        session.execute.return_value.scalar_one_or_none.return_value = None

        result = OpenCorpSearchResult(
            query_text="Swiss Re",
            normalized_query="swiss re",
            jurisdiction_code=None,
            fetched_pages=1,
            companies=[],
        )

        repository.save_search(result)

        session.delete.assert_not_called()
        session.add.assert_called_once()
        session.commit.assert_called_once()

    def test_save_search_rolls_back_on_error(self) -> None:
        """A commit failure triggers a rollback and re-raises."""
        repository, session = _build_repository_with_mock_session()
        session.execute.return_value.scalar_one_or_none.return_value = None
        session.commit.side_effect = RuntimeError("boom")

        result = OpenCorpSearchResult(
            query_text="Swiss Re",
            normalized_query="swiss re",
            fetched_pages=1,
            companies=[],
        )

        with self.assertRaises(RuntimeError):
            repository.save_search(result)

        session.rollback.assert_called_once()


class TestOpenCorpRepositorySaveDetails(unittest.TestCase):
    """Upsert tests for save_company_details."""

    def test_save_company_details_upserts_and_returns_id(self) -> None:
        """save_company_details executes one upsert and returns the row id."""
        repository, session = _build_repository_with_mock_session()
        expected_id = uuid4()
        session.execute.return_value.scalar_one.return_value = expected_id

        detail = OpenCorpCompanyDetail(
            name="OPENCORPORATES LTD",
            company_number="07444723",
            jurisdiction_code="gb",
        )

        returned_id = repository.save_company_details(detail)

        self.assertEqual(returned_id, expected_id)
        session.execute.assert_called_once()
        session.commit.assert_called_once()
        session.close.assert_called_once()


class TestOpenCorpRepositorySearch(unittest.TestCase):
    """Read-path tests for search."""

    def test_search_miss_returns_none(self) -> None:
        """A missing cache key returns None."""
        repository, session = _build_repository_with_mock_session()
        session.execute.return_value.scalar_one_or_none.return_value = None

        self.assertIsNone(repository.search("swiss re", "ch"))
        session.close.assert_called_once()

    def test_search_filters_out_errored_rows(self) -> None:
        """search()'s query excludes rows with error_class set."""
        repository, session = _build_repository_with_mock_session()
        session.execute.return_value.scalar_one_or_none.return_value = None

        repository.search("swiss re", "ch")

        select_statement = session.execute.call_args.args[0]
        self.assertIn("error_class IS NULL", str(select_statement))


class TestOpenCorpRepositorySaveQueryError(unittest.TestCase):
    """Transaction-shape tests for save_query_error."""

    def test_save_query_error_deletes_then_inserts_error_row(self) -> None:
        """An existing query row is replaced by one carrying the error columns."""
        repository, session = _build_repository_with_mock_session()
        existing = mock.sentinel.existing_query
        session.execute.return_value.scalar_one_or_none.return_value = existing

        returned_id = repository.save_query_error(
            query_text="Swiss Re",
            normalized_query="swiss re",
            jurisdiction_code="ch",
            error_class="OpenCorpRateLimitError",
            error_status_code=403,
            error_message="OpenCorporates throttled this IP",
        )

        session.delete.assert_called_once_with(existing)
        session.add.assert_called_once()
        inserted_row = session.add.call_args.args[0]
        self.assertEqual(inserted_row.error_class, "OpenCorpRateLimitError")
        self.assertEqual(inserted_row.error_status_code, 403)
        self.assertEqual(
            inserted_row.error_message, "OpenCorporates throttled this IP"
        )
        self.assertEqual(inserted_row.fetched_pages, 0)
        session.commit.assert_called_once()
        session.close.assert_called_once()
        self.assertEqual(returned_id, inserted_row.id)

    def test_save_query_error_without_existing_skips_delete(self) -> None:
        """With no cached row, no delete is issued but the error row is added."""
        repository, session = _build_repository_with_mock_session()
        session.execute.return_value.scalar_one_or_none.return_value = None

        repository.save_query_error(
            query_text="Swiss Re",
            normalized_query="swiss re",
            jurisdiction_code=None,
            error_class="OpenCorpApiError",
            error_status_code=None,
            error_message="transport error",
        )

        session.delete.assert_not_called()
        session.add.assert_called_once()
        session.commit.assert_called_once()

    def test_save_query_error_rolls_back_on_error(self) -> None:
        """A commit failure triggers a rollback and re-raises."""
        repository, session = _build_repository_with_mock_session()
        session.execute.return_value.scalar_one_or_none.return_value = None
        session.commit.side_effect = RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            repository.save_query_error(
                query_text="Swiss Re",
                normalized_query="swiss re",
                jurisdiction_code="ch",
                error_class="OpenCorpApiError",
                error_status_code=500,
                error_message="boom",
            )

        session.rollback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
