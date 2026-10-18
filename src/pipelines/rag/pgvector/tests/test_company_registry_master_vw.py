"""Invariant tests for ``celex_facts.company_registry_master_vw``.

The view unions four company sources onto one roster anchor, so the ways it can
break are structural rather than arithmetic: a lost ``LIMIT 1`` on the
OpenCorporates candidate pool, a sentinel string leaking through as if it were
data, or a branch emitting the wrong ``source_rank`` literal.  Each test below
targets one such failure mode.

Row counts per source are deliberately NOT asserted.  They are data facts that
drift as the OpenCorporates and LLM batches run, not invariants of the view.

The suite self-skips when the view is absent so it stays green before
``migrate_registry_master_view.sql`` is applied.
"""
import unittest

from sqlalchemy import text

from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL

VIEW = "celex_facts.company_registry_master_vw"
ROSTER = "celex_facts.company_investigated2celex_id"

#: The trust ladder the view promises consumers.  A branch emitting the wrong
#: literal would silently invert every ``DISTINCT ON (company_key)`` read.
EXPECTED_SOURCE_RANKS = {
    "lei": 1,
    "handelsregister": 2,
    "opencorporates": 3,
    "llm": 4,
}

#: The relaxation rungs ``opencorp_resolution_ladder.py`` can persist.  An
#: unrecognised value means the ladder grew a rung the view's ordering array
#: does not rank.
KNOWN_RESOLUTION_RUNGS = frozenset({
    "verbatim", "verbatim@any", "split", "no-paren",
    "no-role", "core", "core@country", "core@any",
})

#: Columns where the literal 'NA' can only be the "asked, did not know"
#: sentinel.  ``iso_country_code`` and ``jurisdiction_code`` are excluded on
#: purpose: 'NA' is the real ISO 3166-1 code for Namibia, and corp_lei holds
#: genuinely Namibian rows.
SENTINEL_FREE_COLUMNS = (
    "registry_id",
    "registry_name",
    "registry_id_scheme",
    "registered_address",
    "status_text",
    "legal_form",
)


