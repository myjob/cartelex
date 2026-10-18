import sys
import asyncio
import logging
from pathlib import Path
from graphlib import TopologicalSorter

import yaml
import dspy

from config.llm_factory import batch_profile, create_lm
from config.settings import settings
from batch.BatchJobRunner import BatchJobRunner

from pipelines.rag.pgvector.fact_repository import FactRepository
from pipelines.rag.pgvector.chunk_repository import ChunkRepository
from pipelines.rag.pgvector.lineage_repository import LineageRepository
from tools.celex_tools import CelexDatabaseToolkit
from tools.PipelineFactIngestor import FactIngestor
from extract.rag.ranker.ranker_factory import create_ranker
from extract.rag.hybrid_search import HybridSearch
from extract.rag.enumeration_search import EnumerationSearch
from extract.rag.hybrid_book_search import HybridBookSearchToolkit
from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from extract.rag.key_word_search import KeywordSearch
from extract.rag.regex_retriever import RegexRetriever
from pipelines.agent_dspy.universal_fact_extraction_pipeline import FactRagPipeline
from pipelines.agent_dspy.universal_modules import (
    UniversalFactModule,
    DocKeywordSearchModule,
    DocHybridSearchModule,
    KeywordRegexModule,
    InfringementConceptModule,
    BookKeywordModule,
    BookFactModule,
    BookFactChapterTitleModule,
    BookLandmarkModule,
    FactContextModule,
    FactDetailRetriever,
    FactComparisonModule,
    NaceModule,
    FactSummaryModule,
    CelexCaseIdModule,
    FactCaseTitleModule,
)
from pipelines.agent_dspy.enumeration_fact_module import EnumerationFactModule
from pipelines.agent_dspy.query_router import QueryRouterModule

from pipelines.heideltime.heideltime_module import (
    HeidelTimeEventModule,
    HeidelTimeEventAggregateModule,
    HeidelTimeModule,
    HeideltimeCartelPeriodModule,
)
from pipelines.heideltime.extract_heideltime import HeidelTime
from pipelines.rag.pgvector.book_chunk_repository import BookChunkRepository
from pipelines.rag.pgvector.heideltime_ingestor import HeidelTimeIngestor
from pipelines.nace.nace_tree import NaceTree
from pipelines.rag.pgvector.embeddings.fact_embedding_loader_qwen import (
    FactEmbeddingLoaderQwen,
)
from pipelines.rag.pgvector.session import Session

# Canonical mapping from a YAML ``pipeline`` name to the DSPy module class that
# executes it. Single source of truth for ``_build_agents`` (which instantiates
# these classes); the live ingestion path records ``cls.__name__`` as the
# ``dspy_module`` lineage provenance. Reference these classes here rather than
# hard-coding module names elsewhere.
PIPELINE_DSPY_MODULE_CLASSES: dict[str, type[dspy.Module]] = {
    "universal": UniversalFactModule,
    "enumeration": EnumerationFactModule,
    "router": QueryRouterModule,
    "keyword_search": DocKeywordSearchModule,
    "keyword_regex": KeywordRegexModule,
    "infringement_concept": InfringementConceptModule,
    "doc_search": DocHybridSearchModule,
    "hybrid_search": FactContextModule,
    "fact_context": FactContextModule,
    "detail_retriever": FactDetailRetriever,
    "fact_comparison": FactComparisonModule,
    "nace_class_code": NaceModule,
    "case_summary": FactSummaryModule,
    "book_keywords": BookKeywordModule,
    "book_universal": BookFactModule,
    "book_infringement_type": BookFactChapterTitleModule,
    "book_landmark": BookLandmarkModule,
    "celex_case_id": CelexCaseIdModule,
    "case_title": FactCaseTitleModule,
    "heideltime_raw": HeidelTimeModule,
    "heideltime_event": HeidelTimeEventModule,
    "heideltime_event_agg": HeidelTimeEventAggregateModule,
    "heideltime_cartel_period": HeideltimeCartelPeriodModule,
}

# Answers to the overwrite-cascade prompt that count as yes; anything else,
# including a bare Enter, aborts the run.
_CONFIRM_ANSWERS: frozenset[str] = frozenset({"y", "yes"})

"""
Refactor summary:

Split Main_ingest_generic_facts.py into two cohesive classes plus a thin main():

* PipelineExecutor — per-CELEX execution. 
    Methods: process_celex (BatchJobRunner entrypoint), _run_all_pipelines, _run_single_pipeline, _resolve_agent, _update_context_data. 
    Shared state (agents, pipelines, yaml_params, overwrite) lives on the instance instead of being threaded through a context tuple.

* FactIngestionOrchestrator — setup + drive. 
    Topic-centric methods: _register_cli_arguments, _configure_llm, _build_agents, _resolve_yaml_path, _load_yaml_configs, _filter_active_structs, _topological_order, _build_pipelines, 
    plus a short run() that orchestrates them.

* _apply_debug_cli_overrides() — extracted the dev-time sys.argv injection block.
* main() — two lines.

Test updated to use PipelineExecutor directly (no more tuple-shaped context fixture).


"""


