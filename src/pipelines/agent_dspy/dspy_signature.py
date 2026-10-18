import logging
import re
from typing import List, Dict, Any, Literal, Optional

import dspy
from pydantic import BaseModel, Field, field_validator, model_validator

from pipelines.agent_dspy.dspy_protocols import FactComparisonOutput

logger = logging.getLogger(__name__)

_NULL_LIKE_STRINGS: frozenset[str] = frozenset({"null", "none", "n/a", "na", ""})

# A cartel decision states its infringement window at whatever precision the
# evidence supports: "from 1994 to 1999", "from March 1994", or a full date.
# Demanding YYYY-MM-DD leaves the model two bad options -- invent a day, or
# answer nothing -- and the 2026-09-02 corpus shows it did both (198 ranges
# ending on a Jan-1/Dec-31 boundary, 33 empty facts). Partial precision is
# therefore accepted and stored verbatim, so a year-only period stays a
# year-only period instead of becoming a fabricated day.
_PARTIAL_ISO_DATE = re.compile(r"^\d{4}(?:-\d{2}(?:-\d{2})?)?$")

# The cartel-period prompt bounds the search to 1900..2030. The model reads
# those bounds as legal answers and emits them as placeholders when it finds
# no period at all -- 60 facts in the 2026-09-02 corpus carry one. They are
# absence dressed as data, so they are mapped back to absence.
_SENTINEL_PERIOD_DATES: frozenset[str] = frozenset({"1900-01-01", "2030-12-31"})

_DATE_RANGE_SEPARATOR = " : "


class CelexValidationResult(BaseModel):
    """Outcome of the celex/case-ID gate at the entry of the retrieval pipeline.

    Attributes:
        status: ``valid`` when a celex/case-like token resolves in the DB,
            ``invalid`` when a candidate was extracted but is unknown (the
            pipeline must halt and surface ``message`` to the user), and
            ``no_celex`` when the question contains no celex/case attempt at
            all (the pipeline proceeds with general retrieval over the facts).
        candidate: The raw token extracted from the question.
        canonical_celex_id: The canonical celex_id returned by the DB lookup;
            only populated when ``status == 'valid'``.
        message: User-facing explanation when ``status == 'invalid'``.
    """

    status: Literal["valid", "invalid", "no_celex"] = Field(
        description="Validation outcome — 'valid', 'invalid', or 'no_celex'."
    )
    candidate: str = Field(
        default="",
        description="The raw celex/case-ID token detected in the question.",
    )
    canonical_celex_id: str = Field(
        default="",
        description="Canonical celex_id resolved from the database (e.g. 'CELEX-31969D0216').",
    )
    message: str = Field(
        default="",
        description="User-facing message explaining why the candidate was rejected.",
    )


class ExtractionResult(BaseModel):
    extracted_facts: List[Dict[str, Any]] = Field(
        description="List of dictionaries representing the extracted structured data."
    )


class VerificationResult(BaseModel):
    verified_facts: List[Dict[str, Any]] = Field(
        description="List of dictionaries that have been cross-referenced and verified against the context."
    )


class ChunkReferenceOutput(BaseModel):
    doc_positions: List[int] = Field(
        description="List of integer indices for the supporting text chunks (at most 3)."
    )


# --- Signatures (Rule 1) ---


class GenericExtractionSignature(dspy.Signature):
    """
    Extract structured data from the context into a list of dictionaries.
    """

    context = dspy.InputField(desc="The document text to analyze.")
    target_fact_name = dspy.InputField(
        desc="The strictly focused data category (e.g., 'fines', 'commissioner'). If an entity is mentioned but its value for this target fact is missing, EXCLUDE it."
    )
    # schema_description dictates the EXACT keys the LLM must use in the output dictionaries.
    # Ex: 'company, fine_amount' -> [{"company": "...", "fine_amount": "..."}]
    schema_description = dspy.InputField(
        desc="Use STRICTLY EXCLUSIVELY these exact keys for each dict (e.g., 'company, fine_amount'). Do NOT add any extra keys such as 'Role' or 'source' inside the dictionaries."
    )
    question = dspy.InputField(desc="The specific question guiding the extraction.")
    output: ExtractionResult = dspy.OutputField(
        desc="Structured extraction result with extracted_facts list."
    )


class GenericVerificationSignature(dspy.Signature):
    """
    Cross-reference extraction against the context and the optional golden_reference_list.
    Remove any item that lacks explicit support in the text.
    """

    context = dspy.InputField(desc="The document text to verify against.")
    extracted_list = dspy.InputField(
        desc="The list of dictionaries found. Return verified items using the exact same keys. DO NOT add extra metadata keys like 'Role', 'status', or 'source'."
    )
    golden_reference_list = dspy.InputField(
        desc="Verified entities from database as a guide. If an entity is NOT in this list but has clear, explicit support in the context text, it SHOULD be verified."
    )
    target_fact_name = dspy.InputField(desc="The target data category being verified.")
    output: VerificationResult = dspy.OutputField(
        desc="Verification result containing only facts confirmed by the context."
    )


class DetermineSupportingChunksSignature(dspy.Signature):
    """
    Determine which parts of the text (chunks indicated by [doc_pos: X]) explicitly support the extracted facts.
    Return at most 3 doc_pos integers. If multiple chunks contribute equally, pick the top 3.
    """

    context = dspy.InputField(
        desc="The document text blocks, each prefixed with [doc_pos: <id>]."
    )
    extracted_fact = dspy.InputField(
        desc="The list of extracted facts to find references for."
    )
    output: ChunkReferenceOutput = dspy.OutputField(
        desc="List of doc_pos integers (max 3) that explicitly support the extracted facts."
    )


