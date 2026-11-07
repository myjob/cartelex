"""Tests for atomic placement of MinerU result ZIPs."""

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from pipelines.convert import result_unpacker
from pipelines.convert.result_unpacker import ResultInvalid, ResultMetrics, place_result


def _middle(pages: int, tables: int, formulas: int) -> str:
    blocks = ([{"type": "table"}] * tables + [{"type": "equation"}] * formulas
              + [{"type": "text"}, {"type": "equation_inline"}])
    return json.dumps({"pages": [{"page_idx": 0, "blocks": blocks}]
                       + [{"page_idx": i, "blocks": []} for i in range(1, pages)]})


def _zip(members: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


class PlaceResultTest(unittest.TestCase):
    """Behaviour of place_result."""

    def setUp(self) -> None:
        """Create a scratch directory."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.target = self.root / "mineru" / "doc.md"

    def test_places_markdown_images_and_json(self) -> None:
        """All artifacts land beside the target and counts match."""
        text = "# Title\nbody"
        data = _zip({"markdown.md": text, "middle_json.json": _middle(1, 0, 0),
                     "structured_content.json": "{}",
                     "images/a.jpg": b"1", "images/b.jpg": b"2"})
        metrics = place_result(data, self.target)
        self.assertEqual(self.target.read_text(encoding="utf-8"), text)
        self.assertEqual(metrics.md_chars, len(text))
        self.assertEqual(metrics.image_count, 2)
        self.assertTrue((self.target.parent / "images" / "a.jpg").is_file())
        self.assertTrue((self.target.parent / "doc_middle.json").is_file())
        self.assertTrue((self.target.parent / "doc_structured_content.json").is_file())
        self.assertEqual([p.name for p in self.target.parent.glob(".tmp-*")], [])

    def test_counts_from_middle_json(self) -> None:
        """Pages, tables and display formulas come from middle_json."""
        data = _zip({"markdown.md": "x", "middle_json.json": _middle(3, 2, 1)})
        metrics = place_result(data, self.target)
        self.assertEqual((metrics.pdf_pages, metrics.table_count, metrics.formula_count),
                         (3, 2, 1))

    def test_counts_none_when_middle_json_absent(self) -> None:
        """Missing middle_json yields None counts."""
        metrics = place_result(_zip({"markdown.md": "x"}), self.target)
        self.assertEqual(metrics, ResultMetrics(md_chars=1, image_count=0))

    def test_missing_markdown_raises_and_leaves_no_temp(self) -> None:
        """A ZIP without markdown.md leaves no temp dir and no md."""
        with self.assertRaises(ResultInvalid):
            place_result(_zip({"middle_json.json": _middle(1, 0, 0)}), self.target)
        self.assertEqual(list(self.target.parent.iterdir()), [])

    def test_corrupt_zip_raises(self) -> None:
        """Garbage bytes are rejected."""
        with self.assertRaises(ResultInvalid):
            place_result(b"not a zip", self.target)

    def test_rejects_path_traversal_member(self) -> None:
        """A ../ member is rejected and nothing escapes."""
        with self.assertRaises(ResultInvalid):
            place_result(_zip({"markdown.md": "x", "../evil.md": "bad"}), self.target)
        self.assertFalse((self.root / "evil.md").exists())
        self.assertFalse(self.target.exists())

    def test_force_replaces_own_files_and_keeps_old_on_failure(self) -> None:
        """Good ZIP merges images; bad ZIP and mid-swap errors keep old files."""
        images = self.target.parent / "images"
        images.mkdir(parents=True)
        (images / "old.jpg").write_bytes(b"old")
        (images / "same.jpg").write_bytes(b"before")
        self.target.write_text("old md", encoding="utf-8")
        sidecar = self.target.parent / "doc_middle.json"
        sidecar.write_text("old json", encoding="utf-8")

        with self.assertRaises(ResultInvalid):
            place_result(_zip({"images/new.jpg": b"n"}), self.target)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "old md")
        self.assertTrue((images / "old.jpg").is_file())

        real_replace = result_unpacker.os.replace

        def failing(src: object, dst: object) -> None:
            if Path(str(dst)) == self.target:
                raise OSError("disk full")
            real_replace(src, dst)

        good = _zip({"markdown.md": "new md", "middle_json.json": _middle(1, 0, 0),
                     "images/new.jpg": b"n", "images/same.jpg": b"after"})
        with mock.patch.object(result_unpacker.os, "replace", side_effect=failing):
            with self.assertRaises(OSError):
                place_result(good, self.target)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "old md")
        self.assertEqual(sidecar.read_text(encoding="utf-8"), "old json")
        self.assertEqual(sorted(p.name for p in images.iterdir()), ["old.jpg", "same.jpg"])
        self.assertEqual((images / "same.jpg").read_bytes(), b"before")
        self.assertEqual([p.name for p in self.target.parent.glob(".tmp-*")], [])

        metrics = place_result(good, self.target)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "new md")
        self.assertEqual(sorted(p.name for p in images.iterdir()),
                         ["new.jpg", "old.jpg", "same.jpg"])
        self.assertEqual((images / "same.jpg").read_bytes(), b"after")
        self.assertEqual(metrics.image_count, 2)

    def test_two_results_share_folder_without_destroying_each_other(self) -> None:
        """Placing B beside A keeps A's md, sidecars and images; re-placing A too."""
        other = self.target.parent / "other.md"
        a = _zip({"markdown.md": "A", "middle_json.json": _middle(1, 0, 0),
                  "structured_content.json": "{\"a\": 1}", "images/a.jpg": b"a"})
        b = _zip({"markdown.md": "B", "middle_json.json": _middle(2, 0, 0),
                  "structured_content.json": "{\"b\": 1}", "images/b.jpg": b"b"})
        place_result(a, self.target)
        place_result(b, other)
        place_result(a, self.target)
        folder = self.target.parent
        self.assertEqual(self.target.read_text(encoding="utf-8"), "A")
        self.assertEqual(other.read_text(encoding="utf-8"), "B")
        for name in ("doc_middle.json", "doc_structured_content.json",
                     "other_middle.json", "other_structured_content.json"):
            self.assertTrue((folder / name).is_file(), name)
        self.assertEqual(sorted(p.name for p in (folder / "images").iterdir()),
                         ["a.jpg", "b.jpg"])

    def test_failure_rollback_leaves_other_files_intact(self) -> None:
        """A failing placement of A removes only A's new images, not B's."""
        other = self.target.parent / "other.md"
        b = _zip({"markdown.md": "B", "middle_json.json": _middle(1, 0, 0), "images/b.jpg": b"b"})
        place_result(b, other)
        real_replace = result_unpacker.os.replace

        def failing(src: object, dst: object) -> None:
            if Path(str(dst)) == self.target:
                raise OSError("disk full")
            real_replace(src, dst)

        a = _zip({"markdown.md": "A", "middle_json.json": _middle(1, 0, 0), "images/a.jpg": b"a"})
        with mock.patch.object(result_unpacker.os, "replace", side_effect=failing):
            with self.assertRaises(OSError):
                place_result(a, self.target)
        folder = self.target.parent
        self.assertEqual(other.read_text(encoding="utf-8"), "B")
        self.assertTrue((folder / "other_middle.json").is_file())
        self.assertEqual([p.name for p in (folder / "images").iterdir()], ["b.jpg"])
        self.assertFalse((folder / "doc_middle.json").exists())

    def test_creates_missing_mineru_dir(self) -> None:
        """The mineru directory is created on demand."""
        self.assertFalse(self.target.parent.exists())
        place_result(_zip({"markdown.md": "x"}), self.target)
        self.assertTrue(self.target.is_file())


if __name__ == "__main__":
    unittest.main()
