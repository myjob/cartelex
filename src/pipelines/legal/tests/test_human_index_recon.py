"""Unit tests for :mod:`pipelines.legal.human_index_recon` (pure, no DB).

The examples are the value pairs found in the workbook and the facts table.

Run with:
    PYTHONPATH=src .venv/bin/python -m unittest \\
        src/pipelines/legal/tests/test_human_index_recon.py -v
"""

import unittest

from pipelines.legal.human_index_recon import (
    FACT_COMMISSIONER,
    FACT_CONDUCT,
    FACT_DOMINANCE,
    FACT_INFRINGEMENT,
    FACT_PROCEDURAL,
    ExtractedFacts,
    HumanIndexRow,
    ReconStatus,
    ReconTarget,
    SizeBand,
    normalise_formal_decision,
    parse_human_firms,
    reconcile_commissioner,
    reconcile_firms,
    reconcile_formal_decision,
    reconcile_nature_of_conduct,
    reconcile_row,
    size_band,
)


def _row(**cells: str | None) -> HumanIndexRow:
    """Build an index row with the given cells; the rest stay blank."""
    return HumanIndexRow(celex_id="CELEX-TEST", source_row=2, **cells)


def _facts(roster: list[str] | None = None, **values: str) -> ExtractedFacts:
    """Build extracted facts from fact-name keyword values and a roster."""
    return ExtractedFacts(values=values, roster=roster or [])


class TestHumanIndexRow(unittest.TestCase):
    """Worksheet cells are cleaned at the boundary."""

    def test_blank_cells_become_none(self) -> None:
        row = _row(formal_decision="   ", firms="", title=" Vitamins ")
        self.assertIsNone(row.formal_decision)
        self.assertIsNone(row.firms)
        self.assertEqual(row.title, "Vitamins")


class TestSizeBand(unittest.TestCase):
    """Decisions are banded by stored tokens."""

    def test_bands_split_above_each_threshold(self) -> None:
        self.assertEqual(size_band(12_000, 12_000, 40_000), SizeBand.SMALL)
        self.assertEqual(size_band(12_001, 12_000, 40_000), SizeBand.MEDIUM)
        self.assertEqual(size_band(40_001, 12_000, 40_000), SizeBand.LARGE)

    def test_no_content_has_no_band(self) -> None:
        self.assertIsNone(size_band(None, 12_000, 40_000))
        self.assertIsNone(size_band(0, 12_000, 40_000))


class TestFormalDecision(unittest.TestCase):
    """Column Z against case_commission_infringement."""

    def test_the_twelve_spellings_fold_onto_their_class(self) -> None:
        cases = {
            "Infringement": "infringement",
            "Exemption": "exemption",
            "Renewal of Exemption": "exemption",
            "Renewal of exemption, related to Greek Ferries": "exemption",
            "Negative Clearance": "negative_clearance",
            "Non-applicability": "negative_clearance",
            "Exemption and Negative clearance": "exemption_and_negative_clearance",
            "Decision with Commitments": "commitments",
            "Interim Measure": "interim_measure",
        }
        for spelling, expected in cases.items():
            with self.subTest(spelling=spelling):
                self.assertEqual(normalise_formal_decision(spelling), expected)

    def test_infringement_agrees_with_true(self) -> None:
        result = reconcile_formal_decision(
            _row(formal_decision="Infringement"), _facts(**{FACT_INFRINGEMENT: "True"})
        )
        self.assertEqual(result.status, ReconStatus.AGREE)
        self.assertEqual(result.human_norm, "infringement")
        self.assertEqual(result.extracted_norm, "true")

    def test_exemption_agrees_with_false_and_conflicts_with_true(self) -> None:
        row = _row(formal_decision="Exemption")
        agree = reconcile_formal_decision(row, _facts(**{FACT_INFRINGEMENT: "False"}))
        conflict = reconcile_formal_decision(row, _facts(**{FACT_INFRINGEMENT: "True"}))
        self.assertEqual(agree.status, ReconStatus.AGREE)
        self.assertEqual(conflict.status, ReconStatus.CONFLICT)

    def test_interim_measure_is_not_comparable(self) -> None:
        result = reconcile_formal_decision(
            _row(formal_decision="Interim measure"), _facts(**{FACT_INFRINGEMENT: "True"})
        )
        self.assertEqual(result.status, ReconStatus.NOT_COMPARABLE)

    def test_unknown_spelling_is_not_comparable(self) -> None:
        result = reconcile_formal_decision(
            _row(formal_decision="Settlement"), _facts(**{FACT_INFRINGEMENT: "True"})
        )
        self.assertEqual(result.status, ReconStatus.NOT_COMPARABLE)

    def test_missing_na_and_malformed_extraction_are_blank(self) -> None:
        row = _row(formal_decision="Infringement")
        for value in (None, "", "NA", "| company | fine_amount |"):
            with self.subTest(value=value):
                facts = _facts(**({FACT_INFRINGEMENT: value} if value is not None else {}))
                result = reconcile_formal_decision(row, facts)
                self.assertEqual(result.status, ReconStatus.EXTRACTION_BLANK)
        self.assertIn("reason", result.detail)

    def test_blank_human_cell(self) -> None:
        result = reconcile_formal_decision(_row(), _facts(**{FACT_INFRINGEMENT: "True"}))
        self.assertEqual(result.status, ReconStatus.HUMAN_BLANK)