class PipelineExecutor:
    """Executes the configured fact pipelines against a single CELEX document.

    Instances are callable by :class:`BatchJobRunner` via :meth:`process_celex`,
    which conforms to the ``(celex_id, context, dry_run)`` signature the runner
    expects. Shared state (agents, pipelines, YAML parameters, overwrite flag)
    is held on the instance rather than threaded through a context tuple.
    """

    def __init__(
        self,
        agents: dict,
        pipelines: list,
        yaml_params: list,
        overwrite: bool,
    ):
        self.agents = agents
        self.pipelines = pipelines
        self.yaml_params = yaml_params
        self.overwrite = overwrite
        self.logger = logging.getLogger(__name__)

    def process_celex(self, celex_id: str, context=None, dry_run: bool = False) -> list:
        """Process every configured fact pipeline for ``celex_id``.

        Args:
            celex_id: CELEX identifier of the document to process.
            context: Unused; retained for :class:`BatchJobRunner` compatibility.
            dry_run: When True, no pipelines are invoked; a marker result is
                recorded per configured fact instead.

        Returns:
            List of per-pipeline result dictionaries in execution order.
        """
        del context  # state lives on self
        return asyncio.run(self._run_all_pipelines(celex_id, dry_run))

    async def _run_all_pipelines(self, celex_id: str, dry_run: bool) -> list:
        """Iterate configured pipelines sequentially, sharing a context dict.

        A fact whose predecessors cannot be resolved is skipped individually;
        the sequence continues. Facts that genuinely depend on the skipped one
        re-check their own predecessors and skip in turn, so the cascade is
        preserved without sacrificing the independent facts further down the
        execution order.
        """
        results: list = []
        context_data: dict = {}
        for pipe, struct in zip(self.pipelines, self.yaml_params):
            param_vo = FactExtractorParamVO.from_yaml_struct(
                celex_id=celex_id, struct=struct, force_override=self.overwrite
            )

            if dry_run:
                self.logger.info(
                    f"[DRY RUN] Would process fact '{param_vo.target_fact_name}' for {celex_id}"
                )
                results.append({"status": "dry_run", "celex_id": celex_id})
                continue

            res = await self._run_single_pipeline(pipe, param_vo, context_data)
            if res is not None:
                results.append(res)
        return results

    async def _run_single_pipeline(
        self, pipe, param_vo: FactExtractorParamVO, context_data: dict
    ) -> dict | None:
        """Dispatch one pipeline and update the shared context on success/skip."""
        missing_predecessors = self._missing_predecessors(pipe, param_vo, context_data)
        if missing_predecessors:
            self.logger.warning(
                "Skipping fact '%s' for %s because predecessor(s) are missing or empty: %s",
                param_vo.target_fact_name,
                param_vo.celex_id,
                missing_predecessors,
            )
            return {
                "status": "dependency_missing",
                "celex_id": param_vo.celex_id,
                "target_fact_name": param_vo.target_fact_name,
                "missing_predecessors": missing_predecessors,
            }

        agent = self._resolve_agent(param_vo)
        if agent is None:
            return None

        if settings.THERMAL_GRACE_DURATION > 0:
            self.logger.debug(
                "Thermal-grace pause of %.1fs before extracting fact '%s' for %s",
                settings.THERMAL_GRACE_DURATION,
                param_vo.target_fact_name,
                param_vo.celex_id,
            )
            await asyncio.sleep(settings.THERMAL_GRACE_DURATION)

        try:
            res = await pipe.process(
                param_vo, dspy_agent=agent, context_data=context_data
            )
        except Exception as exc:
            # BatchJobRunner reports the failure per CELEX only, so without this
            # a timeout names the document but never which of the 51 facts hung.
            self.logger.error(
                "Fact '%s' failed for %s: %s",
                param_vo.target_fact_name,
                param_vo.celex_id,
                exc,
            )
            raise
        self._update_context_data(pipe, param_vo, res, context_data)
        return res

    def _resolve_agent(self, param_vo: FactExtractorParamVO):
        """Look up the DSPy agent for a fact, falling back to the universal one."""
        agent = self.agents.get(param_vo.pipeline, self.agents.get("universal"))
        if not agent:
            self.logger.error(
                f"Pipeline '{param_vo.pipeline}' not found for fact '{param_vo.target_fact_name}'"
            )
        return agent

    def _missing_predecessors(
        self,
        pipe,
        param_vo: FactExtractorParamVO,
        context_data: dict,
    ) -> list[str]:
        """Return predecessors without non-empty context or persisted fact data."""
        missing: list[str] = []
        for predecessor in param_vo.predecessors:
            if self._has_value(context_data.get(predecessor)):
                continue

            fact_table = getattr(pipe.ingestor, "fact_table", None)
            if fact_table:
                existing = fact_table.select_fact4celex(param_vo.celex_id, predecessor)
                if existing:
                    value = existing[-1].value_json or existing[-1].value_md
                    if self._has_value(value):
                        context_data[predecessor] = value
                        continue

            missing.append(predecessor)
        return missing

    def _has_value(self, value) -> bool:
        """Treat empty fact outputs such as ``[]``/``NA`` as unfulfilled."""
        if value is None:
            return False
        if isinstance(value, str):
            stripped = value.strip()
            return bool(stripped) and stripped not in {"[]", "{}", "NA"}
        if isinstance(value, (list, tuple, set, dict)):
            return bool(value)
        return True

    def _update_context_data(
        self,
        pipe,
        param_vo: FactExtractorParamVO,
        res: dict,
        context_data: dict,
    ) -> None:
        """Feed downstream pipelines using extracted or previously-stored facts."""
        status = res.get("status")
        if status == "success" and "data" in res:
            context_data[param_vo.target_fact_name] = res["data"]
            return

        if status == "skipped":
            fact_table = getattr(pipe.ingestor, "fact_table", None)
            if not fact_table:
                return
            existing = fact_table.select_fact4celex(
                param_vo.celex_id, param_vo.target_fact_name
            )
            if existing:
                context_data[param_vo.target_fact_name] = (
                    existing[-1].value_json or existing[-1].value_md
                )


