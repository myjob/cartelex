"""Tests for pipelines.heideltime.extract_heideltime.HeidelTime.

Unit tests mock ``_run_heideltime_jar`` (our pool-free JAR wrapper) so no
Java / TreeTagger installation is required.  The integration test class
calls the real binary and should only be executed in an environment where
py_heideltime has been fully set up (Java + TreeTagger).

Run with:
    .venv/bin/python -m unittest discover -s src/pipelines/heideltime/tests -v
"""

import re
import unittest
from unittest.mock import MagicMock, patch, call

from sqlalchemy import text as sa_text

from pipelines.rag.pgvector.models import HeidelTimeVO
from pipelines.heideltime.extract_heideltime import HeidelTime
from pipelines.heideltime.heideltime_module import (
    HeidelTimeEventModule,
    HeidelTimeEventAggregateModule,
    HeidelTimeModule,
    _chunk_hash,
)
from pipelines.agent_dspy.dspy_signature import (
    HeidelTimeCriticalEvent,
    HeidelTimeCriticalEventResult,
    HeidelTimeEvent,
    HeidelTimeEventResult,
)
from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.rag.pgvector.heideltime_ingestor import HeidelTimeIngestor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_timex(span: tuple, text: str = "dummy", tid: str = "t1") -> dict:
    """Return a minimal py_heideltime prediction dict with the given span."""
    return {
        "span": list(span),
        "text": text,
        "tid": tid,
        "type": "DATE",
        "value": "XXXX",
    }


_CELEX = "CELEX-12345"
_CHUNK = "chunk-001"


# ---------------------------------------------------------------------------
# Unit tests — _run_heideltime_jar is mocked
# ---------------------------------------------------------------------------


class TestHeidelTimeUnit(unittest.TestCase):
    """Unit tests for HeidelTime.predict — no external process is spawned."""

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def test_default_init(self):
        """Default language and document_type are set correctly."""
        ht = HeidelTime()
        self.assertEqual(ht.language, "english")
        self.assertEqual(ht.document_type, "news")

    def test_custom_init(self):
        """Custom language / document_type are normalised to lowercase at construction."""
        ht = HeidelTime(language="German", document_type="Narrative")
        self.assertEqual(ht.language, "german")
        self.assertEqual(ht.document_type, "narrative")

    # ------------------------------------------------------------------
    # predict — happy-path
    # ------------------------------------------------------------------

    @patch("pipelines.heideltime.extract_heideltime._run_heideltime_jar")
    def test_predict_returns_heideltime_vos(self, mock_ht):
        """predict returns HeidelTimeVO instances, not plain tuples."""
        mock_ht.return_value = [
            _make_timex((0, 11), text="30 June 2013", tid="t1"),
            _make_timex((20, 30), text="22 March 2006", tid="t2"),
        ]

        ht = HeidelTime()
        result = ht.predict(_CELEX, _CHUNK, ["30 June 2013 and 22 March 2006"])

        self.assertEqual(len(result), 1)  # one text → one inner list
        inner = result[0]
        self.assertEqual(len(inner), 2)
        vo0, vo1 = inner
        self.assertIsInstance(vo0, HeidelTimeVO)
        self.assertEqual(vo0.span_start, 0)
        self.assertEqual(vo0.span_end, 11)
        self.assertEqual(vo0.text, "30 June 2013")
        self.assertEqual(vo0.celex_id, _CELEX)
        self.assertEqual(vo0.fk_chunk_id, _CHUNK)
        self.assertEqual(vo1.span_start, 20)
        self.assertEqual(vo1.span_end, 30)

    @patch("pipelines.heideltime.extract_heideltime._run_heideltime_jar")
    def test_predict_multiple_texts(self, mock_ht):
        """predict processes each text independently and returns one list per text."""
        mock_ht.side_effect = [
            [_make_timex((0, 4), tid="t1")],
            [_make_timex((5, 9), tid="t2"), _make_timex((12, 16), tid="t3")],
        ]

        ht = HeidelTime()
        texts = ["2006 was the year.", "From 2000 to 2005 the case ran."]
        result = ht.predict(_CELEX, _CHUNK, texts)

        self.assertEqual(len(result), 2)
        self.assertEqual(len(result[0]), 1)
        self.assertEqual(result[0][0].span_start, 0)
        self.assertEqual(result[0][0].span_end, 4)
        self.assertEqual(len(result[1]), 2)
        self.assertEqual(result[1][0].span_start, 5)
        self.assertEqual(result[1][1].span_start, 12)

    @patch("pipelines.heideltime.extract_heideltime._run_heideltime_jar")
    def test_predict_no_timexs(self, mock_ht):
        """predict returns an empty inner list when no temporal expression is found."""
        mock_ht.return_value = []

        ht = HeidelTime()
        result = ht.predict(_CELEX, _CHUNK, ["This text has no dates at all."])

        self.assertEqual(result, [[]])

    @patch("pipelines.heideltime.extract_heideltime._run_heideltime_jar")
    def test_predict_passes_dct(self, mock_jar):
        """The document creation time (dct) is forwarded to the JAR runner."""
        mock_jar.return_value = [_make_timex((0, 8), text="tomorrow", tid="t1")]

        ht = HeidelTime()
        texts = ["tomorrow"]
        dcts = ["2006-03-22"]
        ht.predict(_CELEX, _CHUNK, texts, dcts=dcts)

        mock_jar.assert_called_once_with(
            text="tomorrow",
            language="english",
            document_type="news",
            dct="2006-03-22",
        )

    @patch("pipelines.heideltime.extract_heideltime._run_heideltime_jar")
    def test_predict_default_dct_is_none(self, mock_jar):
        """When dcts is omitted, None is passed to the JAR runner for each text."""
        mock_jar.return_value = []

        ht = HeidelTime()
        ht.predict(_CELEX, _CHUNK, ["some text", "another text"])

        expected_calls = [
            call(text="some text", language="english", document_type="news", dct=None),
            call(
                text="another text", language="english", document_type="news", dct=None
            ),
        ]
        mock_jar.assert_has_calls(expected_calls, any_order=False)

    @patch("pipelines.heideltime.extract_heideltime._run_heideltime_jar")
    def test_predict_language_forwarded(self, mock_jar):
        """language/document_type are normalised to lowercase before reaching the JAR runner."""
        mock_jar.return_value = []

        ht = HeidelTime(language="French", document_type="Scientific")
        ht.predict(_CELEX, _CHUNK, ["Demain"])

        mock_jar.assert_called_once_with(
            text="Demain",
            language="french",  # normalised in model_post_init
            document_type="scientific",  # normalised in model_post_init
            dct=None,
        )

    # ------------------------------------------------------------------
    # predict — CELEX-style legal text (from play_heideltime.py)
    # ------------------------------------------------------------------

    @patch("pipelines.heideltime.extract_heideltime._run_heideltime_jar")
    def test_predict_celex_article_text(self, mock_ht):
        """Simulate the FAPL decision text from play_heideltime.py."""
        mock_ht.return_value = [
            _make_timex((144, 156), text="30 June 2013", tid="t1"),
            _make_timex((245, 256), text="2 2 III 2006", tid="t2"),
        ]

        celex_text = (
            "This decision shall apply from the date on which it is notified to "
            "Football Association Premier League Limited until 30 June 2013.\n"
            "Brussels, 2 2 III 2006"
        )

        ht = HeidelTime()
        result = ht.predict(_CELEX, _CHUNK, [celex_text], dcts=["2006-03-22"])

        self.assertEqual(len(result), 1)
        inner = result[0]
        self.assertIsInstance(inner, list)
        self.assertEqual(len(inner), 2)
        # Each element must be a HeidelTimeVO with valid span
        for vo in inner:
            self.assertIsInstance(vo, HeidelTimeVO)
            self.assertIsInstance(vo.span_start, int)
            self.assertIsInstance(vo.span_end, int)
            self.assertLess(vo.span_start, vo.span_end)
            self.assertEqual(vo.celex_id, _CELEX)
            self.assertEqual(vo.fk_chunk_id, _CHUNK)

    @patch("pipelines.heideltime.extract_heideltime._run_heideltime_jar")
    def test_bug_predict_celex_D0426_article_text_returns_empty(self, mock_ht):
        """Documents the bug: when HeidelTime returns no timexs the result is [[]].

        The CELEX-31965D0426 text contains three obvious dates (30 January 1963,
        28 February 1962, 7 April 1965).  This unit test mocks the JAR to return
        an empty list, verifying that predict() correctly propagates an empty inner
        list rather than crashing.  See test_integration_celex_D0426_finds_three_dates
        in TestHeidelTimeIntegration for the real-JAR assertion.
        """
        mock_ht.return_value = []

        celex_text = (
            "CELEX-31965D0426 "
            "with regard to the registration of an agreement made by the company "
            "Edmond Isbecque, Brussels, on 30 January 1963 pursuant to Article 5 "
            "paragraph (1) of Regulation No. 17 (2), which it concluded on "
            "28 February 1962 with the company Ludwig Hummel, "
            "Heitersheim/Baden (Germany), and which was amended on 7 April 1965, "
            "after hearing of the Advisory Committee on Cartel and Monopoly Issues."
        )

        ht = HeidelTime()
        result = ht.predict(_CELEX, _CHUNK, [celex_text])

        self.assertEqual(len(result), 1)
        inner = result[0]
        self.assertIsInstance(inner, list)
        # mock returns [] → inner list must be empty
        self.assertEqual(inner, [])


