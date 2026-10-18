import unittest
from unittest.mock import MagicMock

from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.universal_modules import BookLandmarkModule

# A book section path that lies under the landmark subsection marker
# (``settings.BOOK_LANDMARK_SECTION_MARKERS`` contains "F Economic landmark cases").
_LANDMARK_PATH = (
    "Infringements -> 2 Horizontal restrictions -> F Economic landmark cases "
    "-> F.1 Decca Navigator System"
)
# A plain chapter-prose path (case named only as an example).
_SIMPLE_PATH = "Infringements -> 2 Horizontal restrictions -> 2.1 Price fixing"


class TestBookLandmarkModule(unittest.TestCase):
    """The landmark fact is true only when a case id sits under the book's
    'Economic landmark cases' subsection."""

    def setUp(self):
        self.mock_sql_store = MagicMock()
        self.mock_book_chunk_repo = MagicMock()
        self.module = BookLandmarkModule(
            sql_store=self.mock_sql_store,
            book_chunk_repo=self.mock_book_chunk_repo,
        )
        self.params = FactExtractorParamVO(
            celex_id="TEST-456",
            target_fact_name="case_is_landmark_in_book",
            question="case id's is mentioned in the book",
            output_schema="",
            pipeline="book_landmark",
        )

    def test_landmark_path_returns_case_id(self):
        """A case referenced under the landmark subsection yields its case id."""
        self.mock_sql_store.select_fact4celex.return_value = []
        self.mock_sql_store.select_case_ids_for_celex.return_value = ["IV/30.979"]
        self.mock_book_chunk_repo.select_reference_paths.return_value = [
            _LANDMARK_PATH
        ]

        prediction = self.module.forward(self.params)

        self.assertEqual(prediction.output.source, "book_landmark")
        self.assertEqual(
            prediction.output.items, [{"case_is_landmark_in_book": "IV/30.979"}]
        )
        self.assertIsNone(prediction.output.references_doc)

    def test_simple_prose_path_returns_false(self):
        """A case named only in chapter prose is not landmark."""
        self.mock_sql_store.select_fact4celex.return_value = []
        self.mock_sql_store.select_case_ids_for_celex.return_value = ["IV/30.979"]
        self.mock_book_chunk_repo.select_reference_paths.return_value = [
            _SIMPLE_PATH
        ]

        prediction = self.module.forward(self.params)

        self.assertEqual(
            prediction.output.items, [{"case_is_landmark_in_book": "False"}]
        )

    def test_punctuation_variant_is_resolved(self):
        """Stored 'IV/30979' matches the book surface 'IV/30.979' via variants."""
        self.mock_sql_store.select_fact4celex.return_value = []
        self.mock_sql_store.select_case_ids_for_celex.return_value = ["IV/30979"]

        def paths_for(keyword: str) -> list[str]:
            return [_LANDMARK_PATH] if keyword == "IV/30.979" else []

        self.mock_book_chunk_repo.select_reference_paths.side_effect = paths_for

        prediction = self.module.forward(self.params)

        # The original stored case id is recorded, not the matched variant.
        self.assertEqual(
            prediction.output.items, [{"case_is_landmark_in_book": "IV/30979"}]
        )

    def test_no_case_ids_returns_false(self):
        """A celex with no book-resolved case ids is not landmark."""
        self.mock_sql_store.select_fact4celex.return_value = []
        self.mock_sql_store.select_case_ids_for_celex.return_value = []

        prediction = self.module.forward(self.params)

        self.assertEqual(
            prediction.output.items, [{"case_is_landmark_in_book": "False"}]
        )

    def test_sql_hit_short_circuits(self):
        """A cached fact value short-circuits before any book lookup."""
        mock_fact = MagicMock()
        mock_fact.value_json = None
        mock_fact.value_md = "IV/30.979"
        self.mock_sql_store.select_fact4celex.return_value = [mock_fact]

        prediction = self.module.forward(self.params)

        self.assertEqual(prediction.output.source, "facts")
        self.mock_sql_store.select_case_ids_for_celex.assert_not_called()
        self.mock_book_chunk_repo.select_reference_paths.assert_not_called()

    def test_force_override_skips_sql(self):
        """force_override=True bypasses the SQL cache entirely."""
        self.params.force_override = True
        self.mock_sql_store.select_case_ids_for_celex.return_value = []

        prediction = self.module.forward(self.params)

        self.mock_sql_store.select_fact4celex.assert_not_called()
        self.assertEqual(
            prediction.output.items, [{"case_is_landmark_in_book": "False"}]
        )


if __name__ == "__main__":
    unittest.main()