class FactIngestionOrchestrator:
    """Wires shared resources, agents, and pipelines for generic fact ingestion.

    Responsibilities are split into small topic-centric methods:
    configure LLM, build agents, load YAML, topologically order facts,
    construct pipelines, and drive the batch runner.
    """

    def __init__(self, description: str):
        self.logger = logging.getLogger(__name__)
        self.runner = BatchJobRunner(description=description)
        self._register_cli_arguments()

    def run(self) -> None:
        """Entry point — configures DSPy, builds agents + pipelines, runs batch."""
        # Parsed first, and strictly: BatchJobRunner.run() rejects unknown
        # arguments anyway, so a typo or a rejected flag combination is
        # reported before the LLM and the reranker weights are loaded.
        args = self.runner.parser.parse_args()
        if args.all_facts and args.filter_fact:
            self.runner.parser.error(
                "--all-facts and --filter-fact are mutually exclusive"
            )

        yaml_path = self._resolve_yaml_path(args)

        structs = self._load_yaml_configs(yaml_path)
        if structs is None:
            return

        active_structs = self._filter_active_structs(
            structs, args.filter_fact, args.all_facts
        )
        if active_structs is None:
            return
        if not active_structs:
            self.logger.warning(f"No active facts matched criteria in {yaml_path}")
            return

        # Asked before the LLM and the reranker are loaded, so declining costs
        # nothing.
        if not self._confirm_overwrite_cascade(args, structs, active_structs):
            self.logger.warning("Run aborted: overwrite cascade not confirmed.")
            return

        execution_order = self._topological_order(active_structs)
        if execution_order is None:
            return
        active_structs.sort(key=lambda s: execution_order.index(s["target_fact_name"]))

        self._configure_llm()
        shared_repo = FactRepository()
        agents = self._build_agents(shared_repo)

        pipelines = self._build_pipelines(active_structs, shared_repo)
        self.logger.info(f"Loaded {len(active_structs)} sorted fact configurations.")
        self.logger.info(f"Execution sequence: {execution_order}")

        executor = PipelineExecutor(
            agents=agents,
            pipelines=pipelines,
            yaml_params=active_structs,
            overwrite=args.overwrite,
        )
        self.runner.run(executor.process_celex)

    def _register_cli_arguments(self) -> None:
        """Add the script-specific CLI flags on top of BatchJobRunner defaults."""
        self.runner.parser.add_argument(
            "--config-path", type=str, help="Path to the YAML configuration file"
        )
        self.runner.parser.add_argument(
            "--overwrite",
            action="store_true",
            help=(
                "Force override existing facts: re-extract even when a fact row "
                "already exists. OR-ed with the per-fact force_override in the "
                "YAML config, so it applies to every selected fact but cannot "
                "switch a YAML 'force_override: true' back off."
            ),
        )
        self.runner.parser.add_argument(
            "--all-facts",
            action="store_true",
            help=(
                "Select every fact in the YAML catalogue, overruling "
                "run_state_active. Mutually exclusive with --filter-fact. "
                "Combine with --overwrite to re-extract facts that already "
                "have rows."
            ),
        )
        self.runner.parser.add_argument(
            "--yes",
            action="store_true",
            help=(
                "Skip the [y/N] confirmation that --filter-fact with --overwrite "
                "asks for when the fact has predecessors. Needed for "
                "unattended runs, where a missing answer counts as no."
            ),
        )

    def _confirm_overwrite_cascade(
        self, args, structs: list, selected_structs: list
    ) -> bool:
        """Warn about an overwrite cascade and ask the user to confirm it.

        ``--overwrite`` also applies to the predecessors that ``--filter-fact``
        pulls in, so asking for one fact re-extracts its whole predecessor
        chain. Facts that depend on the rebuilt chain but are outside the
        selection keep their old values. When predecessors are selected, both
        lists and the CELEX scope are logged and the user must answer ``y``.
        Pressing Enter, any other answer, or EOF (no terminal attached) counts
        as no.

        Args:
            args: Parsed CLI namespace; reads ``overwrite``, ``filter_fact``,
                ``dry_run``, ``yes``, ``filter_celex_id``, ``celex_list_file``
                and ``limit``.
            structs: All fact configs from the YAML file, used to find
                dependents outside the selection.
            selected_structs: The configs chosen by ``_filter_active_structs``.

        Returns:
            True when the run may continue, False when the user declined.
        """
        if not (args.overwrite and args.filter_fact) or args.dry_run:
            return True
        selected_names = {s["target_fact_name"] for s in selected_structs}
        predecessors = sorted(selected_names - {args.filter_fact})
        if not predecessors:
            return True

        stale_dependents = sorted(
            self._collect_dependents(selected_names, structs) - selected_names
        )
        self.logger.warning(
            "--overwrite with --filter-fact %s also re-extracts its predecessors: "
            "%s. CELEX scope: %s.",
            args.filter_fact,
            ", ".join(predecessors),
            self._describe_celex_scope(args),
        )
        if stale_dependents:
            self.logger.warning(
                "Dependent facts NOT selected keep values built from the old "
                "chain: %s.",
                ", ".join(stale_dependents),
            )
        if args.yes:
            self.logger.info("--yes given: overwrite cascade confirmed.")
            return True
        try:
            answer = input("Proceed with the overwrite cascade? [y/N] ")
        except EOFError:
            return False
        return answer.strip().lower() in _CONFIRM_ANSWERS

    def _collect_dependents(self, fact_names: set[str], structs: list) -> set[str]:
        """Return every fact that directly or transitively depends on ``fact_names``.

        Args:
            fact_names: Facts whose downstream closure is wanted.
            structs: Fact configs from the YAML file; ``predecessors`` may be a
                string, a list, or missing.

        Returns:
            Names of the dependent facts, excluding ``fact_names`` themselves
            unless a cycle leads back to them.
        """
        successors: dict[str, set[str]] = {}
        for struct in self._normalize_predecessors(structs) or []:
            for predecessor in struct["predecessors"]:
                successors.setdefault(predecessor, set()).add(
                    struct["target_fact_name"]
                )
        dependents: set[str] = set()
        stack = list(fact_names)
        while stack:
            for child in successors.get(stack.pop(), set()):
                if child not in dependents:
                    dependents.add(child)
                    stack.append(child)
        return dependents

    @staticmethod
    def _describe_celex_scope(args) -> str:
        """Summarise which CELEX documents the run will touch.

        Args:
            args: Parsed CLI namespace; reads ``filter_celex_id``,
                ``celex_list_file`` and ``limit``.

        Returns:
            A short description, following BatchJobRunner's precedence.
        """
        if args.filter_celex_id:
            return f"single document {args.filter_celex_id}"
        source = (
            f"list file {args.celex_list_file}"
            if args.celex_list_file
            else "the whole corpus"
        )
        return f"first {args.limit} of {source}" if args.limit else source

    def _configure_llm(self) -> None:
        """Configure the global DSPy LM from the shared batch profile.

        Greedy decoding (``settings.LLM_TEMPERATURE``, default 0.0) so ingestion
        is reproducible; the provider default flips borderline extractions
        between an entity and NA and varies the emitted entity count. Output
        cap, request deadline, retry count and thinking mode come from
        ``config.llm_factory`` (see ``lm_runtime_kwargs`` for why they must not
        be set here).
        """
        dspy.configure(
            lm=create_lm(batch_profile(temperature=settings.LLM_TEMPERATURE))
        )

    def _build_agents(self, shared_repo: FactRepository) -> dict:
        """Provision shared search tools and return the agent registry."""
        tools = CelexDatabaseToolkit(fact_repo=shared_repo)
        chunk_repo = tools.chunk_repo
        ranker = create_ranker()
        hybrid_search = HybridSearch(
            repo=chunk_repo, ranker=ranker, embed_query=tools.embed_query
        )
        enumeration_search = EnumerationSearch(
            repo=chunk_repo, ranker=ranker, embed_query=tools.embed_query
        )
        keyword_search_tool = KeywordSearch(repo=chunk_repo, ranker=ranker)
        regex_retriever = RegexRetriever(repo=chunk_repo)
        book_keyword_search_tool = HybridBookSearchToolkit(tools=tools)
        book_chunk_repo = BookChunkRepository()
        nace_tree = NaceTree(settings.NACE_JSON_PATH)

        # Reference the canonical PIPELINE_DSPY_MODULE_CLASSES classes so the
        # agent registry and the backfill's dspy_module provenance never diverge.
        cls = PIPELINE_DSPY_MODULE_CLASSES
        universal_agent = cls["universal"](
            sql_store=shared_repo, searcher=hybrid_search, chunk_repo=chunk_repo
        )
        enumeration_agent = cls["enumeration"](
            sql_store=shared_repo, searcher=enumeration_search
        )

        return {
            "universal": universal_agent,
            "enumeration": enumeration_agent,
            "router": cls["router"](
                qa=universal_agent, enumeration=enumeration_agent
            ),
            "keyword_search": cls["keyword_search"](
                sql_store=shared_repo, searcher=keyword_search_tool
            ),
            "keyword_regex": cls["keyword_regex"](
                sql_store=shared_repo, searcher=regex_retriever
            ),
            "infringement_concept": cls["infringement_concept"](
                sql_store=shared_repo
            ),
            "doc_search": cls["doc_search"](
                sql_store=shared_repo, searcher=hybrid_search
            ),
            "hybrid_search": cls["hybrid_search"](sql_store=shared_repo),
            "fact_context": cls["fact_context"](sql_store=shared_repo),
            "detail_retriever": cls["detail_retriever"](
                sql_store=shared_repo,
                keyword_search=keyword_search_tool,
                hybrid_search=hybrid_search,
            ),
            "fact_comparison": cls["fact_comparison"](sql_store=shared_repo),
            "nace_class_code": cls["nace_class_code"](
                sql_store=shared_repo, nace_tree=nace_tree, ranker=ranker
            ),
            "case_summary": cls["case_summary"](sql_store=shared_repo),
            "book_keywords": cls["book_keywords"](
                sql_store=shared_repo, book_searcher=book_keyword_search_tool
            ),
            "book_universal": cls["book_universal"](
                sql_store=shared_repo, book_searcher=book_keyword_search_tool
            ),
            "book_infringement_type": cls["book_infringement_type"](
                sql_store=shared_repo, book_searcher=book_keyword_search_tool
            ),
            "book_landmark": cls["book_landmark"](
                sql_store=shared_repo, book_chunk_repo=book_chunk_repo
            ),
            "celex_case_id": cls["celex_case_id"](sql_store=shared_repo),
            "case_title": cls["case_title"](sql_store=shared_repo),
            "heideltime_raw": cls["heideltime_raw"](
                sql_store=shared_repo,
                ht=HeidelTime(
                    language=settings.HEIDELTIME_LANGUAGE,
                    document_type=settings.HEIDELTIME_DOCUMENT_TYPE,
                ),
                ingestor=HeidelTimeIngestor(),
            ),
            "heideltime_event": cls["heideltime_event"](
                sql_store=shared_repo,
                retriever=HeidelTimeIngestor(),
            ),
            "heideltime_event_agg": cls["heideltime_event_agg"](
                sql_store=shared_repo,
            ),
            "heideltime_cartel_period": cls["heideltime_cartel_period"](
                sql_store=shared_repo,
            ),
        }

    def _resolve_yaml_path(self, args) -> Path:
        """Pick the YAML config path, preferring the explicit CLI override."""
        if args.config_path:
            return Path(args.config_path)
        return settings.FACT_EXTRACTOR_CONFIG_DIR

    def _load_yaml_configs(self, yaml_path) -> list | None:
        """Parse the YAML file into a list of fact-config structs."""
        try:
            with open(yaml_path, "rt") as f:
                data = yaml.safe_load(f)
        except Exception as e:
            self.logger.error(f"Failed to load {yaml_path}: {e}")
            return None
        return data if isinstance(data, list) else [data]

    def _filter_active_structs(
        self, structs: list, filter_fact: str | None, all_facts: bool = False
    ) -> list | None:
        """Keep active configs, expanding ``--filter-fact`` to its predecessors.

        ``--filter-fact`` is an explicit run request and therefore overrules the
        YAML ``run_state_active`` flag: the named fact and its recursive
        predecessor closure are selected regardless of their run state. Facts
        outside that closure are never selected. ``--all-facts`` is the
        catalogue-wide equivalent: every configured fact is selected, again
        regardless of its run state. Without either flag the YAML run state
        governs as before.

        Args:
            structs: Raw fact-config dicts parsed from the YAML file.
            filter_fact: Value of ``--filter-fact``, or None for a full run.
            all_facts: Value of ``--all-facts``; selects the whole catalogue.

        Returns:
            The selected fact configs, an empty list when the requested fact is
            unknown, or None when a config is malformed.
        """
        candidates = self._candidate_structs(structs, filter_fact, all_facts)
        candidates = self._normalize_predecessors(candidates)
        if candidates is None:
            return None

        struct_by_name = self._index_structs_by_target(candidates)
        if struct_by_name is None:
            return None

        if all_facts:
            self.logger.info("Applying --all-facts: whole catalogue selected")
            self._log_run_state_overrides(candidates, "--all-facts")
            self._log_missing_predecessors(candidates, struct_by_name)
            return candidates

        if not filter_fact:
            self._log_missing_predecessors(candidates, struct_by_name)
            return candidates

        self.logger.info(f"Applying --filter-fact: {filter_fact}")
        if filter_fact not in struct_by_name:
            return []

        selected_names = self._collect_fact_with_predecessors(
            filter_fact, struct_by_name
        )
        if selected_names is None:
            return None

        selected_structs = [
            s for s in candidates if s["target_fact_name"] in selected_names
        ]
        self._log_run_state_overrides(selected_structs, "--filter-fact")
        selected_by_name = {s["target_fact_name"]: s for s in selected_structs}
        self._log_missing_predecessors(selected_structs, selected_by_name)
        return selected_structs

    def _candidate_structs(
        self, structs: list, filter_fact: str | None, all_facts: bool = False
    ) -> list:
        """Return the configs eligible for selection.

        With ``--filter-fact`` every config is eligible so that the predecessor
        closure can reach entries marked ``run_state_active: false``; the
        closure walk narrows the result afterwards. ``--all-facts`` keeps every
        config without any narrowing. Without either flag, only configs flagged
        active in the YAML are eligible.

        Args:
            structs: Raw fact-config dicts parsed from the YAML file.
            filter_fact: Value of ``--filter-fact``, or None for a full run.
            all_facts: Value of ``--all-facts``; selects the whole catalogue.

        Returns:
            The eligible subset of ``structs``.
        """
        if all_facts or filter_fact:
            return list(structs)
        return [s for s in structs if s.get("run_state_active", True)]

    def _log_run_state_overrides(self, selected_structs: list, source_flag: str) -> None:
        """Report which selected facts had their YAML run state overruled.

        Args:
            selected_structs: The configs picked by the selection flag.
            source_flag: Name of the CLI flag that overruled the run state,
                used verbatim in the log line.
        """
        overruled = [
            s["target_fact_name"]
            for s in selected_structs
            if not s.get("run_state_active", True)
        ]
        if overruled:
            self.logger.info(
                "%s overrules run_state_active: false for: %s",
                source_flag,
                ", ".join(overruled),
            )

    def _collect_fact_with_predecessors(
        self, filter_fact: str, struct_by_name: dict
    ) -> set[str] | None:
        """Return the requested fact plus all recursive predecessors."""
        selected_names: set[str] = set()
        stack = [filter_fact]
        while stack:
            target_fact_name = stack.pop()
            if target_fact_name in selected_names:
                continue
            struct = struct_by_name.get(target_fact_name)
            if struct is None:
                self.logger.warning(
                    "Requested dependency '%s' has no YAML configuration; dependent fact will be skipped per CELEX unless an existing fact is available.",
                    target_fact_name,
                )
                continue
            selected_names.add(target_fact_name)
            stack.extend(struct.get("predecessors", []))
        return selected_names

    def _normalize_predecessors(self, structs: list) -> list | None:
        """Return copied structs with ``predecessors`` normalized to ``list[str]``."""
        normalized: list = []
        for struct in structs:
            target_fact_name = struct.get("target_fact_name")
            if not target_fact_name:
                self.logger.error(
                    f"Fact configuration is missing target_fact_name {target_fact_name}. Check YAML file for missing target_fact_name."
                )
                return None

            predecessors = struct.get("predecessors", [])
            if predecessors is None:
                predecessors = []
            elif isinstance(predecessors, str):
                predecessors = [predecessors]
            elif isinstance(predecessors, list):
                if not all(isinstance(item, str) for item in predecessors):
                    self.logger.error(
                        "Fact '%s' has non-string predecessor entries: %r",
                        target_fact_name,
                        predecessors,
                    )
                    return None
            else:
                self.logger.error(
                    "Fact '%s' has invalid predecessors value %r; expected string or list.",
                    target_fact_name,
                    predecessors,
                )
                return None

            copied = dict(struct)
            copied["predecessors"] = predecessors
            normalized.append(copied)
        return normalized

    def _index_structs_by_target(self, structs: list) -> dict | None:
        """Build a target_fact_name lookup, rejecting duplicate fact configs."""
        struct_by_name: dict = {}
        for struct in structs:
            target_fact_name = struct["target_fact_name"]
            if target_fact_name in struct_by_name:
                self.logger.error(
                    "Duplicate active fact configuration for '%s'.", target_fact_name
                )
                return None
            struct_by_name[target_fact_name] = struct
        return struct_by_name

    def _log_missing_predecessors(self, structs: list, struct_by_name: dict) -> None:
        """Log declared predecessors absent from the selected config set."""
        for struct in structs:
            target_fact_name = struct["target_fact_name"]
            for predecessor in struct.get("predecessors", []):
                if predecessor not in struct_by_name:
                    self.logger.warning(
                        "Fact '%s' declares missing or inactive predecessor '%s'; it will be checked per CELEX at runtime.",
                        target_fact_name,
                        predecessor,
                    )

    def _topological_order(self, active_structs: list) -> list | None:
        """Return a topologically-sorted list of target_fact_name values."""
        defined_tasks = {s["target_fact_name"] for s in active_structs}
        missing = {
            s["target_fact_name"]: [
                d for d in s.get("predecessors", []) if d not in defined_tasks
            ]
            for s in active_structs
        }
        missing = {name: deps for name, deps in missing.items() if deps}
        if missing:
            self.logger.warning(
                "Missing predecessors in selected YAML structure; runtime will skip affected CELEX rows if unresolved: %s",
                missing,
            )

        graph = {
            s["target_fact_name"]: set(s.get("predecessors", []))
            for s in active_structs
        }
        try:
            return list(TopologicalSorter(graph).static_order())
        except Exception as e:
            self.logger.error(f"Circular Dependency Detected in YAML structure: {e}")
            return None

    def _build_pipelines(
        self, active_structs: list, shared_repo: FactRepository
    ) -> list:
        """Instantiate one FactRagPipeline per active fact config.

        A single ``LineageRepository`` and ``ChunkRepository`` are shared across
        all ingestors so that fact provenance is recorded (chunk lineage derived
        from ``references_doc``) on the same connection pool as ``shared_repo``.
        """
        lineage_repo = LineageRepository(engine=shared_repo.engine)
        chunk_repo = ChunkRepository()
        pipelines = []
        for s in active_structs:
            target_fact_name = s.get("target_fact_name", "")
            pipelines.append(
                FactRagPipeline(
                    target_fact_name=target_fact_name,
                    ingestor=FactIngestor(
                        target_fact_name=target_fact_name,
                        fact_table=shared_repo,
                        lineage_repo=lineage_repo,
                        chunk_repo=chunk_repo,
                    ),
                    fact_embedding_loader=FactEmbeddingLoaderQwen(
                        session_factory=Session
                    ),
                    fact_repo=shared_repo,
                )
            )
        return pipelines


