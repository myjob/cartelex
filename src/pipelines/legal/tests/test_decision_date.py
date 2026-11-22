"""Unit tests for ``pipelines.legal.decision_date``.

The inputs mimic the failure shapes of ISSUES #369 (review A26): a body that
cites earlier decisions, an amending decision whose title names the amended
decision's date, a signature line whose date MinerU dropped, footnotes after
the operative part, and section numbers HeidelTime read as dates (#314(e)).
"""

import datetime as dt
import unittest

from pipelines.legal.decision_date import (
    DateReference,
    DateSource,
    adoption_date,
    id_date,
    reference_conflicts,
)

FILLER = "The undertakings participated in a single and continuous infringement. " * 40

AMENDING = (
    "# COMMISSION DECISION\n\nof 4 August 2009\n\namending Decision C(2008) 926 final of 5 March 2008 "
    "relating to a proceeding under Article 81 of the Treaty (Case COMP/38.700 - Calcium chloride)\n\n"
    + FILLER
    + "In its Decision of 27 June 1994 the Commission found ...\n\nHAS ADOPTED THIS DECISION:\n\n"
    "Article 1\n\nDecision C(2008) 926 final is amended.\n\nDone at Brussels, 4 August 2009.\n\n"
    "For the Commission\n\nNeelie KROES\n\nMember of the Commission\n"
)