# ---------------------------------------------------------------------------
# Unit tests — HeidelTimeVO value object
# ---------------------------------------------------------------------------


class TestHeidelTimeVO(unittest.TestCase):
    """Unit tests for HeidelTimeVO.from_dict and immutability."""

    _RAW = {
        "celex_id": _CELEX,
        "fk_chunk_id": _CHUNK,
        "text": "30 June 2013",
        "tid": "t3",
        "type": "DATE",
        "value": "2013-06-30",
        "span": [117, 129],
    }

    def _vo(self) -> HeidelTimeVO:
        return HeidelTimeVO.from_dict(self._RAW)

    def test_from_dict_span_decomposed(self):
        """span list is split into span_start / span_end."""
        vo = self._vo()
        self.assertEqual(vo.span_start, 117)
        self.assertEqual(vo.span_end, 129)

    def test_from_dict_fields(self):
        """All scalar fields are mapped verbatim."""
        vo = self._vo()
        self.assertEqual(vo.celex_id, _CELEX)
        self.assertEqual(vo.fk_chunk_id, _CHUNK)
        self.assertEqual(vo.text, "30 June 2013")
        self.assertEqual(vo.tid, "t3")
        self.assertEqual(vo.type, "DATE")
        self.assertEqual(vo.value, "2013-06-30")

    def test_raw_jsonb_preserved(self):
        """raw_jsonb contains the original dict (span as list, not tuple)."""
        vo = self._vo()
        self.assertEqual(vo.raw_jsonb["span"], [117, 129])
        self.assertEqual(vo.raw_jsonb["tid"], "t3")

    def test_immutable(self):
        """HeidelTimeVO is frozen — attribute mutation raises ValidationError."""
        from pydantic import ValidationError

        vo = self._vo()
        with self.assertRaises((ValidationError, TypeError)):
            vo.span_start = 0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Integration tests — real py_heideltime binary required
# ---------------------------------------------------------------------------


