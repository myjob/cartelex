"""Tests for the master backfill mapper (no DB: the case lookup is a fake)."""

import unittest
from pathlib import Path

import polars as pl

from pipelines.download.mappers import MAPPERS, build_mapper, mapper_for
from pipelines.download.master_backfill import (
    REASON_AMBIGUOUS_CASE_X,
    REASON_CASE_UNRESOLVED,
    CelexCaseLookup,
    MasterBackfillMapper,
)

MASTER_FILE = Path("master_doc_descriptors.json")
URL_HTTPS = "https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX:31964D0233"
URL_HTTP_FROM = ("http://eur-lex.europa.eu/legal-content/EN/TXT/PDF/"
                 "?uri=CELEX:31964D0233&from=EN")
EC_URL = "https://ec.europa.eu/competition/antitrust/cases/dec_docs/35388/35388_9_5.pdf"


class FakeCases:
    """CaseLookup fake with canned answers."""

    def __init__(self, by_key: dict[str, list[str]] | None = None,
                 by_x: dict[str, list[str]] | None = None) -> None:
        """Create the fake.

        Args:
            by_key: master key -> case ids.
            by_x: raw case_x -> case ids.
        """
        self.by_key = by_key or {}
        self.by_x = by_x or {}

    def case_ids_for(self, key: str) -> list[str]:
        """Return the canned case ids of a key.

        Args:
            key: Master key.

        Returns:
            Case ids.
        """
        return self.by_key.get(key, [])

    def normalise_case_x(self, raw: str) -> list[str]:
        """Return the canned case ids of a raw case.x.

        Args:
            raw: Raw case.x.

        Returns:
            Case ids.
        """
        return self.by_x.get(raw, [])


def _entry(url: str, lang: str = "en", **web: str) -> dict:
    """Build a master entry.

    Args:
        url: url_pdf.
        lang: language_init.
        **web: Extra webDocDescriptor keys.

    Returns:
        Entry dict.
    """
    return {"language_init": lang,
            "webDocDescriptor": {"url_pdf": url, "title": "", "case": "", **web}}


def _loader(*rows: dict[str, str | None]) -> pl.DataFrame:
    """Build an initial-loader frame.

    Args:
        *rows: Row dicts (case.x, url_pdf, title, ...).

    Returns:
        All-string frame.
    """
    cols = list(dict.fromkeys(k for r in rows for k in r))
    return pl.DataFrame({c: [r.get(c) for r in rows] for c in cols},
                        schema=dict.fromkeys(cols, pl.String))


def _map(master: dict, loader: pl.DataFrame | None, cases: FakeCases):
    """Run the mapper.

    Args:
        master: Master documents dict.
        loader: Initial loader or None.
        cases: Case lookup.

    Returns:
        MapperResult.
    """
    return MasterBackfillMapper(master, loader, cases).map(pl.DataFrame(), MASTER_FILE)