class DependentFactExtractionSignature(dspy.Signature):
    """
    Extract structured data from the multiple preceding facts into a list of dictionaries.
    """

    context = dspy.InputField(
        desc="The aggregated preceding extracted facts to analyze."
    )
    target_fact_name = dspy.InputField(
        desc="The strictly focused data category being targeted."
    )
    schema_description = dspy.InputField(
        desc="Use STRICTLY EXCLUSIVELY these exact keys for each dict. Do NOT add any extra keys such as 'Role' or 'source'."
    )
    question = dspy.InputField(
        desc="The specific question guiding the extraction based on the context."
    )
    output: ExtractionResult = dspy.OutputField(
        desc="Structured extraction result derived from aggregated preceding facts."
    )


class EntityDetailExtractionSignature(dspy.Signature):
    """
    Extract detail fields for EXACTLY ONE named entity from the context.
    Return exactly one dictionary describing this entity. If a detail field
    is not stated in the context, set its value to the literal string 'NA'.
    Never invent values and never return details of other entities.
    """

    context = dspy.InputField(
        desc="Document text chunks retrieved specifically for this entity."
    )
    entity = dspy.InputField(
        desc="The exact entity name (e.g. a company) whose details must be extracted. The output dictionary describes ONLY this entity."
    )
    target_fact_name = dspy.InputField(
        desc="The strictly focused data category being targeted (e.g. 'companies_address')."
    )
    schema_description = dspy.InputField(
        desc="Use STRICTLY EXCLUSIVELY these exact keys for the dict (e.g. 'company, address'). The FIRST key carries the entity name verbatim. Do NOT add any extra keys."
    )
    question = dspy.InputField(
        desc="The specific question guiding the detail extraction for this entity."
    )
    output: ExtractionResult = dspy.OutputField(
        desc="ExtractionResult whose extracted_facts contains exactly one dict for this entity; missing detail values are the literal string 'NA'."
    )


class FactComparisonSignature(dspy.Signature):
    """
    Determine if the legacy rule fact and the new LLM fact represent the exact same fundamental information.
    Ignore formatting differences, abbreviations, unit placements, casing, and minor stylistic variations.
    """

    question = dspy.InputField(desc="The fact being compared (e.g. 'companies_country')")
    rule_fact = dspy.InputField(
        desc="The legacy fact extracted by the old rule engine."
    )
    llm_fact = dspy.InputField(desc="The new fact extracted by the LLM pipeline.")
    output: FactComparisonOutput = dspy.OutputField(
        desc="Comparison result indicating whether rule and LLM facts match."
    )


class NaceCodeSignature(dspy.Signature):
    """
    Extract structured data from the context into a list of dictionaries.
    """

    context = dspy.InputField(desc="The document text to analyze.")
    target_fact_name = dspy.InputField(
        desc="The strictly focused data category (e.g., 'fines', 'commissioner'). If an entity is mentioned but its value for this target fact is missing, EXCLUDE it."
    )
    # schema_description dictates the EXACT keys the LLM must use in the output dictionaries.
    # Ex: 'company, fine_amount' -> [{"company": "...", "fine_amount": "..."}]
    schema_description = dspy.InputField(
        desc="Use STRICTLY EXCLUSIVELY these exact keys for each dict (e.g., 'company, fine_amount'). Do NOT add any extra keys such as 'Role' or 'source' inside the dictionaries."
    )
    question = dspy.InputField(desc="The specific question guiding the extraction.")
    output: ExtractionResult = dspy.OutputField(
        desc="Structured extraction result with extracted NACE code facts."
    )


