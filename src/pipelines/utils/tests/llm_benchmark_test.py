"""
LLM benchmark: qwen3-coder:30b (baseline) vs qwen3.6:35b (no-think candidate).

Loads fact-extraction questions from FACT_EXTRACTOR.yaml, runs them against a
hardcoded EU cartel decision chunk on both OLLAMA models, and prints a
side-by-side accuracy / latency comparison table.

Integration test — requires live OLLAMA with both models pulled.
Expected runtime: 3-8 minutes.
"""

import logging
import time
import unittest

import dspy
import yaml
from pydantic import BaseModel, Field

from config.llm_thinking import THINKING_DISABLED, thinking_kwargs
from config.settings import settings

logger = logging.getLogger(__name__)

MODEL_BASELINE = "qwen3-coder:30b"
MODEL_CANDIDATE = "qwen3.6:35b"

# EU cartel decision excerpt — Vitamins cartel, COMP/E-1/37.512, 21 Nov 2001.
# Covers Article 81 EC Treaty, horizontal price-fixing, large fines, 1989–1999.
DOCUMENT_CHUNK = """\
EUROPEAN COMMISSION

Decision of 21 November 2001

relating to a proceeding under Article 81 of the EC Treaty and Article 53
of the EEA Agreement

(Case COMP/E-1/37.512 — Vitamins)

3. THE INFRINGEMENT

3.1 The cartel

(160) F. Hoffmann-La Roche Ltd and BASF AG were the principal architects and
driving forces behind the global vitamin cartel. The cartel covered bulk
vitamins A, E, B2 (riboflavin), B5 (pantothenate), C, D3, biotin (H),
folic acid (Bc), beta-carotene and carotenoids.

(161) The undertakings concerned participated in the cartel over different
periods. The overall cartel arrangements lasted from January 1989 until
February 1999 — a period of ten years.

(162) During the operation of the cartel the participating undertakings:
  - fixed prices and target prices for each vitamin product;
  - allocated annual sales quotas for each vitamin product;
  - agreed on a budget system to monitor and enforce the quota allocations;
  - maintained actual prices at or above the agreed levels;
  - exchanged detailed sales revenue and volume information;
  - attended a large number of clandestine cartel meetings.

(163) The Commission finds that the infringement is of a very serious nature.
No abuse of a dominant position by any single undertaking has been
established. All participants are undertakings engaged in the manufacture
and sale of vitamins at the same level of the production chain.

3.2 Fines

Article 3 — The following fines are hereby imposed on the undertakings
named in Article 1 for the infringements found therein:

(a) F. Hoffmann-La Roche Ltd, Basel, Switzerland:   EUR 462 000 000
(b) BASF AG, Ludwigshafen, Germany:                 EUR 236 845 000
(c) Aventis SA, Strasbourg, France:                  EUR   5 040 000
(d) Solvay Pharmaceuticals BV, Weesp, Netherlands:  EUR   9 100 000

Article 4 — The undertakings named in Article 1 shall immediately bring
to an end the infringements referred to in that Article, in so far as they
have not already done so. They shall refrain from repeating any act or
conduct described in Article 1.
"""

# Expected keyword sets per question — used for accuracy scoring.
# Keys match target_fact_name entries in FACT_EXTRACTOR.yaml.
_EXPECTED_KEYWORDS: dict[str, list[str]] = {
    "infringement_category_abuse_of_dominance": ["false"],
    "infringement_category_hor_vert_lic_joint": ["horizontal"],
    "infringement_subcategory_horizontal_restraints": ["price fixing", "market sharing"],
    "infringement_gravity": ["high"],
    "decision_remedies": ["fine", "cease"],
}

BENCHMARK_FACT_NAMES: list[str] = list(_EXPECTED_KEYWORDS.keys())


class BenchmarkResult(BaseModel):
    """Single model-question benchmark record."""

    model_id: str = Field(description="OLLAMA model identifier")
    fact_name: str = Field(description="FACT_EXTRACTOR target_fact_name")
    elapsed_s: float = Field(description="Wall-clock seconds for the LLM call")
    answer: str = Field(description="Raw model output")
    accuracy_score: float = Field(description="0.0-1.0 keyword-match accuracy against expected answers")


class FactExtractionSig(dspy.Signature):
    """Extract a specific fact or classification from an EU cartel decision document chunk."""

    document: str = dspy.InputField(
        desc="Excerpt from an EU Commission cartel decision document"
    )
    question: str = dspy.InputField(
        desc="Fact extraction or classification question from the FACT_EXTRACTOR config"
    )
    answer: str = dspy.OutputField(
        desc="Extracted fact or classification; reply True/False/NA for classification questions"
    )


