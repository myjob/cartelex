"""Unit tests for the source-document viewer endpoints in ``oi_dspy``.

These exercise path resolution, rendering, and the error responses (404 for a
missing file, 400 for a traversal id) in isolation: the docs root is redirected
to a temporary directory and the FastAPI ``TestClient`` drives the routes
without any DB/LLM dependency.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from fastapi.testclient import TestClient

import ui.backend_server.oi_dspy as oi_dspy


class DocViewerTests(unittest.TestCase):
    """Cover ``/doc/{celex_id}`` and its ``/md`` and ``/pdf`` leaf routes."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.docs_root = Path(self._tmp.name).resolve()
        self.celex_id = "32450_35_6"

        case_dir = self.docs_root / self.celex_id
        mineru_dir = case_dir / "mineru" / "auto"
        mineru_dir.mkdir(parents=True)
        (mineru_dir / f"{self.celex_id}.md").write_text(
            "# Decision\n\nMarket sharing finding.\n", encoding="utf-8"
        )
        (case_dir / f"{self.celex_id}.pdf").write_bytes(b"%PDF-1.4 minimal")

        # Point the module's resolved docs root at the temp store.
        self._patch = mock.patch.object(oi_dspy, "_DOCS_ROOT", self.docs_root)
        self._patch.start()
        self.client = TestClient(oi_dspy.app)

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def test_markdown_renders_to_html(self) -> None:
        response = self.client.get(f"/doc/{self.celex_id}/md")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("<h1>Decision</h1>", response.text)

    def test_pdf_served_inline(self) -> None:
        response = self.client.get(f"/doc/{self.celex_id}/pdf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertIn("inline", response.headers["content-disposition"])
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_viewer_page_embeds_both_tabs(self) -> None:
        response = self.client.get(f"/doc/{self.celex_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"./{self.celex_id}/md", response.text)
        self.assertIn(f"./{self.celex_id}/pdf", response.text)

    def test_missing_markdown_returns_404(self) -> None:
        response = self.client.get("/doc/99999_0_0/md")
        self.assertEqual(response.status_code, 404)
        self.assertIn("Document not found", response.text)

    def test_missing_pdf_returns_404(self) -> None:
        response = self.client.get("/doc/99999_0_0/pdf")
        self.assertEqual(response.status_code, 404)

    def test_malformed_id_rejected(self) -> None:
        # A dotted id reaches the handler and must be rejected before any path
        # resolution (the encoded ``../`` form is normalised away by the router
        # and never serves a file either).
        response = self.client.get("/doc/has.dot/md")
        self.assertEqual(response.status_code, 400)

    def test_resolve_doc_path_rejects_dotdot(self) -> None:
        with self.assertRaises(oi_dspy.HTTPException) as ctx:
            oi_dspy._resolve_doc_path("../secret", kind="md")
        self.assertEqual(ctx.exception.status_code, 400)


class DocFooterTests(unittest.TestCase):
    """Cover the chat-response source-document footer helper."""

    def test_footer_links_both_documents(self) -> None:
        celex_id = "CELEX-31994D0823"
        with mock.patch.object(
            oi_dspy.settings, "CHUNK_VIEW_BASE_URL", "http://localhost:7777"
        ):
            footer = oi_dspy._doc_footer(celex_id)
        # The emoji is inside the link text so the icon itself is clickable.
        self.assertIn(f"[\U0001F4C4 MD](http://localhost:7777/doc/{celex_id}/md)", footer)
        self.assertIn(f"[\U0001F4D5 PDF](http://localhost:7777/doc/{celex_id}/pdf)", footer)

    def test_footer_strips_trailing_slash_in_base_url(self) -> None:
        with mock.patch.object(
            oi_dspy.settings, "CHUNK_VIEW_BASE_URL", "http://localhost:7777/"
        ):
            footer = oi_dspy._doc_footer("32450_35_6")
        self.assertIn("http://localhost:7777/doc/32450_35_6/md", footer)
        self.assertNotIn("7777//doc", footer)

    def test_footer_empty_when_no_case(self) -> None:
        self.assertEqual(oi_dspy._doc_footer(""), "")


if __name__ == "__main__":
    unittest.main()
