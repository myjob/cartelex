"""Tests for the shortlist-then-pick NACE classification module.

``NaceModule`` ranks NACE classes against the case title plus the ``products``
fact, lets the LLM pick one class from the top of that shortlist, and emits
the class or its parent group depending on ``level``. Mocks sit at the SQL
store, reranker and LLM boundaries; the NACE tree is a small real one.
"""

import json
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.universal_modules import NaceModule
from pipelines.nace.nace_tree import NaceTree, RankedNaceVo

_CELEX = "DEC-AT39258-20101109"
_TITLE_ID = uuid.uuid4()

_TREE = [
    {
        "code": "H",
        "title": "Transportation and storage",
        "divisions": [
            {
                "code": "51",
                "title": "Air transport",
                "groups": [
                    {
                        "code": "51.1",
                        "title": "Passenger air transport",
                        "classes": [{"code": "51.10", "title": "Passenger air transport"}],
                    },
                    {
                        "code": "51.2",
                        "title": "Freight air transport and space transport",
                        "classes": [
                            {"code": "51.21", "title": "Freight air transport"},
                            {"code": "51.22", "title": "Space transport"},
                        ],
                    },
                ],
            }
        ],
    },
    {
        "code": "C",
        "title": "Manufacturing",
        "divisions": [
            {
                "code": "30",
                "title": "Manufacture of other transport equipment",
                "groups": [
                    {
                        "code": "30.3",
                        "title": "Manufacture of air and spacecraft and related machinery",
                        "classes": [
                            {
                                "code": "30.30",
                                "title": "Manufacture of air and spacecraft and related machinery",
                            }
                        ],
                    }
                ],
            }
        ],
    },
]


def _tree() -> NaceTree:
    """Write the miniature NACE tree to a temp file and load it."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(_TREE, f)
    return NaceTree(f.name)


def _row(value_md: str, row_id: uuid.UUID | None = None) -> SimpleNamespace:
    """Return a stand-in for a stored ``facts`` row."""
    return SimpleNamespace(id=row_id or uuid.uuid4(), value_md=value_md, value_json="[]")


def _params(target: str, pipeline: str, predecessors: list[str]) -> FactExtractorParamVO:
    """Build NACE params as loaded from FACT_EXTRACTOR.yaml."""
    return FactExtractorParamVO.from_yaml_struct(
        celex_id=_CELEX,
        struct={
            "target_fact_name": target,
            "question": "classify the economic activity",
            "output_schema": "",
            "pipeline": pipeline,
            "predecessors": predecessors,
        },
        force_override=True,
    )


class _RankByTitle:
    """Reranker stub: scores nodes by word overlap with the query, keeping order stable."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def rank_nacevo(self, query: str, nodes: list, top_k: int | None = None) -> list[RankedNaceVo]:
        """Rank *nodes* by shared lower-case words with *query*."""
        self.calls.append((query, [n.code for n in nodes]))
        words = set(query.lower().split())
        ranked = sorted(
            (RankedNaceVo(vo=n, score=float(len(words & set(n.title.lower().split())))) for n in nodes),
            key=lambda r: -r.score,
        )
        return ranked[:top_k] if top_k else ranked


class TestNaceModule(unittest.TestCase):
    """Verify shortlist construction, LLM pick handling and output shape."""

    def _module(self, level: str, facts: dict, picked: str) -> tuple[NaceModule, _RankByTitle]:
        sql_store = MagicMock()
        sql_store.select_fact4celex.side_effect = lambda cid, name: facts.get(name, [])
        ranker = _RankByTitle()
        module = NaceModule(sql_store=sql_store, nace_tree=_tree(), ranker=ranker, level=level)
        module.pick = MagicMock(
            return_value=SimpleNamespace(output=SimpleNamespace(nace_class_code=picked))
        )
        return module, ranker

    def _facts(self, group: str | None = None) -> dict:
        facts = {
            "products": [_row("airfreight services")],
            "case_title": [_row("('AT.39258', 'Airfreight')", _TITLE_ID)],
        }
        if group is not None:
            facts["NACE_group_code"] = [_row(group)]
        return facts

    def test_group_level_emits_parent_group_of_picked_class(self) -> None:
        module, ranker = self._module("group", self._facts(), "51.21")

        pred = module(params=_params("NACE_group_code", "nace_group_code", ["products"]))

        self.assertEqual(pred.output.items, [{"value": "51.2:Freight air transport and space transport"}])
        query, codes = ranker.calls[0]
        self.assertIn("Airfreight", query)
        self.assertIn("airfreight services", query)
        self.assertEqual(sorted(codes), ["30.30", "51.10", "51.21", "51.22"])
        candidates = module.pick.call_args.kwargs["candidates"]
        self.assertIn("51.21: Freight air transport (group: Freight air transport and space transport)", candidates)

    def test_class_level_shortlists_only_the_stored_group(self) -> None:
        facts = self._facts(group="51.2:Freight air transport and space transport")
        module, ranker = self._module("class", facts, "51.21")

        pred = module(params=_params("NACE_class_code", "nace_class_code", ["products", "NACE_group_code"]))

        self.assertEqual(pred.output.items, [{"nace_class_code": "51.21: Freight air transport"}])
        self.assertEqual(sorted(ranker.calls[0][1]), ["51.21", "51.22"])

    def test_pick_outside_shortlist_falls_back_to_ranker_top1(self) -> None:
        module, _ = self._module("class", self._facts(group="51.2:Freight air transport and space transport"), "99.99")

        pred = module(params=_params("NACE_class_code", "nace_class_code", ["products", "NACE_group_code"]))

        self.assertEqual(pred.output.items, [{"nace_class_code": "51.21: Freight air transport"}])

    def test_unknown_stored_group_shortlists_all_classes(self) -> None:
        module, ranker = self._module("class", self._facts(group="NA"), "51.21")

        module(params=_params("NACE_class_code", "nace_class_code", ["products", "NACE_group_code"]))

        self.assertEqual(len(ranker.calls[0][1]), 4)

    def test_na_products_yields_empty_output_without_llm(self) -> None:
        facts = self._facts()
        facts["products"] = [_row("NA")]
        module, ranker = self._module("group", facts, "51.21")

        pred = module(params=_params("NACE_group_code", "nace_group_code", ["products"]))

        self.assertEqual(pred.output.items, [])
        self.assertEqual(ranker.calls, [])
        module.pick.assert_not_called()

    def test_case_title_recorded_as_lineage_source(self) -> None:
        module, _ = self._module("group", self._facts(), "51.21")

        pred = module(params=_params("NACE_group_code", "nace_group_code", ["products"]))

        (lineage,) = pred.output.lineage_sources
        self.assertEqual(lineage.source_fact_id, _TITLE_ID)
        self.assertEqual(lineage.relation_type, "derived_from")
        self.assertEqual(lineage.description, "fact:case_title")

    def test_missing_case_title_still_classifies_on_products(self) -> None:
        facts = self._facts()
        del facts["case_title"]
        module, _ = self._module("group", facts, "51.21")

        pred = module(params=_params("NACE_group_code", "nace_group_code", ["products"]))

        self.assertEqual(pred.output.items, [{"value": "51.2:Freight air transport and space transport"}])
        self.assertEqual(pred.output.lineage_sources, [])

    def test_invalid_level_raises(self) -> None:
        with self.assertRaises(ValueError):
            NaceModule(sql_store=MagicMock(), nace_tree=_tree(), ranker=_RankByTitle(), level="division")


if __name__ == "__main__":
    unittest.main()