class AdoptionDateTest(unittest.TestCase):
    """The decision's own date line wins over every date cited in the text."""

    def test_date_line_beats_cited_and_amended_dates(self) -> None:
        result = adoption_date(AMENDING)
        self.assertEqual(result.date, dt.date(2009, 8, 4))
        self.assertIs(result.source, DateSource.DATE_LINE)
        self.assertIn("Done at Brussels, 4 August 2009", result.quote)

    def test_date_line_beats_title_when_both_differ(self) -> None:
        text = "COMMISSION DECISION of 2 April 2003\n" + FILLER + "Done at Brussels, 30 April 2003.\n"
        self.assertEqual(adoption_date(text).date, dt.date(2003, 4, 30))

    def test_blank_date_line_falls_back_to_title(self) -> None:
        text = AMENDING.replace("Done at Brussels, 4 August 2009.", "Done at Brussels,")
        result = adoption_date(text)
        self.assertEqual(result.date, dt.date(2009, 8, 4))
        self.assertIs(result.source, DateSource.TITLE)

    def test_amending_title_takes_own_date_not_amended(self) -> None:
        text = AMENDING.replace("Done at Brussels, 4 August 2009.", "")
        self.assertEqual(adoption_date(text).date, dt.date(2009, 8, 4))

    def test_french_and_german_date_lines(self) -> None:
        self.assertEqual(
            adoption_date(FILLER + "Fait à Bruxelles, le 11 juin 1992.\n").date, dt.date(1992, 6, 11))
        self.assertEqual(
            adoption_date(FILLER + "Geschehen zu Brüssel am 21. Januar 1999.\n").date, dt.date(1999, 1, 21))

    def test_luxembourg_and_numeric_date(self) -> None:
        self.assertEqual(adoption_date(FILLER + "Done at Luxembourg, 7.10.2009\n").date, dt.date(2009, 10, 7))

    def test_footnote_after_date_line_is_ignored(self) -> None:
        text = (FILLER + "Done at Brussels, 31 March 2011.\n"
                "<small>(1) See Decision of the Commission, Done at Brussels, 19 September 2002.</small>\n")
        self.assertEqual(adoption_date(text).date, dt.date(2011, 3, 31))

    def test_section_number_is_never_a_date(self) -> None:
        text = "# 9.3.10. Reduction of the fine\n" + FILLER + "Done at Brussels, 9.3.10.\n"
        result = adoption_date(text)
        self.assertIsNone(result.date)
        self.assertIs(result.source, DateSource.NONE)

    def test_bare_signature_line_of_older_decisions(self) -> None:
        # CELEX-31964D0599, CELEX-31965D0426: no "Done at", but a sign-off follows.
        self.assertEqual(
            adoption_date(FILLER + "Brussels, 22 October 1964 For the Commission The President\n").date,
            dt.date(1964, 10, 22))
        self.assertEqual(
            adoption_date(FILLER + "Brussels, 17 September 1965 Article 4 For the Commission\n").date,
            dt.date(1965, 9, 17))

    def test_bare_header_and_address_are_not_signature_lines(self) -> None:
        # DEC-AT39796: the "Brussels, <date> C(...) final" header is no signature.
        text = "Brussels, 24.5.2011 C(2011) 3640 final\n" + FILLER + "Rue de la Loi, Brussels, 1 May 2010.\n"
        self.assertIsNone(adoption_date(text).date)

    def test_translated_made_in_and_month_first_dates(self) -> None:
        self.assertEqual(
            adoption_date(FILLER + "Made in Brussels, 16 May 1995 by the Commission\n").date, dt.date(1995, 5, 16))
        self.assertEqual(
            adoption_date(FILLER + "Brussels, June1,1964 For the Commission\n").date, dt.date(1964, 6, 1))
        self.assertEqual(
            adoption_date(FILLER + "Brussels, July 30, 1964 For the Commission\n").date, dt.date(1964, 7, 30))

    def test_title_beats_decision_cited_later_in_head(self) -> None:
        # 38337_251_1: the decide parser alone took the cited 2004 decision.
        text = ("COMMISSION OF THE EUROPEAN COMMUNITIES Brussels, the DECISION OF THE COMMISSION from the "
                "14 September 2005 in a procedure under Article 81\n" + FILLER
                + "see Commission Decision of 15 March 2004 in Case COMP/38.000\n" + FILLER
                + "Events in Brussels on [...] In the name of the Commission\n")
        self.assertEqual(adoption_date(text).date, dt.date(2005, 9, 14))

    def test_title_without_date_ignores_amended_decision_date(self) -> None:
        text = ("COMMISSION DECISION relating to a proceeding under Article 81 of the Treaty, amending "
                "Decision C(2008) 926 final of 5 March 2008\n" + FILLER + "Done at Brussels,\n")
        self.assertIsNone(adoption_date(text).date)

    def test_dashed_date_line(self) -> None:
        self.assertEqual(adoption_date(FILLER + "Done at Brussels, 26-05-2004\n").date, dt.date(2004, 5, 26))

    def test_decision_of_the_commission_title_forms(self) -> None:
        cases = {
            "DECISION OF THE COMMISSION of 1 June 1964 on an application": dt.date(1964, 6, 1),
            "DECISION OF THE COMMISSION dated 16/10/2003 in a proceeding": dt.date(2003, 10, 16),
            "DECISION OF THE COMMISSION you 02.10.2001 concerning": dt.date(2001, 10, 2),
            "Decision of the Commission TEXT to translate: dated 17 September 1965": dt.date(1965, 9, 17),
            # 38337_251_1: "the" after the lead; a later date in the window must not win.
            "DECISION OF THE COMMISSION from the 14 September 2005 The decision C(2004) of 15.3.2004":
                dt.date(2005, 9, 14),
            "DECISION OF THE COMMISSION on 24 September 1971 in a proceeding": dt.date(1971, 9, 24),
            "COMMISSION DECISION OF THE COMMISSION 6 November 1968 about a method": dt.date(1968, 11, 6),
            "COMMISSION DECISION Of [1 October 2008] relating to a proceeding": dt.date(2008, 10, 1),
            "COMMISSION DECISION of 26-05-2004 relating to a proceeding": dt.date(2004, 5, 26),
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                result = adoption_date(title + "\n" + FILLER + "Brussels, the [...] For the Commission\n")
                self.assertEqual((result.date, result.source), (expected, DateSource.TITLE))

    def test_no_date_anywhere(self) -> None:
        self.assertEqual(adoption_date(FILLER).source, DateSource.NONE)


class IdDateTest(unittest.TestCase):
    """Only ``DEC-*-YYYYMMDD`` ids carry a date."""

    def test_dec_id(self) -> None:
        self.assertEqual(id_date("DEC-AT39168-20110331"), dt.date(2011, 3, 31))

    def test_other_keys_and_invalid_dates(self) -> None:
        for key in ("CELEX-31992D0568", "40436_949_5", "DEC-AT39168-20111331", ""):
            with self.subTest(key=key):
                self.assertIsNone(id_date(key))


class ReferenceConflictsTest(unittest.TestCase):
    """Every disagreeing reference is listed; NA references are no conflict."""

    def test_all_agree(self) -> None:
        self.assertEqual(
            reference_conflicts(dt.date(2011, 3, 31), "DEC-AT39168-20110331", "2011-03-31", "2011"), [])

    def test_issue_example_disagrees_with_every_reference(self) -> None:
        conflicts = reference_conflicts(dt.date(2002, 9, 19), "DEC-AT39168-20110331", "31.03.2011", "2011")
        self.assertEqual([c.reference for c in conflicts], list(DateReference))
        self.assertEqual(conflicts[0].label(), "dec_id_date=2011-03-31")

    def test_na_references_are_ignored(self) -> None:
        self.assertEqual(reference_conflicts(dt.date(2002, 9, 19), "40436_949_5", "NA", "NA"), [])

    def test_day_only_difference_is_a_conflict(self) -> None:
        conflicts = reference_conflicts(dt.date(2008, 3, 5), "DEC-AT38700-20080305", "2008-03-04 00:00:00", None)
        self.assertEqual([c.reference for c in conflicts], [DateReference.HUMAN_INDEX_DECISION_DATE])


if __name__ == "__main__":
    unittest.main()
