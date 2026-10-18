"""DSPy module for resolving corporate registry identifiers."""

import logging
from typing import Any

import dspy

from config.llm_factory import corp_index_profile, create_lm
from pipelines.corp_index.dspy_signature import (
    CorpIndexLookupSignature,
    LlmCompanyIndexSignature,
)
from pipelines.corp_index.models import (
    CorpIndexLookupInput,
    CorpIndexResult,
    LlmCompanyIndexProfile,
    LlmCompanyIndexRow,
    LlmCompanyIndexTarget,
)

logger = logging.getLogger(__name__)


def create_corp_index_lm() -> dspy.LM:
    """Create the language model this package's modules run on.

    Reads the ``corp_index`` profile (``settings.CORP_INDEX_LLM_PROVIDER`` /
    ``settings.CORP_INDEX_LLM_MODEL_ID``), which is configured independently of
    the UI planner's: this stage runs where every register lookup already
    failed, so it is the one place that wants a broad-world-knowledge model,
    and repointing it must not repoint the UI.

    Returns:
        Configured ``dspy.LM`` instance.

    Raises:
        ValueError: If the configured provider or model id is unusable.
    """
    return create_lm(corp_index_profile())


class CorpIndexModule(dspy.Module):
    """Resolve corporate BRIS, EUID, LEI, and state using a DSPy LM.

    Args:
        lm: Provisioned DSPy language model. The default uses the corp_index
            LLM profile.
        predictor: Optional predictor injected for tests.
    """

    lm: dspy.LM
    lookup: Any

    def __init__(self, lm: dspy.LM | None = None, predictor: Any | None = None) -> None:
        """Initialize the corporate identifier lookup module.

        Args:
            lm: Provisioned DSPy language model. If omitted, the corp_index
                LLM profile is used to create one outside ``forward``.
            predictor: Optional DSPy-compatible callable used for tests.

        Raises:
            ValueError: If the settings-based LM cannot be created.
        """
        super().__init__()
        self.lm = lm or create_corp_index_lm()
        self.lookup = predictor or dspy.Predict(CorpIndexLookupSignature)

    def forward(self, corp_canoncical: str) -> dspy.Prediction:
        """Resolve corporate identifiers for a company.

        Args:
            corp_canoncical: Canonical company name to resolve.

        Returns:
            DSPy prediction whose ``output`` is a validated ``CorpIndexResult``.

        Raises:
            ValueError: If input validation or LLM output validation fails.
        """
        lookup_input = CorpIndexLookupInput(
            corp_canoncical=corp_canoncical,
        )

        logger.debug(
            "Starting corporate identifier lookup for company=%s",
            lookup_input.corp_canoncical,
        )

        with dspy.context(lm=self.lm):
            prediction = self.lookup(
                corp_canoncical=lookup_input.corp_canoncical,
            )

        output = getattr(prediction, "output", prediction)
        validated_output = CorpIndexResult.model_validate(output)
        return dspy.Prediction(output=validated_output)


class LlmCompanyIndexModule(dspy.Module):
    """Profile a roster company no register could resolve.

    Runs where the deterministic ladder has already given up: GLEIF, the German
    Handelsregister and OpenCorporates all missed the company, so the only
    remaining source is the model's own knowledge. The answer is therefore an
    assertion, not a resolution, and is persisted with the model id that made it
    (see ``celex_facts.llm_index_companies``).

    Provisioner pattern: the language model is built once in ``__init__`` and
    never inside ``forward``.

    Args:
        lm: Provisioned DSPy language model. The default uses the corp_index
            LLM profile.
        predictor: Optional DSPy-compatible callable injected for tests.
    """

    lm: dspy.LM
    lookup: Any
    llm_model_id: str

    def __init__(
        self,
        lm: dspy.LM | None = None,
        predictor: Any | None = None,
        llm_model_id: str | None = None,
    ) -> None:
        """Initialize the company-profile lookup module.

        Args:
            lm: Provisioned DSPy language model. If omitted, the corp_index
                LLM profile is used to create one outside ``forward``.
            predictor: Optional DSPy-compatible callable used for tests.
            llm_model_id: Model id recorded on every row this module produces.
                Defaults to the corp_index profile's resolved model. Pass it
                explicitly alongside an injected ``lm``, whose model the
                profile settings do not describe.

        Raises:
            ValueError: If the settings-based LM cannot be created.
        """
        super().__init__()
        self.lm = lm or create_corp_index_lm()
        self.lookup = predictor or dspy.Predict(LlmCompanyIndexSignature)
        # Resolved once, from the corp_index profile rather than the shared
        # cloud setting: pointing this stage at another model must relabel its
        # rows, and must not relabel anything the UI planner produced.
        self.llm_model_id = llm_model_id or corp_index_profile().model_id

    def forward(
        self,
        company_name: str,
        address: str | None = None,
        iso_country_code: str | None = None,
        jurisdication_code: str | None = None,
        lei: str | None = None,
        handelsregister: str | None = None,
        use_address: bool = False,
    ) -> dspy.Prediction:
        """Profile one company from its roster name and known attributes.

        Args:
            company_name: Verbatim roster company name; the functional key.
            address: Registered or postal address, or None when unknown.
            iso_country_code: ISO 3166-1 country code, or None when unknown.
            jurisdication_code: Jurisdiction resolved for the company (spelled
                as the roster column is), or None when unresolved.
            lei: LEI already confirmed against GLEIF, or None.
            handelsregister: Handelsregister number already confirmed against
                the German register, or None.
            use_address: Ask about the company by name *and* postal address,
                and hand over the address with its country filled in. For the
                companies whose life cycle and founding year came back unknown,
                where the decision's address is the strongest remaining signal.

        Returns:
            DSPy prediction whose ``output`` is a validated
            :class:`LlmCompanyIndexRow`, ready to persist.

        Raises:
            ValueError: If input validation or LLM output validation fails.
        """
        target = LlmCompanyIndexTarget(
            company_name=company_name,
            address=address,
            iso_country_code=iso_country_code,
            jurisdication_code=jurisdication_code,
            lei=lei,
            handelsregister=handelsregister,
        )

        logger.debug(
            "Starting company index profiling for company=%s (country=%s)",
            target.company_name,
            target.iso_country_code,
        )

        with dspy.context(lm=self.lm):
            prediction = self.lookup(
                # With use_address the question carries the postal address and
                # the hint carries its completed form; without it, both are
                # exactly what the roster stored. The row key below is taken
                # from the target either way, so the question never reaches it.
                company_name=target.query_name(use_address),
                # The target validator has already folded NULL and the roster's
                # 'NA' marker onto None; an empty string is what the signature
                # documents as "unknown".
                address_hint=(
                    target.postal_address() if use_address else target.address or ""
                ),
                country_hint=target.iso_country_code or "",
                jurisdiction_hint=target.jurisdication_code or "",
                lei_hint=target.lei or "",
                handelsregister_hint=target.handelsregister or "",
            )

        output = getattr(prediction, "output", prediction)
        profile = LlmCompanyIndexProfile.model_validate(output)
        # The key and the model id are ours, not the model's: taking the name
        # from the request keeps it joinable to the roster even when the model
        # answers under the canonical name it just identified.
        row = LlmCompanyIndexRow(
            company_name=target.company_name,
            llm_model_id=self.llm_model_id,
            profile=profile,
        )
        return dspy.Prediction(output=row)