class CaseSummaryChapters(BaseModel):
    """Chapters of a case summary; each names the context facts it draws from.

    The source fact names must stay in step with the ``context_facts`` list of
    the ``case_summary`` entry in ``FACT_EXTRACTOR.yaml``.
    """

    celex_case_id: str = Field(description="CELEX and CASE ID of the case, from the 'celex_case_id' fact")
    case_title: str = Field(
        description="Title of the case from the 'case_title' fact, cut the leading and trailing and  brackets if present; replace the comma with em dash "
    )
    overview: str = Field(description="Overview of the case")
    industry: str = Field(
        description="Industry context, from the 'companies_industry', 'products' and 'services' facts"
    )
    oecd_nace: str = Field(
        description="OECD & NACE classifications, from the 'OECD_sector', 'NACE_group_code' and 'NACE_class_code' facts"
    )
    companies_undertakings: str = Field(
        description=(
            "Companies and undertakings investigated, from the 'companies_investigated' fact, "
            "with their country from the 'companies_country' fact. Preserve markdown lists if present."
        )
    )
    market: str = Field(
        description=(
            "Relevant market: geography ('market_geographical', 'market_outside_eu'), "
            "structure ('market_oligopoly') and turbulence ('market_turbulence')"
        )
    )
    cartel_period: str = Field(
        description=(
            "Cartel period start and end date. "
            "MUST be taken verbatim from the 'heideltime_cartel_period' fact if present in the context. "
            "Do not derive or infer this value from other sources."
        )
    )
    key_timeline: str = Field(
        description="Key timeline of events, from the 'heideltime_event_agg' and 'eu_commisssion_decision_date' facts"
    )
    legal_framework: str = Field(
        description=(
            "Legal framework applying to the case: the operative articles ('law_article_operative', "
            "falling back to 'law_article_all_relevant') and the canonical concept ('infringement_concept')"
        )
    )
    infringement_category: str = Field(
        description=(
            "Infringement category of the case, from the 'infringement_category_procedural', "
            "'infringement_category_member_states', 'infringement_category_abuse_of_dominance' and "
            "'infringement_category_hor_vert_lic_joint' facts"
        )
    )
    infringement_sub_category: str = Field(
        description=(
            "Infringement sub-category of the case, from the 'infringement_subcategory_horizontal_restraints' "
            "and 'infringement_subcategory_vertical_restraints' facts"
        )
    )
    infringement_gravity: str = Field(
        description="Gravity of the infringement; MUST be taken verbatim from the 'infringement_gravity' fact if present in the context."
    )
    exemption: str = Field(
        description="Exemption sought or granted, from the 'exemption_case' and 'exemption_applicants' facts. Preserve markdown lists if present."
    )
    leniency: str = Field(
        description=(
            "Leniency: applicants, reductions granted and applications rejected, from the 'leniency_case', "
            "'leniency_applicants', 'leniency_granted' and 'leniency_rejected' facts. Preserve markdown lists and tables if present."
        )
    )
    fines: str = Field(
        description="Fines imposed, from the 'fines' fact. CRITICAL: You must exactly preserve any markdown tables from the context."
    )
    remedies: str = Field(
        description="Remedies and orders to terminate the infringement, from the 'decision_remedies' fact"
    )
    company_recidivism: str = Field(description="Companies with prior infringement history (recidivism). MUST be taken verbatim from the 'company_recidivism' fact if present in the context. ")
    case_commission_info_request: str = Field(description="commission requested info; MUST be taken verbatim from the 'case_commission_info_request' fact if present in the context. ")
    case_commission_no_action: str = Field(description="commission has taken no action; MUST be taken verbatim from the 'case_commission_no_action' fact if present in the context. ")
    case_commission_decision: str = Field(description="commission has taken a decision; MUST be taken verbatim from the 'case_commission_decision' fact if present in the context. ")
    commissioner: str = Field(
        description="Commissioner who signed the decision; MUST be taken verbatim from the 'case_commissioner' fact if present in the context."
    )
    courts: str = Field(
        description="Courts involved and their rulings, from the 'is_court_involved' and 'court_list' facts. Preserve markdown tables if present."
    )
    book_reference: str = Field(
        description=(
            "Treatment of the case in the reference book, from the 'case_is_in_book', "
            "'case_is_landmark_in_book', 'case_infringement_type_book' and 'case_infringement_book' facts"
        )
    )
    outcome: str = Field(description="Outcome of the case")

    @model_validator(mode="before")
    @classmethod
    def _coerce_lists_to_str(cls, values: Any) -> Any:
        """Coerce list values to newline-joined strings and fill omitted chapters.

        The LLM occasionally returns a JSON array for fields that should be
        plain text (e.g. companies_undertakings, key_timeline). This validator
        runs before field-type validation and joins any list with newlines so
        that Pydantic's ``str`` type check always succeeds.

        With 25 chapters the LLM also drops one now and then (observed:
        ``remedies`` on 39824_6567_14 with qwen3-coder:30b), which used to fail
        the whole summary. An omitted chapter becomes ``'NA'`` -- the value the
        signature prescribes for a chapter without facts -- and is logged. The
        fields stay required so the JSON schema shown to the LLM still asks for
        every chapter.
        """
        if not isinstance(values, dict):
            return values
        for key, val in values.items():
            if isinstance(val, list):
                values[key] = "\n".join(str(item) for item in val)
        omitted = [name for name in cls.model_fields if values.get(name) is None]
        if omitted:
            logger.warning("Summary LLM omitted chapters %s; filled with 'NA'.", omitted)
            values = {**values, **dict.fromkeys(omitted, "NA")}
        return values


class CaseSummarySignature(dspy.Signature):
    """
    Summarize a case into specific chapters based on all extracted facts.
    CRITICAL: If the context contains tabular data (Markdown tables) or formatting like bullet lists, you MUST preserve the exact Markdown formatting in your summary chapters. Do NOT flatten or reformat tables into inline text.
    If none of the facts a chapter names is present in the context, write 'NA' for that chapter; do not infer it from other chapters.
    """

    context = dspy.InputField(
        desc="All extracted facts formatted as text to build the summary from."
    )
    case_summary: CaseSummaryChapters = dspy.OutputField(
        desc="Structured case summary divided into chapters."
    )


class FactCaseTitleOutput(BaseModel):
    case_id: str = Field(
        description="The case ID as found in the text, such as 'IV/35814', 'COMP/B-2/37.766' or 'AT.40023'. reply with 'NA' if not found."
    )
    title: str = Field(
        description="The title as found in the text, such as 'Extra alloy' or 'Cross-border access to pay-TV'. reply with 'NA' if not found."
    )


class FactCaseTitleSignature(dspy.Signature):
    """
    Extract the case ID and title exactly as they appear in the provided document chunk.
    Return the entire row that contains the case ID and title.
    """

    context = dspy.InputField(desc="The document text (first page of the case).")
    output: FactCaseTitleOutput = dspy.OutputField(desc="The extracted case title row.")


# --- HeidelTime Event Extraction ---


class HeidelTimeEvent(BaseModel):
    """Structured event entry anchored on a canonical HeidelTime date."""

    event_date: str = Field(
        description="ISO date YYYY-MM-DD matching exactly one of the provided canonical dates."
    )
    actor: str = Field(
        description="Named party or entity involved in the event; use 'redacted' if unknown."
    )
    predicate_object: str = Field(
        description="Short verb phrase describing what happened at this date (e.g., 'sent email attaching presentation')."
    )

    @field_validator("event_date", "actor", "predicate_object", mode="before")
    @classmethod
    def _reject_null_strings(cls, v: object) -> object:
        """Replace null-sentinel strings produced by the LLM with safe defaults."""
        if isinstance(v, str) and v.strip().lower() in _NULL_LIKE_STRINGS:
            return "unknown"
        return v


class HeidelTimeEventResult(BaseModel):
    """Collection of structured events, one per canonical HeidelTime date."""

    events: List[HeidelTimeEvent] = Field(
        description=(
            "One event per provided canonical date. "
            "If no specific activity is found for a date, include it with actor='redacted' "
            "and a neutral predicate_object such as 'no activity described in text'."
        )
    )


