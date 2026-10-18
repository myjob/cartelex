"""Unit tests for the LLM company index DAO.

The engine is never connected: the repository is built with a mock engine and
its session factory is replaced with a mock session, so these tests exercise the
statement shape and the row mapping without a database.
"""

import os
import unittest
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from sqlalchemy.dialects import postgresql

# Provide a dummy token before importing settings-dependent modules.
os.environ.setdefault("opencorporates_api_token", "dummy")

from pipelines.corp_index.llm_index_repository import (  # noqa: E402
    _PROFILE_COLUMNS,
    LlmCompanyIndexRepository,
)
from pipelines.corp_index.models import (  # noqa: E402
    LlmCompanyIndexProfile,
    LlmCompanyIndexRow,
)


def _build_repository() -> tuple[LlmCompanyIndexRepository, mock.MagicMock]:
    """Construct a repository whose engine and session factory are mocks.

    Returns:
        A tuple of the repository and the mock connection its ``connect()``
        context manager hands out.
    """
    repository = LlmCompanyIndexRepository(engine=mock.MagicMock())
    connection = repository.engine.connect.return_value.__enter__.return_value
    return repository, connection


def _executed_sql(connection: mock.MagicMock) -> str:
    """Return the SQL text of the single statement executed on a connection.

    Args:
        connection: The mock connection the repository executed against.

    Returns:
        The rendered SQL string.
    """
    return str(connection.execute.call_args.args[0])


class TestSelectTargets(unittest.TestCase):
    """Target selection over the roster view."""

    def setUp(self) -> None:
        """Build a repository returning one view row."""
        self.repository, self.connection = _build_repository()
        self.connection.execute.return_value.fetchall.return_value = [
            SimpleNamespace(
                company_name="Brasserie de Wiltz",
                address="14 rue Joseph Simon, L-9550 Wiltz",
                iso_country_code="LU",
                jurisdication_code="LU",
                lei=None,
                handelsregister=None,
            )
        ]

    def test_rows_map_to_targets(self) -> None:
        """A view row becomes a validated target."""
        targets = self.repository.select_targets()

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].company_name, "Brasserie de Wiltz")
        self.assertEqual(targets[0].iso_country_code, "LU")
        self.assertIsNone(targets[0].lei)

    def test_default_selection_is_the_opencorporates_residue(self) -> None:
        """By default only fk_opencorp_idx = 'NA' companies are selected."""
        self.repository.select_targets()

        params = self.connection.execute.call_args.args[1]
        self.assertEqual(params["marker"], "NA")
        self.assertIn("fk_opencorp_idx = :marker", _executed_sql(self.connection))

    def test_marker_none_selects_every_company(self) -> None:
        """A None marker drops the fk_opencorp_idx filter entirely."""
        self.repository.select_targets(marker=None)

        self.assertNotIn("fk_opencorp_idx", _executed_sql(self.connection))
        self.assertNotIn("marker", self.connection.execute.call_args.args[1])

    def test_stored_companies_are_anti_joined_out(self) -> None:
        """Resume works by absence: profiled companies leave the target set."""
        self.repository.select_targets()

        sql = _executed_sql(self.connection)
        self.assertIn("NOT EXISTS", sql)
        self.assertIn("llm_index_companies", sql)

    def test_refresh_reopens_stored_companies(self) -> None:
        """include_existing drops the anti-join so answers can be re-asked."""
        self.repository.select_targets(include_existing=True)

        self.assertNotIn("NOT EXISTS", _executed_sql(self.connection))

    def test_retry_unknown_selects_the_profiles_that_answered_nothing(self) -> None:
        """The inverse of the resume anti-join: stored, but useless.

        A row whose life cycle and founding year are both unknown cost a cloud
        call and told the fact pipeline nothing. Selecting it needs EXISTS
        rather than NOT EXISTS -- and the two must never both appear, since no
        company is simultaneously absent from and present in the table.
        """
        self.repository.select_targets(retry_unknown=True)

        sql = _executed_sql(self.connection)
        self.assertIn("EXISTS", sql)
        self.assertNotIn("NOT EXISTS", sql)
        self.assertIn("coalesce(stored.life_cycle_status", sql)
        self.assertIn("coalesce(stored.established_since", sql)
        self.assertEqual(
            self.connection.execute.call_args.args[1]["marker_no_hit"], "NA"
        )

    def test_retry_unknown_treats_null_as_unknown(self) -> None:
        """NULL means "written before the column existed"; 'NA' means "asked".

        Both are unknown for this purpose. Matching only 'NA' found 11 rows on
        2026-08-02 where 680 were unknown, because established_since is NULL on
        every row written before it was added.
        """
        self.repository.select_targets(retry_unknown=True)

        sql = _executed_sql(self.connection)
        for column_name in ("life_cycle_status", "established_since"):
            with self.subTest(column=column_name):
                self.assertIn(
                    f"coalesce(stored.{column_name}, :marker_no_hit) = :marker_no_hit",
                    sql,
                )

    def test_retry_unknown_overrides_the_resume_anti_join(self) -> None:
        """It implies include_existing, so the default must not fight it."""
        self.repository.select_targets(retry_unknown=True, include_existing=False)

        self.assertNotIn("NOT EXISTS", _executed_sql(self.connection))

    def test_junk_names_are_never_selected(self) -> None:
        """Short names and names with no alphanumeric character are excluded."""
        self.repository.select_targets(min_name_length=5)

        sql = _executed_sql(self.connection)
        self.assertIn("char_length(btrim(company_name)) >= :min_name_length", sql)
        self.assertIn("[[:alnum:]]", sql)
        self.assertEqual(
            self.connection.execute.call_args.args[1]["min_name_length"], 5
        )

    def test_country_and_limit_are_bound_parameters(self) -> None:
        """The country filter and the cap travel as parameters, not literals."""
        self.repository.select_targets(iso_country_code="DE", limit=25)

        params = self.connection.execute.call_args.args[1]
        self.assertEqual(params["iso_country_code"], "DE")
        self.assertEqual(params["limit"], 25)
        self.assertIn("LIMIT :limit", _executed_sql(self.connection))

    def test_confirmed_identifiers_win_over_the_na_marker(self) -> None:
        """A name whose rows mix 'NA' and a real LEI contributes the real one."""
        self.repository.select_targets()
        sql = _executed_sql(self.connection)

        self.assertIn("max(fk_lei) FILTER (WHERE fk_lei <> 'NA')", sql)
        self.assertIn("fk_handelsregister <> 'NA'", sql)


