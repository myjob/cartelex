"""Tests for the atomic master descriptor store (temp dirs only)."""

import datetime as dt
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from pipelines.download.descriptor_store import MasterDescriptorStore, TranslationEntry
from pipelines.download.models import CandidateDocument, DecisionRequest

URL_EN = "https://example.org/en/doc.pdf"
URL_DE = "https://example.org/de/doc.pdf"


def _decision(decision_id: str = "CELEX-31990D0645", **kw: object) -> DecisionRequest:
    """Build a decision request for tests.

    Args:
        decision_id: Decision id.
        **kw: Field overrides.

    Returns:
        A DecisionRequest.
    """
    data: dict[str, object] = {
        "decision_id": decision_id,
        "case_ids": ["IV/123"],
        "title": "Some title",
        "parties": "Acme; Beta",
        "decision_date": dt.date(1990, 5, 6),
        "meta": {"year": "1990", "case": "123"},
        "loader_file": "x.xlsx",
        "loader_row": 1,
    }
    data.update(kw)
    return DecisionRequest.model_validate(data)


def _doc(url: str = URL_EN, lang: str | None = "EN") -> CandidateDocument:
    """Build a candidate document for tests.

    Args:
        url: Source URL.
        lang: Language code.

    Returns:
        A CandidateDocument.
    """
    return CandidateDocument(document_id="doc", source_url=url, language=lang)


class StoreTestCase(unittest.TestCase):
    """Shared temp-dir fixture."""

    def setUp(self) -> None:
        """Create a temp dir holding a master file with one existing entry."""
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "master.json"
        self.existing = {
            "documents": {
                "OLD-1": {
                    "gs_public_url": "gs://x",
                    "txt_md_file": "/m/OLD-1.md",
                    "mineru_flag": True,
                    "facts": "/f/OLD-1.json",
                    "local_file_pdf": "/p/OLD-1.pdf",
                    "language_translated": "NA",
                    "CELEX": "OLD-1",
                    "language_init": "en",
                    "unknown_key": {"a": 1},
                    "webDocDescriptor": {
                        "case": "1",
                        "url_pdf": "https://old/x.pdf",
                        "title": "Old title",
                        "firms": "Old firms",
                        "odd": "keep",
                    },
                },
                "OLD-2": {"CELEX": "OLD-2", "webDocDescriptor": {}},
            }
        }
        self.path.write_text(
            json.dumps(self.existing, indent=4, ensure_ascii=False), encoding="utf-8"
        )
        self.store = MasterDescriptorStore(self.path)

    def _reload(self) -> dict:
        """Read the master file back.

        Returns:
            Parsed JSON.
        """
        return json.loads(self.path.read_text(encoding="utf-8"))["documents"]