class TestHeidelTimeIntegration(unittest.TestCase):
    """Integration tests that invoke the actual HeidelTime Java binary.

    Skip this class in CI unless TreeTagger + Java are available.
    """

    def setUp(self):
        self.ht = HeidelTime(language="english", document_type="news")

    def test_integration_fapl_decision_finds_dates(self):
        """The FAPL decision text (from play_heideltime.py) should contain at least
        one recognised temporal expression: '30 June 2013'."""
        text = (
            "This decision shall apply from the date on which it is notified to "
            "Football Association Premier League Limited until 30 June 2013.\n"
            "Brussels, 2 2 III 2006"
        )
        result = self.ht.predict(_CELEX, _CHUNK, [text], dcts=["2006-03-22"])

        self.assertEqual(len(result), 1, "Expected exactly one result list")
        vos = result[0]
        self.assertGreater(len(vos), 0, "Expected at least one temporal expression")
        for vo in vos:
            self.assertIsInstance(vo, HeidelTimeVO)
            self.assertIsInstance(vo.span_start, int)
            self.assertIsInstance(vo.span_end, int)
            self.assertLess(vo.span_start, vo.span_end, "Start must be before end")
            self.assertEqual(vo.celex_id, _CELEX)

    def test_integration_no_dates_text(self):
        """Pure prose with no temporal expression should return an empty inner list."""
        text = "The parties agreed to the terms as set out in the annex."
        result = self.ht.predict(_CELEX, _CHUNK, [text])
        self.assertEqual(result, [[]])

    def test_integration_multiple_texts_independent(self):
        """Results for two texts must be independent lists."""
        texts = [
            "This applies until 30 June 2013.",
            "No date here.",
        ]
        result = self.ht.predict(_CELEX, _CHUNK, texts, dcts=["2006-03-22", None])

        self.assertEqual(len(result), 2)
        self.assertGreater(len(result[0]), 0)
        for vo in result[0]:
            self.assertIsInstance(vo, HeidelTimeVO)
        self.assertEqual(result[1], [])

    def test_integration_celex_D0426_finds_three_dates(self):
        """The CELEX-31965D0426 text contains three obvious dates that HeidelTime
        should recognise: 30 January 1963, 28 February 1962, and 7 April 1965."""
        celex_text = (
            "CELEX-31965D0426 "
            "with regard to the registration of an agreement made by the company "
            "Edmond Isbecque, Brussels, on 30 January 1963 pursuant to Article 5 "
            "paragraph (1) of Regulation No. 17 (2), which it concluded on "
            "28 February 1962 with the company Ludwig Hummel, "
            "Heitersheim/Baden (Germany), and which was amended on 7 April 1965, "
            "after hearing of the Advisory Committee on Cartel and Monopoly Issues."
        )
        result = self.ht.predict(_CELEX, _CHUNK, [celex_text])

        self.assertEqual(len(result), 1)
        inner = result[0]
        self.assertIsInstance(inner, list)
        self.assertGreaterEqual(
            len(inner),
            3,
            f"Expected ≥3 temporal expressions (30 Jan 1963, 28 Feb 1962, 7 Apr 1965), "
            f"got {len(inner)}: {[v.text for v in inner]}",
        )
        for vo in inner:
            self.assertIsInstance(vo, HeidelTimeVO)
            self.assertIsInstance(vo.span_start, int)
            self.assertIsInstance(vo.span_end, int)
            self.assertLess(vo.span_start, vo.span_end)
            self.assertEqual(vo.celex_id, _CELEX)
            self.assertEqual(vo.fk_chunk_id, _CHUNK)
        extracted_texts = {vo.text for vo in inner}
        self.assertTrue(
            any("1963" in t for t in extracted_texts),
            f"Expected a 1963 date among extracted texts: {extracted_texts}",
        )
        self.assertTrue(
            any("1962" in t for t in extracted_texts),
            f"Expected a 1962 date among extracted texts: {extracted_texts}",
        )
        self.assertTrue(
            any("1965" in t for t in extracted_texts),
            f"Expected a 1965 date among extracted texts: {extracted_texts}",
        )
        
    @unittest.expectedFailure
    def test_integration_celex_D0426_bad_formated_dates(self):
        """The CELEX-31965D0426 text contains three obvious dates that HeidelTime
        should recognise: 30 January 1963, 28 February 1962, and 7 April 1965.

        in malformated 28February1962 date strings
        """
        celex_text = """ CELEX-31965D0426 
                # THE COMMISSION OF THE EUROPEAN ECONOMIC COMMUNITY
                based on the Treaty establishing the European Economic Community, in particular Article85,
                based on Regulation No.17 of 6February1962 (1), in particular on Articles6,7and8,
                with regard to the registration of an agreement made by the company Edmond Isbecque, Brussels, on 30January1963 pursuant to Article5 paragraph(1) of Regulation No.17(2), which it concluded on 28February1962 with the company Ludwig Hummel, Heitersheim/Baden (Germany), ‘and which was amended on 7April1965,
                after hearing of the Advisory Committee on Cartel and Monopoly Issues pursuant to Article10 of Regulation No.I7 and
                in consideration of the following reasons:
                I
                Der Vertrag enthält hauptsachlich folgende Bestimmungen :  ->  The contract primarily contains the following provisions :
                The company Ludwig Hummel (Hummel), which manufactures soil‑cultivation equipment and tractors—namely single‑axle tractors, four‑wheel tractors and all‑wheel tractors with accessories—transfers to the company Edmond Isbecque (Isbecque) the exclusive distribution of these products for Belgium; it commits not to sell to other persons or companies in this territory. The agreement, after the aforementioned amendment, no longer applies to the Grand Duchy of Luxembourg.
                Isbecque, who buys and sells on his own account, sets his selling prices at his own discretion. He is not prohibited from exporting to countries outside Belgium.
                The facts essential for the appraisal of the case can, incidentally, be summarized as follows:
                Isbecque assumes responsibility for providing demonstration and customer service $\cdot$ and for establishing a stock of spare parts, activities that are of essential importance for the relatively complicated technical and expensive products of the present type.
                The accessories manufactured by Hummel are not all adapted to the soil conditions and working conditions in Belgium; therefore Isbecque shall have accessories made that correspond to these conditions and circumstances, in particular harrows, cultivators, plows, etc.
                Isbecque has not attempted, invoking the exclusive distribution agreement, to prevent other dealers from importing the contract goods into Belgium.
        """
        result = self.ht.predict(_CELEX, _CHUNK, [celex_text])

        self.assertEqual(len(result), 1)
        inner = result[0]
        self.assertIsInstance(inner, list)
        self.assertGreaterEqual(
            len(inner),
            3,
            f"Expected ≥3 temporal expressions (30 Jan 1963, 28 Feb 1962, 7 Apr 1965), "
            f"got {len(inner)}: {[v.text for v in inner]}",
        )
        for vo in inner:
            self.assertIsInstance(vo, HeidelTimeVO)
            self.assertIsInstance(vo.span_start, int)
            self.assertIsInstance(vo.span_end, int)
            self.assertLess(vo.span_start, vo.span_end)
            self.assertEqual(vo.celex_id, _CELEX)
            self.assertEqual(vo.fk_chunk_id, _CHUNK)
        extracted_texts = {vo.text for vo in inner}
        self.assertTrue(
            any("1963" in t for t in extracted_texts),
            f"Expected a 1963 date among extracted texts: {extracted_texts}",
        )
        self.assertTrue(
            any("1962" in t for t in extracted_texts),
            f"Expected a 1962 date among extracted texts: {extracted_texts}",
        )
        self.assertTrue(
            any("1965" in t for t in extracted_texts),
            f"Expected a 1965 date among extracted texts: {extracted_texts}",
        )


# ---------------------------------------------------------------------------
# Unit tests — HeidelTimeModule.forward
# ---------------------------------------------------------------------------


