"""Tests for routing EUR-Lex PDF URLs to the Cellar REST API."""

import unittest

from config.settings import settings
from pipelines.download.cellar import (
    CellarRequest,
    cellar_request_for,
    is_missing_datastream,
)

EURLEX_EN = (
    "https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX:31988D0172"
)


class CellarRequestTests(unittest.TestCase):
    """Behaviour of cellar_request_for."""

    def test_eurlex_pdf_url_routes_to_cellar_celex_resource(self) -> None:
        """An EUR-Lex PDF URL becomes the Cellar celex resource with headers."""
        req = cellar_request_for(EURLEX_EN)
        self.assertIsInstance(req, CellarRequest)
        self.assertEqual(req.url, f"{settings.DOWNLOAD_CELLAR_BASE_URL}31988D0172")
        self.assertEqual(req.headers["Accept"], settings.DOWNLOAD_CELLAR_ACCEPT)
        self.assertEqual(req.headers["Accept-Language"], "en")

    def test_language_and_spelling_variants(self) -> None:
        """http, a from= parameter, lower case and suffixed CELEX all route."""
        req = cellar_request_for(
            "http://eur-lex.europa.eu/legal-content/DE/TXT/PDF/"
            "?uri=CELEX:31964D0233&from=EN"
        )
        self.assertEqual(req.url, f"{settings.DOWNLOAD_CELLAR_BASE_URL}31964D0233")
        self.assertEqual(req.headers["Accept-Language"], "de")
        suffixed = cellar_request_for(
            "https://eur-lex.europa.eu/legal-content/en/TXT/PDF/"
            "?uri=CELEX:32002D0758(01)"
        )
        self.assertEqual(
            suffixed.url, f"{settings.DOWNLOAD_CELLAR_BASE_URL}32002D0758(01)"
        )
        self.assertEqual(suffixed.headers["Accept-Language"], "en")

    def test_other_urls_are_not_routed(self) -> None:
        """Non-EUR-Lex, EUR-Lex HTML and URLs without a CELEX stay as they are."""
        for url in (
            "https://ec.europa.eu/competition/antitrust/cases/dec_docs/36623/36623_338_1.pdf",
            "https://db-comp.eu/document_1754.download",
            "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:31988D0172",
            "https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=OJ:C:2020:001:FULL",
            "https://eur-lex.europa.eu/LexUriServ/LexUriServ.do?uri=CELEX:31988D0172",
            "not a url",
        ):
            with self.subTest(url=url):
                self.assertIsNone(cellar_request_for(url))

    def test_unsafe_celex_values_are_not_routed(self) -> None:
        """Path, query or fragment characters in the CELEX value are never routed."""
        base = "https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX:"
        for value in ("..%2F..%2Fcellar%2Fx", "31988D0172%3Fa%3Db%23f",
                      "31988D0172/../x", "31988D0172 x", "31988D0172(1)"):
            with self.subTest(value=value):
                self.assertIsNone(cellar_request_for(base + value))

    def test_lower_case_celex_is_upper_cased(self) -> None:
        """A lower-case CELEX number is sent upper-cased."""
        req = cellar_request_for(
            "https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=celex:31988d0172")
        self.assertEqual(req.url, f"{settings.DOWNLOAD_CELLAR_BASE_URL}31988D0172")

    def test_missing_datastream_marker(self) -> None:
        """Cellar's 'no content datastream of the requested type' body is recognised."""
        self.assertTrue(is_missing_datastream(
            b"None of the requests returned successfully a redirection. The "
            b"following exception was thrown: [cellar identifier cellar:67f2 "
            b"does not hold a content datastream of the requested type]"
        ))
        self.assertFalse(is_missing_datastream(b"<html>Not Found</html>"))
        self.assertFalse(is_missing_datastream(b""))


if __name__ == "__main__":
    unittest.main()