class TestUpsert(StoreTestCase):
    """Entry creation and merge rules."""

    def test_new_entry_has_design_fields(self) -> None:
        """A new entry carries every design field."""
        pdf = Path("/docs/CELEX-31990D0645/CELEX-31990D0645.pdf")
        self.store = MasterDescriptorStore(self.path)
        self.store.upsert(_decision(), _doc(), pdf, "en")
        self.store.flush()
        entry = self._reload()["CELEX-31990D0645"]
        self.assertEqual(entry["CELEX"], "CELEX-31990D0645")
        self.assertEqual(entry["local_file_pdf"], str(pdf))
        self.assertEqual(
            entry["txt_md_file"],
            "/docs/CELEX-31990D0645/mineru/CELEX-31990D0645.md",
        )
        self.assertIs(entry["mineru_flag"], False)
        self.assertEqual(entry["language_init"], "en")
        self.assertEqual(entry["language_translated"], "NA")
        self.assertEqual(entry["case_ids"], ["IV/123"])
        web = entry["webDocDescriptor"]
        self.assertEqual(
            set(web),
            {"case", "url_pdf", "url_html", "year", "text", "comment", "title",
             "decision_date", "firms", "nace"},
        )
        self.assertEqual(web["url_pdf"], URL_EN)
        self.assertEqual(web["decision_date"], "1990-05-06")
        self.assertEqual(web["url_html"], "")
        self.assertEqual(web["nace"], "")

    def test_extra_meta_keys_kept(self) -> None:
        """Unknown loader meta keys land in webDocDescriptor."""
        dec = _decision(meta={"sector": "steel"})
        self.store.upsert(dec, _doc(), Path("/p/a.pdf"), "en")
        self.store.flush()
        web = self._reload()["CELEX-31990D0645"]["webDocDescriptor"]
        self.assertEqual(web["sector"], "steel")

    def test_existing_entry_preserves_mineru_and_fact_fields(self) -> None:
        """Merging never touches mineru/fact/unknown fields."""
        self.store.upsert(
            _decision("OLD-1"), _doc(), Path("/new/other.pdf"), "de"
        )
        self.store.flush()
        entry = self._reload()["OLD-1"]
        for key, value in self.existing["documents"]["OLD-1"].items():
            if key != "webDocDescriptor":
                self.assertEqual(entry[key], value, key)
        self.assertEqual(entry["case_ids"], ["IV/123"])

    def test_only_supplied_webdoc_keys_updated(self) -> None:
        """Keys the loader does not supply keep their stored value."""
        dec = _decision("OLD-1", title="", parties="", decision_date=None,
                        meta={"year": "1999"})
        self.store.upsert(dec, _doc(URL_EN), Path("/p/a.pdf"), "en")
        self.store.flush()
        web = self._reload()["OLD-1"]["webDocDescriptor"]
        self.assertEqual(web["year"], "1999")
        self.assertEqual(web["title"], "Old title")
        self.assertEqual(web["firms"], "Old firms")
        self.assertEqual(web["odd"], "keep")
        self.assertEqual(web["case"], "1")
        self.assertEqual(web["url_pdf"], "https://old/x.pdf")

    def test_existing_entry_gains_no_extra_meta_keys(self) -> None:
        """Loader meta keys beyond the standard set stay out of old entries."""
        dec = _decision("OLD-1", meta={"sector": "steel", "year": "1990"})
        self.store.upsert(dec, _doc("https://old/x.pdf"), Path("/p"), "en")
        self.store.flush()
        web = self._reload()["OLD-1"]["webDocDescriptor"]
        self.assertNotIn("sector", web)
        self.assertEqual(
            set(web), {"case", "url_pdf", "title", "firms", "odd", "year",
                       "decision_date"},
        )

    def test_existing_non_blank_values_never_rewritten(self) -> None:
        """A stored date/title/case is kept even when the loader differs."""
        docs = self.existing["documents"]
        docs["OLD-1"]["webDocDescriptor"]["decision_date"] = "06/05/1990"
        self.path.write_text(json.dumps(self.existing), encoding="utf-8")
        self.store = MasterDescriptorStore(self.path)
        dec = _decision("OLD-1", title="New title", meta={"case": "999"})
        self.store.upsert(dec, _doc("http://old/x.pdf"), Path("/p"), "en")
        self.store.flush()
        web = self._reload()["OLD-1"]["webDocDescriptor"]
        self.assertEqual(web["decision_date"], "06/05/1990")
        self.assertEqual(web["title"], "Old title")
        self.assertEqual(web["firms"], "Old firms")
        self.assertEqual(web["case"], "1")
        self.assertEqual(web["url_pdf"], "https://old/x.pdf")

    def test_existing_blank_values_are_filled(self) -> None:
        """Missing or blank standard keys of an old entry are filled."""
        self.existing["documents"]["OLD-2"]["webDocDescriptor"] = {
            "title": "", "nace": None,
        }
        self.path.write_text(json.dumps(self.existing), encoding="utf-8")
        self.store = MasterDescriptorStore(self.path)
        dec = _decision("OLD-2", meta={"nace": "C24", "sector": "steel"})
        self.store.upsert(dec, _doc(URL_EN), Path("/p"), "en")
        self.store.flush()
        entry = self._reload()["OLD-2"]
        web = entry["webDocDescriptor"]
        self.assertEqual(web["title"], "Some title")
        self.assertEqual(web["nace"], "C24")
        self.assertEqual(web["url_pdf"], URL_EN)
        self.assertEqual(web["decision_date"], "1990-05-06")
        self.assertNotIn("sector", web)
        self.assertEqual(entry["case_ids"], ["IV/123"])
        self.assertEqual(self.store.url_pdf("OLD-2"), URL_EN)

    def test_case_ids_merged_without_duplicates(self) -> None:
        """Repeated upserts union the case ids in order."""
        self.store.upsert(_decision("OLD-1"), _doc(), Path("/p"), "en")
        self.store.upsert(
            _decision("OLD-1", case_ids=["IV/123", "AT.9"]), _doc(), Path("/p"), "en"
        )
        self.store.flush()
        self.assertEqual(self._reload()["OLD-1"]["case_ids"], ["IV/123", "AT.9"])

    def test_two_language_versions_never_become_two_entries(self) -> None:
        """EN and DE versions of one decision share one entry."""
        dec = _decision()
        self.store.upsert(dec, _doc(URL_EN, "EN"), Path("/p/en.pdf"), "en")
        self.store.upsert(dec, _doc(URL_DE, "DE"), Path("/p/de.pdf"), "de")
        self.store.flush()
        docs = self._reload()
        self.assertEqual(len(docs), 3)
        entry = docs["CELEX-31990D0645"]
        self.assertEqual(entry["local_file_pdf"], "/p/en.pdf")
        self.assertEqual(entry["webDocDescriptor"]["url_pdf"], URL_EN)
        self.assertEqual(entry["language_init"], "en")

    def test_url_pdf_conflict_keeps_stored_and_reports(self) -> None:
        """A differing url_pdf is kept out, logged and returned."""
        with self.assertLogs("pipelines.download.descriptor_store", "WARNING"):
            reasons = self.store.upsert(
                _decision("OLD-1"), _doc(URL_DE), Path("/p"), "de"
            )
        self.assertEqual(
            reasons, [f"url_pdf conflict: stored https://old/x.pdf supplied {URL_DE}"]
        )
        self.store.flush()
        web = self._reload()["OLD-1"]["webDocDescriptor"]
        self.assertEqual(web["url_pdf"], "https://old/x.pdf")
        self.assertEqual(self.store.upsert(
            _decision("OLD-1"), _doc("https://old/x.pdf"), Path("/p"), "en"), [])

    def test_url_pdf_spelling_variants_are_no_conflict(self) -> None:
        """http vs https and the eur-lex from parameter do not conflict."""
        eurlex = ("https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/"
                  "?uri=CELEX:31990D0645")
        self.store.upsert(_decision("NEW"), _doc(eurlex), Path("/p"), "en")
        for variant in (eurlex.replace("https", "http", 1), f"{eurlex}&from=EN",
                        eurlex.replace("eur-lex", "EUR-LEX", 1)):
            self.assertEqual(self.store.upsert(
                _decision("NEW"), _doc(variant), Path("/p"), "en"), [], variant)
        self.assertEqual(self.store.upsert(
            _decision("OLD-1"), _doc("http://old/x.pdf?from=DE"), Path("/p"), "en"), [])
        self.store.flush()
        self.assertEqual(self._reload()["NEW"]["webDocDescriptor"]["url_pdf"], eurlex)

    def test_url_pdf_other_query_still_conflicts(self) -> None:
        """A different uri parameter is still a conflict."""
        with self.assertLogs("pipelines.download.descriptor_store", "WARNING"):
            reasons = self.store.upsert(
                _decision("OLD-1"), _doc("https://old/x.pdf?uri=2"), Path("/p"), "en")
        self.assertEqual(len(reasons), 1)

    def test_language_and_url_accessors(self) -> None:
        """language_init and url_pdf read the stored values; absent -> None."""
        self.assertEqual(self.store.language_init("OLD-1"), "en")
        self.assertEqual(self.store.url_pdf("OLD-1"), "https://old/x.pdf")
        self.assertIsNone(self.store.language_init("OLD-2"))
        self.assertIsNone(self.store.url_pdf("OLD-2"))
        self.assertIsNone(self.store.language_init("NEW"))
        self.assertIsNone(self.store.url_pdf("NEW"))

    def test_empty_fixed_fields_do_not_hide_meta(self) -> None:
        """Blank title/parties/date keep the meta value; blanks never clear."""
        dec = _decision(
            "NEW", title="", parties="", decision_date=None,
            meta={"title": "meta title", "firms": "meta firms"},
        )
        self.store.upsert(dec, _doc(), Path("/p"), "en")
        self.store.flush()
        web = self._reload()["NEW"]["webDocDescriptor"]
        self.assertEqual(web["title"], "meta title")
        self.assertEqual(web["firms"], "meta firms")

    def test_lookup_helpers(self) -> None:
        """has_key and entries see stored and new entries; entries is a copy."""
        self.assertTrue(self.store.has_key("OLD-1"))
        self.assertFalse(self.store.has_key("NEW"))
        entries = dict(self.store.entries())
        self.assertEqual(entries["OLD-1"]["webDocDescriptor"]["url_pdf"],
                         "https://old/x.pdf")
        entries["OLD-1"]["webDocDescriptor"]["url_pdf"] = "changed"
        self.assertEqual(self.store.url_pdf("OLD-1"), "https://old/x.pdf")
        self.store.upsert(_decision("NEW"), _doc(), Path("/p"), "en")
        self.assertEqual(dict(self.store.entries())["NEW"]["webDocDescriptor"]["url_pdf"],
                         URL_EN)

    def test_local_file_pdf_returns_stored_path(self) -> None:
        """local_file_pdf reads the stored path; absent or blank -> None."""
        self.assertEqual(self.store.local_file_pdf("OLD-1"), "/p/OLD-1.pdf")
        self.assertIsNone(self.store.local_file_pdf("OLD-2"))
        self.assertIsNone(self.store.local_file_pdf("NEW"))

    def test_concurrent_upserts_all_land(self) -> None:
        """Parallel upserts lose no entry."""
        def work(i: int) -> None:
            self.store.upsert(_decision(f"ID-{i}"), _doc(f"https://e/{i}.pdf"),
                              Path(f"/p/{i}.pdf"), "en")
        threads = [threading.Thread(target=work, args=(i,)) for i in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.store.flush()
        self.assertEqual(len(self._reload()), 42)


class TestFlush(StoreTestCase):
    """Atomic write and backup behaviour."""

    def _backups(self) -> list[Path]:
        """List backup files next to the master file.

        Returns:
            Backup paths.
        """
        return sorted(p for p in self.dir.iterdir() if ".bak" in p.name)

    def test_flush_appends_new_entries_in_order_and_json_style(self) -> None:
        """Without changes the file round-trips with the repo's json style."""
        self.store.upsert(_decision("NEW"), _doc(), Path("/p"), "en")
        self.store.flush()
        raw = self.path.read_text(encoding="utf-8")
        self.assertEqual(list(self._reload()), ["OLD-1", "OLD-2", "NEW"])
        self.assertIn('\n    "documents": {\n        "OLD-1"', raw)

    def test_flush_writes_backup_once(self) -> None:
        """One backup per run, holding the pre-run content."""
        original = self.path.read_bytes()
        self.store.upsert(_decision("A"), _doc(), Path("/p"), "en")
        self.store.flush()
        self.store.upsert(_decision("B"), _doc(URL_DE), Path("/p"), "en")
        self.store.flush()
        backups = self._backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)

    def test_backup_never_overwrites_earlier_run(self) -> None:
        """A second store in the same second gets its own backup file."""
        self.store.upsert(_decision("A"), _doc(), Path("/p"), "en")
        self.store.flush()
        second = MasterDescriptorStore(self.path)
        second.upsert(_decision("B"), _doc(URL_DE), Path("/p"), "en")
        second.flush()
        self.assertEqual(len(self._backups()), 2)

    def test_flush_failure_leaves_original_untouched(self) -> None:
        """A failing os.replace keeps the file byte-identical, no temp left."""
        original = self.path.read_bytes()
        self.store.upsert(_decision("A"), _doc(), Path("/p"), "en")
        with mock.patch("os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                self.store.flush()
        self.assertEqual(self.path.read_bytes(), original)
        leftovers = [p for p in self.dir.iterdir()
                     if p.name != "master.json" and ".bak" not in p.name]
        self.assertEqual(leftovers, [])

    def test_mode_preserved(self) -> None:
        """A 0664 master file stays 0664 after flush."""
        os.chmod(self.path, 0o664)
        self.store = MasterDescriptorStore(self.path)
        self.store.upsert(_decision("A"), _doc(), Path("/p"), "en")
        self.store.flush()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o664)

    def test_concurrent_external_edit_survives(self) -> None:
        """An edit made by another writer after load is not lost."""
        self.store.upsert(_decision("A"), _doc(), Path("/p"), "en")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["documents"]["OLD-1"]["facts"] = "/changed.json"
        data["documents"]["EXT"] = {"CELEX": "EXT"}
        self.path.write_text(json.dumps(data, indent=4), encoding="utf-8")
        with self.assertLogs("pipelines.download.descriptor_store", "WARNING"):
            self.store.flush()
        docs = self._reload()
        self.assertEqual(docs["OLD-1"]["facts"], "/changed.json")
        self.assertIn("EXT", docs)
        self.assertIn("A", docs)
        self.store.upsert(_decision("B"), _doc(URL_DE), Path("/p"), "en")
        self.store.flush()
        self.assertEqual(
            set(self._reload()), {"OLD-1", "OLD-2", "EXT", "A", "B"}
        )

    def test_backup_failure_aborts_write(self) -> None:
        """If the backup copy fails nothing is written."""
        original = self.path.read_bytes()
        self.store.upsert(_decision("A"), _doc(), Path("/p"), "en")
        with mock.patch("shutil.copy2", side_effect=OSError("nospace")):
            with self.assertRaises(OSError):
                self.store.flush()
        self.assertEqual(self.path.read_bytes(), original)

    def test_unrelated_top_level_keys_round_trip(self) -> None:
        """Top-level keys other than documents are written back."""
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["schema_version"] = 7
        self.path.write_text(json.dumps(data), encoding="utf-8")
        store = MasterDescriptorStore(self.path)
        store.upsert(_decision("A"), _doc(), Path("/p"), "en")
        store.flush()
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8"))["schema_version"], 7
        )

    def test_corrupt_or_malformed_file_raises(self) -> None:
        """Bad JSON or a missing documents key fails at construction."""
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            MasterDescriptorStore(self.path)
        self.path.write_text('{"other": {}}', encoding="utf-8")
        with self.assertRaises(ValueError):
            MasterDescriptorStore(self.path)

    def test_flush_without_changes_is_noop(self) -> None:
        """Nothing dirty means no write and no backup."""
        self.store.flush()
        self.assertEqual(self._backups(), [])

    def test_missing_file_starts_empty(self) -> None:
        """A store over a missing file creates it on flush, without backup."""
        path = self.dir / "sub" / "new.json"
        store = MasterDescriptorStore(path)
        store.upsert(_decision("A"), _doc(), Path("/p"), "en")
        store.flush()
        self.assertIn("A", json.loads(path.read_text())["documents"])
        self.assertTrue(os.path.exists(path))