def _score(answer: str, expected_keywords: list[str]) -> float:
    """
    Compute the fraction of expected keywords found in the answer (case-insensitive).

    Parameters
    ----------
    answer:
        The raw model output string.
    expected_keywords:
        Tokens or phrases that should appear in a correct answer.

    Returns
    -------
    float
        A value in [0.0, 1.0]; 1.0 when all keywords are present.
    """
    if not expected_keywords:
        return 1.0
    lowered = answer.lower()
    hits = sum(1 for kw in expected_keywords if kw.lower() in lowered)
    return hits / len(expected_keywords)


def _load_questions(fact_names: list[str]) -> dict[str, str]:
    """
    Load question text from FACT_EXTRACTOR.yaml for the given fact names.

    Parameters
    ----------
    fact_names:
        List of target_fact_name values to look up.

    Returns
    -------
    dict[str, str]
        Mapping of fact name to question text.

    Raises
    ------
    KeyError
        If any requested fact name is absent from the YAML config.
    """
    yaml_path = settings.FACT_EXTRACTOR_CONFIG_DIR
    with yaml_path.open("r", encoding="utf-8") as fh:
        entries = yaml.safe_load(fh)
    index: dict[str, str] = {
        e["target_fact_name"]: e["question"]
        for e in entries
        if "target_fact_name" in e
    }
    missing = [name for name in fact_names if name not in index]
    if missing:
        raise KeyError(f"Facts not found in FACT_EXTRACTOR.yaml: {missing}")
    return {name: index[name] for name in fact_names}


