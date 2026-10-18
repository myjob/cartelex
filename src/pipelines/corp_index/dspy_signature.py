"""DSPy signatures for corporate identifier lookup."""

import dspy

from pipelines.corp_index.models import CorpIndexResult, LlmCompanyIndexProfile


class CorpIndexLookupSignature(dspy.Signature):
    """Resolve BRIS, EUID, LEI, and registry state for a company."""

    corp_canoncical = dspy.InputField(
        desc=(
            "The canonical company name to resolve. Preserve the company name in "
            "the output exactly as provided unless registry evidence requires a "
            "minor legal-form spelling correction."
        )
    )
    output: CorpIndexResult = dspy.OutputField(
        desc=(
            "Structured result with records containing corp_canonical, BRIS, "
            "EUID, LEI, SIREN, and life cycle status. Use NA for unavailable identifiers. Preserve "
            "states such as archived, lapsed, dissolved, inactive, or active."
        )
    )


class LlmCompanyIndexSignature(dspy.Signature):
    """Profile a company investigated in an EU competition decision.

    The company failed every register lookup available to the pipeline (GLEIF,
    the German Handelsregister, OpenCorporates), so the answer comes from the
    model's own knowledge. Many of these companies are decades old and no longer
    trade under the name the decision used.

    Resolve such a name forward rather than abandoning it: a cooperative or
    works absorbed into a larger group is still describable through the group
    that runs it today, and "merged into X" is an answer where "unknown" is not.
    """

    company_name = dspy.InputField(
        desc=(
            "The company name exactly as it appears in the competition decision. "
            "It may be historical, abbreviated, misspelled, or carry a "
            "parenthetical alias. Profile the legal entity this name refers to; "
            "do not substitute a better-known company with a similar name. It "
            "may also carry a trailing postal address, appended when the "
            "pipeline could not otherwise place the company: the entity being "
            "asked about is still the company, and the address is there to say "
            "which one."
        )
    )
    address_hint = dspy.InputField(
        desc=(
            "Registered or postal address recorded in the decision, sometimes "
            "completed with the country the roster holds separately, or an "
            "empty string when none is known. Empty means unknown, not 'no "
            "address'. For a decades-old local undertaking that no register "
            "still lists, this is the strongest signal available for working "
            "out which company is meant and what became of it -- but it is the "
            "address the decision printed, so do not repeat it back as the "
            "headquarters unless it genuinely is one."
        )
    )
    country_hint = dspy.InputField(
        desc=(
            "ISO 3166-1 country code recorded for the company, or an empty "
            "string when unknown. Empty means unknown, not stateless."
        )
    )
    jurisdiction_hint = dspy.InputField(
        desc=(
            "Jurisdiction the pipeline resolved from headquarters signals, or an "
            "empty string when unknown. It is a hint about where the company is "
            "based, not evidence of where it is registered -- an offshore or "
            "holding-company registration legitimately disagrees with it."
        )
    )
    lei_hint = dspy.InputField(
        desc=(
            "Legal Entity Identifier already confirmed against the GLEIF "
            "register, or an empty string when none was found. When non-empty it "
            "is authoritative: reproduce it verbatim in the output."
        )
    )
    handelsregister_hint = dspy.InputField(
        desc=(
            "Handelsregister company number already confirmed against the German "
            "commercial register, or an empty string when none was found. When "
            "non-empty it is authoritative: reproduce it verbatim in the output."
        )
    )
    output: LlmCompanyIndexProfile = dspy.OutputField(
        desc=(
            "Structured company profile. Two opposite rules apply to it, and "
            "each field's own description says which one it follows. The "
            "descriptive fields -- canonical_name, abbreviation, legal_form, "
            "established_since, life_cycle_status, succession, "
            "products_services, geo_market, headquarter_address, country, "
            "jurisdiction -- want the best-supported answer you have: a hedge, "
            "an approximate year, or a description by way of the group that "
            "absorbed the company all beat NA, which throws away what you know. "
            "The identifier fields -- lei, euid, bris, handelsregister, siren, "
            "generic_corp_id, generic_corp_registrar -- want "
            "the opposite: report one only when you actually recall it for this "
            "company, never reconstruct a plausible-looking one, and answer "
            "exactly NA otherwise. Any company id or enterprise number the "
            "named fields have no column for belongs in generic_corp_id, with "
            "the registrar that issued it in generic_corp_registrar. Preserve "
            "life cycle wording such as active, merged, acquired, renamed, "
            "dissolved, or liquidated."
        )
    )
