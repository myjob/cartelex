"""Pydantic models for corporate identifier lookup."""

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from tools.country_normalizer import ends_with_country, to_country_name


_NA_VALUE = "NA"

# Two rules, not one. Everything in a company profile falls into one of two
# classes, and the recall/precision trade-off runs in opposite directions:
#
#   * A descriptive field is prose. A hedged, approximate or successor-based
#     answer is still worth having, and 'NA' throws away knowledge the model
#     demonstrably holds -- which is exactly what a single blanket "when unsure,
#     answer NA" instruction was producing.
#   * An identifier is a key. A wrong one is worse than none at all, because it
#     is indistinguishable downstream from a register match, so a reconstructed
#     identifier is never acceptable and 'NA' genuinely is the right answer.
#
# The rules live on the fields rather than only in the signature's output
# description because the DSPy JSON adapter renders this model's schema --
# including each field's description -- into the prompt, so per-field guidance
# arrives where the field is actually filled in.
_DESCRIPTIVE_RULE = (
    "Give the best-supported answer you have; hedged or approximate wording "
    "('probably', 'by 1982 part of ...') is a better answer than NA. Use NA "
    "only when you cannot place this company at all."
)
_IDENTIFIER_RULE = (
    "Report this identifier only when you actually recall it for this company. "
    "Never derive, pattern-fill or reconstruct one from the company name, the "
    "country, or another identifier. NA is the correct answer otherwise."
)

_MISSING_IDENTIFIER_VALUES = frozenset(
    {
        "",
        "n/a",
        "na",
        "none",
        "null",
        "not available",
        "not found",
        "unknown",
    }
)


def normalize_missing_to_na(value: Any) -> str:
    """Fold every spelling of "the model does not know" onto ``NA``.

    A model asked for an identifier it cannot supply answers with an empty
    string, ``null``, ``"unknown"`` or ``"not found"`` depending on the day.
    Downstream SQL has to distinguish "absent" from "present" with one
    predicate, so all of them collapse to a single sentinel here, at the output
    boundary.

    Args:
        value: Raw field value from the LLM.

    Returns:
        The trimmed text, or ``NA`` when the value carries no information.
    """
    if value is None:
        return _NA_VALUE

    text_value = str(value).strip()
    if text_value.lower() in _MISSING_IDENTIFIER_VALUES:
        return _NA_VALUE
    return text_value


class CorpIndexLookupInput(BaseModel):
    """Input boundary for a corporate identifier lookup.

    Args:
        corp_canoncical: Canonical company name to resolve.

    Raises:
        ValueError: If the company field is empty after trimming whitespace.
    """

    corp_canoncical: str = Field(description="Canonical company name to resolve.")

    @field_validator("corp_canoncical")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        """Return trimmed text when present.

        Args:
            value: User-provided text field.

        Returns:
            Trimmed non-empty text.

        Raises:
            ValueError: If the text is empty after trimming.
        """
        trimmed_value = value.strip()
        if not trimmed_value:
            raise ValueError("Lookup input fields must not be empty.")
        return trimmed_value


class CorpIndexRecord(BaseModel):
    """Resolved corporate identifiers for one company.

    Args:
        corp_canoncical: Canonical company name that was resolved.
        native_company_number: National registry number, or ``NA``.
        BRIS: Business Registers Interconnection System identifier or ``NA``.
        EUID: European Unique Identifier or ``NA``.
        LEI: Legal Entity Identifier or ``NA``.
        SIREN: French SIREN identifier, or ``NA``.
        SIRET: French SIRET identifier, or ``NA``.
        description: Free-text notes about the company, or ``NA``.
        state: Registry state, such as active, archived, or lapsed.
    """

    corp_canoncical: str = Field(description="Canonical company name that was resolved.")
    native_company_number: str = Field(
        default=_NA_VALUE,
        description="Native company number issued by the registrar of the country, or NA when unavailable.",
    )

    BRIS: str = Field(
        default=_NA_VALUE, description="BRIS identifier, or NA when unavailable."
    )
    EUID: str = Field(
        default=_NA_VALUE, description="EUID identifier, or NA when unavailable."
    )
    LEI: str = Field(
        default=_NA_VALUE, description="LEI identifier, or NA when unavailable."
    )
    SIREN: str = Field(
        default=_NA_VALUE, description="SIREN identifier, or NA when unavailable."
    )
    SIRET: str = Field(
        default=_NA_VALUE, description="SIRET identifier, or NA when unavailable."
    )

    description: str = Field(
        default=_NA_VALUE, description="Notes about the company, or NA when unavailable."
    )

    state: str = Field(description="Company registry state, such as active or lapsed.")

    @field_validator("corp_canoncical", "state")
    @classmethod
    def validate_required_record_text(cls, value: str) -> str:
        """Return trimmed required record text.

        Args:
            value: Record text field provided by the LLM.

        Returns:
            Trimmed non-empty text.

        Raises:
            ValueError: If the text is empty after trimming.
        """
        trimmed_value = value.strip()
        if not trimmed_value:
            raise ValueError("Company name and state must not be empty.")
        return trimmed_value

    @field_validator(
        "native_company_number",
        "BRIS",
        "EUID",
        "LEI",
        "SIREN",
        "SIRET",
        "description",
        mode="before",
    )
    @classmethod
    def normalize_identifier(cls, value: Any) -> str:
        """Normalize absent identifier values to ``NA``.

        Args:
            value: Raw identifier value from the LLM.

        Returns:
            A trimmed identifier string, or ``NA`` when the value is absent.
        """
        return normalize_missing_to_na(value)