class TestNatureOfConduct(unittest.TestCase):
    """Column AD (or its codes) against the infringement category facts."""

    def test_label_agrees_with_the_agreement_category(self) -> None:
        result = reconcile_nature_of_conduct(
            _row(nature_of_conduct="JV"), _facts(**{FACT_CONDUCT: "joint venture"})
        )
        self.assertEqual(result.status, ReconStatus.AGREE)
        self.assertEqual(result.human_norm, "joint_venture")

    def test_dominance_agrees_although_extraction_adds_vertical(self) -> None:
        # Intel: the independent category facts assert two natures.
        result = reconcile_nature_of_conduct(
            _row(nature_of_conduct="Dominance"),
            _facts(**{FACT_CONDUCT: "vertical", FACT_DOMINANCE: "True", FACT_PROCEDURAL: "NA"}),
        )
        self.assertEqual(result.status, ReconStatus.AGREE)
        self.assertEqual(result.detail["extra"], ["vertical"])
        self.assertEqual(result.extracted_norm, "dominance+vertical")

    def test_different_category_is_a_conflict(self) -> None:
        # Candle Waxes: a cartel filed as joint venture by the extraction.
        result = reconcile_nature_of_conduct(
            _row(nature_of_conduct="Horizontal"), _facts(**{FACT_CONDUCT: "joint venture"})
        )
        self.assertEqual(result.status, ReconStatus.CONFLICT)
        self.assertEqual(result.detail["missing"], ["horizontal"])

    def test_codes_are_used_when_the_label_is_empty(self) -> None:
        # TACA: "1, 2" is horizontal and dominance.
        row = _row(conduct_indicator="1, 2")
        agree = reconcile_nature_of_conduct(
            row, _facts(**{FACT_CONDUCT: "horizontal", FACT_DOMINANCE: "True"})
        )
        conflict = reconcile_nature_of_conduct(row, _facts(**{FACT_CONDUCT: "horizontal"}))
        self.assertEqual(agree.status, ReconStatus.AGREE)
        self.assertEqual(agree.human_norm, "dominance+horizontal")
        self.assertEqual(conflict.detail["missing"], ["dominance"])

    def test_parenthesised_code_is_a_secondary_aspect(self) -> None:
        result = reconcile_nature_of_conduct(
            _row(conduct_indicator="1 (4)"), _facts(**{FACT_CONDUCT: "horizontal"})
        )
        self.assertEqual(result.status, ReconStatus.AGREE)

    def test_na_category_facts_are_an_extraction_blank(self) -> None:
        result = reconcile_nature_of_conduct(
            _row(nature_of_conduct="Horizontal"), _facts(**{FACT_CONDUCT: "NA"})
        )
        self.assertEqual(result.status, ReconStatus.EXTRACTION_BLANK)