class TestHeidelTimeModule(unittest.TestCase):
    """Unit tests for HeidelTimeModule.forward — no real DB or JAR required."""

    _CELEX = "CELEX-12345"
    _CHUNK_ID = "chunk-uuid-1"
    _CHUNK_TEXT = "On 30 June 2013 the infringement ended."

    def _make_params(self, force_override: bool = True) -> FactExtractorParamVO:
        return FactExtractorParamVO(
            target_fact_name="heideltime_raw",
            question="Extract temporal expressions",
            celex_id=self._CELEX,
            output_schema="",
            force_override=force_override,
        )

    def _make_vo(self) -> HeidelTimeVO:
        return HeidelTimeVO.from_dict(
            {
                "celex_id": self._CELEX,
                "fk_chunk_id": self._CHUNK_ID,
                "text": "30 June 2013",
                "tid": "t1",
                "type": "DATE",
                "value": "2013-06-30",
                "span": [3, 15],
            }
        )

    def _setup_ingestor_mock(
        self,
        chunk_rows: list | None = None,
        fresh: set | None = None,
        proxy_payload: list | None = None,
        extraction_sources: list | None = None,
    ) -> tuple[MagicMock, MagicMock]:
        """Return (mock_ingestor, mock_session) with session wired as context manager.

        HeidelTimeIngestor.Session is an instance attribute (set in __init__), so
        spec= would hide it.  A plain MagicMock() is used instead.
        """
        mock_ingestor = MagicMock()

        mock_session = MagicMock()
        mock_ingestor.Session.return_value.__enter__ = MagicMock(
            return_value=mock_session
        )
        mock_ingestor.Session.return_value.__exit__ = MagicMock(return_value=False)
        mock_session.execute.return_value.fetchall.return_value = chunk_rows or []

        mock_ingestor.fresh_chunk_ids.return_value = (
            fresh if fresh is not None else set()
        )
        mock_ingestor.delete_chunks.return_value = 0
        mock_ingestor.build_proxy_fact_payload.return_value = proxy_payload or []
        mock_ingestor.select_extraction_sources.return_value = extraction_sources or []
        return mock_ingestor, mock_session

    def test_forward_fast_return_when_proxy_payload_exists(self):
        """When force_override=False and raw rows exist, predict is never called."""
        proxy_items = [
            {
                "status": "available",
                "source_table": "celex_facts.heideltime_extractions",
                "extraction_count": 1,
                "chunk_count": 1,
                "canonical_date_count": 1,
                "min_date": "2013-06-30",
                "max_date": "2013-06-30",
            }
        ]
        mock_chunk = MagicMock()
        mock_chunk.id = self._CHUNK_ID
        mock_chunk.document = self._CHUNK_TEXT

        mock_sql_store = MagicMock()
        mock_ht = MagicMock(spec=HeidelTime)
        mock_ingestor, _ = self._setup_ingestor_mock(
            chunk_rows=[mock_chunk],
            fresh={self._CHUNK_ID},
            proxy_payload=proxy_items,
        )

        module = HeidelTimeModule(
            sql_store=mock_sql_store, ht=mock_ht, ingestor=mock_ingestor
        )
        result = module.forward(self._make_params(force_override=False))

        mock_ht.predict.assert_not_called()
        mock_ingestor.delete_chunks.assert_not_called()
        self.assertEqual(result.output.source, "heideltime_extractions")
        self.assertEqual(result.output.items, proxy_items)

    def test_forward_no_chunks_returns_empty_fact_output(self):
        """When no chunks exist for the CELEX document, items are empty and source is 'none'."""
        mock_sql_store = MagicMock()
        mock_ht = MagicMock(spec=HeidelTime)
        mock_ingestor, _ = self._setup_ingestor_mock(chunk_rows=[])

        module = HeidelTimeModule(
            sql_store=mock_sql_store, ht=mock_ht, ingestor=mock_ingestor
        )
        result = module.forward(self._make_params(force_override=True))

        mock_ht.predict.assert_not_called()
        self.assertEqual(result.output.source, "none")
        self.assertEqual(result.output.items, [])

    def test_forward_processes_new_chunks_and_returns_proxy_payload(self):
        """Happy path: one new chunk is processed, ingested, and summarized as proxy metadata."""
        mock_chunk = MagicMock()
        mock_chunk.id = self._CHUNK_ID
        mock_chunk.document = self._CHUNK_TEXT

        mock_sql_store = MagicMock()
        mock_ht = MagicMock(spec=HeidelTime)
        vo = self._make_vo()
        mock_ht.predict.return_value = [[vo]]
        proxy_items = [
            {
                "status": "available",
                "source_table": "celex_facts.heideltime_extractions",
                "extraction_count": 1,
                "chunk_count": 1,
                "canonical_date_count": 1,
                "min_date": "2013-06-30",
                "max_date": "2013-06-30",
            }
        ]

        mock_ingestor, _ = self._setup_ingestor_mock(
            chunk_rows=[mock_chunk], fresh=set(), proxy_payload=proxy_items
        )

        module = HeidelTimeModule(
            sql_store=mock_sql_store, ht=mock_ht, ingestor=mock_ingestor
        )
        result = module.forward(self._make_params(force_override=True))

        mock_ht.predict.assert_called_once_with(
            celex_id=self._CELEX,
            fk_chunk_id=self._CHUNK_ID,
            texts=[self._CHUNK_TEXT],
        )
        mock_ingestor.ingest_vos.assert_called_once_with(
            [vo], source_hash=_chunk_hash(self._CHUNK_TEXT)
        )
        self.assertEqual(result.output.source, "heideltime_extractions")
        self.assertEqual(len(result.output.items), 1)
        self.assertEqual(result.output.items, proxy_items)

    def test_forward_skips_fresh_chunks_and_still_returns_proxy(self):
        """A chunk whose hash still matches is not re-extracted."""
        mock_chunk = MagicMock()
        mock_chunk.id = self._CHUNK_ID
        mock_chunk.document = self._CHUNK_TEXT

        mock_sql_store = MagicMock()
        mock_ht = MagicMock(spec=HeidelTime)
        proxy_items = [
            {
                "status": "available",
                "source_table": "celex_facts.heideltime_extractions",
                "extraction_count": 1,
                "chunk_count": 1,
                "canonical_date_count": 1,
                "min_date": "2013-06-30",
                "max_date": "2013-06-30",
            }
        ]
        mock_ingestor, _ = self._setup_ingestor_mock(
            chunk_rows=[mock_chunk],
            fresh={self._CHUNK_ID},
            proxy_payload=proxy_items,
        )

        module = HeidelTimeModule(
            sql_store=mock_sql_store, ht=mock_ht, ingestor=mock_ingestor
        )
        result = module.forward(self._make_params(force_override=False))

        mock_ht.predict.assert_not_called()
        mock_ingestor.ingest_vos.assert_not_called()
        mock_ingestor.delete_chunks.assert_not_called()
        self.assertEqual(result.output.items, proxy_items)
        self.assertEqual(result.output.source, "heideltime_extractions")

    def test_forward_reextracts_a_stale_chunk_and_deletes_its_rows(self):
        """A chunk missing from the fresh set is deleted, then re-run by the JAR."""
        mock_chunk = MagicMock()
        mock_chunk.id = self._CHUNK_ID
        mock_chunk.document = self._CHUNK_TEXT

        mock_ht = MagicMock(spec=HeidelTime)
        vo = self._make_vo()
        mock_ht.predict.return_value = [[vo]]

        mock_ingestor, _ = self._setup_ingestor_mock(
            chunk_rows=[mock_chunk],
            fresh=set(),
            proxy_payload=[{"status": "available"}],
        )

        module = HeidelTimeModule(
            sql_store=MagicMock(), ht=mock_ht, ingestor=mock_ingestor
        )
        module.forward(self._make_params(force_override=False))

        mock_ingestor.delete_chunks.assert_called_once_with({self._CHUNK_ID})
        mock_ht.predict.assert_called_once()
        mock_ingestor.ingest_vos.assert_called_once_with(
            [vo], source_hash=_chunk_hash(self._CHUNK_TEXT)
        )

    def test_forward_deletes_before_it_reextracts(self):
        """Order matters: ON CONFLICT DO NOTHING would keep the old row otherwise."""
        mock_chunk = MagicMock()
        mock_chunk.id = self._CHUNK_ID
        mock_chunk.document = self._CHUNK_TEXT

        mock_ht = MagicMock(spec=HeidelTime)
        mock_ht.predict.return_value = [[self._make_vo()]]

        mock_ingestor, _ = self._setup_ingestor_mock(
            chunk_rows=[mock_chunk], fresh=set(), proxy_payload=[{"s": 1}]
        )
        order: list[str] = []
        mock_ingestor.delete_chunks.side_effect = lambda ids: order.append("delete") or 0
        mock_ingestor.ingest_vos.side_effect = lambda *a, **kw: order.append("ingest")

        module = HeidelTimeModule(
            sql_store=MagicMock(), ht=mock_ht, ingestor=mock_ingestor
        )
        module.forward(self._make_params(force_override=False))

        self.assertEqual(order, ["delete", "ingest"])

    def test_forward_override_marks_every_chunk_stale(self):
        """--overwrite re-runs a chunk whose hash still matches (the old TODO)."""
        mock_chunk = MagicMock()
        mock_chunk.id = self._CHUNK_ID
        mock_chunk.document = self._CHUNK_TEXT

        mock_ht = MagicMock(spec=HeidelTime)
        mock_ht.predict.return_value = [[self._make_vo()]]

        mock_ingestor, _ = self._setup_ingestor_mock(
            chunk_rows=[mock_chunk],
            fresh={self._CHUNK_ID},
            proxy_payload=[{"status": "available"}],
        )

        module = HeidelTimeModule(
            sql_store=MagicMock(), ht=mock_ht, ingestor=mock_ingestor
        )
        module.forward(self._make_params(force_override=True))

        # The freshness probe is not even consulted; everything is stale.
        mock_ingestor.fresh_chunk_ids.assert_not_called()
        mock_ingestor.delete_chunks.assert_called_once_with({self._CHUNK_ID})
        mock_ht.predict.assert_called_once()

    def test_forward_reextracts_only_the_stale_chunk_of_a_document(self):
        """A partially stale document re-runs the stale chunk and leaves the rest."""
        fresh_chunk = MagicMock()
        fresh_chunk.id = "chunk-fresh"
        fresh_chunk.document = "Nothing temporal here."
        stale_chunk = MagicMock()
        stale_chunk.id = "chunk-stale"
        stale_chunk.document = self._CHUNK_TEXT

        mock_ht = MagicMock(spec=HeidelTime)
        mock_ht.predict.return_value = [[self._make_vo()]]

        mock_ingestor, _ = self._setup_ingestor_mock(
            chunk_rows=[fresh_chunk, stale_chunk],
            fresh={"chunk-fresh"},
            proxy_payload=[{"status": "available"}],
        )

        module = HeidelTimeModule(
            sql_store=MagicMock(), ht=mock_ht, ingestor=mock_ingestor
        )
        module.forward(self._make_params(force_override=False))

        mock_ingestor.delete_chunks.assert_called_once_with({"chunk-stale"})
        self.assertEqual(
            mock_ht.predict.call_args.kwargs["fk_chunk_id"], "chunk-stale"
        )

    def test_forward_hash_matches_postgres_md5(self):
        """The stored digest must equal what md5(document) computes server-side."""
        import hashlib

        text_with_accents = "Décision du 7 avril 1965 — Grundig/Consten"
        self.assertEqual(
            _chunk_hash(text_with_accents),
            hashlib.md5(text_with_accents.encode("utf-8")).hexdigest(),
        )

    _RAW_EXTRACTION_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
    _RAW_EXTRACTION_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
    _RAW_EXTRACTION_C = "cccccccc-3333-4333-8333-cccccccccccc"

    def _fresh_chunk_rows(self) -> list[MagicMock]:
        """The two chunks behind ``_extraction_source_rows``, as fetched rows.

        The fast-return now sits *after* the staleness probe, so a lineage test
        has to present chunks that exist and are fresh; an empty chunk list
        means "document not chunked" and returns early.
        """
        rows = []
        for chunk_id in ("chunk-1", "chunk-2"):
            row = MagicMock()
            row.id = chunk_id
            row.document = self._CHUNK_TEXT
            rows.append(row)
        return rows

    def _extraction_source_rows(self) -> list[dict]:
        """Three extractions across two chunks, as the repository returns them."""
        return [
            {
                "id": self._RAW_EXTRACTION_A,
                "fk_chunk_id": "chunk-1",
                "timex_text": "30 June 2013",
                "doc_pos": 0,
            },
            {
                "id": self._RAW_EXTRACTION_B,
                "fk_chunk_id": "chunk-1",
                "timex_text": "1 July 2013",
                "doc_pos": 0,
            },
            {
                "id": self._RAW_EXTRACTION_C,
                "fk_chunk_id": "chunk-2",
                "timex_text": "P3Y",
                "doc_pos": 4,
            },
        ]

    def test_raw_emits_one_source_per_extraction_and_chunk(self):
        """Every extraction row is cited, plus one chunk source per distinct chunk."""
        mock_chunk = MagicMock()
        mock_chunk.id = self._CHUNK_ID
        mock_chunk.document = self._CHUNK_TEXT
        mock_ingestor, _ = self._setup_ingestor_mock(
            chunk_rows=[mock_chunk],
            fresh={self._CHUNK_ID},
            proxy_payload=[{"status": "available"}],
            extraction_sources=self._extraction_source_rows(),
        )

        module = HeidelTimeModule(
            sql_store=MagicMock(), ht=MagicMock(spec=HeidelTime), ingestor=mock_ingestor
        )
        result = module.forward(self._make_params(force_override=False))

        sources = result.output.lineage_sources
        self.assertIsNotNone(sources)
        heideltime = [s for s in sources if s.source_type == "heideltime"]
        chunks = [s for s in sources if s.source_type == "chunk"]

        self.assertEqual(
            {str(s.heideltime_event_id) for s in heideltime},
            {self._RAW_EXTRACTION_A, self._RAW_EXTRACTION_B, self._RAW_EXTRACTION_C},
        )
        self.assertEqual({s.chunk_id for s in chunks}, {"chunk-1", "chunk-2"})
        self.assertEqual(heideltime[0].description, "30 June 2013")
        self.assertEqual(heideltime[0].dspy_module, "HeidelTimeModule")

    def test_raw_carries_doc_pos_as_chunk_seq(self):
        """The chunk source records the chunk's document position for display."""
        mock_ingestor, _ = self._setup_ingestor_mock(
            chunk_rows=self._fresh_chunk_rows(),
            fresh={"chunk-1", "chunk-2"},
            proxy_payload=[{"status": "available"}],
            extraction_sources=self._extraction_source_rows(),
        )

        module = HeidelTimeModule(
            sql_store=MagicMock(), ht=MagicMock(spec=HeidelTime), ingestor=mock_ingestor
        )
        result = module.forward(self._make_params(force_override=False))

        by_chunk = {
            s.chunk_id: s
            for s in result.output.lineage_sources
            if s.source_type == "chunk"
        }
        self.assertEqual(by_chunk["chunk-1"].chunk_seq, [0])
        self.assertEqual(by_chunk["chunk-2"].chunk_seq, [4])

    def test_raw_cites_extraction_whose_chunk_left_the_embedding_table(self):
        """A null doc_pos still yields a chunk source, just without a position."""
        rows = self._extraction_source_rows()
        rows[2]["doc_pos"] = None
        mock_ingestor, _ = self._setup_ingestor_mock(
            chunk_rows=self._fresh_chunk_rows(),
            fresh={"chunk-1", "chunk-2"},
            proxy_payload=[{"status": "available"}],
            extraction_sources=rows,
        )

        module = HeidelTimeModule(
            sql_store=MagicMock(), ht=MagicMock(spec=HeidelTime), ingestor=mock_ingestor
        )
        result = module.forward(self._make_params(force_override=False))

        orphan = next(
            s
            for s in result.output.lineage_sources
            if s.source_type == "chunk" and s.chunk_id == "chunk-2"
        )
        self.assertIsNone(orphan.chunk_seq)

    def test_raw_fast_return_rebuilds_lineage_without_running_the_jar(self):
        """Re-running heals provenance from persisted rows, with no JAR call."""
        mock_ht = MagicMock(spec=HeidelTime)
        mock_ingestor, _ = self._setup_ingestor_mock(
            chunk_rows=self._fresh_chunk_rows(),
            fresh={"chunk-1", "chunk-2"},
            proxy_payload=[{"status": "available"}],
            extraction_sources=self._extraction_source_rows(),
        )

        module = HeidelTimeModule(
            sql_store=MagicMock(), ht=mock_ht, ingestor=mock_ingestor
        )
        result = module.forward(self._make_params(force_override=False))

        mock_ht.predict.assert_not_called()
        self.assertEqual(result.output.source, "heideltime_extractions")
        self.assertEqual(len(result.output.lineage_sources), 5)

    def test_raw_emits_no_lineage_without_extractions(self):
        """A document the JAR found nothing in carries no lineage."""
        mock_ingestor, _ = self._setup_ingestor_mock(
            chunk_rows=[], proxy_payload=[], extraction_sources=[]
        )

        module = HeidelTimeModule(
            sql_store=MagicMock(), ht=MagicMock(spec=HeidelTime), ingestor=mock_ingestor
        )
        result = module.forward(self._make_params(force_override=True))

        self.assertEqual(result.output.source, "none")
        self.assertIsNone(result.output.lineage_sources)


