"""Unit tests for the source-decision rule ladder (pure, evidence built in code).

Run with:
    PYTHONPATH=src .venv/bin/python -m unittest pipelines.decide.tests.test_ladder
"""

import datetime as dt
import unittest

from pipelines.decide.ladder import index_by_case_and_date, plan_decision
from pipelines.decide.models import (
    CandidateEvidence,
    DocumentKind,
    ExistingSource,
    TitleKey,
    UndecidedDecision,
    Verdict,
)

D_2014 = dt.date(2014, 12, 10)


def _key(case: str | tuple[str, ...], date: dt.date, subject: str = "subject") -> TitleKey:
    """Build a title key."""
    cases = (case,) if isinstance(case, str) else case
    return TitleKey(case_numbers=cases, subject=subject, doc_date=date)


def _ev(doc: str, *, kind: DocumentKind = DocumentKind.DECISION, key: TitleKey | None = None,
        origin: str = "ec.europa.eu", sha: str | None = None, lang: str | None = "EN",
        operative: bool = True, chars: int = 1000, vocab: frozenset[str] = frozenset({"common"})
        ) -> CandidateEvidence:
    """Build candidate evidence."""
    return CandidateEvidence(
        document_id=doc, decision_id="D", origin=origin, sha256=sha or f"sha-{doc}",
        local_file_pdf=f"/p/{doc}.pdf", local_file_md=f"/p/{doc}.md", language=lang, kind=kind,
        title_key=key, operative=operative, md_chars=chars, vocabulary=vocab)


def _decision(decision_id: str, case: str, date: dt.date | None) -> UndecidedDecision:
    """Build an undecided manifest decision."""
    return UndecidedDecision(decision_id=decision_id, decision_date=date, case_ids=[case])


def _plan(decision, candidates, existing=(), taken=frozenset()):
    """Run the ladder with existing sources indexed like the CLI does."""
    by_sha = {e.sha256: e for e in existing if e.sha256}
    return plan_decision(decision, candidates, by_sha, index_by_case_and_date(list(existing)), set(taken))