class LLMBenchmarkTest(unittest.TestCase):
    """Benchmark qwen3-coder:30b vs qwen3.6:35b (no-think) on EU cartel fact extraction."""

    lm_baseline: dspy.LM
    lm_candidate: dspy.LM
    questions: dict[str, str]

    @classmethod
    def setUpClass(cls) -> None:
        """Initialise both LMs, load YAML questions, and warm up OLLAMA."""
        # Both arms are pinned to no-think explicitly rather than through
        # settings.LLM_THINKING_MODE: the benchmark compares decoding speed and
        # extraction quality, so the environment must not flip one arm.
        baseline_model_name = f"openai/{MODEL_BASELINE}"
        candidate_model_name = f"openai/{MODEL_CANDIDATE}"
        cls.lm_baseline = dspy.LM(
            baseline_model_name,
            api_base=f"{settings.OLLAMA_HOST}/v1",
            api_key="ollama",
            cache=False,
            timeout=settings.OLLAMA_TIMEOUT,
            **thinking_kwargs(baseline_model_name, mode=THINKING_DISABLED),
        )
        cls.lm_candidate = dspy.LM(
            candidate_model_name,
            api_base=f"{settings.OLLAMA_HOST}/v1",
            api_key="ollama",
            cache=False,
            timeout=settings.OLLAMA_TIMEOUT,
            **thinking_kwargs(candidate_model_name, mode=THINKING_DISABLED),
        )
        cls.questions = _load_questions(BENCHMARK_FACT_NAMES)

        logger.info("Warming up %s ...", MODEL_BASELINE)
        cls._warmup(cls.lm_baseline)
        logger.info("Warming up %s ...", MODEL_CANDIDATE)
        cls._warmup(cls.lm_candidate)
        logger.info("Warmup complete — starting benchmark.")

    @staticmethod
    def _warmup(lm: dspy.LM) -> None:
        """
        Send a trivial request to pre-load the model into VRAM before timing begins.

        Parameters
        ----------
        lm:
            The DSPy LM instance to warm up.
        """
        dspy.configure(lm=lm)
        predictor = dspy.Predict(FactExtractionSig)
        predictor(document="test", question="say ok")

    def _run_one(self, lm: dspy.LM, model_id: str, fact_name: str) -> BenchmarkResult:
        """
        Run one fact-extraction question through the given LM and return a BenchmarkResult.

        Parameters
        ----------
        lm:
            Configured DSPy LM to use for this call.
        model_id:
            Human-readable model identifier (used in the result record).
        fact_name:
            FACT_EXTRACTOR target_fact_name; must be present in self.questions.

        Returns
        -------
        BenchmarkResult
            Timing, raw answer, and accuracy score for this question/model pair.
        """
        question = self.questions[fact_name]
        dspy.configure(lm=lm)
        predictor = dspy.Predict(FactExtractionSig)

        t0 = time.perf_counter()
        prediction = predictor(document=DOCUMENT_CHUNK, question=question)
        elapsed = time.perf_counter() - t0

        answer = (prediction.answer or "").strip()
        score = _score(answer, _EXPECTED_KEYWORDS[fact_name])

        return BenchmarkResult(
            model_id=model_id,
            fact_name=fact_name,
            elapsed_s=round(elapsed, 2),
            answer=answer,
            accuracy_score=round(score, 2),
        )

    def test_benchmark(self) -> None:
        """
        Run all benchmark questions through both models, print a comparison report,
        and assert minimum quality thresholds.

        The candidate model (qwen3.6:35b, no-think) must reach at least 80% of the
        baseline model's average accuracy score, acknowledging that disabling reasoning
        may trade some accuracy for speed.
        """
        baseline_results: list[BenchmarkResult] = []
        candidate_results: list[BenchmarkResult] = []

        for fact_name in BENCHMARK_FACT_NAMES:
            logger.info("Running %s on %s ...", fact_name, MODEL_BASELINE)
            baseline_results.append(
                self._run_one(self.lm_baseline, MODEL_BASELINE, fact_name)
            )

            logger.info("Running %s on %s ...", fact_name, MODEL_CANDIDATE)
            candidate_results.append(
                self._run_one(self.lm_candidate, MODEL_CANDIDATE, fact_name)
            )

        self._print_report(baseline_results, candidate_results)
        self._assert_quality(baseline_results, candidate_results)

    @staticmethod
    def _print_report(
        baseline: list[BenchmarkResult],
        candidate: list[BenchmarkResult],
    ) -> None:
        """
        Print a formatted side-by-side comparison table to stdout.

        Parameters
        ----------
        baseline:
            BenchmarkResult list for the baseline model.
        candidate:
            BenchmarkResult list for the candidate model.
        """
        ans_w = 26

        header = (
            f"{'fact_name':<42} "
            f"{'baseline answer':<{ans_w}} {'base_s':>6} {'acc':>4} | "
            f"{'candidate answer':<{ans_w}} {'cand_s':>6} {'acc':>4}  {'winner'}"
        )
        sep = "-" * len(header)

        print(f"\n{sep}")
        print(f"BENCHMARK  {MODEL_BASELINE}  vs  {MODEL_CANDIDATE} (no-think)")
        print(sep)
        print(header)
        print(sep)

        for b, c in zip(baseline, candidate, strict=True):
            b_snip = b.answer[:ans_w].replace("\n", " ")
            c_snip = c.answer[:ans_w].replace("\n", " ")

            if b.accuracy_score > c.accuracy_score:
                winner = MODEL_BASELINE.split(":")[0]
            elif c.accuracy_score > b.accuracy_score:
                winner = MODEL_CANDIDATE.split(":")[0]
            elif b.elapsed_s <= c.elapsed_s:
                winner = f"{MODEL_BASELINE.split(':')[0]}(speed)"
            else:
                winner = f"{MODEL_CANDIDATE.split(':')[0]}(speed)"

            print(
                f"{b.fact_name:<42} "
                f"{b_snip:<{ans_w}} {b.elapsed_s:>6.1f} {b.accuracy_score:>4.2f} | "
                f"{c_snip:<{ans_w}} {c.elapsed_s:>6.1f} {c.accuracy_score:>4.2f}  {winner}"
            )

        b_avg_acc = sum(r.accuracy_score for r in baseline) / len(baseline)
        c_avg_acc = sum(r.accuracy_score for r in candidate) / len(candidate)
        b_avg_s = sum(r.elapsed_s for r in baseline) / len(baseline)
        c_avg_s = sum(r.elapsed_s for r in candidate) / len(candidate)

        print(sep)
        print(
            f"{'AVERAGE':<42} "
            f"{'':<{ans_w}} {b_avg_s:>6.1f} {b_avg_acc:>4.2f} | "
            f"{'':<{ans_w}} {c_avg_s:>6.1f} {c_avg_acc:>4.2f}"
        )
        print(f"{sep}\n")

    def _assert_quality(
        self,
        baseline: list[BenchmarkResult],
        candidate: list[BenchmarkResult],
    ) -> None:
        """
        Assert minimum quality constraints on both model result sets.

        Both models must return non-empty answers with positive elapsed times.
        The candidate model must reach at least 80% of the baseline's average
        accuracy score (20% tolerance for the no-think speed tradeoff).

        Parameters
        ----------
        baseline:
            BenchmarkResult list for the baseline model.
        candidate:
            BenchmarkResult list for the candidate model.

        Raises
        ------
        AssertionError
            If any model returns an empty answer or the candidate accuracy
            falls below the 80% threshold.
        """
        for result in baseline + candidate:
            self.assertGreater(
                len(result.answer),
                0,
                f"{result.model_id} returned an empty answer for {result.fact_name}",
            )
            self.assertGreater(
                result.elapsed_s,
                0.0,
                f"{result.model_id} reported non-positive elapsed time for {result.fact_name}",
            )

        b_avg_acc = sum(r.accuracy_score for r in baseline) / len(baseline)
        c_avg_acc = sum(r.accuracy_score for r in candidate) / len(candidate)

        self.assertGreaterEqual(
            c_avg_acc,
            b_avg_acc * 0.80,
            f"Candidate {MODEL_CANDIDATE} avg accuracy {c_avg_acc:.2f} fell below "
            f"80% of baseline {MODEL_BASELINE} avg accuracy {b_avg_acc:.2f}. "
            "Consider whether no-think mode is appropriate for these extraction tasks.",
        )


if __name__ == "__main__":
    unittest.main()