class CorpIndexResult(BaseModel):
    """Structured corporate identifier lookup result.

    Args:
        records: Resolved corporate identifier rows.

    Raises:
        ValueError: If the result contains no records.
    """

    records: list[CorpIndexRecord] = Field(
        description="Resolved corporate identifier records."
    )

    @model_validator(mode="after")
    def validate_records_present(self) -> "CorpIndexResult":
        """Ensure at least one lookup record is present.

        Returns:
            The validated result.

        Raises:
            ValueError: If no records are present.
        """
        if not self.records:
            raise ValueError("Corporate identifier lookup returned no records.")
        return self


class LlmCompanyIndexTarget(BaseModel):
    """One roster company awaiting an LLM company-index profile.

    Carries the company name together with whatever the roster already knows
    about it. Every enrichment column of
    ``company_investigated2lei2corpidx2hr_vw`` is tri-state -- NULL means "not
    processed", ``'NA'`` means "processed, no match", a value means "matched" --
    so both NULL and ``'NA'`` are folded to ``None`` on the way in. Handing
    ``'NA'`` to the model verbatim would read as a claim about the company
    rather than as the absence of one.

    Args:
        company_name: Verbatim roster company name; the functional key.
        address: Registered or postal address, or None when unknown.
        iso_country_code: ISO 3166-1 country code, or None when unknown.
        jurisdication_code: Jurisdiction the roster resolved (spelled as the
            roster column is), or None when unresolved.
        lei: Legal Entity Identifier already resolved against GLEIF, or None.
        handelsregister: German commercial register number, or None.

    Raises:
        ValueError: If the company name is empty after trimming whitespace.
    """

    model_config = {"frozen": True}

    company_name: str = Field(description="Verbatim roster company name.")
    address: str | None = Field(
        default=None, description="Registered/postal address, or None when unknown."
    )
    iso_country_code: str | None = Field(
        default=None, description="ISO 3166-1 country code, or None when unknown."
    )
    jurisdication_code: str | None = Field(
        default=None, description="Resolved jurisdiction code, or None when unknown."
    )
    lei: str | None = Field(
        default=None, description="GLEIF-resolved LEI, or None when unresolved."
    )
    handelsregister: str | None = Field(
        default=None,
        description="Handelsregister company number, or None when unresolved.",
    )

    @field_validator("company_name")
    @classmethod
    def validate_company_name(cls, value: str) -> str:
        """Return the trimmed company name.

        Args:
            value: Roster company name.

        Returns:
            The trimmed name.

        Raises:
            ValueError: If the name is empty after trimming.
        """
        trimmed_value = value.strip()
        if not trimmed_value:
            raise ValueError("Company name must not be empty.")
        return trimmed_value

    @field_validator(
        "address",
        "iso_country_code",
        "jurisdication_code",
        "lei",
        "handelsregister",
        mode="before",
    )
    @classmethod
    def normalize_absent_hint(cls, value: Any) -> str | None:
        """Fold roster placeholders to ``None`` so they are never sent as facts.

        Args:
            value: Raw enrichment value read from the roster view.

        Returns:
            The trimmed value, or None when the roster holds no information.
        """
        if value is None:
            return None

        text_value = str(value).strip()
        if text_value.lower() in _MISSING_IDENTIFIER_VALUES:
            return None
        return text_value

    def postal_address(self) -> str:
        """Return the decision's address, completed with its country.

        The roster splits the two: ``address`` holds what the decision printed
        (``Av. Emile Digneffe 26, 4000 Liege``), and the country lives in
        ``iso_country_code``. A model asked to place a company that no register
        holds is better served by one complete postal address than by a street
        and a two-letter code in different fields.

        An address that already ends in a country keeps it, rather than
        acquiring a second: the roster contains rows whose code and address
        disagree, and the address is the more specific claim.

        Returns:
            The address with its country appended where one is missing and
            resolvable, the address unchanged otherwise, or an empty string
            when the roster recorded no address.
        """
        if not self.address:
            return ""
        if ends_with_country(self.address):
            return self.address

        country_name = to_country_name(self.iso_country_code or "")
        if not country_name:
            return self.address
        return f"{self.address}, {country_name}"

    def query_name(self, use_address: bool = False) -> str:
        """Return the name to ask the model about.

        With ``use_address`` the postal address is folded into the question, so
        the model is asked the way a person would ask it -- name and address in
        one breath -- rather than being handed the address in a side field it is
        told to use sparingly.

        This is the *prompt* only. The stored row is keyed by
        ``company_name``, taken straight from the target, so a composed question
        cannot fork the table.

        Args:
            use_address: Whether to fold the postal address into the question.

        Returns:
            The company name, optionally followed by its postal address.
        """
        if not use_address:
            return self.company_name

        postal_address = self.postal_address()
        if not postal_address:
            return self.company_name
        return f"{self.company_name}, {postal_address}"


