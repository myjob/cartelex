"""Unit tests for candidate evidence extraction (pure, fixture strings).

Run with:
    PYTHONPATH=src .venv/bin/python -m unittest pipelines.decide.tests.test_evidence
"""

import datetime as dt
import unittest

from pipelines.decide.evidence import extract_evidence
from pipelines.decide.models import DocumentKind

FILLER = "\n\n" + "(1) The Commission found that the undertakings infringed the rules. " * 120 + "\n\n"

FULL_39780 = (
    "## CASE AT.39780 – Envelopes\n(Only the English text is authentic)\n# CARTEL PROCEDURE\n"
    "Date: 10/12/2014\nBrussels, 10.12.2014\nC(2014) 9295 final\nPublic version\n"
    "# COMMISSION DECISION\n**of 10.12.2014**\n## relating to proceedings under Article 101 of the "
    "Treaty (AT.39780 - Envelopes)\n(Only the English text is authentic)\n"
    + FILLER
    + "HAS ADOPTED THIS DECISION:\nArticle 1\nThis Decision is addressed to Bong AB.\n"
    "Done at Brussels,\nFor the Commission\nMargrethe VESTAGER\nMember of the Commission\n"
)
MIRROR_39780 = FULL_39780.replace("Brussels, 10.12.2014\nC(2014)", "Brussels, 10.12.2014 C(2014)")
SUMMARY_39780 = (
    "**CASE AT.39780 – Envelopes**\nDate: 10/12/2014\nThis text is a summary of the decision, which is "
    "made available for information purposes only.\n# Case AT.39780 – ENVELOPES\n"
    "## Summary of Commission Decision\n## of 10 December 2014\n" + FILLER
    + "## 3. CONCLUSION\n(18) The following fines were imposed.\n"
)
COMBINED = (
    "COMMISSION DECISION **of 20.1.2021** **relating to proceedings under Article 101** "
    "**Cases AT.40413 - Focus Home, AT.40414 - Koch Media, AT.40420 – ZeniMax, AT.40422 – Bandai "
    "Namco and AT.40424 - Capcom** (Text with EEA relevance)" + FILLER + "Done at Brussels,\n"
)
INTERIM_34174 = (
    "## COMMISSION DECISION\n## of 11 June 1992\nrelating to a proceeding under Article 86 of the EEC "
    "Treaty (IV/34.174 - Sealink/B&I - Holyhead : Interim measures)\nTHE COMMISSION OF THE EUROPEAN "
    "COMMUNITIES," + FILLER + "HAS ADOPTED THIS DECISION:\nArticle 1\nDone at Brussels, 11 June 1992.\n"
)
LETTER = (
    "Studio Carbonare e D'Angelo\nSUBJECT: CASE No IV/35808 - MEDSAP Consortium\n"
    "Ref: Your letter of 20 October 1995\nDear Sirs," + FILLER
    + "I confirm that the agreement falls within the group exemption.\nYours sincerely\nSerge Durande\n"
)
FOOTNOTES = "".join(
    f'<small><span class="docvortex-page-footnote">{n} Dossier p. {n}</span></small>\n' for n in range(400)
)
FRENCH = (
    "Bruxelles, 31.3.2011\nC(2011) 2070 final\n# DECISION DE LA COMMISSION\ndu 31.3.2011\nmodifiant la "
    "décision C(2007) 4257 final du 19 septembre 2007 (COMP/E-1/39.168 PO/ Articles de mercerie)\n"
    + FILLER + "A ADOPTÉ LA PRÉSENTE DÉCISION:\nArticle premier\nFait à Bruxelles, le 31.3.2011\n" + FOOTNOTES
)
GERMAN = (
    "Brüssel, den 31.3.2011\n# BESCHLUSS DER KOMMISSION\nvom 31.3.2011\nzur Änderung der Entscheidung "
    "(COMP/E-1/39.168 PO/Hartkurzwaren)\n" + FILLER + "HAT FOLGENDEN BESCHLUSS ERLASSEN:\n"
    "Geschehen zu Brüssel am 31.3.2011\n"
)
FOREX = (
    "COMMISSION DECISION **of 16.5.2019**\n# relating to a proceeding under Article 101 of the Treaty\n"
    "# AT.40135-FOREX ({name})\n(Text with EEA relevance)\n" + FILLER + "Done at Brussels,\n"
)