class TestCommissioner(unittest.TestCase):
    """Column AO against case_commissioner, on the surname."""

    def _status(self, human: str, extracted: str) -> ReconStatus:
        return reconcile_commissioner(
            _row(responsible_commissioner=human), _facts(**{FACT_COMMISSIONER: extracted})
        ).status

    def test_role_note_casing_and_first_name_are_ignored(self) -> None:
        self.assertEqual(self._status("Karel van Miert (M)", "Karel VAN MIERT"), ReconStatus.AGREE)
        self.assertEqual(self._status("Nelie Kroes", "Neelie Kroes"), ReconStatus.AGREE)
        self.assertEqual(
            self._status("Francois-Xavier Ortoli (P)", "François-Xavier ORTOLI"), ReconStatus.AGREE
        )

    def test_truncated_and_misspelled_surnames_agree_as_fuzzy(self) -> None:
        for human, extracted in (
            ("Franco M. Malfatti (P)", "Franco M. Malfa"),
            ("Frans Andriessen (M)", "Franz Andriesen"),
            ("A. Borschette (M)", "A. BORSCHEITE"),
            ("Raymond Vouel (M)", "Raymond VOUUEL"),
        ):
            with self.subTest(extracted=extracted):
                result = reconcile_commissioner(
                    _row(responsible_commissioner=human), _facts(**{FACT_COMMISSIONER: extracted})
                )
                self.assertEqual(result.status, ReconStatus.AGREE)
                self.assertEqual(result.detail["match"], "fuzzy")

    def test_another_person_is_a_conflict(self) -> None:
        self.assertEqual(self._status("Mario Monti (M)", "Neelie Kroes"), ReconStatus.CONFLICT)
        self.assertEqual(
            self._status("Franco M. Malfatti (P)", "S.L. Mansholt"), ReconStatus.CONFLICT
        )
        self.assertEqual(
            self._status("Walter Hallstein (P)", "European Economic Community Commission"),
            ReconStatus.CONFLICT,
        )

    def test_empty_or_na_extraction_is_blank(self) -> None:
        self.assertEqual(self._status("Nelie Kroes", ""), ReconStatus.EXTRACTION_BLANK)
        self.assertEqual(self._status("Nelie Kroes", "NA"), ReconStatus.EXTRACTION_BLANK)


class TestParseHumanFirms(unittest.TestCase):
    """The firms cell is split into firm names."""

    def test_country_items_are_dropped(self) -> None:
        cell = "Shell;UK/The Netherlands; Sasol;South Africa and Germany; MOL;Hungary; IMI;UK"
        self.assertEqual(parse_human_firms(cell), ["Shell", "Sasol", "MOL", "IMI"])

    def test_parenthesised_notes_do_not_split_a_name(self) -> None:
        cell = "ArcelorMittal (L;F;B;I) Emesa (ES;L);Proderac (ES)"
        self.assertEqual(parse_human_firms(cell), ["ArcelorMittal Emesa", "Proderac"])

    def test_legal_form_item_joins_the_name_before_it(self) -> None:
        cell = "Citibank; N.A; Mueller Industries; Inc; HSBC Bank plc"
        self.assertEqual(
            parse_human_firms(cell), ["Citibank N.A", "Mueller Industries Inc", "HSBC Bank plc"]
        )

    def test_multi_word_name_made_of_legal_forms_stays_a_firm(self) -> None:
        self.assertEqual(parse_human_firms("Quantas;SAS AB;SAS Cargo Group A/S"),
                         ["Quantas", "SAS AB", "SAS Cargo Group A/S"])

    def test_short_firm_names_are_not_mistaken_for_country_codes(self) -> None:
        self.assertEqual(parse_human_firms("SAS AB; ENI; DE"), ["SAS AB", "ENI", "DE"])

    def test_empty_cell(self) -> None:
        self.assertEqual(parse_human_firms(None), [])
        self.assertEqual(parse_human_firms(" ; ;"), [])