class MasterBackfillMapperTest(unittest.TestCase):
    """Mapping of master entries to decisions."""

    def test_registered_and_flags(self) -> None:
        """The mapper is registered; it needs the master but no file manifest."""
        self.assertIs(MAPPERS["master_backfill"], MasterBackfillMapper)
        self.assertIs(mapper_for(MASTER_FILE, "master_backfill"), MasterBackfillMapper)
        self.assertTrue(MasterBackfillMapper.needs_master)
        self.assertFalse(MasterBackfillMapper.needs_file_manifest)
        self.assertFalse(MAPPERS["case_index"].needs_master)

    def test_build_mapper_passes_master_loader_and_cases(self) -> None:
        """build_mapper injects the three collaborators."""
        mapper = build_mapper(MASTER_FILE, "master_backfill", None,
                              master={}, initial_loader=None, cases=FakeCases())
        self.assertIsInstance(mapper, MasterBackfillMapper)
        with self.assertRaises(ValueError):
            build_mapper(MASTER_FILE, "master_backfill", None)

    def test_each_master_entry_becomes_one_decision_with_existing_key(self) -> None:
        """One decision per entry: key as id and existing key, CELEX only if real."""
        master = {"CELEX-31964D0233": _entry(URL_HTTPS),
                  "35388_9_5": _entry(EC_URL, "de"),
                  "c1417": _entry(EC_URL.replace("_5", "_6"))}
        result = _map(master, None, FakeCases())
        self.assertEqual(result.failures, [])
        self.assertEqual([d.decision_id for d in result.decisions],
                         ["CELEX-31964D0233", "35388_9_5", "c1417"])
        self.assertEqual([d.celex_number for d in result.decisions],
                         ["CELEX-31964D0233", None, None])
        self.assertEqual([d.loader_row for d in result.decisions], [1, 2, 3])
        self.assertEqual({d.loader_file for d in result.decisions},
                         {MASTER_FILE.name})
        first, second, _ = result.decisions
        self.assertEqual(len(first.candidates), 1)
        cand = first.candidates[0]
        self.assertEqual((cand.source_url, cand.file_format, cand.language),
                         (URL_HTTPS, "pdf", "EN"))
        self.assertEqual(second.candidates[0].language, "DE")

    def test_initial_loader_joined_on_pdf_url_key_into_meta(self) -> None:
        """http/https and &from=EN variants join; loader columns land in meta."""
        master = {"CELEX-31964D0233": _entry(URL_HTTPS, extra="x")}
        loader = _loader({"case.x": "61", "url_pdf": URL_HTTP_FROM,
                          "title": "Grosfillex", "Parties": "Grosfillex & F",
                          "decision_date": "1964-03-11 00:00:00", "Fines": "0"})
        decision = _map(master, loader, FakeCases(by_key={
            "CELEX-31964D0233": ["IV/61"]})).decisions[0]
        self.assertEqual(decision.title, "Grosfillex")
        self.assertEqual(decision.parties, "Grosfillex & F")
        self.assertEqual(decision.decision_date.isoformat(), "1964-03-11")
        self.assertEqual(decision.meta["Fines"], "0")
        self.assertEqual(decision.meta["case.x"], "61")
        self.assertEqual(decision.meta["extra"], "x")
        self.assertNotIn("matched initial loader", " ".join(decision.review_reasons))

    def test_master_entry_without_loader_row_still_mapped(self) -> None:
        """No loader row: title, parties and date come from the entry."""
        master = {"40013_1084_2": _entry(
            EC_URL, title="Lighting Systems", firms="A; B",
            decision_date="2017-06-21 00:00:00")}
        loader = _loader({"case.x": "9", "url_pdf": "http://x/other.pdf"})
        decision = _map(master, loader, FakeCases()).decisions[0]
        self.assertEqual(decision.title, "Lighting Systems")
        self.assertEqual(decision.parties, "A; B")
        self.assertEqual(decision.decision_date.isoformat(), "2017-06-21")
        self.assertEqual(decision.meta["title"], "Lighting Systems")

    def test_duplicate_loader_url_first_row_wins_with_review_reason(self) -> None:
        """Two loader rows with one url_pdf: first wins, reason lists the others."""
        master = {"CELEX-31964D0233": _entry(URL_HTTPS)}
        loader = _loader(
            {"case.x": "61", "url_pdf": URL_HTTPS, "title": "First"},
            {"case.x": "62", "url_pdf": URL_HTTP_FROM, "title": "Second"})
        decision = _map(master, loader, FakeCases(by_key={
            "CELEX-31964D0233": ["IV/61"]})).decisions[0]
        self.assertEqual(decision.title, "First")
        self.assertTrue(any("duplicate url_pdf" in r and "2" in r
                            for r in decision.review_reasons))

    def test_case_ids_from_celex_case_id_first(self) -> None:
        """Rows of the master key win over the loader's case.x."""
        master = {"CELEX-31964D0233": _entry(URL_HTTPS)}
        loader = _loader({"case.x": "61", "url_pdf": URL_HTTPS})
        cases = FakeCases(by_key={"CELEX-31964D0233": ["IV/61"]},
                          by_x={"61": ["IV/OTHER"]})
        decision = _map(master, loader, cases).decisions[0]
        self.assertEqual(decision.case_ids, ["IV/61"])
        self.assertEqual(decision.review_reasons, [])

    def test_case_x_fallback_via_celex_case_id(self) -> None:
        """No row for the key: the loader's case.x is looked up."""
        master = {"CELEX-31964D0233": _entry(URL_HTTPS)}
        loader = _loader({"case.x": "61", "url_pdf": URL_HTTPS})
        decision = _map(master, loader, FakeCases(by_x={"61": ["IV/61"]})).decisions[0]
        self.assertEqual(decision.case_ids, ["IV/61"])
        self.assertEqual(decision.review_reasons, [])

    def test_ambiguous_case_x_gives_review_reason(self) -> None:
        """Several case ids for one raw case.x: none picked, reason set."""
        master = {"CELEX-31964D0233": _entry(URL_HTTPS)}
        loader = _loader({"case.x": "61", "url_pdf": URL_HTTPS})
        cases = FakeCases(by_x={"61": ["IV/61", "IV/A/61"]})
        decision = _map(master, loader, cases).decisions[0]
        self.assertEqual(decision.case_ids, [])
        self.assertIn(REASON_AMBIGUOUS_CASE_X, decision.review_reasons)

    def test_descriptor_case_fallback_for_master_only_entry(self) -> None:
        """No key rows, no loader: webDocDescriptor.case is looked up as case_x."""
        master = {"c1": _entry(URL_HTTPS, case="61")}
        decision = _map(master, None, FakeCases(by_x={"61": ["IV/61"]})).decisions[0]
        self.assertEqual(decision.case_ids, ["IV/61"])
        self.assertEqual(decision.review_reasons, [])
        ambiguous = _map(master, None, FakeCases(by_x={"61": ["a", "b"]})).decisions[0]
        self.assertEqual(ambiguous.case_ids, [])
        self.assertIn(REASON_AMBIGUOUS_CASE_X, ambiguous.review_reasons)

    def test_unresolved_case_gives_review_reason(self) -> None:
        """Neither route resolves: reason 'case id unresolved', still mapped."""
        for loader in (None, _loader({"case.x": "61", "url_pdf": URL_HTTPS})):
            master = {"CELEX-31964D0233": _entry(URL_HTTPS)}
            decision = _map(master, loader, FakeCases()).decisions[0]
            self.assertEqual(decision.case_ids, [])
            self.assertIn(REASON_CASE_UNRESOLVED, decision.review_reasons)

    def test_entry_without_url_is_a_row_failure(self) -> None:
        """An entry with no url_pdf cannot download: audited row failure."""
        master = {"c1": {"webDocDescriptor": {}}, "CELEX-31964D0233": _entry(URL_HTTPS)}
        result = _map(master, None, FakeCases())
        self.assertEqual(len(result.decisions), 1)
        self.assertEqual([f.loader_row for f in result.failures], [1])