class LlmCompanyIndexProfile(BaseModel):
    """Company profile as asserted by the LLM.

    The LLM-facing half of the result: every field is model-derived, so the
    signature's output slot references this model and not
    :class:`LlmCompanyIndexRow`. Keeping ``company_name`` and ``llm_model_id``
    out of it is deliberate -- neither is the model's to answer, and a field the
    model can see is a field it can invent a value for.

    Every field defaults to ``NA`` and is normalized through
    :func:`normalize_missing_to_na`, so a model that omits a field entirely
    produces the same row as one that answers "unknown".

    Field order is the order the adapter emits JSON keys in, so the descriptive
    fields come first and the identifiers last: what the model recalls about the
    company is written down before it is asked to commit to a key.

    Args:
        canonical_name: Full legal name as registered.
        abbreviation: Short form or trading abbreviation.
        legal_form: Legal form, e.g. GmbH, SpA, S.A., Ltd.
        established_since: Year or date the company was founded.
        life_cycle_status: Whether the company is active, merged, dissolved.
        succession: What became of the company, and who holds it now.
        products_services: Products or services the company offers.
        geo_market: Geographic market served.
        headquarter_address: Headquarters address.
        country: Country of the headquarters.
        jurisdiction: Jurisdiction of registration.
        lei: Legal Entity Identifier, or ``NA``.
        euid: European Unique Identifier, or ``NA``.
        bris: Business Registers Interconnection System id, or ``NA``.
        handelsregister: German commercial register number, or ``NA``.
        siren: French SIREN number, or ``NA``.
        generic_corp_id: Any company id the other fields have no column for.
        generic_corp_registrar: The registrar that issued ``generic_corp_id``.
    """

    canonical_name: str = Field(
        default=_NA_VALUE,
        description="Full legal name as registered. " + _DESCRIPTIVE_RULE,
    )
    abbreviation: str = Field(
        default=_NA_VALUE,
        description="Short form or trading abbreviation. " + _DESCRIPTIVE_RULE,
    )
    legal_form: str = Field(
        default=_NA_VALUE,
        description="Legal form, e.g. GmbH, SpA, S.A., Ltd, Cooperatie. "
        + _DESCRIPTIVE_RULE,
    )
    established_since: str = Field(
        default=_NA_VALUE,
        description="Year the company was founded or incorporated, as a plain "
        "year (1918) when only the year is known, or a date when it is known "
        "exactly. An approximate or circa year is worth more than NA. "
        + _DESCRIPTIVE_RULE,
    )
    life_cycle_status: str = Field(
        default=_NA_VALUE,
        description="Life cycle status: active, merged, acquired, renamed, "
        "dissolved, or liquidated. A company absorbed into a larger group is "
        "merged or acquired, not unknown -- say so here and name the acquirer "
        "in succession. " + _DESCRIPTIVE_RULE,
    )
    succession: str = Field(
        default=_NA_VALUE,
        description="What became of this legal entity and who holds it now: the "
        "acquirer, merger partner, renaming or successor, with the event that "
        "produced it where you know it (e.g. 'absorbed into DOMO in 1982, part "
        "of FrieslandCampina since 2008'). NA when the company still trades "
        "under its own name and never changed hands. " + _DESCRIPTIVE_RULE,
    )
    products_services: str = Field(
        default=_NA_VALUE,
        description="Products or services the company offers. " + _DESCRIPTIVE_RULE,
    )
    geo_market: str = Field(
        default=_NA_VALUE,
        description="Geographic market the company serves. " + _DESCRIPTIVE_RULE,
    )
    headquarter_address: str = Field(
        default=_NA_VALUE,
        description="Headquarters address; the town and country alone are worth "
        "more than NA when the street is unknown. " + _DESCRIPTIVE_RULE,
    )
    country: str = Field(
        default=_NA_VALUE,
        description="Country of the headquarters. " + _DESCRIPTIVE_RULE,
    )
    jurisdiction: str = Field(
        default=_NA_VALUE,
        description="Jurisdiction the company is registered in. " + _DESCRIPTIVE_RULE,
    )
    lei: str = Field(
        default=_NA_VALUE, description="Legal Entity Identifier. " + _IDENTIFIER_RULE
    )
    euid: str = Field(
        default=_NA_VALUE,
        description="European Unique Identifier. " + _IDENTIFIER_RULE,
    )
    bris: str = Field(
        default=_NA_VALUE,
        description="Business Registers Interconnection System id. "
        + _IDENTIFIER_RULE,
    )
    handelsregister: str = Field(
        default=_NA_VALUE,
        description="German Handelsregister number, e.g. HRB 6750. "
        + _IDENTIFIER_RULE,
    )
    siren: str = Field(
        default=_NA_VALUE,
        description="French SIREN number, nine digits. " + _IDENTIFIER_RULE,
    )
    generic_corp_id: str = Field(
        default=_NA_VALUE,
        description="Any company id or enterprise number you hold for this "
        "company that the fields above have no column for, exactly as its "
        "registrar issues it (e.g. the Belgian CBE enterprise number "
        "BE 0402.398.659, a Dutch KVK number, an EU VAT number, a Companies "
        "House, RCS or CVR number). " + _IDENTIFIER_RULE,
    )
    generic_corp_registrar: str = Field(
        default=_NA_VALUE,
        description="The registrar or scheme that issued generic_corp_id, e.g. "
        "Crossroads Bank for Enterprises, KVK Handelsregister, Companies "
        "House, RCS, VAT, LEI, BRIS. Answer it whenever generic_corp_id is "
        "answered -- a "
        "number without its registrar cannot be interpreted or verified -- and "
        "NA when it is NA. " + _IDENTIFIER_RULE,
    )

    @field_validator("*", mode="before")
    @classmethod
    def normalize_absent_field(cls, value: Any) -> str:
        """Normalize every absent profile field to ``NA``.

        Args:
            value: Raw field value from the LLM.

        Returns:
            The trimmed value, or ``NA`` when it carries no information.
        """
        return normalize_missing_to_na(value)