class TestSetMarkdown(StoreTestCase):
    """Markdown path/flag recording by the converter."""

    def test_new_entry_txt_md_file_follows_pdf(self) -> None:
        """A new entry's markdown path is derived from the PDF path."""
        store = MasterDescriptorStore(self.dir / "empty.json")
        store.upsert(_decision("K"), _doc(), Path("/d/K/K.pdf"), "en")
        entry = dict(store.entries())["K"]
        self.assertEqual(entry["txt_md_file"], "/d/K/mineru/K.md")

    def test_set_markdown_sets_flag_and_path(self) -> None:
        """set_markdown flags the entry and fills an unknown language."""
        self.store.upsert(_decision("K"), _doc(), Path("/d/K/K.pdf"), "NA")
        self.assertTrue(self.store.set_markdown("K", "/d/K/mineru/K.md", "de"))
        entry = dict(self.store.entries())["K"]
        self.assertIs(entry["mineru_flag"], True)
        self.assertEqual(entry["txt_md_file"], "/d/K/mineru/K.md")
        self.assertEqual(entry["language_init"], "de")
        self.assertEqual(
            self.store.markdown_entry("K"), ("/d/K/K.pdf", "/d/K/mineru/K.md")
        )

    def test_mineru_flag_reads_boolean_or_none(self) -> None:
        """mineru_flag reports the stored boolean, None for absent keys."""
        self.store.upsert(_decision("K"), _doc(), Path("/d/K/K.pdf"), "NA")
        self.assertIs(self.store.mineru_flag("K"), False)
        self.store.set_markdown("K", "/d/K/mineru/K.md", None)
        self.assertIs(self.store.mineru_flag("K"), True)
        self.assertIsNone(self.store.mineru_flag("missing"))

    def test_set_markdown_keeps_existing_language(self) -> None:
        """A known language_init is never overwritten."""
        self.assertTrue(self.store.set_markdown("OLD-1", "/m/x.md", "de"))
        self.assertEqual(dict(self.store.entries())["OLD-1"]["language_init"], "en")

    def test_set_markdown_none_language_leaves_it(self) -> None:
        """A None language does not touch language_init."""
        self.store.set_markdown("OLD-2", "/m/y.md", None)
        self.assertNotIn("language_init", dict(self.store.entries())["OLD-2"])

    def test_set_markdown_unknown_key_returns_false(self) -> None:
        """An absent key reports False."""
        self.assertFalse(self.store.set_markdown("NOPE", "/m/z.md", "en"))
        self.assertEqual(self.store.markdown_entry("NOPE"), (None, None))

    def test_set_markdown_survives_stale_write_replay(self) -> None:
        """A markdown update is replayed over a file edited on disk."""
        self.assertTrue(self.store.set_markdown("OLD-2", "/m/o2.md", "fr"))
        edited = json.loads(self.path.read_text(encoding="utf-8"))
        edited["documents"]["USER-1"] = {"CELEX": "USER-1", "note": "added by hand"}
        self.path.write_text(json.dumps(edited, indent=2), encoding="utf-8")
        self.store.flush()
        docs = self._reload()
        self.assertIn("USER-1", docs)
        self.assertIs(docs["OLD-2"]["mineru_flag"], True)
        self.assertEqual(docs["OLD-2"]["txt_md_file"], "/m/o2.md")
        self.assertEqual(docs["OLD-2"]["language_init"], "fr")


