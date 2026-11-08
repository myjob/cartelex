"""Tests for the downloader identifier rules."""

import datetime as dt
import unittest

from pipelines.download.identifiers import (
    UnsafeIdentifier,
    celex_key,
    decision_id_for,
    detect_language,
    document_id_for,
    normalize_case_id,
    pdf_url_key,
)

EURLEX_EN = "https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX:31964D0233"
EURLEX_DE = "https://eur-lex.europa.eu/legal-content/DE/TXT/PDF/?uri=CELEX:31964D0233"


class NormalizeCaseIdTest(unittest.TestCase):
    """normalize_case_id only trims and collapses whitespace."""

    def test_trim_and_collapse(self) -> None:
        """Surrounding and inner whitespace is normalised."""
        self.assertEqual(normalize_case_id("AT.35388 "), "AT.35388")
        self.assertEqual(normalize_case_id(" COMP/M.1  234"), "COMP/M.1 234")

    def test_unresolved_variants_kept(self) -> None:
        """Convention ids are returned untouched."""
        self.assertEqual(normalize_case_id("IV/61"), "IV/61")
        self.assertEqual(normalize_case_id("IV/36.123"), "IV/36.123")


class CelexKeyTest(unittest.TestCase):
    """celex_key builds the prefixed CELEX- key (decision_id spelling)."""

    def test_bare_number_is_prefixed(self) -> None:
        """A bare CELEX number gains the CELEX- prefix."""
        self.assertEqual(celex_key("31964D0233"), "CELEX-31964D0233")
        self.assertEqual(celex_key(" 31964D0233 "), "CELEX-31964D0233")

    def test_prefix_is_never_doubled(self) -> None:
        """Prefixed input in any case keeps a single upper-case prefix."""
        self.assertEqual(celex_key("CELEX-31964D0233"), "CELEX-31964D0233")
        self.assertEqual(celex_key("celex-31964D0233"), "CELEX-31964D0233")
        self.assertEqual(celex_key("Celex-31964D0233"), "CELEX-31964D0233")

    def test_parenthesised_suffix_becomes_underscore(self) -> None:
        """32002D0758(01) maps to the _01 convention, idempotently."""
        self.assertEqual(celex_key("32002D0758(01)"), "CELEX-32002D0758_01")
        self.assertEqual(celex_key("CELEX-32002D0758(01)"), "CELEX-32002D0758_01")
        self.assertEqual(celex_key("CELEX-32002D0758_01"), "CELEX-32002D0758_01")

    def test_unsafe_input_raises(self) -> None:
        """Unusable spellings raise instead of being slugified."""
        for raw in ("", "../etc/passwd", "3196 4D0233", "CELEX-", "32002D0758(x)"):
            with self.assertRaises(UnsafeIdentifier, msg=raw):
                celex_key(raw)

    def test_non_celex_master_keys_raise(self) -> None:
        """Values without real CELEX syntax raise (master-key fallback)."""
        for raw in ("c1417", "35388_9_5", "iv34801_en", "33585_2_4",
                    "CELEX-c1417", "31964D02", "3196D0233", "31964DAB0233"):
            with self.assertRaises(UnsafeIdentifier, msg=raw):
                celex_key(raw)

    def test_number_is_upper_cased_and_sectors_accepted(self) -> None:
        """Lower-case numbers are upper-cased; C/E sectors and 2 letters pass."""
        self.assertEqual(celex_key("31964d0233"), "CELEX-31964D0233")
        self.assertEqual(celex_key("C2006XC1231"), "CELEX-C2006XC1231")
        self.assertEqual(celex_key("E2004J0012"), "CELEX-E2004J0012")
        self.assertEqual(celex_key("62011CJ0001"), "CELEX-62011CJ0001")
        self.assertEqual(celex_key("CELEX-32002D0758"), "CELEX-32002D0758")

    def test_decision_id_for_uses_same_spelling(self) -> None:
        """decision_id_for and celex_key agree for CELEX decisions."""
        for raw in ("31964D0233", "32002D0758(01)", "CELEX-31964D0233"):
            self.assertEqual(decision_id_for(raw, None, None, None, set())[0], celex_key(raw))


class PdfUrlKeyTest(unittest.TestCase):
    """pdf_url_key ignores scheme, host case and the from parameter."""

    def test_variants_share_a_key(self) -> None:
        """http/https, host case and from=EN give the same key."""
        base = pdf_url_key(EURLEX_EN)
        for variant in (EURLEX_EN.replace("https", "http", 1), f"{EURLEX_EN}&from=EN",
                        EURLEX_EN.replace("eur-lex", "EUR-LEX", 1)):
            self.assertEqual(pdf_url_key(variant), base, variant)

    def test_different_documents_differ(self) -> None:
        """Another uri or path is another key."""
        self.assertNotEqual(pdf_url_key(EURLEX_EN), pdf_url_key(EURLEX_DE))
        self.assertNotEqual(pdf_url_key("https://a/x.pdf"), pdf_url_key("https://a/y.pdf"))