class CelexCaseLookupTest(unittest.TestCase):
    """The adapter over CelexIdentityRepository.select_distinct_book_cases."""

    class _Repo:
        """Repository fake counting snapshot reads."""

        def __init__(self) -> None:
            """Create the fake."""
            self.reads = 0

        def select_distinct_book_cases(self) -> list[dict]:
            """Return canned celex_case_id rows.

            Returns:
                Rows with celex_id, case_x, case_id.
            """
            self.reads += 1
            return [
                {"celex_id": "CELEX-1", "case_x": "61", "case_id": "IV/61"},
                {"celex_id": "CELEX-1", "case_x": "61", "case_id": "IV/61"},
                {"celex_id": "k_2", "case_x": "7", "case_id": "IV/7"},
                {"celex_id": "k_3", "case_x": "7", "case_id": "IV/A/7"},
                {"celex_id": "k_4", "case_x": None, "case_id": None},
            ]

    def test_lookups_use_one_snapshot_read(self) -> None:
        """Key and raw case.x lookups are distinct, sorted and cached."""
        repo = self._Repo()
        lookup = CelexCaseLookup(repo)
        self.assertEqual(lookup.case_ids_for("CELEX-1"), ["IV/61"])
        self.assertEqual(lookup.case_ids_for("nope"), [])
        self.assertEqual(lookup.normalise_case_x("7"), ["IV/7", "IV/A/7"])
        self.assertEqual(lookup.normalise_case_x(" 61 "), ["IV/61"])
        self.assertEqual(lookup.normalise_case_x("zzz"), [])
        self.assertEqual(repo.reads, 1)


if __name__ == "__main__":
    unittest.main()