class TestCompanyRegistryMasterView(unittest.TestCase):
    """Structural guarantees the view must hold whatever the data says."""

    @classmethod
    def setUpClass(cls) -> None:
        """Bind an engine, skipping the whole suite when the view is absent."""
        cls.engine = make_engine(DATABASE_URL)
        with cls.engine.connect() as conn:
            present = conn.execute(
                text(f"SELECT to_regclass('{VIEW}')")
            ).scalar()
        if present is None:
            raise unittest.SkipTest(
                "company_registry_master_vw absent -- apply "
                "migrate_registry_master_view.sql first"
            )

    def _scalar(self, sql: str):
        """Run a read-only scalar query against the configured database.

        Args:
            sql: A SELECT returning exactly one column of one row.

        Returns:
            The single value the query produced.
        """
        with self.engine.connect() as conn:
            return conn.execute(text(sql)).scalar()

    def _rows(self, sql: str) -> list[tuple]:
        """Run a read-only query and return every row as a tuple.

        Args:
            sql: A SELECT statement.

        Returns:
            All result rows.
        """
        with self.engine.connect() as conn:
            return list(conn.execute(text(sql)).all())

    def test_one_row_per_company_and_source(self) -> None:
        """No branch may fan out -- this is the load-bearing invariant.

        The OpenCorporates link table is a candidate pool averaging 20.4 rows
        per roster company, so a dropped ``LIMIT 1`` turns roughly 6,400 anchors
        into roughly 83,000 rows.  The GLEIF name lookups can fan out too if
        their de-duplication is removed.
        """
        duplicates = self._rows(
            f"SELECT company_key, source, count(*) FROM {VIEW} "
            "GROUP BY 1, 2 HAVING count(*) > 1"
        )
        self.assertEqual([], duplicates, "view fans out per (company_key, source)")

    def test_cardinality_stays_within_four_rows_per_roster_row(self) -> None:
        """Four sources, one row each at most, so 4x the roster is the ceiling."""
        view_rows = self._scalar(f"SELECT count(*) FROM {VIEW}")
        roster_rows = self._scalar(f"SELECT count(*) FROM {ROSTER}")
        self.assertLessEqual(view_rows, 4 * roster_rows)
        self.assertGreater(view_rows, 0, "view is empty -- every branch lost its join")

    def test_every_company_key_is_a_roster_row(self) -> None:
        """The anchor must survive every branch's joins."""
        orphans = self._scalar(
            f"SELECT count(*) FROM {VIEW} v "
            f"WHERE NOT EXISTS (SELECT 1 FROM {ROSTER} r WHERE r.id = v.company_key)"
        )
        self.assertEqual(0, orphans)

    def test_source_rank_ladder_is_exactly_as_documented(self) -> None:
        """Each source carries one rank, and the ladder is the promised one."""
        pairs = self._rows(
            f"SELECT source, min(source_rank), max(source_rank) FROM {VIEW} "
            "GROUP BY 1"
        )
        observed = {}
        for source, low, high in pairs:
            self.assertEqual(low, high, f"{source} carries two different ranks")
            observed[source] = int(low)
        self.assertEqual(EXPECTED_SOURCE_RANKS, observed)

    def test_no_na_sentinel_leaks_into_data_columns(self) -> None:
        """The 'NA' sentinels on the roster and on llm_index_companies are stripped.

        Country and jurisdiction are excluded by design -- there 'NA' is Namibia.
        """
        for column in SENTINEL_FREE_COLUMNS:
            with self.subTest(column=column):
                leaked = self._scalar(
                    f"SELECT count(*) FROM {VIEW} WHERE {column} = 'NA'"
                )
                self.assertEqual(0, leaked, f"{column} leaks the 'NA' sentinel")

    def test_iso_country_code_is_always_a_two_letter_code(self) -> None:
        """Free-text country from the llm branch must never reach consumers."""
        malformed = self._scalar(
            f"SELECT count(*) FROM {VIEW} "
            "WHERE iso_country_code IS NOT NULL AND iso_country_code !~ '^[A-Z]{2}$'"
        )
        self.assertEqual(0, malformed)

    def test_opencorporates_rows_are_scored_and_carry_a_known_rung(self) -> None:
        """Dropping the ``match_score IS NOT NULL`` filter readmits the pool.

        Roughly 78,000 of the 83,000 link rows were never adjudicated, so an
        unscored row in the view means the restriction was lost.
        """
        unscored = self._scalar(
            f"SELECT count(*) FROM {VIEW} "
            "WHERE source = 'opencorporates' AND link_confidence IS NULL"
        )
        self.assertEqual(0, unscored)

        rungs = {
            row[0]
            for row in self._rows(
                f"SELECT DISTINCT link_method FROM {VIEW} "
                "WHERE source = 'opencorporates'"
            )
        }
        self.assertTrue(
            rungs <= KNOWN_RESOLUTION_RUNGS,
            f"unrecognised resolution rungs: {sorted(rungs - KNOWN_RESOLUTION_RUNGS)}",
        )

    def test_lei_branch_emits_well_formed_leis(self) -> None:
        """A 20-character identifier is the cheapest proof of the right join."""
        malformed = self._scalar(
            f"SELECT count(*) FROM {VIEW} "
            "WHERE source = 'lei' "
            "AND (length(registry_id) <> 20 OR registry_id_scheme <> 'LEI')"
        )
        self.assertEqual(0, malformed)

    def test_roster_view_keeps_all_seven_columns(self) -> None:
        """Guard the replay trap that has broken this view once already.

        ``company_investigated2lei2corpidx2hr_vw`` lost ``jurisdication_code``
        when a stale six-column definition was replayed, which breaks
        ``Main_ingest_opencorp_index_companies.py`` with UndefinedColumn.
        """
        columns = {
            row[0]
            for row in self._rows(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'celex_facts' "
                "AND table_name = 'company_investigated2lei2corpidx2hr_vw'"
            )
        }
        self.assertEqual(
            {
                "company_name", "address", "iso_country_code", "fk_lei",
                "fk_opencorp_idx", "fk_handelsregister", "jurisdication_code",
            },
            columns,
        )


if __name__ == "__main__":
    unittest.main()