class HeidelTimeEventSignature(dspy.Signature):
    """
    For each canonical HeidelTime date found in the document chunk, extract a structured event.
    EVENT is the ISO date. ACTOR is the named party or 'redacted' if unknown.
    PREDICATE OBJECT is a short verb phrase describing what happened (e.g., 'sent email collecting price information').
    Include all provided canonical dates in the output, even if no specific action is described.
    """

    chunk_text: str = dspy.InputField(desc="The raw document chunk text.")
    canonical_dates: str = dspy.InputField(
        desc="Newline-separated ISO dates (YYYY-MM-DD) extracted by HeidelTime from this chunk."
    )
    output: HeidelTimeEventResult = dspy.OutputField(
        desc="One HeidelTimeEvent per canonical date, covering all provided dates."
    )


class HeidelTimeCriticalEvent(BaseModel):
    """Aggregated critical event entry for final timeline reporting."""

    event: str = Field(
        description=(
            "Date or concise label for the critical event. Use the precision the "
            "text actually supports: 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD'. Never "
            "invent a day or month that the document does not state."
        )
    )
    actor: str = Field(
        default="unknown",
        description="Named party, institution, or entity involved in the event.",
    )
    predicate_object: str = Field(
        default="unknown",
        description="Concise phrase describing the critical action and object.",
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_upstream_event_key(cls, values: Any) -> Any:
        """Accept ``event_date`` as an alias for ``event``.

        The aggregation prompt is fed the serialized ``heideltime_event`` fact,
        whose rows are keyed ``event_date``.  The LLM frequently echoes that key
        back instead of the ``event`` this model declares, which fails
        validation and loses the whole document.  Mapping it here is cheaper
        than fighting the prompt, and only fills ``event`` when it is absent so
        a correct response is never rewritten.

        ``actor`` and ``predicate_object`` default rather than being required
        for the same reason.  On 2026-09-02 the model answered event 170 of
        38344_5856_3 with the bare ``{"event_date": "2002-06"}`` -- a real
        year-month event it could not attribute -- and the two missing fields
        failed the whole ``events`` list, discarding the other 170 good events
        and the document with them.  One unattributable event must cost one
        event's worth of detail, not the document.
        """
        if not isinstance(values, dict):
            return values
        if "event" not in values and "event_date" in values:
            values = {**values, "event": values["event_date"]}
        return values

    @field_validator("event", "actor", "predicate_object", mode="before")
    @classmethod
    def _reject_null_strings(cls, v: object) -> object:
        """Replace null-sentinel strings produced by the LLM with safe defaults."""
        if isinstance(v, str) and v.strip().lower() in _NULL_LIKE_STRINGS:
            return "unknown"
        return v


class HeidelTimeCriticalEventResult(BaseModel):
    """Collection of the most critical aggregated HeidelTime events."""

    events: List[HeidelTimeCriticalEvent] = Field(
        description=(
            "Up to 12 deduplicated critical events from the input, always including "
            "the earliest conduct event (e.g. conclusion of the agreement) and the "
            "final outcome."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_event_list(cls, values: Any) -> Any:
        """Wrap a bare list of events into the declared ``events`` envelope.

        The LLM is shown a list-shaped input and often answers with a bare JSON
        array rather than ``{"events": [...]}``.  Pydantic then rejects the
        whole response with "Input should be a valid dictionary", which failed
        one document of the 2026-09-02 aggregation run outright.  Accepting
        both shapes costs nothing and makes the pipeline robust to a
        formatting choice the model is entitled to make.

        Entries that are not event mappings are dropped rather than allowed to
        fail the list.  A long answer occasionally carries one malformed
        element -- ``["event"]``, the bare column name, ended CELEX-31994D0815
        at event 133 and 39226_8310_11 at event 188 on 2026-09-02 -- and a
        single stray token is not worth the other ~190 events.
        """
        if isinstance(values, list):
            values = {"events": values}
        if isinstance(values, dict) and isinstance(values.get("events"), list):
            values = {
                **values,
                "events": [
                    event
                    for event in values["events"]
                    if isinstance(event, (dict, HeidelTimeCriticalEvent))
                ],
            }
        return values


class HeidelTimeEventAggregateSignature(dspy.Signature):
    """
    Analyze the input globally to identify the critical lifecycle events of a case. The selection must span the WHOLE case: from the start of the conduct under assessment to the final outcome. Downstream facts (cartel period, decision date) are read from this selection, so a missing start or end event corrupts them.

    You must strictly follow these rules:
    1. BRACKET THE LIFECYCLE: Scan the entire input first, then always keep
       a. the EARLIEST conduct event: the agreement being concluded, the cartel being formed, the first meeting or first collusive act. In notification or exemption cases this is the conclusion of the notified agreement, plus its notification or registration;
       b. the LAST conduct event: the stated end of the conduct, the last meeting, or the end of an exemption period;
       c. the FINAL OUTCOME: the Commission decision, fines imposed, or a court judgment.
       Never drop an early conduct event in favour of a later procedural step.
    2. LIMIT: Select at most 12 events. When there are more candidates, drop events from the middle of the case first -- never the events of rule 1.
    3. EXCLUSIONS: Skip all trivial statements and events that describe no activity. Ignore the actor "Council of the European Union" and any events regarding law-making or treaty signing.
    4. DATE PRECISION: Report each event at the precision the text supports -- 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD'. Never invent a day or month to complete a date.
    5. ROW SHAPE: Every row must be a complete event object. If the actor or the action is not stated, write 'unknown' in that field; never emit a bare value or a partial row.
    """

    heideltime_event_result: str = dspy.InputField(
        desc="Serialized HeidelTimeEventResult produced by the heideltime_event pipeline."
    )
    
    output: HeidelTimeCriticalEventResult = dspy.OutputField(
        desc=(
            "1 to 12 deduplicated critical events spanning the whole case -- the "
            "earliest conduct event, the last conduct event and the final outcome "
            "are always included -- sorted chronologically."
        )
    )


class HeideltimeCartelPeriodResult(BaseModel):
    """The extracted cartel period date range."""

    date_range: Optional[str] = Field(
        default=None,
        description=(
            "The minimal start and maximal end date as 'START : END'. Give each "
            "side the precision the document states and no more: 'YYYY', "
            "'YYYY-MM' or 'YYYY-MM-DD'. If the decision says only a year, "
            "answer the year -- never invent a day or month to fill the format. "
            "Answer null when no infringement period is stated. Output **only** "
            "the single date range; no text, reasoning, or markdown code blocks."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_start_end_pair(cls, values: Any) -> Any:
        """Assemble ``date_range`` from the start/end pair shapes the LLM invents.

        The model frequently ignores the declared single-string envelope and
        answers with the two bounds as separate keys, optionally nested under a
        ``period`` object and padded with extra keys of its own invention::

            {"period": {"start": "1997-09-05", "end": "2019-07-04"},
             "entities": [...]}

        Nothing in that response is named ``date_range``, so the field fell back
        to its ``None`` default and the whole extraction was recorded as "no
        cartel period found" -- the shape observed for 37956_1607_5 on
        2026-09-05, and a likely contributor to the 33 empty facts in the
        corpus. The bounds are present and usable, so they are joined here
        rather than discarded; ``date_range`` is only filled when absent, so a
        schema-compliant answer is never rewritten.
        """
        if not isinstance(values, dict) or values.get("date_range") is not None:
            return values

        source = values.get("period")
        if not isinstance(source, dict):
            source = values

        start = source.get("start") or source.get("start_date")
        end = source.get("end") or source.get("end_date")
        if isinstance(start, str) and isinstance(end, str):
            values = {
                **values,
                "date_range": f"{start.strip()}{_DATE_RANGE_SEPARATOR}{end.strip()}",
            }
        return values

    @field_validator("date_range", mode="before")
    @classmethod
    def _reject_null_strings(cls, v: object) -> object:
        """Map LLM null-sentinel strings to None so the module can return empty items."""
        if isinstance(v, str) and v.strip().lower() in _NULL_LIKE_STRINGS:
            return None
        return v

    @field_validator("date_range", mode="after")
    @classmethod
    def _reject_unusable_range(cls, v: str | None) -> str | None:
        """Drop ranges that carry no real period, and normalize the separator.

        Three shapes reach this point that are absence rather than data, and
        each is worse stored than missing because downstream period arithmetic
        cannot tell them from a genuine finding:

        * a side that is not a date at all (prose such as "no valid cartel
          period found", which reached the 2026-09-02 corpus as a stored fact);
        * the prompt's own 1900/2030 search bounds echoed back as the answer;
        * a range that ends before it starts.

        A partial-precision side (``1994``, ``1994-03``) is explicitly *not*
        rejected -- that is the honest answer when the decision states only a
        year, and it is preserved verbatim for the module to store.
        """
        if v is None:
            return None

        parts = [part.strip() for part in v.split(":")]
        if len(parts) != 2 or not all(_PARTIAL_ISO_DATE.match(p) for p in parts):
            return None

        start, end = parts
        # Either side is enough to reject: the range is one atomic fact and
        # downstream period arithmetic reads both bounds, so a single
        # fabricated bound ("1989-10-01 : 2030-12-31") yields a 41-year cartel
        # rather than a partially-known one.
        if start in _SENTINEL_PERIOD_DATES or end in _SENTINEL_PERIOD_DATES:
            return None
        # Lexicographic comparison is chronological for zero-padded ISO dates,
        # and a shorter prefix ("1994" vs "1994-03-01") orders before any date
        # inside that year -- which is the wanted answer for a partial bound.
        if start > end and not end.startswith(start) and not start.startswith(end):
            return None

        return f"{start}{_DATE_RANGE_SEPARATOR}{end}"


class HeideltimeCartelPeriodSignature(dspy.Signature):
    """
    Find the start and end of the INFRINGEMENT itself (the cartel period) in the provided event list.

    ### The one mistake to avoid
    The event list is NOT the cartel period. It also contains the investigation and
    the litigation that followed the cartel, which often run for years or decades
    after the collusion stopped. So:

    -   **NEVER take the first event as the start and the last event as the end.**
        Selecting min(date) and max(date) over the list is always wrong.
    -   The cartel END is when the collusive conduct stopped -- typically the last
        cartel meeting, price agreement, or stated participation end. It is
        **always earlier than** the Commission decision, and earlier still than any
        appeal or court judgment.

    ### Extraction Logic
    1.  **Look for keywords:** "Infringement lasting from...", "Collusive conduct period...", "Participation from [Date] to [Date]", "the cartel ended on...".
    2.  **Count only** dates of the violation itself: cartel meetings, price fixing, quota or market allocation, bid rigging, exchange of sensitive information.
    3.  **Ignore entirely** -- these are NOT cartel dates, even when they are the most prominent dates present:
        - Commission steps: inspections/dawn raids, statement of objections, leniency applications, the adoption of the decision, fines imposed or paid.
        - Court steps: appeals, annulments, judgments, re-adopted decisions.
        - Corporate events: mergers, acquisitions, liquidations, ownership or name changes.
    4.  **Date Scope:** Only consider dates between 1900 and 2030. These are search bounds, not answers: never return 1900-01-01 or 2030-12-31 as a start or end date.
    5.  **Precision:** Report each side at the precision the text supports -- 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD'. If the decision says "from 1994 until March 1999", answer '1994 : 1999-03'. Do NOT pad a year out to January 1st or December 31st to satisfy a format.
    6.  **No period:** If the text states no infringement period, answer null. An invented range is worse than no answer, and a range that runs to the decision or judgment date is an invented range.

    ### Self-check before answering
    Ask: "is my end date a cartel activity, or is it the Commission deciding /
    a court ruling / a fine?" If it is the latter, discard it and use the last
    actual collusive event instead.
    """

    heideltime_event_result: str = dspy.InputField(
        desc="Serialized HeidelTimeEventResult produced by the heideltime_event pipeline."
    )

    output: HeideltimeCartelPeriodResult = dspy.OutputField(
        desc=(
            "Extracted cartel period as 'START : END', each side being 'YYYY', "
            "'YYYY-MM' or 'YYYY-MM-DD' at the precision the document states, "
            "or null when no period is stated."
        )
    )


# ---------------------------------------------------------------------------
# Q&A Retrieval Pipeline — Plan → Execute → Synthesize
# ---------------------------------------------------------------------------


# Edge relation that defines co-membership for a graph_search traversal. A
# small, stable semantic vocabulary mirroring celex_facts.edge_relation_enum -
# the exact case a closed Literal is for; it is NOT a fact name.
GraphRelation = Literal[
    "participated_in",
    "fined",
    "leniency_applied",
    "leniency_granted",
    "leniency_rejected",
]


class RetrievalStep(BaseModel):
    """A single step in the retrieval plan produced by the Planner."""

    step_id: int = Field(description="1-based ordinal of this step.")
    tool: str = Field(
        description=(
            "Tool to invoke.  Must be one of: "
            "'kb_sql' (aggregate SQL over facts KB), "
            "'case_facts' (lookup all facts for one celex_id), "
            "'rag_search' (semantic search over fact embeddings across all cases), "
            "'rag_search_in_case' (hybrid doc search scoped to one case), "
            "'doc_search' (semantic search over raw document chunks across all cases — "
            "best for specific entities like commissioner names, countries, towns), "
            "'knowledge_search' (full-text search over distilled knowledge nuggets), "
            "'graph_search' (traverse the entity graph for companies that shared "
            "cases with a seed company — the only tool that walks company-to-company "
            "adjacency)."
        )
    )
    query: str = Field(
        description="The query string or SQL fragment to pass to the tool."
    )
    case_id: str = Field(
        default="",
        description="celex_id to scope the search; required for case_facts and rag_search_in_case, empty otherwise.",
    )
    input_from_step: int | None = Field(
        default=None,
        description=(
            "step_id of an EARLIER step whose retrieved celex_ids feed this step. "
            "Use together with a {celex_ids} placeholder in kb_sql queries "
            "(e.g. \"... AND celex_id IN ({celex_ids})\"), or to fan out "
            "case_facts / rag_search_in_case over each discovered case. "
            "Leave unset for independent steps."
        ),
    )
    graph_seed: str = Field(
        default="",
        description=(
            "For tool='graph_search' only: SHORT company name stem to seed the "
            "traversal, e.g. 'bayer'. Never a full legal name — company aliases "
            "are split across separate graph nodes, so a stem seeds all of them "
            "while a full name seeds one and under-reports. Empty otherwise."
        ),
    )
    graph_relation: GraphRelation = Field(
        default="participated_in",
        description=(
            "For tool='graph_search' only: the edge type that defines "
            "co-membership between two companies."
        ),
    )
    graph_hops: int = Field(
        default=1,
        description=(
            "For tool='graph_search' only: traversal depth. 1 = direct "
            "co-participants (the normal case), 2 = peers of peers. Never above 2."
        ),
    )
    rationale: str = Field(description="Why this step is needed for answering the question.")


class RetrievalPlan(BaseModel):
    """Structured output of the Planner: an ordered list of retrieval steps."""

    question_type: str = Field(
        description=(
            "Classification of the user question.  "
            "One of: 'single_case', 'multi_case_aggregation', 'exploratory'."
        )
    )
    steps: List[RetrievalStep] = Field(
        description="Ordered retrieval steps.  The executor will run them sequentially."
    )


class QAPlannerSignature(dspy.Signature):
    """Decompose a user question about EU competition-law cases into an ordered
    retrieval plan.

    Available tools
    ---------------
    - **kb_sql**: Run a read-only SQL query against the structured facts KB.
      The query targets the table ``celex_facts.facts`` with columns
      (celex_id TEXT, name TEXT, value_md TEXT, value_json JSONB).
      Use this for aggregation / counting / filtering across many cases.
      When the kb_schema lists ``celex_facts.company_case_relations_vw``,
      kb_sql over that view is also the tool for a named company's cases
      (see Rule 4). Do NOT search the raw ``facts`` table for company names
      or other free-text entity values.
    - **case_facts**: Retrieve ALL stored facts for a single case (by celex_id).
    - **rag_search**: Semantic search over fact embeddings across ALL cases.
    - **rag_search_in_case**: Hybrid search scoped to ONE case's document chunks.
    - **doc_search**: Semantic search over raw document chunks across ALL cases.
      BEST for free-text entity lookups that no structured view resolves:
      commissioner names, towns, products, and company names only when the
      kb_schema does not list ``company_case_relations_vw`` or that view
      returned nothing.
    - **knowledge_search**: Full-text search over distilled knowledge nuggets
      (``celex_facts.knowledge_items``) extracted from previous Q&A interactions.
      Each nugget is a natural-language fact about one or more cases.
      SUPPLEMENTARY only — see Rule 0. Knowledge matching a previously answered
      question is surfaced to the synthesizer automatically, so never plan
      knowledge_search as a substitute for kb_sql or case_facts.
    - **graph_search**: Traverse the typed entity graph
      (``celex_facts.entity_edges``) to find companies that shared cases with a
      seed company. Set ``graph_seed`` to a SHORT company stem ('bayer', never
      'Bayer AG'), optionally ``graph_relation`` and ``graph_hops``. This is the
      ONLY tool that walks company-to-company adjacency; the facts table and the
      flat relation views hold no such edge. Do NOT use it for counting,
      grouping, or country filtering — those are kb_sql questions.

    Rules
    -----
    0. KNOWLEDGE RULE: **knowledge_search** is SUPPLEMENTARY. Use it only for
       cross-case questions WITHOUT a celex_id that ask about procedural
       context, legal principles, or analytical synthesis NOT answerable from
       the structured facts table. Any question answerable by SQL over
       ``celex_facts.facts`` (counts, lists, distributions, fact lookups)
       goes to **kb_sql** — never lead an aggregation with knowledge_search.
    1. For macroscopic / aggregation questions (counts, trends, grouping over
       many cases) prefer **kb_sql** first.
    2. SINGLE-CASE RULE (highest priority for case-scoped questions):
       Whenever the user mentions a specific celex_id — e.g. 'CELEX-31991D0562'
       or '38344_5856_3' — the FIRST step MUST be a `case_facts` lookup with
       that celex_id. `case_facts` returns the full fact bag for the case at
       low cost and is robust to vocabulary drift in the question (e.g. the
       user writes "summary", "overview", or "abstract" while the canonical
       fact name is `case_summary`).
       Only add a follow-up narrower step (kb_sql with
       `WHERE name = '<exact_fact_name>'`, or rag_search_in_case) when the
       topic word in the question matches a fact `name` from the schema
       EXACTLY. If you are not sure, omit the narrower step — the synthesizer
       can pick the relevant fact from the bag.
    3. When the celex_id is NOT known but a case number / case name is
       mentioned, use **rag_search** to discover the celex_id, then plan a
       follow-up step against that celex_id.
    4. ENTITY LOOKUP RULE (CRITICAL):
       (a) COMPANY / UNDERTAKING names — when the kb_schema lists
           ``celex_facts.company_case_relations_vw``, use **kb_sql** over that
           view. It is fact-backed and complete for company rosters, fines
           and leniency. Match ONE distinctive whole word of the name with a
           case-insensitive word-boundary regex, never the full legal name
           and never a bare ILIKE substring (a short name such as 'nsk' also
           matches 'Dansk', 'Svenska'). Project source_fact_id. Example —
           "list all cases related to NSK Deutschland GmbH":
             SELECT celex_id, case_name, company_name, relation, source_fact_id
             FROM celex_facts.company_case_relations_vw
             WHERE company_name ~* '[[:<:]]nsk[[:>:]]'
             ORDER BY celex_id, relation
           Without that view in the kb_schema, use **doc_search**.
       (b) Every OTHER free-text entity (commissioner names, countries as
           places, towns, products, any proper noun no view resolves) →
           **doc_search**. Examples:
             - "cases involving Commissioner Karel Van Miert" → doc_search
             - "cartels in the Netherlands" → doc_search
           Do NOT search ``celex_facts.facts`` for these — the names are in
           document chunks.
    5. Always generate valid, read-only SQL (SELECT only, no INSERT/UPDATE/
       DELETE/DROP/ALTER/CREATE/TRUNCATE).
    6. UNSTRUCTURED TOPIC RULE: The structured facts 
       ``court_list`` summarise WHICH courts are involved (names, countries,
       jurisdictions).  However, the actual **rulings, assessments, opinions,
       orders, and judgment content** from those courts exist ONLY in the raw
       document text — NOT in the facts table.
       When the question asks about court rulings, assessments, opinions,
       tribunal decisions, appeals, judgments, annulment actions, dawn raids,
       statements of objections, oral hearings, or settlement procedures:
         - If a celex_id is known, you MUST include BOTH:
           (a) a `case_facts` step (provides context — which courts are
               involved via ``court_list_raw``), AND
           (b) a **rag_search_in_case** step targeting the document chunks
               for that case.  Use a descriptive query such as
               'court ruling assessment judgment order opinion'.
         - If no celex_id is known, use **doc_search** instead.
       CRITICAL: `case_facts` alone is NOT sufficient for ruling/assessment
       questions — it returns only the court *list*, not the ruling *content*.
       Examples:
         - "what was the ruling of courts in CELEX-31982D0123"
           → case_facts (context) + rag_search_in_case (ruling content)
         - "court involvement in 39612_12448_6"
           → case_facts + rag_search_in_case
         - "which cases had appeals" → doc_search with query 'appeal court judgment'
    7. ENUMERATION OF STRUCTURED ENTITIES RULE: For pure enumeration
       questions in a SINGLE case (the user is asking *who/what is in the
       list*, not *what they did/decided/ruled*), the answer is already
       stored as a structured fact in the case's ``case_facts`` bag.  The
       canonical entity → fact-name mapping is:
         - courts / tribunals      → ``court_list_raw`` (markdown table with
                                     court, country, jurisdiction) and
                                     ``court_list`` (compact summary)
         - companies / parties /   → ``companies_investigated``
           undertakings
         - fines / penalties       → ``fines``
         - leniency applicants     → ``leniency_applicants``
         - remedies / commitments  → ``decision_remedies``
       For these enumeration questions a SINGLE ``case_facts`` step is
       sufficient — the synthesizer will extract the relevant structured
       fact (e.g. ``court_list_raw``) from the bag and format it.  Do NOT
       also add ``rag_search_in_case``; the structured facts ARE the
       canonical list, and chunk search adds noise without new content.
       Examples (note: NO rag_search_in_case):
         - "list all courts in CELEX-31991D0562"
           → case_facts (court_list_raw is in the bag)
         - "every party involved in 39226_8310_11"
           → case_facts (companies_investigated is in the bag)
         - "all fines imposed in CELEX-31994D0601"
           → case_facts (fines is in the bag)
       Distinction from Rule 6: this rule is for *who/what* questions
       (enumeration); Rule 6 is for *what did they decide/rule/order*
       questions (content).  If both are asked in the same question, both
       rules compose: case_facts + rag_search_in_case.
    8. MULTI-HOP CHAINING RULE: When the relevant case SET must be discovered
       first (an industry, product, topic, or entity — e.g. "brewery cases",
       "chemical cartels") and then a fact fetched PER member, emit TWO
       chained steps:
         (a) a FINDER step — kb_sql with an ILIKE filter over the
             classification facts ``companies_industry``, ``OECD_sector``,
             ``case_title``, ``products_name`` (or doc_search for free-text
             entities), projecting DISTINCT celex_id;
         (b) a DEPENDENT step with ``input_from_step`` set to the finder's
             step_id and the literal placeholder ``{celex_ids}`` in its SQL:
             ``... AND celex_id IN ({celex_ids})``.  The executor replaces
             the placeholder with the celex_ids the finder retrieved.
       Example — "list all cartel periods of brewery cases":
         Step 1 (kb_sql):
           SELECT DISTINCT celex_id FROM celex_facts.facts
           WHERE name IN ('companies_industry','OECD_sector','case_title',
                          'products_name')
             AND value_md ILIKE '%brew%'
         Step 2 (kb_sql, input_from_step=1):
           SELECT celex_id, name, value_md FROM celex_facts.facts
           WHERE name IN ('cartel_period','heideltime_cartel_period')
             AND celex_id IN ({celex_ids})
       Do NOT dump a fact for ALL cases when the question scopes it to a
       subset — always chain via {celex_ids} instead.
    9. STEM RULE for ILIKE: patterns MUST use the shortest word stem —
       '%brew%' (matches brewing, brewery, brewers), NEVER '%brewery%'.
       Stored classification values use forms like 'brewing', so a
       full-word pattern silently returns zero rows.  Exception: COMPANY
       names use the whole-word regex of Rule 4(a), not a stem.
    10. GRAPH ADJACENCY RULE: For a relationship BETWEEN TWO COMPANIES —
       "which companies were in cartels with Bayer", "who is connected to
       Hoechst through shared cases", "co-participants of X" — use
       **graph_search** with a SHORT ``graph_seed`` stem ('bayer', never
       'Bayer AG'). Neither the facts table nor the flat relation views hold a
       company-to-company edge, so kb_sql cannot answer these at all. Keep
       using kb_sql over the views for counting, grouping, or filtering
       company/case relations by country or LEI.
    """

    question: str = dspy.InputField(desc="The user's natural-language question.")
    kb_schema: str = dspy.InputField(
        desc="Description of the KB schema: tables, columns, value examples."
    )
    plan: RetrievalPlan = dspy.OutputField(
        desc="Ordered retrieval plan with typed steps."
    )


class QASynthesizerSignature(dspy.Signature):
    """Synthesize a final, grounded answer from retrieved evidence.

    CRITICAL for doc_search / rag_search results:
    - EXTRACT AND LIST EVERY celex_id mentioned in the evidence.
    - Do NOT pick just one or filter the results.
    - If the user asked for "all cases" or to "list cases," enumerate ALL
      celex_ids with the entity/company/feature mentioned in each chunk.
    - A celex_id + chunk pair IS sufficient evidence for that case being related.
    - For a general query (no specific case in the question), present the
      answer as a list of celex_ids, and for each celex_id include its
      case title taken from the ``case_title="..."`` field in the evidence.
      Format each entry as ``celex_id — case title``. Omit the title only
      when the evidence carries no ``case_title`` for that celex_id.

    General Rules:
    1. Base your answer ONLY on the provided evidence. Do not hallucinate.
    2. When evidence is insufficient (no celex_ids or no chunk content),
       say so explicitly.
    3. Always cite sources by celex_id or step_id.
    4. For aggregations, show numbers clearly.
    """

    question: str = dspy.InputField(desc="The original user question.")
    evidence: str = dspy.InputField(
        desc="Concatenated evidence collected during plan execution, with step labels."
    )
    answer: str = dspy.OutputField(
        desc="Final answer grounded in the evidence, with inline citations."
    )


class TurnDependency(BaseModel):
    """Whether the latest chat turn depends on the conversation before it.

    'refine_previous' means the turn cannot be understood on its own: it points
    back at entities or a result set the earlier turns established.
    'standalone' means the turn opens a new topic and the conversation before
    it is irrelevant.
    """

    dependency: Literal["standalone", "refine_previous"] = Field(
        description=(
            "'refine_previous' when the turn refers back to the conversation "
            "(anaphora, an implicit subject, or a narrowing of the previous "
            "answer); 'standalone' when it opens a new, self-contained topic."
        )
    )
    standalone_question: str = Field(
        description=(
            "The latest turn rewritten so it can be understood with no "
            "conversation context. Identical to the input question when the "
            "dependency is 'standalone'."
        )
    )
    reason: str = Field(
        default="",
        description="One short sentence justifying the dependency verdict.",
    )


class CondenseFollowupSignature(dspy.Signature):
    """Rewrite the latest user turn as a standalone question.

    The retrieval pipeline embeds the question and plans SQL from it, so a turn
    like "list those cases" is useless on its own: "those" has no referent and
    its embedding has nothing to do with cartels. Resolve the reference against
    the conversation instead.

    Rules:
    1. Resolve anaphora ("those cases", "that company", "them", "the above",
       "the same period") against the earlier turns, substituting the actual
       entity, case, or result set they refer to.
    2. Carry over an implicit subject. "and the fines?" after a question about
       a case becomes "what fines were imposed in <that case>?".
    3. Do NOT answer the question, add facts, or invent case identifiers that
       do not appear in the conversation.
    4. When the turn already stands on its own — a new topic, a different
       company, a fully-specified question — return it UNCHANGED and classify
       it as 'standalone'. Do not fold in earlier context that the user has
       moved on from. A wrongly-carried context is worse than none.
    5. Keep the user's own wording wherever it is already unambiguous; rewrite
       only the parts that depend on the conversation.
    """

    conversation: str = dspy.InputField(
        desc=(
            "Prior turns of the conversation, oldest first, rendered as "
            "'User: ...' / 'Agent: ...' pairs. Agent answers are stripped of "
            "progress status lines and source-provenance blocks."
        )
    )
    question: str = dspy.InputField(desc="The latest user turn, verbatim.")
    result: TurnDependency = dspy.OutputField(
        desc="The dependency verdict and the rewritten standalone question."
    )