def _apply_debug_cli_overrides() -> None:
    """Local developer-time knobs. Keep defaults at None in committed code.

    Why: historically used to pin a specific CELEX / fact / limit during
    debugging without editing the command line. Left in place so the
    contract at the bottom of the file (CLI example block) remains valid.
    """
    filter_celex_id: str | None = None
    filter_fact: str | None = None
    limit: int | None = None

    if filter_celex_id and "--filter-celex-id" not in sys.argv:
        sys.argv.extend(["--filter-celex-id", filter_celex_id])
    if filter_fact and "--filter-fact" not in sys.argv:
        sys.argv.extend(["--filter-fact", filter_fact])
    if limit is not None and "--limit" not in sys.argv:
        sys.argv.extend(["--limit", str(limit)])


def main() -> None:
    """Entry point for the generic fact-ingestion batch script.

    CELEX source precedence (highest to lowest):
        1. ``--filter-celex-id ID``   — single document; intended for dev/debug.
        2. ``--celex-list-file PATH`` — plain-text file, one CELEX ID per line;
           blank lines and ``#``-prefixed comment lines are ignored.
        3. Default master-sheet + filter-file pair (``--json-fn`` / ``--filter-fn``).

    Only fact configurations with ``run_state_active: true`` in the YAML are
    executed regardless of which CELEX source is used, unless ``--filter-fact``
    or ``--all-facts`` explicitly overrules that flag.

    Trade-offs:
        - ``--celex-list-file`` and ``--filter-celex-id`` together:
          ``--filter-celex-id`` wins, preserving the single-document debug
          workflow without surprises.
        - ``--limit N`` still applies on top of the list file, which lets you
          test the first N entries of an otherwise large selection.
        - ``--filter-fact FACT`` still applies; combine with ``--celex-list-file``
          for targeted re-runs of one fact over a curated document set.
        - ``--all-facts`` is the catalogue-wide counterpart of ``--filter-fact``
          and is rejected when both are given: one selects a closure, the other
          selects everything, so combining them can only mean one of the two.
        - File order is preserved; ``--limit`` slices from the top, so put
          priority documents first in the file when using both flags.
    """
    _apply_debug_cli_overrides()
    orchestrator = FactIngestionOrchestrator(
        description="Batch processes generic fact extraction using Universal DSPy Pipeline."
    )
    orchestrator.run()