class TestFirms(unittest.TestCase):
    """Column I against the companies_investigated roster."""

    def test_same_entities_in_another_order_agree(self) -> None:
        result = reconcile_firms(
            _row(firms="F. Hoffmann-La Roche AG;BASF AG;Solvay Pharmaceuticals BV."),
            _facts(roster=["BASF AG", "Solvay Pharmaceuticals BV", "F. Hoffmann-La Roche AG"]),
        )
        self.assertEqual(result.status, ReconStatus.AGREE)
        self.assertEqual(result.detail, {"human_count": 3, "extracted_count": 3})

    def test_group_level_index_against_legal_entities_is_a_superset(self) -> None:
        # Carglass: four groups in the index, their subsidiaries in the roster.
        result = reconcile_firms(
            _row(firms="Saint-Gobain (France); Asahi/AGC Flat Glass (Japan); Soliver (Belgium)"),
            _facts(
                roster=[
                    "Asahi Glass Co. Ltd",
                    "AGC Flat Glass Europe SA/NV",
                    "Glaverbel France SA",
                    "La Compagnie de Saint-Gobain SA",
                    "Saint-Gobain Sekurit France SA",
                    "Soliver NV",
                ]
            ),
        )
        self.assertEqual(result.status, ReconStatus.EXTRACTION_SUPERSET)
        self.assertEqual(result.detail["extra"], ["Glaverbel France SA"])
        self.assertNotIn("missing", result.detail)

    def test_lost_spaces_misspellings_and_dotted_names_still_match(self) -> None:
        result = reconcile_firms(
            _row(firms="InBev NV; GDF SUEZ SA; Quantas; Frabo"),
            _facts(roster=["InBevNV", "GDFSuez SA", "Qantas Airways Limited", "FRA.BO S.p.A"]),
        )
        self.assertEqual(result.status, ReconStatus.AGREE)

    def test_acronym_matches_the_spelled_out_name_both_ways(self) -> None:
        result = reconcile_firms(
            _row(firms="SNCF; L’Ordre national des pharmaciens"),
            _facts(roster=["Société nationale des chemins de fer français", "ONP"]),
        )
        self.assertEqual(result.status, ReconStatus.AGREE)

    def test_single_token_entity_found_inside_the_human_name(self) -> None:
        result = reconcile_firms(
            _row(firms="Bundesverband der Deutschen Zementindustrie eV-BDZ"),
            _facts(roster=["BDZ"]),
        )
        self.assertEqual(result.status, ReconStatus.AGREE)

    def test_human_firm_absent_from_the_roster_is_a_conflict(self) -> None:
        # Trucks: the roster holds a fraction of the addressees.
        result = reconcile_firms(
            _row(firms="AB Volvo; Daimler AG; MAN SE"),
            _facts(roster=["Daimler AG", "[Industry association]"]),
        )
        self.assertEqual(result.status, ReconStatus.CONFLICT)
        self.assertEqual(result.detail["missing"], ["AB Volvo", "MAN SE"])
        self.assertEqual(result.detail["extra"], ["[Industry association]"])

    def test_firms_sharing_only_a_generic_word_do_not_match(self) -> None:
        result = reconcile_firms(
            _row(firms="Barclays Bank plc"), _facts(roster=["Deutsche Bank AG"])
        )
        self.assertEqual(result.status, ReconStatus.CONFLICT)

    def test_blank_sides(self) -> None:
        self.assertEqual(
            reconcile_firms(_row(), _facts(roster=["BASF AG"])).status, ReconStatus.HUMAN_BLANK
        )
        self.assertEqual(
            reconcile_firms(_row(firms="BASF AG"), _facts()).status, ReconStatus.EXTRACTION_BLANK
        )


class TestReconcileRow(unittest.TestCase):
    """One index row yields one result per target."""

    def test_one_result_per_target_in_enum_order(self) -> None:
        results = reconcile_row(_row(formal_decision="Infringement"), _facts())
        self.assertEqual([r.target for r in results], list(ReconTarget))
        self.assertTrue(all(r.celex_id == "CELEX-TEST" for r in results))

    def test_decision_without_facts_is_blank_on_the_extraction_side(self) -> None:
        row = _row(
            formal_decision="Infringement",
            nature_of_conduct="Horizontal",
            responsible_commissioner="Mario Monti (M)",
            firms="BASF AG",
        )
        statuses = {r.status for r in reconcile_row(row, _facts())}
        self.assertEqual(statuses, {ReconStatus.EXTRACTION_BLANK})


if __name__ == "__main__":
    unittest.main()