# ---------------------------------------------------------------------------
# Unit tests — HeidelTimeEventModule.forward
# ---------------------------------------------------------------------------


class TestHeidelTimeEventModule(unittest.TestCase):
    """Unit tests for HeidelTimeEventModule.forward — no real DB or LLM required."""

    _CELEX = "CELEX-99999"
    _CHUNK_ID = "chunk-uuid-event-1"
    _CHUNK_TEXT = "On 8 June 2005 an email was sent. On 4 July 2005 a meeting was held."
    _EXTRACTION_ID_A = "11111111-1111-4111-8111-111111111111"
    _EXTRACTION_ID_B = "22222222-2222-4222-8222-222222222222"

    def _make_params(self, force_override: bool = True) -> FactExtractorParamVO:
        return FactExtractorParamVO(
            target_fact_name="heideltime_event",
            question="Extract structured events for each HeidelTime date",
            celex_id=self._CELEX,
            output_schema="event_date, actor, predicate_object",
            force_override=force_override,
        )

    def _make_event_result(
        self, events: list[tuple[str, str, str]]
    ) -> HeidelTimeEventResult:
        """Build a HeidelTimeEventResult from (event_date, actor, predicate_object) tuples."""
        return HeidelTimeEventResult(
            events=[
                HeidelTimeEvent(event_date=d, actor=a, predicate_object=p)
                for d, a, p in events
            ]
        )

    def test_fast_return_when_fact_exists(self):
        """When force_override=False and fact is cached, LLM extract is never called."""
        cached_items = [
            {
                "event_date": "2005-06-08",
                "actor": "redacted",
                "predicate_object": "sent email",
            }
        ]
        existing_fact = MagicMock()
        existing_fact.value_json = cached_items

        mock_sql_store = MagicMock()
        mock_sql_store.select_fact4celex.return_value = [existing_fact]
        mock_retriever = MagicMock()

        with patch("pipelines.heideltime.heideltime_module.dspy.ChainOfThought"):
            module = HeidelTimeEventModule(
                sql_store=mock_sql_store, retriever=mock_retriever
            )
            result = module.forward(self._make_params(force_override=False))

        mock_retriever.select_chunks_with_dates.assert_not_called()
        self.assertEqual(result.output.source, "sql")
        self.assertEqual(result.output.items, cached_items)

    def test_returns_empty_when_no_chunks(self):
        """When the retriever returns no chunks, output is empty with source='none'."""
        mock_sql_store = MagicMock()
        mock_sql_store.select_fact4celex.return_value = []
        mock_retriever = MagicMock()
        mock_retriever.select_chunks_with_dates.return_value = []

        with patch("pipelines.heideltime.heideltime_module.dspy.ChainOfThought"):
            module = HeidelTimeEventModule(
                sql_store=mock_sql_store, retriever=mock_retriever
            )
            result = module.forward(self._make_params(force_override=True))

        self.assertEqual(result.output.source, "none")
        self.assertEqual(result.output.items, [])

    def test_forward_extracts_and_formats_events(self):
        """Happy path: one chunk yields two events with correct keys and source."""
        mock_sql_store = MagicMock()
        mock_sql_store.select_fact4celex.return_value = []
        mock_retriever = MagicMock()
        mock_retriever.select_chunks_with_dates.return_value = [
            {
                "fk_chunk_id": self._CHUNK_ID,
                "chunk_text": self._CHUNK_TEXT,
                "canonical_dates": ["2005-06-08", "2005-07-04"],
                "date_sources": {
                    "2005-06-08": [
                        {"id": self._EXTRACTION_ID_A, "timex_text": "8 June 2005"}
                    ],
                    "2005-07-04": [
                        {"id": self._EXTRACTION_ID_B, "timex_text": "4 July 2005"}
                    ],
                },
            }
        ]

        mock_pred = MagicMock()
        mock_pred.output = self._make_event_result(
            [
                ("2005-06-08", "redacted", "sent email collecting price information"),
                (
                    "2005-07-04",
                    "Representatives from parties",
                    "held Internationaler Wettbewerberkreis meeting",
                ),
            ]
        )

        with patch(
            "pipelines.heideltime.heideltime_module.dspy.ChainOfThought"
        ) as mock_cot:
            mock_cot.return_value.return_value = mock_pred
            module = HeidelTimeEventModule(
                sql_store=mock_sql_store, retriever=mock_retriever
            )
            result = module.forward(self._make_params(force_override=True))

        self.assertEqual(result.output.source, "heideltime_event")
        self.assertEqual(len(result.output.items), 2)
        first = result.output.items[0]
        self.assertIn("event_date", first)
        self.assertIn("actor", first)
        self.assertIn("predicate_object", first)
        self.assertEqual(first["event_date"], "2005-06-08")
        self.assertEqual(first["actor"], "redacted")

    def test_forward_deduplicates_across_chunks(self):
        """The same (event_date, predicate_object) from two chunks is collapsed to one entry."""
        mock_sql_store = MagicMock()
        mock_sql_store.select_fact4celex.return_value = []
        shared_event = (
            "2005-06-08",
            "redacted",
            "sent email collecting price information",
        )
        mock_retriever = MagicMock()
        mock_retriever.select_chunks_with_dates.return_value = [
            {
                "fk_chunk_id": "chunk-1",
                "chunk_text": "text A",
                "canonical_dates": ["2005-06-08"],
                "date_sources": {
                    "2005-06-08": [
                        {"id": self._EXTRACTION_ID_A, "timex_text": "8 June 2005"}
                    ]
                },
            },
            {
                "fk_chunk_id": "chunk-2",
                "chunk_text": "text B",
                "canonical_dates": ["2005-06-08"],
                "date_sources": {
                    "2005-06-08": [
                        {"id": self._EXTRACTION_ID_B, "timex_text": "8.6.2005"}
                    ]
                },
            },
        ]

        mock_pred = MagicMock()
        mock_pred.output = self._make_event_result([shared_event])

        with patch(
            "pipelines.heideltime.heideltime_module.dspy.ChainOfThought"
        ) as mock_cot:
            mock_cot.return_value.return_value = mock_pred
            module = HeidelTimeEventModule(
                sql_store=mock_sql_store, retriever=mock_retriever
            )
            result = module.forward(self._make_params(force_override=True))

        self.assertEqual(len(result.output.items), 1)
        self.assertEqual(result.output.items[0]["event_date"], "2005-06-08")

    def test_forward_sorts_by_event_date(self):
        """Events from different chunks are sorted ascending by event_date."""
        mock_sql_store = MagicMock()
        mock_sql_store.select_fact4celex.return_value = []
        mock_retriever = MagicMock()
        mock_retriever.select_chunks_with_dates.return_value = [
            {
                "fk_chunk_id": "chunk-1",
                "chunk_text": "later text",
                "canonical_dates": ["2006-01-01"],
                "date_sources": {
                    "2006-01-01": [
                        {"id": self._EXTRACTION_ID_A, "timex_text": "1 January 2006"}
                    ]
                },
            },
            {
                "fk_chunk_id": "chunk-2",
                "chunk_text": "earlier text",
                "canonical_dates": ["2005-06-08"],
                "date_sources": {
                    "2005-06-08": [
                        {"id": self._EXTRACTION_ID_B, "timex_text": "8 June 2005"}
                    ]
                },
            },
        ]

        def side_effect(chunk_text, canonical_dates):
            date = canonical_dates.strip()
            pred = MagicMock()
            pred.output = self._make_event_result([(date, "redacted", "some activity")])
            return pred

        with patch(
            "pipelines.heideltime.heideltime_module.dspy.ChainOfThought"
        ) as mock_cot:
            mock_cot.return_value.side_effect = side_effect
            module = HeidelTimeEventModule(
                sql_store=mock_sql_store, retriever=mock_retriever
            )
            result = module.forward(self._make_params(force_override=True))

        dates = [ev["event_date"] for ev in result.output.items]
        self.assertEqual(dates, sorted(dates))

    def test_forward_emits_heideltime_lineage_sources(self):
        """Each event date is credited to the extraction row that produced it."""
        mock_sql_store = MagicMock()
        mock_sql_store.select_fact4celex.return_value = []
        mock_retriever = MagicMock()
        mock_retriever.select_chunks_with_dates.return_value = [
            {
                "fk_chunk_id": self._CHUNK_ID,
                "chunk_text": self._CHUNK_TEXT,
                "canonical_dates": ["2005-06-08", "2005-07-04"],
                "date_sources": {
                    "2005-06-08": [
                        {"id": self._EXTRACTION_ID_A, "timex_text": "8 June 2005"}
                    ],
                    "2005-07-04": [
                        {"id": self._EXTRACTION_ID_B, "timex_text": "4 July 2005"}
                    ],
                },
            }
        ]

        mock_pred = MagicMock()
        mock_pred.output = self._make_event_result(
            [("2005-06-08", "redacted", "sent email")]
        )

        with patch(
            "pipelines.heideltime.heideltime_module.dspy.ChainOfThought"
        ) as mock_cot:
            mock_cot.return_value.return_value = mock_pred
            module = HeidelTimeEventModule(
                sql_store=mock_sql_store, retriever=mock_retriever
            )
            result = module.forward(self._make_params(force_override=True))

        sources = result.output.lineage_sources
        self.assertIsNotNone(sources)
        self.assertEqual(len(sources), 1, "Only the used date must be credited")
        source = sources[0]
        self.assertEqual(source.source_type, "heideltime")
        self.assertEqual(source.celex_id, self._CELEX)
        self.assertEqual(str(source.heideltime_event_id), self._EXTRACTION_ID_A)
        self.assertEqual(source.description, "8 June 2005")
        self.assertEqual(source.extraction_method, "heideltime_event")
        self.assertEqual(source.dspy_module, "HeidelTimeEventModule")

    def test_lineage_collects_every_chunk_supporting_the_same_date(self):
        """One date evidenced in two chunks yields both extraction rows as sources."""
        mock_sql_store = MagicMock()
        mock_sql_store.select_fact4celex.return_value = []
        mock_retriever = MagicMock()
        mock_retriever.select_chunks_with_dates.return_value = [
            {
                "fk_chunk_id": "chunk-1",
                "chunk_text": "text A",
                "canonical_dates": ["2005-06-08"],
                "date_sources": {
                    "2005-06-08": [
                        {"id": self._EXTRACTION_ID_A, "timex_text": "8 June 2005"}
                    ]
                },
            },
            {
                "fk_chunk_id": "chunk-2",
                "chunk_text": "text B",
                "canonical_dates": ["2005-06-08"],
                "date_sources": {
                    "2005-06-08": [
                        {"id": self._EXTRACTION_ID_B, "timex_text": "8.6.2005"}
                    ]
                },
            },
        ]

        mock_pred = MagicMock()
        mock_pred.output = self._make_event_result(
            [("2005-06-08", "redacted", "sent email")]
        )

        with patch(
            "pipelines.heideltime.heideltime_module.dspy.ChainOfThought"
        ) as mock_cot:
            mock_cot.return_value.return_value = mock_pred
            module = HeidelTimeEventModule(
                sql_store=mock_sql_store, retriever=mock_retriever
            )
            result = module.forward(self._make_params(force_override=True))

        self.assertEqual(len(result.output.items), 1, "Event is deduplicated")
        source_ids = {
            str(src.heideltime_event_id) for src in result.output.lineage_sources
        }
        self.assertEqual(
            source_ids, {self._EXTRACTION_ID_A, self._EXTRACTION_ID_B}
        )

    def test_lineage_is_none_when_retriever_omits_date_sources(self):
        """A retriever without date_sources still extracts events, with no lineage."""
        mock_sql_store = MagicMock()
        mock_sql_store.select_fact4celex.return_value = []
        mock_retriever = MagicMock()
        mock_retriever.select_chunks_with_dates.return_value = [
            {
                "fk_chunk_id": self._CHUNK_ID,
                "chunk_text": self._CHUNK_TEXT,
                "canonical_dates": ["2005-06-08"],
            }
        ]

        mock_pred = MagicMock()
        mock_pred.output = self._make_event_result(
            [("2005-06-08", "redacted", "sent email")]
        )

        with patch(
            "pipelines.heideltime.heideltime_module.dspy.ChainOfThought"
        ) as mock_cot:
            mock_cot.return_value.return_value = mock_pred
            module = HeidelTimeEventModule(
                sql_store=mock_sql_store, retriever=mock_retriever
            )
            result = module.forward(self._make_params(force_override=True))

        self.assertEqual(len(result.output.items), 1)
        self.assertIsNone(result.output.lineage_sources)