class TestTranslation(StoreTestCase):
    """English master switch with the original kept as reference."""

    ENTRY = TranslationEntry(
        txt_md_file="/m/OLD-1_EN.md", original_file_md="/m/OLD-1.md",
        language_init="de", language_translated="en",
    )

    def test_set_translation_writes_four_keys(self) -> None:
        """All four keys land in the flushed file."""
        self.store.set_translation("OLD-1", self.ENTRY)
        self.store.flush()
        entry = self._reload()["OLD-1"]
        self.assertEqual(entry["txt_md_file"], "/m/OLD-1_EN.md")
        self.assertEqual(entry["original_file_md"], "/m/OLD-1.md")
        self.assertEqual(entry["language_init"], "de")
        self.assertEqual(entry["language_translated"], "en")
        self.assertEqual(entry["facts"], "/f/OLD-1.json")

    def test_translation_entry_roundtrip(self) -> None:
        """The snapshot reads back what was set; unknown keys give None."""
        self.store.set_translation("OLD-1", self.ENTRY)
        self.assertEqual(self.store.translation_entry("OLD-1"), self.ENTRY)
        self.assertIsNone(self.store.translation_entry("NOPE"))

    def test_translation_entry_of_untranslated_entry(self) -> None:
        """An entry without original_file_md snapshots it as None."""
        snap = self.store.translation_entry("OLD-1")
        self.assertEqual(snap, TranslationEntry(
            txt_md_file="/m/OLD-1.md", original_file_md=None,
            language_init="en", language_translated="NA"))

    def test_set_translation_none_original_removes_key(self) -> None:
        """Restoring a snapshot without original removes the key."""
        self.store.set_translation("OLD-1", self.ENTRY)
        self.store.set_translation(
            "OLD-1", self.ENTRY.model_copy(update={"original_file_md": None}))
        self.store.flush()
        self.assertNotIn("original_file_md", self._reload()["OLD-1"])

    def test_set_translation_unknown_key_raises(self) -> None:
        """An absent key is a programming error."""
        with self.assertRaises(KeyError):
            self.store.set_translation("NOPE", self.ENTRY)

    def test_translation_replayed_after_external_change(self) -> None:
        """A translation survives a user edit of the file during the run."""
        self.store.set_translation("OLD-1", self.ENTRY)
        edited = json.loads(self.path.read_text(encoding="utf-8"))
        edited["documents"]["OLD-2"]["note"] = "edited by hand"
        self.path.write_text(json.dumps(edited, indent=2), encoding="utf-8")
        self.store.flush()
        docs = self._reload()
        self.assertEqual(docs["OLD-2"]["note"], "edited by hand")
        self.assertEqual(docs["OLD-1"]["txt_md_file"], "/m/OLD-1_EN.md")
        self.assertEqual(docs["OLD-1"]["original_file_md"], "/m/OLD-1.md")