if __name__ == "__main__":
    main()


"""
CLI vs. config_main/FACT_EXTRACTOR.yaml
---------------------------------------
--filter-fact NAME
    Overrules run_state_active. NAME and its recursive predecessor closure are
    selected even when their YAML entries say `run_state_active: false`; facts
    outside that closure are never selected. Without --filter-fact the YAML run
    state governs and only `run_state_active: true` facts are processed.

--all-facts
    Overrules run_state_active for the *whole* catalogue: every configured fact
    is selected, in topological order. Mutually exclusive with --filter-fact
    (argparse error). It exists because a bare --overwrite deliberately does
    NOT widen the selection -- --overwrite only forces re-extraction of the
    facts already selected, which without a selection flag is just the
    `run_state_active: true` entries. Facts whose predecessors cannot be
    resolved are skipped individually; the rest of the catalogue still runs.

--overwrite
    OR-ed with the per-fact `force_override`, so it forces re-extraction of
    every selected fact -- including the predecessors pulled in by
    --filter-fact. It cannot switch a YAML `force_override: true` back off.
    Without --overwrite each fact keeps its own YAML `force_override` value:
    a predecessor with `force_override: false` and an existing row is skipped
    and its stored value is reused as context.
    Combined with --filter-fact on a fact that has predecessors, the run logs
    the re-extracted predecessors, the dependents left stale and the CELEX
    scope, then asks "[y/N]"; Enter, any other answer, or no terminal aborts.
    Skipped under --dry-run.

--yes
    Answers the --overwrite cascade prompt with yes, for unattended runs.

# PYTHONPATH=src .venv/bin/python src/batch/Main_ingest_generic_facts.py --filter-celex-id CELEX-31982D0367 --filter-fact fines --limit 1

.venv/bin/python src/batch/Main_ingest_generic_facts.py --filter-celex-id 39824_6567_14 --all-facts --overwrite
.venv/bin/python src/batch/Main_ingest_generic_facts.py --filter-celex-id CELEX-31982D0367 --filter-fact leniency_applicants --limit 1
.venv/bin/python src/batch/Main_ingest_generic_facts.py --filter-celex-id 38121_1007_1 --filter-fact leniency_applicants --limit 1
.venv/bin/python src/batch/Main_ingest_generic_facts.py --filter-celex-id 38121_1007_1 --filter-fact leniency_granted --limit 1
.venv/bin/python src/batch/Main_ingest_generic_facts.py --filter-celex-id 38121_1007_1 --filter-fact leniency_rejected --limit 1
.venv/bin/python src/batch/Main_ingest_generic_facts.py --filter-celex-id AT_40136_7970992_6095_3 --limit 1
.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id CELEX-31975D0358 \
    --filter-fact products \
    --limit 1 \
    --overwrite

PYTHONPATH=~/a/celex-facts:~/a/celex-facts/src .venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id CELEX-31972D0068 \
    --filter-fact NACE_group_code \
    --limit 1 \
    --overwrite

 .venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id CELEX-31972D0068 \
    --filter-fact NACE_group_code \
    --limit 1 \
    --overwrite



 .venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id CELEX-31972D0068 \
    --filter-fact NACE_class_code \
    --limit 1 \
    --overwrite

 .venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 37784_8_4  \
    --filter-fact case_infringement_type_book \
    --limit 1 \
    --overwrite


 .venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 32450_35_6  \
    --filter-fact law_article_all_relevant \
    --limit 1 \
    --overwrite



 .venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 39530_2671_5  \
    --filter-fact abuse_of_dominance \
    --limit 1 \
    --overwrite



 .venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 37784_8_4  \
    --filter-fact company_recidivism \
    --limit 1 \
    --overwrite


 .venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 39525_1916_7  \
    --filter-fact case_summary \
    --limit 1 \
    --overwrite

 .venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 39525_1916_7  \
    --filter-fact celex_case_id \
    --limit 1 \
    --overwrite

.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id CELEX-31997D0181  \
    --filter-fact case_summary \
    --limit 1 \
    --overwrite


.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 39612_12448_6  \
    --filter-fact case_summary \
    --limit 1 \
    --overwrite
    
.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id CELEX-32000D0146  \
    --filter-fact heideltime_event \
    --limit 1 \
    --overwrite

    
.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 39612_12448_6  \
    --filter-fact heideltime_event \
    --limit 1 \
    --overwrite

    

.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 39612_12448_6  \
    --filter-fact heideltime_event_agg \
    --limit 1 \
    --overwrite

    
.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id CELEX-31997D0181  \
    --filter-fact heideltime_event_agg \
    --limit 1 \
    --overwrite

    
.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 40013_1084_2  \
    --filter-fact heideltime_raw \
    --limit 1 \
    --overwrite

.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 40013_1084_2  \
    --filter-fact heideltime_event \
    --limit 1 \
    --overwrite

    
    
.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 40013_1084_2  \
    --filter-fact heideltime_event_agg \
    --limit 1 \
    --overwrite


.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 39389_2761_1  \
    --filter-fact heideltime_raw \
    --limit 1 \
    --overwrite

# -- preselected CELEX list --
# celex_ids.txt (one ID per line, blank lines and # comments ignored):
#   CELEX-31997D0181
#   39612_12448_6
#   40013_1084_2

.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --celex-list-file /path/to/celex_ids.txt \
    --overwrite

# combine with --filter-fact for a targeted re-run of one fact over a curated set:
.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --celex-list-file /path/to/celex_ids.txt \
    --filter-fact heideltime_event_agg \
    --overwrite

# --limit still works on top of the list file:
.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --celex-list-file reports/malformatted_date_celex_20260429.txt \
    --filter-celex-id CELEX-31965D0426
    --limit 1

.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id CELEX-31965D0426 \
    --filter-fact heideltime_cartel_period 
    --limit 1

.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --celex-list-file reports/heideltime_qa_dates_20260430.txt \
    --filter-fact heideltime_event_agg \
    --overwrite

    
    
.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id CELEX-31965D0426 \
    --filter-fact heideltime_event_agg \
    --limit 1 \
    --overwrite

    
.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --celex-list-file reports/heideltime_qa_event_20260506.txt \
    --filter-fact heideltime_cartel_period
    


.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id CELEX-31976D0743 \
    --filter-fact case_summary
    --limit 1    


.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 38344_5856_3 \
    --filter-fact case_summary
    --limit 1    


.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 39226_8310_11 \
    --filter-fact court_list_raw \
    --limit 1 \
    --overwrite
    


.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 39226_8310_11 \
    --filter-fact court_list \
    --limit 1 \
    --overwrite
    
.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-fact court_list_raw \
    --overwrite    

.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-fact court_list_raw \
    --overwrite    



.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 39226_8310_11 \
    --filter-fact court_list_comprehensive \
    --limit 1 \
    --overwrite
    

.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-fact court_list \
    --overwrite    

    
.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id CELEX-31975D0075 \
    --filter-fact products_name \
    --limit 1 \
    --overwrite

.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id CELEX-31982D0123 \
    --filter-fact case_title \
    --limit 1 \
    --overwrite


.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 39813_1554_3 \
    --filter-fact case_title \
    --limit 1 \
    --overwrite

    
    
.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --celex-list-file reports/product_name_na_facts_202606071109.csv \
    --filter-fact products_name \
    --overwrite




.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id CELEX-31989D0113 \
    --filter-fact  case_is_landmark_in_book  \
    --overwrite

.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-fact  case_is_landmark_in_book  
    

.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-fact  case_is_landmark_in_book  
    

.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 38745_32_1 \
    --filter-fact     companies_investigated

.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 38745_32_1 \
    --filter-fact     authorities_canoncial


.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 'CELEX-32000D0146' \
    --filter-fact     companies_investigated




.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 'CELEX-32000D0146' \
    --filter-fact   companies_country


.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 'CELEX-32000D0146' \
    --filter-fact   companies_address



.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id '39389_2761_1' \
    --filter-fact   companies_investigated


.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-fact   companies_investigated


.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 'CELEX-32000D0146' \
    --filter-fact   companies_address



.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --celex-list-file reports/corp-investigated_empty.txt \
    --filter-fact companies_investigated \
    --overwrite


.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-fact   companies_address


.venv/bin/python src/batch/Main_ingest_generic_facts.py \
     --celex-list-file ~/a/celex_docs/reports/filter-poisoned-company-investigated-names-colon.txt \
    --filter-fact companies_investigated 

    .venv/bin/python src/batch/Main_ingest_generic_facts.py \
     --celex-list-file ~/a/celex_docs/reports/filter-poisoned-company-investigated-names.txt \
    --filter-fact companies_address 

    .venv/bin/python src/batch/Main_ingest_generic_facts.py \
     --celex-list-file ~/a/celex_docs/reports/filter-poisoned-company-investigated-names-colon.txt \
    --filter-fact companies_investigated 

.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 'CELEX-31989D0408' \
    --filter-fact   case_commissioner    

# was silently dying wiht OOM in BGE    
.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id '39824_6567_14' \
    --all-facts \
    --overwrite

.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 'AT_40940_10539411_1520_696_3' \
    --all-facts \
    --overwrite

.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id '39824_6567_14' \
    --filter-fact  market_geographical \
    --overwrite

 --filter-fact heideltime_raw \
    --limit 0 --overwrite

    
    .venv/bin/python src/batch/Main_ingest_generic_facts.py --filter-celex-id 'CELEX-31965D0426'  --filter-fact heideltime_raw     --limit 0 --overwrite
    


    .venv/bin/python src/batch/Main_ingest_generic_facts.py --filter-fact heideltime_event_agg --overwrite

    
.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id '39824_6567_14' \
    --filter-fact  case_summary

    
.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-fact  case_summary

    """
