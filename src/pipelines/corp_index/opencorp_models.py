"""Pydantic boundary models for the OpenCorporates REST lookup.

These models validate the raw JSON returned by the OpenCorporates
``/companies/search`` and ``/companies/{jurisdiction}/{number}`` endpoints.
They are deliberately lenient: unknown or extra keys in the API payload are
ignored, and absent scalar values are represented as ``None``. They are kept
separate from the DSPy ``models.py`` in this package because they guard a
different system boundary (a REST API rather than an LLM prediction).
"""

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _empty_string_to_none(value: Any) -> Any:
    """Coerce empty or whitespace-only strings to ``None``.

    OpenCorporates returns ``null`` for absent dates, but some upstream
    registries surface an empty string instead. This normalizes both to
    ``None`` so date parsing does not fail at the boundary.

    Args:
        value: Raw value from the API payload.

    Returns:
        ``None`` when the value is an empty or whitespace-only string,
        otherwise the value unchanged.
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


class OpenCorpCompany(BaseModel):
    """A single company hit from an OpenCorporates search response.

    Args:
        name: Registered company name.
        company_number: Registry-assigned company number.
        jurisdiction_code: OpenCorporates jurisdiction code (e.g. ``ch``).
        incorporation_date: Date of incorporation, when known.
        dissolution_date: Date of dissolution, when known.
        company_type: Registry company-type label, when known.
        registry_url: Link to the source registry entry, when known.
        inactive: Whether the company is flagged inactive, when known.
        current_status: Registry status label, when known.
        opencorporates_url: Canonical OpenCorporates URL, when known.
        previous_names: Prior registered names as raw JSON objects.
    """

    model_config = ConfigDict(extra="ignore")

    name: str = Field(description="Registered company name.")
    company_number: str = Field(description="Registry-assigned company number.")
    jurisdiction_code: str = Field(
        description="OpenCorporates jurisdiction code, such as 'ch'."
    )
    incorporation_date: date | None = Field(
        default=None, description="Date of incorporation, when known."
    )
    dissolution_date: date | None = Field(
        default=None, description="Date of dissolution, when known."
    )
    company_type: str | None = Field(
        default=None, description="Registry company-type label, when known."
    )
    registry_url: str | None = Field(
        default=None, description="Link to the source registry entry, when known."
    )
    inactive: bool | None = Field(
        default=None, description="Whether the company is flagged inactive."
    )
    current_status: str | None = Field(
        default=None, description="Registry status label, when known."
    )
    opencorporates_url: str | None = Field(
        default=None, description="Canonical OpenCorporates URL, when known."
    )
    previous_names: list[dict] = Field(
        default_factory=list,
        description="Prior registered names as raw JSON objects.",
    )
    # The search response has always carried these three; extra="ignore" was
    # silently discarding them. They are what lets the resolution ladder judge a
    # hit from an unfiltered search without spending a second API call on
    # /companies/{jurisdiction}/{number}. Fill rates measured 2026-07-31: the
    # address is present on 29/30 'gb' and 28/30 'ch' hits but only 12/30 'de'
    # and 3/30 'us_de'; industry_codes is empty outside 'gb'. So neither may be
    # required -- they rank a hit, never gate it.
    registered_address: dict | None = Field(
        default=None,
        description="Structured registered address, when the registry gives one.",
    )
    registered_address_in_full: str | None = Field(
        default=None, description="Registered address as one formatted string."
    )
    industry_codes: list[dict] = Field(
        default_factory=list,
        description="Registry industry classifications as raw JSON objects.",
    )

    @field_validator("incorporation_date", "dissolution_date", mode="before")
    @classmethod
    def normalize_optional_date(cls, value: Any) -> Any:
        """Normalize empty-string dates to ``None`` before parsing.

        Args:
            value: Raw date value from the API payload.

        Returns:
            ``None`` for empty strings, otherwise the value unchanged.
        """
        return _empty_string_to_none(value)


class OpenCorpSearchResult(BaseModel):
    """The result of a company-name search, cached or freshly fetched.

    Args:
        query_text: The raw query string supplied by the caller.
        normalized_query: Casefolded, whitespace-collapsed cache key.
        jurisdiction_code: Jurisdiction filter applied, or ``None`` when
            the search spanned all jurisdictions.
        fetched_pages: Number of result pages retrieved from the API.
        total_pages: Total pages reported by the API, when known.
        total_count: Total matching companies reported by the API, when known.
        companies: The company hits collected across all fetched pages.
        from_cache: Whether the result was served from the local cache.
    """

    query_text: str = Field(description="The raw query string supplied by the caller.")
    normalized_query: str = Field(
        description="Casefolded, whitespace-collapsed cache key."
    )
    jurisdiction_code: str | None = Field(
        default=None,
        description="Jurisdiction filter applied, or None for all jurisdictions.",
    )
    fetched_pages: int = Field(
        description="Number of result pages retrieved from the API."
    )
    total_pages: int | None = Field(
        default=None, description="Total pages reported by the API, when known."
    )
    total_count: int | None = Field(
        default=None,
        description="Total matching companies reported by the API, when known.",
    )
    companies: list[OpenCorpCompany] = Field(
        default_factory=list,
        description="Company hits collected across all fetched pages.",
    )
    from_cache: bool = Field(
        default=False,
        description="Whether the result was served from the local cache.",
    )


class OpenCorpCompanyDetail(BaseModel):
    """Full company record from the OpenCorporates detail endpoint.

    Carries the same scalar fields as :class:`OpenCorpCompany` plus the
    detail-only scalars and one field per nested section. Nested sections are
    stored as raw JSON (``list``/``dict``) because their internal shapes vary
    by jurisdiction and are not needed structurally by callers.

    Args:
        name: Registered company name.
        company_number: Registry-assigned company number.
        jurisdiction_code: OpenCorporates jurisdiction code.
        incorporation_date: Date of incorporation, when known.
        dissolution_date: Date of dissolution, when known.
        company_type: Registry company-type label, when known.
        registry_url: Link to the source registry entry, when known.
        inactive: Whether the company is flagged inactive, when known.
        current_status: Registry status label, when known.
        opencorporates_url: Canonical OpenCorporates URL, when known.
        agent_name: Registered agent name, when known.
        agent_address: Registered agent address, when known.
        number_of_employees: Reported employee count, when known.
        native_company_number: Registry-native company number, when known.
        registered_address_in_full: Flattened registered address, when known.
        previous_names: Prior registered names section.
        alternative_names: Alternative names section.
        identifiers: External identifiers section.
        industry_codes: Industry classification codes section.
        officers: Officers section.
        filings: Filings section.
        financial_summary: Financial summary section.
        corporate_groupings: Corporate groupings section.
        ultimate_beneficial_owners: Ultimate beneficial owners section.
        registered_address: Structured registered address section.
        source: Source-registry provenance section.
        from_cache: Whether the record was served from the local cache.
    """

    model_config = ConfigDict(extra="ignore")

    name: str = Field(description="Registered company name.")
    company_number: str = Field(description="Registry-assigned company number.")
    jurisdiction_code: str = Field(
        description="OpenCorporates jurisdiction code, such as 'gb'."
    )
    incorporation_date: date | None = Field(
        default=None, description="Date of incorporation, when known."
    )
    dissolution_date: date | None = Field(
        default=None, description="Date of dissolution, when known."
    )
    company_type: str | None = Field(
        default=None, description="Registry company-type label, when known."
    )
    registry_url: str | None = Field(
        default=None, description="Link to the source registry entry, when known."
    )
    inactive: bool | None = Field(
        default=None, description="Whether the company is flagged inactive."
    )
    current_status: str | None = Field(
        default=None, description="Registry status label, when known."
    )
    opencorporates_url: str | None = Field(
        default=None, description="Canonical OpenCorporates URL, when known."
    )
    agent_name: str | None = Field(
        default=None, description="Registered agent name, when known."
    )
    agent_address: str | None = Field(
        default=None, description="Registered agent address, when known."
    )
    number_of_employees: int | str | None = Field(
        default=None, description="Reported employee count, when known."
    )
    native_company_number: str | None = Field(
        default=None, description="Registry-native company number, when known."
    )
    registered_address_in_full: str | None = Field(
        default=None, description="Flattened registered address, when known."
    )
    previous_names: list[dict] | dict | None = Field(
        default=None, description="Prior registered names section."
    )
    alternative_names: list[dict] | dict | None = Field(
        default=None, description="Alternative names section."
    )
    identifiers: list[dict] | dict | None = Field(
        default=None, description="External identifiers section."
    )
    industry_codes: list[dict] | dict | None = Field(
        default=None, description="Industry classification codes section."
    )
    officers: list[dict] | dict | None = Field(
        default=None, description="Officers section."
    )
    filings: list[dict] | dict | None = Field(
        default=None, description="Filings section."
    )
    financial_summary: list[dict] | dict | None = Field(
        default=None, description="Financial summary section."
    )
    corporate_groupings: list[dict] | dict | None = Field(
        default=None, description="Corporate groupings section."
    )
    ultimate_beneficial_owners: list[dict] | dict | None = Field(
        default=None, description="Ultimate beneficial owners section."
    )
    registered_address: list[dict] | dict | None = Field(
        default=None, description="Structured registered address section."
    )
    source: list[dict] | dict | None = Field(
        default=None, description="Source-registry provenance section."
    )
    from_cache: bool = Field(
        default=False,
        description="Whether the record was served from the local cache.",
    )

    @field_validator("incorporation_date", "dissolution_date", mode="before")
    @classmethod
    def normalize_optional_date(cls, value: Any) -> Any:
        """Normalize empty-string dates to ``None`` before parsing.

        Args:
            value: Raw date value from the API payload.

        Returns:
            ``None`` for empty strings, otherwise the value unchanged.
        """
        return _empty_string_to_none(value)