# ---------------------------------------------------------------------------
# Unit tests — HeidelTimeEvent_aggregateModule.forward
# ---------------------------------------------------------------------------


class TestHeidelTimeEventAggregateModule(unittest.TestCase):
    """Unit tests for critical-event aggregation — no real DB or LLM required."""

    _CELEX = "CELEX-99999"

    def _make_params(self, force_override: bool = True) -> FactExtractorParamVO:
        return FactExtractorParamVO(
            target_fact_name="heideltime_event_agg",
            question="Extract the 1-2 most critical HeidelTime events",
            celex_id=self._CELEX,
            output_schema="EVENT, ACTOR, PREDICATE OBJECT",
            force_override=force_override,
        )

    def test_fast_return_when_aggregate_fact_exists(self):
        cached_items = [
            {
                "EVENT": "2007-02-21",
                "ACTOR": "Commission",
                "PREDICATE OBJECT": "adopted decision imposing fines",
            }
        ]
        existing_fact = MagicMock()
        existing_fact.value_json = cached_items

        mock_sql_store = MagicMock()
        mock_sql_store.select_fact4celex.return_value = [existing_fact]

        with patch("pipelines.heideltime.heideltime_module.dspy.ChainOfThought"):
            module = HeidelTimeEventAggregateModule(sql_store=mock_sql_store)
            result = module.forward(self._make_params(force_override=False))

        self.assertEqual(result.output.source, "sql")
        self.assertEqual(result.output.items, cached_items)
        mock_sql_store.select_fact4celex.assert_called_once_with(
            self._CELEX, "heideltime_event_agg"
        )

    def test_returns_empty_when_source_event_fact_missing(self):
        mock_sql_store = MagicMock()
        mock_sql_store.select_fact4celex.return_value = []

        with patch("pipelines.heideltime.heideltime_module.dspy.ChainOfThought"):
            module = HeidelTimeEventAggregateModule(sql_store=mock_sql_store)
            result = module.forward(self._make_params(force_override=True))

        self.assertEqual(result.output.source, "none")
        self.assertEqual(result.output.items, [])
        mock_sql_store.select_fact4celex.assert_called_once_with(
            self._CELEX, "heideltime_event"
        )

    def test_aggregates_source_events_to_lowercase_markdown_columns(self):
        event_fact = MagicMock()
        event_fact.value_json = (
            '[{"event_date":"1998-01-01","actor":"Producers",'
            '"predicate_object":"created cartel arrangement"},'
            '{"event_date":"2007-02-21","actor":"Commission",'
            '"predicate_object":"adopted decision imposing fines"}]'
        )
        mock_sql_store = MagicMock()
        mock_sql_store.select_fact4celex.return_value = [event_fact]

        mock_pred = MagicMock()
        mock_pred.output = HeidelTimeCriticalEventResult(
            events=[
                HeidelTimeCriticalEvent(
                    event="2007-02-21",
                    actor="Commission",
                    predicate_object="adopted decision imposing fines",
                ),
                HeidelTimeCriticalEvent(
                    event="1998-01-01",
                    actor="Producers",
                    predicate_object="created cartel arrangement",
                ),
            ]
        )

        with patch(
            "pipelines.heideltime.heideltime_module.dspy.ChainOfThought"
        ) as mock_cot:
            mock_cot.return_value.return_value = mock_pred
            module = HeidelTimeEventAggregateModule(sql_store=mock_sql_store)
            result = module.forward(self._make_params(force_override=True))

        self.assertEqual(result.output.source, "heideltime_event_agg")
        self.assertEqual(len(result.output.items), 2)
        self.assertEqual(
            list(result.output.items[0].keys()),
            ["event", "actor", "predicate object"],
        )
        self.assertEqual(
            [item["event"] for item in result.output.items],
            ["1998-01-01", "2007-02-21"],
        )
        passed_source = mock_cot.return_value.call_args.kwargs[
            "heideltime_event_result"
        ]
        self.assertEqual(passed_source, event_fact.value_json)

    def test_event_sort_key_keeps_unknown_dates_last(self):
        items = [
            {"event": "unknown date", "actor": "A", "predicate object": "did x"},
            {"event": "1998-04-23", "actor": "B", "predicate object": "did y"},
            {"event": "1997-06-03", "actor": "C", "predicate object": "did z"},
        ]

        items.sort(key=HeidelTimeEventAggregateModule._event_sort_key)

        self.assertEqual(
            [item["event"] for item in items],
            ["1997-06-03", "1998-04-23", "unknown date"],
        )