class TestEnsureTables(unittest.TestCase):
    """Bringing an already-created table forward."""

    def setUp(self) -> None:
        """Build a repository whose engine reports the real Postgres dialect."""
        self.repository, _ = _build_repository()
        # A real dialect so the rendered column types are the ones Postgres
        # would be given, not a mock's repr.
        self.repository.engine.dialect = postgresql.dialect()
        self.transaction = self.repository.engine.begin.return_value.__enter__.return_value

    def test_missing_columns_are_added_idempotently(self) -> None:
        """create_all stops at the table, so each column is added on its own.

        A table created before a column existed never gains it from
        ``CREATE TABLE IF NOT EXISTS``; the next upsert fails on an unknown
        column instead. Every statement is guarded, so this is a no-op on a
        current table.
        """
        with mock.patch(
            "pipelines.corp_index.llm_index_repository.Base.metadata.create_all"
        ):
            self.repository.ensure_tables()

        statements = [
            str(call.args[0]) for call in self.transaction.execute.call_args_list
        ]
        self.assertEqual(len(statements), len(_PROFILE_COLUMNS))
        for statement in statements:
            self.assertIn("ADD COLUMN IF NOT EXISTS", statement)
        self.assertTrue(
            any("succession TEXT" in statement for statement in statements)
        )
        self.assertTrue(
            any("generic_corp_id VARCHAR(64)" in statement for statement in statements)
        )

    def test_added_columns_are_nullable(self) -> None:
        """NOT NULL cannot be added to a populated table without a default."""
        with mock.patch(
            "pipelines.corp_index.llm_index_repository.Base.metadata.create_all"
        ):
            self.repository.ensure_tables()

        for call in self.transaction.execute.call_args_list:
            self.assertNotIn("NOT NULL", str(call.args[0]))


class TestUpsertCompany(unittest.TestCase):
    """Persisting one profile."""

    def setUp(self) -> None:
        """Build a repository whose session factory returns a mock session."""
        self.repository, _ = _build_repository()
        self.session = mock.MagicMock()
        self.repository._session_factory = mock.MagicMock(return_value=self.session)
        self.row_id = uuid4()
        self.session.execute.return_value.scalar_one.return_value = self.row_id
        self.row = LlmCompanyIndexRow(
            company_name="Brasserie de Wiltz",
            llm_model_id="deepseek-v4-pro",
            profile=LlmCompanyIndexProfile(
                canonical_name="Brasserie de Wiltz S.E.C.S.",
                euid="8434-4121-8815-0614",
            ),
        )

    def test_upsert_commits_and_returns_the_row_id(self) -> None:
        """A successful write commits once and closes the session."""
        returned_id = self.repository.upsert_company(self.row)

        self.assertEqual(returned_id, self.row_id)
        self.session.commit.assert_called_once()
        self.session.close.assert_called_once()

    def test_upsert_conflicts_on_the_functional_key(self) -> None:
        """company_name is the conflict target, and updated_at is stamped."""
        self.repository.upsert_company(self.row)

        statement = str(self.session.execute.call_args.args[0])
        self.assertIn("ON CONFLICT (company_name) DO UPDATE", statement)
        self.assertIn("updated_at", statement)
        # created_at records when the company first entered the table, so an
        # overwrite must not touch it.
        self.assertNotIn("created_at =", statement)

    def test_every_profile_field_is_refreshed_by_a_second_run(self) -> None:
        """A field missing from the update set keeps its first answer forever.

        The failure mode this guards is silent: the row is written, the batch
        reports success, and one column simply never changes again. So the model
        the LLM fills in and the columns the upsert overwrites are pinned to each
        other, and only the two the batch does not own may differ.
        """
        refreshable = set(_PROFILE_COLUMNS)

        self.assertEqual(
            refreshable, set(LlmCompanyIndexProfile.model_fields) | {"llm_model_id"}
        )
        self.assertNotIn("company_name", refreshable)
        self.assertNotIn("created_at", refreshable)

    def test_upsert_names_every_refreshable_column(self) -> None:
        """The rendered statement really does set each of them."""
        self.repository.upsert_company(self.row)

        statement = str(self.session.execute.call_args.args[0])
        update_clause = statement.split("DO UPDATE", 1)[1]
        for column_name in _PROFILE_COLUMNS:
            self.assertIn(column_name, update_clause)

    def test_failed_upsert_rolls_back_and_reraises(self) -> None:
        """A write error leaves no half-written row and reaches the caller."""
        self.session.execute.side_effect = RuntimeError("connection lost")

        with self.assertRaises(RuntimeError):
            self.repository.upsert_company(self.row)

        self.session.rollback.assert_called_once()
        self.session.close.assert_called_once()
        self.session.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