class LadderTests(unittest.TestCase):
    """One test per settled case."""

    def test_mirrors_promote_ec_europa_eu(self) -> None:
        key = _key("39780", D_2014, "envelopes")
        plan = _plan(_decision("DEC-AT39780-20141210", "AT.39780", D_2014), [
            _ev("39780_3527_7", kind=DocumentKind.SUMMARY, operative=False),
            _ev("dbcomp-1125", key=key, origin="db-comp"),
            _ev("AT_39780_4058434_3528_8", key=key),
        ])
        self.assertEqual(plan.verdict, Verdict.PROMOTE)
        self.assertEqual(plan.promotions[0].winner.document_id, "AT_39780_4058434_3528_8")
        self.assertEqual(sorted(plan.promotions[0].members), ["AT_39780_4058434_3528_8", "dbcomp-1125"])
        self.assertEqual(plan.candidate_outcomes["39780_3527_7"], "summary")
        self.assertEqual(plan.candidate_outcomes["AT_39780_4058434_3528_8"], "winner -> DEC-AT39780-20141210")
        self.assertEqual(plan.candidate_outcomes["dbcomp-1125"], "version of AT_39780_4058434_3528_8")
        self.assertEqual(plan.promotions[0].decision_id, "DEC-AT39780-20141210")
        self.assertFalse(plan.promotions[0].is_new)

    def test_all_known_merges(self) -> None:
        date = dt.date(2021, 1, 20)
        owners = {"40413": "AT_40413_8465169_1613_3", "40414": "AT_40414_8465254_1496_3",
                  "40420": "AT_40420_8465317_1450_3"}
        existing = [ExistingSource(decision_id=o, document_id=o, sha256=f"sha-{c}", title_key=_key(c, date))
                    for c, o in owners.items()]
        existing.append(ExistingSource(decision_id="AT_40414_8465464_1500_3", document_id="combo",
                                       sha256="sha-combo", title_key=_key(("40413", "40414"), date)))
        cands = [_ev(f"dbcomp-{c}", key=_key(c, date), sha=f"sha-{c}") for c in owners]
        cands.append(_ev("dbcomp-2628", kind=DocumentKind.COMBINED, key=_key(("40413", "40414"), date),
                         sha="sha-combo"))
        plan = _plan(_decision("DEC-AT40413-20210120", "AT.40413", date), cands, existing)
        self.assertEqual(plan.verdict, Verdict.MERGE)
        self.assertEqual(len(plan.repoints), 4)
        self.assertEqual(plan.merge_into, "AT_40413_8465169_1613_3")
        self.assertEqual(plan.promotions, [])

    def test_partial_known_promotes_new(self) -> None:
        date = dt.date(2014, 10, 21)
        existing = [ExistingSource(decision_id="39924_1155_4", document_id="39924_1155_4", sha256="sha-1155",
                                   title_key=_key("39924", date, "swiss franc a"))]
        plan = _plan(_decision("DEC-AT39924-20141021", "AT.39924", date), [
            _ev("AT_39924_4127866_1155_6", key=_key("39924", date, "swiss franc a"), sha="sha-1155"),
            _ev("amending", key=_key("39924", dt.date(2016, 1, 1), "amending")),
        ], existing)
        self.assertEqual([r.document_id for r in plan.repoints], ["AT_39924_4127866_1155_6"])
        self.assertEqual(plan.promotions[0].winner.document_id, "amending")

    def test_same_case_and_date_other_text_goes_to_review(self) -> None:
        """Swiss Franc: a second decision of case and day already in the corpus is never promoted."""
        date = dt.date(2014, 10, 21)
        key = _key("39924", date, "swiss franc interest rate derivatives")
        v1155 = frozenset(f"w{i}" for i in range(100))
        v1156 = frozenset(f"w{i}" for i in range(17, 117))
        existing = [ExistingSource(decision_id="39924_1155_4", document_id="39924_1155_4", sha256="sha-x",
                                   title_key=key, vocabulary=v1155, language="EN")]
        plan = _plan(_decision("DEC-AT39924-20141021", "AT.39924", date), [
            _ev("AT_39924_4127866_1155_6", key=key, sha="sha-1155", vocab=v1155),
            _ev("AT_39924_4127881_1156_5", key=key, vocab=v1156),
        ], existing)
        self.assertEqual(plan.verdict, Verdict.REVIEW)
        self.assertEqual(plan.review_reason, "same case and date as an existing decision")
        self.assertEqual(plan.promotions, [])
        self.assertEqual(plan.candidate_outcomes["AT_39924_4127881_1156_5"],
                         "same case and date as existing 39924_1155_4")

    def test_language_version_of_existing_is_known(self) -> None:
        """E.On/GdF: the French version of a German source already in the corpus merges."""
        date = dt.date(2009, 7, 8)
        existing = [ExistingSource(decision_id="39401_1535_1", document_id="39401_1535_1", sha256="de",
                                   title_key=_key("39401", date, "e on gdf"), vocabulary=frozenset({"kommission"}),
                                   language="DE", case_ids=["IV/39401"])]
        plan = _plan(_decision("DEC-AT39401-20091016", "AT.39401", dt.date(2009, 10, 16)), [
            _ev("39401_1536_3", key=_key("39401", date, "e on gdf collusion"), lang="FR",
                vocab=frozenset({"commission"}))], existing)
        self.assertEqual((plan.verdict, plan.merge_into), (Verdict.MERGE, "39401_1535_1"))

    def test_owner_matched_by_registry_date_and_links(self) -> None:
        """An OJ owner whose title yields no key is matched by its registry date and case links."""
        date = dt.date(2002, 7, 5)
        existing = [ExistingSource(decision_id="CELEX-32002D0746", document_id="CELEX-32002D0746_EN", sha256="oj",
                                   title_key=None, decision_date=date, case_ids=["IV/37730"],
                                   vocabulary=frozenset({"common"}), language="EN")]
        plan = _plan(_decision("DEC-AT37730-20020705", "AT.37730", date), [
            _ev("dbcomp-1303", key=_key("37730", date))], existing)
        self.assertEqual((plan.verdict, plan.merge_into), (Verdict.MERGE, "CELEX-32002D0746"))

    def test_better_copy_english_original_becomes_source(self) -> None:
        """35470: an EN original replaces a German source with an old translation."""
        date = dt.date(2003, 10, 16)
        existing = [ExistingSource(decision_id="35470_14_4", document_id="35470_14_4", sha256="de",
                                   title_key=_key("35470", date), language="DE", case_ids=["IV/35470"],
                                   vocabulary=frozenset({"x"}), text_chars=1000)]
        plan = _plan(_decision("DEC-35470-20031016", "IV/35470", date), [
            _ev("dbcomp-1414", key=_key("35470", date), lang="EN", vocab=frozenset({"y"}))], existing)
        self.assertEqual(plan.verdict, Verdict.MERGE)
        self.assertTrue(plan.repoints[0].make_source)

    def test_same_case_date_other_subject_is_not_known(self) -> None:
        """FOREX: two same-day decisions of one case with 0.93 vocabulary overlap stay distinct."""
        date = dt.date(2019, 5, 16)
        vocab = frozenset({"forex", "cartel"})
        existing = [ExistingSource(decision_id="DEC-AT40135-20190516_2", document_id="essex", sha256="e",
                                   title_key=_key("40135", date, "forex essex express"), language="EN",
                                   vocabulary=vocab)]
        plan = _plan(_decision("DEC-AT40135-20190516", "AT.40135", date), [
            _ev("banana", key=_key("40135", date, "forex three way banana split"), vocab=vocab)], existing)
        self.assertEqual(plan.verdict, Verdict.REVIEW)
        self.assertEqual(plan.repoints, [])

    def test_better_copy_measured_on_text_not_markup(self) -> None:
        """38359: 352k raw vs 280k raw, but 256k text vs 115k text -> switch."""
        date = dt.date(2003, 12, 3)
        existing = [ExistingSource(decision_id="38359_36_1", document_id="38359_36_1", sha256="a",
                                   title_key=_key("38359", date), language="EN", text_chars=114627,
                                   vocabulary=frozenset({"common"}))]
        cand = _ev("dbcomp-1241", key=_key("38359", date), chars=351772).model_copy(update={"text_chars": 256049})
        plan = _plan(_decision("DEC-38359-20031203", "IV/38359", date), [cand], existing)
        self.assertTrue(plan.repoints[0].make_source)

    def test_equal_copy_keeps_existing_source(self) -> None:
        date = dt.date(2003, 12, 3)
        existing = [ExistingSource(decision_id="X", document_id="X", sha256="a", title_key=_key("38359", date),
                                   language="EN", text_chars=1000, vocabulary=frozenset({"common"}))]
        cand = _ev("c", key=_key("38359", date)).model_copy(update={"text_chars": 1100})
        plan = _plan(_decision("D", "IV/38359", date), [cand], existing)
        self.assertFalse(plan.repoints[0].make_source)

    def test_decision_leftover_blocks_merge(self) -> None:
        """A decision-like document without operative ending is not absorbed by a merge."""
        key = _key("39780", D_2014, "envelopes")
        existing = [ExistingSource(decision_id="OWN", document_id="own", sha256="sha-ec", title_key=key)]
        plan = _plan(_decision("DEC-AT39780-20141210", "AT.39780", D_2014), [
            _ev("known", key=key, sha="sha-ec"),
            _ev("odd", key=_key("39780", dt.date(2015, 1, 1)), operative=False)], existing)
        self.assertEqual(plan.verdict, Verdict.REVIEW)
        self.assertEqual(plan.review_reason, "unresolved decision document beside known documents")

    def test_mirror_of_known_candidate_is_known(self) -> None:
        """dbcomp-1125 mirrors AT_..._3528_8, which is byte-identical to the existing 39780_3528_6."""
        key = _key("39780", D_2014, "envelopes")
        existing = [ExistingSource(decision_id="39780_3528_6", document_id="39780_3528_6", sha256="sha-ec",
                                   title_key=None)]
        plan = _plan(_decision("DEC-AT39780-20141210", "AT.39780", D_2014), [
            _ev("39780_3527_7", kind=DocumentKind.SUMMARY, operative=False),
            _ev("AT_39780_4058434_3528_8", key=key, sha="sha-ec", chars=68759),
            _ev("dbcomp-1125", key=key, origin="db-comp", chars=68325),
        ], existing)
        self.assertEqual(plan.verdict, Verdict.MERGE)
        self.assertEqual(plan.merge_into, "39780_3528_6")
        self.assertEqual({r.document_id for r in plan.repoints},
                         {"AT_39780_4058434_3528_8", "dbcomp-1125", "39780_3527_7"})

    def test_oj_version_with_other_length_is_known(self) -> None:
        """Commission and Official Journal versions differ in length, not in vocabulary."""
        key = _key("36748", dt.date(1999, 9, 15), "reims ii")
        vocab = frozenset(f"w{i}" for i in range(100))
        existing = [ExistingSource(decision_id="CELEX-31999D0695", document_id="CELEX-31999D0695_EN",
                                   sha256="oj", title_key=key, vocabulary=vocab | {"extra"})]
        plan = _plan(_decision("DEC-AT36748-19990915", "IV/36748", dt.date(1999, 9, 15)), [
            _ev("dbcomp-1343", key=key, chars=1400, vocab=vocab)], existing)
        self.assertEqual((plan.verdict, plan.merge_into), (Verdict.MERGE, "CELEX-31999D0695"))

    def test_merge_owner_case_from_link_table(self) -> None:
        """An OJ-formatted owner whose title yields no case number is matched by its case links."""
        existing = [ExistingSource(decision_id="39801_2468_8", document_id="39801_2468_8", sha256="sha-1",
                                   title_key=_key((), dt.date(2014, 1, 29)), case_ids=["IV/39801"])]
        plan = _plan(_decision("DEC-AT39801-20140129", "AT.39801", dt.date(2014, 1, 29)), [
            _ev("AT_39801_3960888_2468_10", key=_key((), dt.date(2014, 1, 29)), sha="sha-1")], existing)
        self.assertEqual((plan.verdict, plan.merge_into), (Verdict.MERGE, "39801_2468_8"))

    def test_split_two_cartels(self) -> None:
        date = dt.date(2019, 5, 16)
        plan = _plan(_decision("DEC-AT40135-20190516", "AT.40135", date), [
            _ev("AT_40135_8399809_5457_11", key=_key("40135", date, "forex essex express")),
            _ev("AT_40135_8399795_5456_11", key=_key("40135", date, "forex three way banana split")),
        ], taken={"DEC-AT40135-20190516"})
        self.assertEqual(plan.verdict, Verdict.SPLIT)
        kept = [p for p in plan.promotions if not p.is_new]
        new = [p for p in plan.promotions if p.is_new]
        self.assertEqual(kept[0].winner.document_id, "AT_40135_8399795_5456_11")
        self.assertEqual(kept[0].decision_id, "DEC-AT40135-20190516")
        self.assertEqual(new[0].decision_id, "DEC-AT40135-20190516_2")

    def test_split_other_case_gets_own_id(self) -> None:
        date = dt.date(2016, 6, 29)
        plan = _plan(_decision("DEC-AT38589-20160629", "AT.38589", date), [
            _ev("a", key=_key("38589", date)), _ev("b", key=_key("39999", date))])
        self.assertEqual(plan.verdict, Verdict.SPLIT)
        self.assertEqual({p.decision_id for p in plan.promotions},
                         {"DEC-AT38589-20160629", "DEC-AT39999-20160629"})

    def test_split_id_collision_gets_suffix(self) -> None:
        date = dt.date(2014, 10, 21)
        plan = _plan(_decision("DEC-AT39924-20141021", "AT.39924", date), [
            _ev("a", key=_key("39924", date, "first")), _ev("b", key=_key("39924", date, "second"))],
            taken={"DEC-AT39924-20141021"})
        self.assertIn("DEC-AT39924-20141021_2", {p.decision_id for p in plan.promotions})

    def test_several_groups_none_matching_review(self) -> None:
        date = dt.date(2016, 6, 29)
        plan = _plan(_decision("DEC-AT38589-20160629", "AT.38589", date), [
            _ev("a", key=_key("11111", date)), _ev("b", key=_key("22222", date))])
        self.assertEqual(plan.verdict, Verdict.REVIEW)
        self.assertEqual(plan.review_reason, "several decisions, none matches the row")

    def test_language_versions_pick_one(self) -> None:
        date = dt.date(2011, 3, 31)
        plan = _plan(_decision("DEC-AT39168-20110331", "AT.39168", date), [
            _ev("39168_1108_5", key=_key("39168", date, "po articles de mercerie"), lang="FR", chars=14179),
            _ev("39168_1109_5", key=_key("39168", date, "po hartkurzwaren"), lang="DE", chars=13703),
        ])
        self.assertEqual(plan.verdict, Verdict.PROMOTE)
        self.assertEqual(len(plan.promotions), 1)
        self.assertEqual(plan.promotions[0].winner.document_id, "39168_1108_5")
        self.assertEqual(plan.promotions[0].language_init, "fr")

    def test_english_version_wins_over_origin(self) -> None:
        date = dt.date(2011, 3, 31)
        plan = _plan(_decision("DEC-X", "AT.39168", date), [
            _ev("fr", key=_key("39168", date, "fr subject"), lang="FR"),
            _ev("en", key=_key("39168", date, "en subject"), lang="EN", origin="db-comp"),
        ])
        self.assertEqual(plan.promotions[0].winner.document_id, "en")

    def test_date_mismatch_promotes_with_document_date(self) -> None:
        plan = _plan(_decision("DEC-AT34174-20120611", "IV/34174", dt.date(2012, 6, 11)), [
            _ev("34174_2_2", key=_key("34174", dt.date(1992, 6, 11)))])
        self.assertEqual(plan.verdict, Verdict.PROMOTE)
        self.assertEqual(plan.promotions[0].decision_date, dt.date(1992, 6, 11))

    def test_letters_only_review(self) -> None:
        plan = _plan(_decision("DEC-AT35808-19951206", "IV/35808", dt.date(1995, 12, 6)), [
            _ev("a", kind=DocumentKind.LETTER, operative=False)])
        self.assertEqual((plan.verdict, plan.review_reason), (Verdict.REVIEW, "letters only"))

    def test_summaries_only_review(self) -> None:
        plan = _plan(_decision("DEC-AT36623-20051005", "AT.36623", dt.date(2005, 10, 5)), [
            _ev("a", kind=DocumentKind.SUMMARY, operative=False),
            _ev("b", kind=DocumentKind.DECISION, key=_key("36623", dt.date(2005, 10, 5)), operative=False)])
        self.assertEqual((plan.verdict, plan.review_reason), (Verdict.REVIEW, "no operative commission decision"))

    def test_title_key_match_with_existing_source_is_known(self) -> None:
        key = _key("39780", D_2014, "envelopes")
        existing = [ExistingSource(decision_id="OWN", document_id="own", sha256="other", title_key=key,
                                   vocabulary=frozenset({"common"}), language="EN")]
        plan = _plan(_decision("DEC-AT39780-20141210", "AT.39780", D_2014), [_ev("rescan", key=key)], existing)
        self.assertEqual(plan.verdict, Verdict.MERGE)
        self.assertEqual(plan.repoints[0].owner_decision_id, "OWN")
        self.assertEqual(plan.promotions, [])

    def test_joined_proceeding_decision_is_valid(self) -> None:
        """A multi-case decision without single-case decisions beside it is the decision."""
        date = dt.date(2001, 9, 17)
        plan = _plan(_decision("DEC-COMP34493-20010917", "COMP/34493", date), [
            _ev("34493_63_6", kind=DocumentKind.COMBINED, key=_key(("34493", "37366"), date))])
        self.assertEqual(plan.verdict, Verdict.PROMOTE)

    def test_combined_beside_single_case_decisions_is_dropped(self) -> None:
        date = dt.date(2021, 1, 20)
        plan = _plan(_decision("DEC-AT40413-20210120", "AT.40413", date), [
            _ev("single", key=_key("40413", date)),
            _ev("combo", kind=DocumentKind.COMBINED, key=_key(("40413", "40414"), date))])
        self.assertEqual(plan.verdict, Verdict.PROMOTE)
        self.assertEqual(plan.candidate_outcomes["combo"], "combined beside single-case decisions")


if __name__ == "__main__":
    unittest.main()