class LlmCompanyIndexRow(BaseModel):
    """One persistable ``celex_facts.llm_index_companies`` row.

    Composition rather than inheritance from :class:`LlmCompanyIndexProfile`:
    the profile's ``NA`` fold applies to every one of its fields, and folding an
    empty company name to ``"NA"`` would silently write a row under a sentinel
    key instead of raising.

    Args:
        company_name: The roster name that was looked up; the functional key.
            Taken from the request, never from the model's answer, so the key
            cannot drift from what was asked.
        llm_model_id: Model that produced the profile
            (the resolved ``corp_index`` LLM profile). Recorded per row so a stored
            answer stays attributable when the configured model changes.
        profile: The model's answer.

    Raises:
        ValueError: If the company name or model id is empty after trimming.
    """

    model_config = {"frozen": True}

    company_name: str = Field(description="Roster company name; the functional key.")
    llm_model_id: str = Field(description="Model id that produced the profile.")
    profile: LlmCompanyIndexProfile = Field(description="The model's answer.")

    @field_validator("company_name", "llm_model_id")
    @classmethod
    def validate_required_key(cls, value: str) -> str:
        """Return trimmed non-empty key text.

        Args:
            value: Company name or model id.

        Returns:
            The trimmed value.

        Raises:
            ValueError: If the value is empty after trimming.
        """
        trimmed_value = value.strip()
        if not trimmed_value:
            raise ValueError("Company name and LLM model id must not be empty.")
        return trimmed_value

    def to_columns(self) -> dict[str, str]:
        """Flatten the row into its database columns.

        Returns:
            A mapping of ``llm_index_companies`` column names to values, with
            the profile's fields lifted alongside the key and the model id.
        """
        return {
            "company_name": self.company_name,
            "llm_model_id": self.llm_model_id,
            **self.profile.model_dump(),
        }