class TestSourceSwitch(StoreTestCase):
    """A better copy replaces the entry's source document."""

    def test_switch_source_sets_files_and_clears_translation(self) -> None:
        self.store.set_translation("OLD-1", TranslationEntry(
            txt_md_file="/m/OLD-1_en.md", original_file_md="/m/OLD-1.md",
            language_init="de", language_translated="en"))
        self.store.switch_source("OLD-1", local_file_pdf="/c/better.pdf", txt_md_file="/c/better.md",
                                 language_init="en")
        self.store.flush()
        entry = self._reload()["OLD-1"]
        self.assertEqual((entry["local_file_pdf"], entry["txt_md_file"]), ("/c/better.pdf", "/c/better.md"))
        self.assertEqual((entry["language_init"], entry["language_translated"]), ("en", "NA"))
        self.assertNotIn("original_file_md", entry)
        self.assertIs(entry["mineru_flag"], True)
        self.assertEqual(entry["facts"], "/f/OLD-1.json")

    def test_switch_source_replayed_after_external_change(self) -> None:
        self.store.switch_source("OLD-1", local_file_pdf="/c/b.pdf", txt_md_file="/c/b.md", language_init="en")
        edited = json.loads(self.path.read_text(encoding="utf-8"))
        edited["documents"]["OLD-2"]["note"] = "hand"
        self.path.write_text(json.dumps(edited), encoding="utf-8")
        self.store.flush()
        docs = self._reload()
        self.assertEqual(docs["OLD-1"]["txt_md_file"], "/c/b.md")
        self.assertEqual(docs["OLD-2"]["note"], "hand")

    def test_switch_source_unknown_key_raises(self) -> None:
        with self.assertRaises(KeyError):
            self.store.switch_source("NOPE", local_file_pdf="/p", txt_md_file="/m", language_init="en")


if __name__ == "__main__":
    unittest.main()