# ---------------------------------------------------------------------------
# Integration tests — HeidelTimeEventModule DB retrieval
# ---------------------------------------------------------------------------


class TestHeidelTimeEventModuleIntegration(unittest.TestCase):
    """Integration tests for HeidelTimeIngestor.select_chunks_with_dates.

    Requires a live PostgreSQL connection with populated celex_facts.heideltime_extractions.
    The known chunk ``7b20cb00-346b-498c-a948-1f2a0d620432`` must be present.
    """

    _KNOWN_CHUNK_ID = "7b20cb00-346b-498c-a948-1f2a0d620432"
    _ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    @classmethod
    def setUpClass(cls) -> None:
        """Resolve the celex_id for the known chunk once before all tests."""
        cls.ingestor = HeidelTimeIngestor()
        with cls.ingestor.Session() as session:
            row = session.execute(
                sa_text(
                    "SELECT celex_id FROM celex_facts.heideltime_extractions"
                    " WHERE fk_chunk_id = :chunk_id LIMIT 1"
                ),
                {"chunk_id": cls._KNOWN_CHUNK_ID},
            ).fetchone()
        cls.celex_id: str | None = row.celex_id if row else None

    def setUp(self) -> None:
        if not self.celex_id:
            self.skipTest(
                f"No heideltime_extractions found for chunk {self._KNOWN_CHUNK_ID}. "
                "Run heideltime_raw ingestion first."
            )

    def test_known_chunk_is_returned(self) -> None:
        """select_chunks_with_dates includes the known fk_chunk_id in its results."""
        chunks = self.ingestor.select_chunks_with_dates(self.celex_id)
        chunk_ids = {c["fk_chunk_id"] for c in chunks}
        self.assertIn(
            self._KNOWN_CHUNK_ID,
            chunk_ids,
            f"Expected chunk {self._KNOWN_CHUNK_ID} in results for celex_id={self.celex_id}",
        )

    def test_canonical_dates_are_valid_iso(self) -> None:
        """All canonical_dates for the known chunk are valid YYYY-MM-DD strings."""
        chunks = self.ingestor.select_chunks_with_dates(self.celex_id)
        known = next(c for c in chunks if c["fk_chunk_id"] == self._KNOWN_CHUNK_ID)
        self.assertGreater(
            len(known["canonical_dates"]), 0, "Expected at least one date"
        )
        for date in known["canonical_dates"]:
            self.assertRegex(date, self._ISO_DATE_RE, f"Not an ISO date: {date!r}")

    def test_expected_dates_present(self) -> None:
        """The dates seen in the user's example query are all present for the known chunk."""
        expected = {
            "2005-06-08",
            "2005-07-04",
            "2005-07-05",
            "2005-07-06",
            "2006-01-01",
            "2006-01-02",
        }
        chunks = self.ingestor.select_chunks_with_dates(self.celex_id)
        known = next(c for c in chunks if c["fk_chunk_id"] == self._KNOWN_CHUNK_ID)
        actual = set(known["canonical_dates"])
        missing = expected - actual
        self.assertFalse(
            missing,
            f"Dates missing from chunk {self._KNOWN_CHUNK_ID}: {missing}. Got: {actual}",
        )

    def test_dates_are_deduplicated(self) -> None:
        """canonical_dates contains no duplicate values."""
        chunks = self.ingestor.select_chunks_with_dates(self.celex_id)
        known = next(c for c in chunks if c["fk_chunk_id"] == self._KNOWN_CHUNK_ID)
        dates = known["canonical_dates"]
        self.assertEqual(len(dates), len(set(dates)), f"Duplicates found: {dates}")

    def test_chunk_text_is_non_empty(self) -> None:
        """chunk_text for the known chunk is a non-empty string."""
        chunks = self.ingestor.select_chunks_with_dates(self.celex_id)
        known = next(c for c in chunks if c["fk_chunk_id"] == self._KNOWN_CHUNK_ID)
        self.assertIsInstance(known["chunk_text"], str)
        self.assertGreater(
            len(known["chunk_text"].strip()), 0, "chunk_text must not be blank"
        )


if __name__ == "__main__":
    unittest.main()