def _ev(markdown: str):
    """Extract evidence with fixed row fields.

    Args:
        markdown: Candidate markdown.

    Returns:
        The evidence.
    """
    return extract_evidence(
        markdown, document_id="doc", decision_id="DEC", origin="ec.europa.eu", sha256="s",
        local_file_pdf="/p.pdf", local_file_md="/p.md", language="EN")


class EvidenceTests(unittest.TestCase):
    """Kind, title key and operative ending."""

    def test_full_decision_en(self) -> None:
        ev = _ev(FULL_39780)
        self.assertEqual(ev.kind, DocumentKind.DECISION)
        self.assertEqual(ev.title_key.doc_date, dt.date(2014, 12, 10))
        self.assertEqual(ev.title_key.case_numbers, ("39780",))
        self.assertTrue(ev.operative)

    def test_summary_is_summary(self) -> None:
        self.assertEqual(_ev(SUMMARY_39780).kind, DocumentKind.SUMMARY)

    def test_combined_decision(self) -> None:
        ev = _ev(COMBINED)
        self.assertEqual(ev.kind, DocumentKind.COMBINED)
        self.assertEqual(len(ev.title_key.case_numbers), 5)

    def test_interim_measures_decision(self) -> None:
        ev = _ev(INTERIM_34174)
        self.assertEqual(ev.kind, DocumentKind.DECISION)
        self.assertEqual(ev.title_key.doc_date, dt.date(1992, 6, 11))
        self.assertEqual(ev.title_key.case_numbers, ("34174",))

    def test_letter(self) -> None:
        ev = _ev(LETTER)
        self.assertEqual(ev.kind, DocumentKind.LETTER)
        self.assertIsNone(ev.title_key)

    def test_french_decision_footnotes_after_operative(self) -> None:
        ev = _ev(FRENCH)
        self.assertEqual(ev.kind, DocumentKind.DECISION)
        self.assertEqual(ev.title_key.doc_date, dt.date(2011, 3, 31))
        self.assertEqual(ev.title_key.case_numbers, ("39168",))
        self.assertTrue(ev.operative)

    def test_french_affaire_case_and_arrete_formula_before_annexes(self) -> None:
        """Older French formula, case written as 'Affaire', annex tables after the operative part."""
        text = (
            "# DÉCISION DE LA COMMISSION\ndu 23.6.2010\nrelative à une procédure (Affaire 36212/Papier "
            "autocopiant)\nLe texte en langue française est le seul faisant foi" + FILLER
            + "A ARRÊTÉ LA PRÉSENTE DÉCISION:\nArticle premier\n"
            + "ANNEXE I | 21.6.94, Francfort | X | X |\n" * 40)
        ev = _ev(text)
        self.assertEqual(ev.kind, DocumentKind.DECISION)
        self.assertEqual(ev.title_key.case_numbers, ("36212",))
        self.assertTrue(ev.operative)

    def test_roman_numeral_month(self) -> None:
        """'5/X/2005' is 5 October 2005 (Peugeot-era non-confidential versions)."""
        text = ("EN Brussels, 5/X/2005 NON CONFIDENTIAL VERSION COMMISSION DECISION of 5/X/2005 relating to a "
                "proceeding pursuant to Article 81 (Case COMP/E-2/36.623 - SEP et autres/Peugeot SA)"
                + FILLER + "HAS ADOPTED THIS DECISION:\nDone at Brussels,\n")
        ev = _ev(text)
        self.assertEqual(ev.kind, DocumentKind.DECISION)
        self.assertEqual(ev.title_key.doc_date, dt.date(2005, 10, 5))
        self.assertEqual(ev.title_key.case_numbers, ("36623",))

    def test_glued_title_words(self) -> None:
        """OCR may glue 'COMMISSIONDECISION'."""
        text = ("CASE AT.40670 - Google - Adtech (Only the English text is authentic) COMMISSIONDECISION of 5.9.2025 "
                "relating to a proceeding under Article 102 (AT.40670 - Google - Adtech and Data-related practices)"
                + FILLER + "HAS ADOPTED THIS DECISION:\nDone at Brussels,\n")
        ev = _ev(text)
        self.assertEqual(ev.kind, DocumentKind.DECISION)
        self.assertEqual(ev.title_key.doc_date, dt.date(2025, 9, 5))

    def test_amending_decision_takes_its_own_date(self) -> None:
        """'amending Decision C(2004) 4030 of 20 October 2004' must not become the date."""
        text = ("Brussels, 16.6.2017 C(2017) 4098 final COMMISSION DECISION amending Decision C(2004) 4030 "
                "(final) of 20 October 2004 relating to a proceeding under Article 81(1) of the EC Treaty "
                "(COMP/38.238 - Raw Tobacco - Spain)" + FILLER + "HAS ADOPTED THIS DECISION:\nDone at Brussels,\n")
        ev = _ev(text)
        self.assertEqual(ev.title_key.doc_date, dt.date(2017, 6, 16))
        self.assertEqual(ev.title_key.case_numbers, ("38238",))

    def test_title_without_date_has_no_key(self) -> None:
        """'DÉCISION DE LA COMMISSION du relative à' (date left blank) takes no later date."""
        text = ("DÉCISION DE LA COMMISSION du relative à une procédure d'application de l'article 81 du traité CE "
                "Affaire COMP/39406 – Marine Hoses LA COMMISSION, vu le règlement (CE) no 1/2003 du Conseil du "
                "16 décembre 2002" + FILLER + "A ADOPTÉ LA PRÉSENTE DÉCISION:\n")
        ev = _ev(text)
        self.assertEqual(ev.kind, DocumentKind.DECISION)
        self.assertIsNone(ev.title_key)

    def test_case_notations(self) -> None:
        """COMP/C.38.238, COMP//39.401, COMP/F-2/36.623 are read."""
        for notation, digits in (("COMP/C.38.238", "38238"), ("COMP//39.401", "39401"),
                                 ("COMP/F-2/36.623", "36623"), ("COMP/E-1/39.168", "39168")):
            text = (f"COMMISSION DECISION of 8.7.2009 relating to a proceeding ({notation} - Subject)"
                    + FILLER + "Done at Brussels,\n")
            self.assertEqual(_ev(text).title_key.case_numbers, (digits,), notation)

    def test_german_beschluss(self) -> None:
        ev = _ev(GERMAN)
        self.assertEqual(ev.kind, DocumentKind.DECISION)
        self.assertEqual(ev.title_key.doc_date, dt.date(2011, 3, 31))
        self.assertTrue(ev.operative)

    def test_different_cartels_have_different_title_keys(self) -> None:
        a = _ev(FOREX.format(name="Three Way Banana Split")).title_key
        b = _ev(FOREX.format(name="Essex Express")).title_key
        self.assertEqual((a.case_numbers, a.doc_date), (b.case_numbers, b.doc_date))
        self.assertNotEqual(a, b)

    def test_mirrors_have_equal_title_keys(self) -> None:
        self.assertEqual(_ev(FULL_39780).title_key, _ev(MIRROR_39780).title_key)

    def test_no_operative_ending(self) -> None:
        ev = _ev(FULL_39780.split("HAS ADOPTED")[0])
        self.assertEqual(ev.kind, DocumentKind.DECISION)
        self.assertFalse(ev.operative)

    def test_vocabulary_is_long_words(self) -> None:
        ev = _ev(FULL_39780)
        self.assertIn("envelopes", ev.vocabulary)
        self.assertNotIn("the", ev.vocabulary)
        self.assertEqual(ev.vocabulary, _ev(MIRROR_39780).vocabulary)

    def test_md_chars_and_row_fields_carried(self) -> None:
        ev = _ev(FULL_39780)
        self.assertEqual(ev.md_chars, len(FULL_39780))
        self.assertEqual((ev.document_id, ev.origin, ev.language), ("doc", "ec.europa.eu", "EN"))


if __name__ == "__main__":
    unittest.main()