class DecisionIdTest(unittest.TestCase):
    """Decision id rules."""

    def test_celex_to_decision_id(self) -> None:
        """CELEX numbers map to CELEX- ids."""
        self.assertEqual(
            decision_id_for("31964D0233", None, None, None, set()),
            ("CELEX-31964D0233", []),
        )
        self.assertEqual(
            decision_id_for("32002D0758(01)", None, None, None, set())[0],
            "CELEX-32002D0758_01",
        )

    def test_existing_master_key_reused(self) -> None:
        """An existing master key wins when there is no CELEX."""
        self.assertEqual(
            decision_id_for(None, "32450_35_6", "IV/61", dt.date(1995, 5, 16), set()),
            ("32450_35_6", []),
        )

    def test_fallback_dec_id(self) -> None:
        """Fallback id is DEC-<case>-<date>."""
        self.assertEqual(
            decision_id_for(None, None, "AT.35388", dt.date(1995, 5, 16), set()),
            ("DEC-AT35388-19950516", []),
        )

    def test_collision_adds_suffix_and_reason(self) -> None:
        """A taken id gets _2 and a review reason."""
        did, reasons = decision_id_for(
            None, None, "AT.35388", dt.date(1995, 5, 16), {"DEC-AT35388-19950516"}
        )
        self.assertEqual(did, "DEC-AT35388-19950516_2")
        self.assertEqual(len(reasons), 1)

    def test_duplicate_celex_gets_suffixes(self) -> None:
        """Repeated CELEX ids in one run get _2, _3 and reasons."""
        taken = {"CELEX-31988D0172"}
        did2, r2 = decision_id_for("31988D0172", None, None, None, taken)
        self.assertEqual(did2, "CELEX-31988D0172_2")
        self.assertEqual(len(r2), 1)
        taken.add(did2)
        did3, r3 = decision_id_for("31988D0172", None, None, None, taken)
        self.assertEqual(did3, "CELEX-31988D0172_3")
        self.assertEqual(len(r3), 1)

    def test_reused_key_collision(self) -> None:
        """Two rows reusing one master key get _2 plus a reason."""
        did, reasons = decision_id_for("", "32450_35_6", None, None, {"32450_35_6"})
        self.assertEqual(did, "32450_35_6_2")
        self.assertEqual(len(reasons), 1)

    def test_punctuation_only_case_raises(self) -> None:
        """A case id without alphanumerics is unusable."""
        with self.assertRaises(UnsafeIdentifier):
            decision_id_for(None, None, "---", dt.date(1995, 5, 16), set())

    def test_unsafe_id_raises_unsafe_identifier(self) -> None:
        """Unsafe or unresolvable ids raise, never slugify."""
        with self.assertRaises(UnsafeIdentifier):
            decision_id_for("../etc/passwd", None, None, None, set())
        with self.assertRaises(UnsafeIdentifier):
            decision_id_for(None, "a/b", None, None, set())
        with self.assertRaises(UnsafeIdentifier):
            decision_id_for(None, None, None, None, set())
        with self.assertRaises(UnsafeIdentifier):
            decision_id_for(None, None, "AT.1", None, set())


class DocumentTest(unittest.TestCase):
    """Language detection and document ids."""

    def test_detect_language_eurlex_marker(self) -> None:
        """Only eur-lex /XX/TXT/PDF/ gives a language."""
        self.assertEqual(detect_language(EURLEX_EN), "EN")
        self.assertEqual(detect_language(EURLEX_DE), "DE")
        self.assertIsNone(
            detect_language(
                "https://ec.europa.eu/competition/antitrust/cases/36623_338_1.pdf"
            )
        )
        self.assertIsNone(detect_language("https://db-comp.eu/document_1754.download"))

    def test_document_id_forms(self) -> None:
        """Document ids per origin."""
        self.assertEqual(
            document_id_for(EURLEX_EN, "CELEX-31964D0233"), "CELEX-31964D0233_EN"
        )
        self.assertEqual(
            document_id_for(
                "https://ec.europa.eu/competition/antitrust/cases/36623_338_1.pdf",
                "36623_338",
            ),
            "36623_338_1",
        )
        self.assertEqual(
            document_id_for("https://db-comp.eu/document_1754.download", "x"),
            "dbcomp-1754",
        )

    def test_unsafe_document_id(self) -> None:
        """Unusable URLs raise."""
        with self.assertRaises(UnsafeIdentifier):
            document_id_for("https://example.org/", "x")

    def test_eurlex_html_and_format_suffix(self) -> None:
        """HTML URLs get a language and a _html suffix; pdf stays bare."""
        html = "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:1"
        pdf = "https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX:1"
        self.assertEqual(detect_language(html), "EN")
        self.assertEqual(document_id_for(html, "D", "html"), "D_EN_html")
        self.assertEqual(document_id_for(pdf, "D"), "D_EN")
        self.assertEqual(document_id_for(pdf, "D", "pdf"), "D_EN")
        self.assertEqual(
            document_id_for("https://x.org/a/b.htm", "D", "HTML"), "b_html")
        self.assertEqual(document_id_for("https://x.org/a/b.doc", "D", "d-o c"), "b_doc")

    def test_dbcomp_slug_url_uses_file_stem(self) -> None:
        """db-comp URLs without document_NNN fall back to the file stem."""
        self.assertEqual(
            document_id_for("https://db-comp.eu/metal-packaging.download", "x"),
            "metal-packaging",
        )

    def test_non_pdf_fallback_id_is_stable_and_pdf_still_raises(self) -> None:
        """Non-pdf URLs without an id get a sha256 based id; pdf ones raise."""
        url = "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=OJ:C:1"
        a = document_id_for(url, "D", "other")
        self.assertRegex(a, r"^D_other_[0-9a-f]{8}$")
        self.assertEqual(a, document_id_for(url, "D", "other"))
        self.assertNotEqual(a, document_id_for(url + "2", "D", "other"))
        with self.assertRaises(UnsafeIdentifier):
            document_id_for(url, "D", "pdf")


if __name__ == "__main__":
    unittest.main()
