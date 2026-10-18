"""Progressive relaxation and hit scoring for OpenCorporates company lookups.

A single-shot search closes a roster row permanently: a clean HTTP 200 with zero
results is written as ``fk_opencorp_idx = 'NA'`` and the row never comes back.
Measurement over 3,548 such rows showed that only a minority are companies
genuinely absent from every registry. The rest fail for two recoverable reasons.

**The name.** OpenCorporates matches ``q`` against the registered name with AND
semantics over tokens (confirmed live 2026-07-31: under ``ae_du`` the full name,
``United Shipping`` with two interior tokens deleted, and ``Shipping United``
reversed all return the same company, while appending a nonsense token returns
none). Every junk token the extractor left in a roster name is therefore an extra
term the registry must also satisfy. Deleting tokens can only broaden.

**The jurisdiction.** ``jurisdication_code`` is derived from headquarters signals
but OpenCorporates indexes by *registration*. Halkis is headquartered in Athens
and stored as ``GR``, yet ``jurisdiction_code=gr`` returns nothing yet
``jurisdiction_code=cy`` returns ``HALKIS CEMENT TRANSPORTERS CO. LIMITED``.
For an offshore or holding-company registration the filter names a jurisdiction
the company cannot be in, and no name relaxation recovers it.

This module supplies the two halves of the answer: :func:`build_ladder` orders
the attempts from most to least constrained, and :func:`rank_and_filter` decides
which hits of a relaxed attempt are actually this company. Scoring is what makes
dropping the jurisdiction filter safe, so the two are not separable.

Deliberately free of I/O: no client, no repository, no session. The batch owns
the loop and the quota; everything here is a pure function of a target and the
hits it was given, which is what makes it testable without mocks.
"""

import logging
import re
from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from config.settings import settings
from pipelines.corp_index.jurisdiction_resolver import fold_match_key
from pipelines.corp_index.opencorp_models import OpenCorpCompany

logger = logging.getLogger(__name__)

# Rung identifiers. Persisted verbatim as ``resolution_rung`` on the link row, so
# a link stays explainable long after the run: a match found by RUNG_VERBATIM
# under the roster's own jurisdiction is a far stronger claim than one found by
# RUNG_CORE_ANY, and only the slug records which happened.
RUNG_VERBATIM = "verbatim"
RUNG_VERBATIM_ANY = "verbatim@any"
RUNG_SPLIT = "split"
RUNG_NO_PAREN = "no-paren"
RUNG_NO_ROLE = "no-role"
RUNG_CORE = "core"
RUNG_CORE_COUNTRY = "core@country"
RUNG_CORE_ANY = "core@any"

# Trailing legal-form tokens, folded. Ordered longest-first is unnecessary --
# they are stripped one token at a time from the end -- but the set must stay
# folded, since `fold_match_key` has already removed the dots from 'S.p.A.'.
_LEGAL_FORM_TOKENS = frozenset({
    "ab", "ae", "ag", "apb", "aps", "as", "asa", "bv", "bvba", "cv", "cie",
    "co", "corp", "corporation", "epe", "gmbh", "inc", "incorporated", "kg",
    "kgaa", "kft", "kk", "lda", "limited", "llc", "ltd", "ltda", "nv", "ooo",
    "oy", "oyj", "plc", "pvba", "sa", "sarl", "sas", "sca", "se", "sl", "snc",
    "spa", "sprl", "srl", "vof", "zao",
})

# Connectives that may sit between legal-form tokens ("GmbH & Co. KG") and must
# be strippable alongside them, but are never a legal form on their own.
_LEGAL_FORM_CONNECTIVES = frozenset({"and", "et", "und"})

_PARENTHETICAL_PATTERN = re.compile(r"\([^()]*\)")
_WHITESPACE_PATTERN = re.compile(r"\s+")
# Splits a multi-company roster value. Commas and the conjunctions that join the
# final member; deliberately not '&', which is far more often part of one name
# ("Bischof + Klein", "GmbH & Co. KG") than a join between two.
_MULTI_COMPANY_PATTERN = re.compile(r",|\band\b|\bund\b|\ben\b", re.IGNORECASE)

# Address tokens shorter than this carry no locating power ('de', 'rue', 'st').
_MIN_ADDRESS_TOKEN_LENGTH = 4

# Shortest token that can carry a company's actual identity. Used to reject a
# split segment made entirely of legal forms and particles.
_MIN_SUBSTANTIVE_TOKEN_LENGTH = 4

# Score awarded for each name-match tier. Exact and prefix both clear the default
# OPENCORP_MIN_MATCH_SCORE; bare containment deliberately does not, because on an
# unfiltered rung it is what links 'GESTION HALKIS INC.' to a roster 'Halkis'.
_NAME_SCORE_EXACT = 1.0
_NAME_SCORE_PREFIX = 0.8
_NAME_SCORE_CONTAINS = 0.5

# Boosts. Additive and small: they rank hits that already passed the name test,
# and must never be able to lift a weak name match over the threshold on their
# own. None of them may be negative -- see `score_hit` for why.
_BOOST_TOWN = 0.10
_BOOST_JURISDICTION = 0.07
_BOOST_INDUSTRY = 0.03


class RelaxationPolicy(BaseModel):
    """Every knob that decides how hard the ladder tries, in one value.

    The ladder's defaults are tuned for precision on rows that already carry a
    country and a jurisdiction. A roster row with neither -- 829 of the 1,088
    unresolved rows measured 2026-08-01 -- needs the same code run at different
    settings, not different code. Passing the policy explicitly keeps
    :func:`build_ladder` and :func:`rank_and_filter` pure: identical inputs give
    identical rungs, with no hidden read of process-wide settings.

    Attributes:
        min_query_length: Shortest relaxed query still worth sending.
        jurisdiction_hit_cap: Largest linkable result set for a rung that
            carries a jurisdiction filter; doubles as its page budget.
        unfiltered_hit_cap: The same for a rung searching every jurisdiction.
        min_match_score: Acceptance threshold on a scored rung.
        max_links: Ceiling on links written for one roster row.
        strip_leading_legal_form: Whether the ``core`` variant also drops
            *leading* legal-form tokens (``S.A. Cimenteries CBR``), not only
            trailing ones.
        score_over_cap: Whether a rung whose ``total_count`` exceeds its cap
            hands its already-fetched first page to the scorer instead of
            discarding it.
    """

    model_config = ConfigDict(frozen=True)

    min_query_length: int = Field(
        default=settings.OPENCORP_MIN_RELAXED_QUERY_LENGTH,
        description="Shortest relaxed query worth sending.",
    )
    jurisdiction_hit_cap: int = Field(
        default=settings.OPENCORP_HIT_CAP_JURISDICTION,
        description="Largest linkable result set for a jurisdiction-filtered rung.",
    )
    unfiltered_hit_cap: int = Field(
        default=settings.OPENCORP_HIT_CAP_UNFILTERED,
        description="Largest linkable result set for an unfiltered rung.",
    )
    min_match_score: float = Field(
        default=settings.OPENCORP_MIN_MATCH_SCORE,
        description="Acceptance threshold on a scored rung.",
    )
    max_links: int = Field(
        default=settings.OPENCORP_MAX_LINKS_PER_TARGET,
        description="Ceiling on links written for one roster row.",
    )
    strip_leading_legal_form: bool = Field(
        default=False, description="Also drop leading legal-form tokens."
    )
    score_over_cap: bool = Field(
        default=False,
        description="Score an over-cap rung's first page instead of discarding it.",
    )

    @classmethod
    def relaxed(cls) -> "RelaxationPolicy":
        """Build the policy for a ``--relax`` run.

        Loosens exactly the three things measurement showed were silencing
        recoverable rows, and nothing else. The caps stay put: ``score_over_cap``
        already rescues an over-cap rung at zero extra API cost, so widening the
        page budget as well would spend calls to buy what is now free.

        Returns:
            The relaxed policy. Match score and link ceiling are unchanged, so a
            relaxed run sends more queries but is no more willing to accept a
            weak hit.
        """
        return cls(
            min_query_length=settings.OPENCORP_RELAXED_MIN_QUERY_LENGTH,
            strip_leading_legal_form=True,
            score_over_cap=True,
        )


DEFAULT_POLICY = RelaxationPolicy()


class OpenCorpTarget(BaseModel):
    """One roster company to resolve, with everything the ladder scores against.

    Attributes:
        company_name: The roster's name, verbatim and possibly polluted.
        iso_country_code: ISO 3166-1 alpha-2 country from the roster, or None.
            Nullable because 829 unresolved roster rows carry no country at all
            and are still searchable -- unfiltered, which is how ``CEJI
            INTERLUDE`` (registered ``fr``) is found for a row that names no
            country.
        jurisdication_code: Canonical ISO 3166-2 / GLEIF jurisdiction resolved
            for the row (``AE-DU``, ``GB``), or None. Spelled as the column is.
        address: Free-text registered address from the decision, or None.
        industries: Industry terms from the document's ``companies_industry``
            fact. Document-level, so a weak per-company signal.
        decision_year: Year of the decision, used to reject a company
            incorporated after it, or None when unknown.
    """

    model_config = ConfigDict(frozen=True)

    company_name: str = Field(description="Roster company name, verbatim.")
    iso_country_code: str | None = Field(
        default=None, description="ISO 3166-1 alpha-2 country code, if known."
    )
    jurisdication_code: str | None = Field(
        default=None, description="Canonical GLEIF jurisdiction code, if resolved."
    )
    address: str | None = Field(
        default=None, description="Free-text registered address, if known."
    )
    industries: tuple[str, ...] = Field(
        default=(), description="Industry terms from the document's fact."
    )
    decision_year: int | None = Field(
        default=None, description="Year of the decision, if known."
    )


class ResolutionRung(BaseModel):
    """One attempt in the ladder, ordered most constrained first.

    Attributes:
        rung: Stable slug, persisted as link provenance.
        query_text: Text to send as ``q``, before WAF sanitization.
        jurisdiction_code: OpenCorporates jurisdiction filter, or None to
            search every jurisdiction.
        hit_cap: Largest result set still worth linking from; doubles as the
            page-fetch budget.
        requires_scoring: Whether hits must pass :func:`rank_and_filter`. True
            for every rung that searched outside the target's own jurisdiction.
    """

    model_config = ConfigDict(frozen=True)

    rung: str = Field(description="Stable rung slug, persisted as provenance.")
    query_text: str = Field(description="Query text for the 'q' parameter.")
    jurisdiction_code: str | None = Field(
        default=None, description="Jurisdiction filter, None to search globally."
    )
    hit_cap: int = Field(description="Largest linkable result set for this rung.")
    requires_scoring: bool = Field(
        description="Whether hits must clear the match threshold to be linked."
    )


class HitScore(BaseModel):
    """Why one hit was accepted or rejected, kept for auditability.

    Attributes:
        score: Final score in [0, 1]; zero when temporally vetoed.
        name_score: The name-match tier before any boost.
        town_match: Whether the registered address shares a town token.
        industry_match: Whether an industry term appears in the name.
        jurisdiction_match: Whether the hit sits in the expected jurisdiction.
        temporally_plausible: False only when the company was demonstrably
            incorporated after the decision.
    """

    model_config = ConfigDict(frozen=True)

    score: float = Field(description="Final score in [0, 1].")
    name_score: float = Field(description="Name-match tier before boosts.")
    town_match: bool = Field(description="Registered address shares a town token.")
    industry_match: bool = Field(description="An industry term appears in the name.")
    jurisdiction_match: bool = Field(description="Hit is in the expected jurisdiction.")
    temporally_plausible: bool = Field(
        description="False when incorporated after the decision."
    )


def _legal_form_key(token: str) -> str:
    """Fold one token to the form the legal-form sets are keyed by.

    ``fold_match_key`` turns every non-alphanumeric run into a space, so a
    dotted abbreviation arrives as several words: ``A.E.`` becomes ``"a e"`` and
    ``S.p.A.`` becomes ``"s p a"``. Removing the spaces again recovers the
    abbreviation as a single key, which is what the sets store.

    Args:
        token: One whitespace-delimited token from a company name.

    Returns:
        The folded, space-free key, e.g. ``ae``, ``spa``, ``gmbh``.
    """
    return fold_match_key(token).replace(" ", "")


def _variant_key(text: str) -> str:
    """Key one name variant for deduplication.

    ``fold_match_key`` drops everything outside ASCII, so a name written in a
    non-Latin script folds to the empty string -- and every variant of it would
    then look like the same empty key and be discarded, silencing the name
    entirely. OpenCorporates does index those names (``"ММ СТЕТ" ЕООД`` is a real
    hit), so a variant that folds to nothing falls back to a casefolded,
    whitespace-collapsed key instead: weaker at spotting spelling variants, but
    it compares a Greek name with itself, which is all deduplication needs.

    Args:
        text: One candidate query variant.

    Returns:
        A non-empty comparison key, or the empty string when the variant carries
        no characters at all.
    """
    return fold_match_key(text) or " ".join(text.split()).casefold()


def strip_parentheticals(name: str) -> str:
    """Delete parenthesised groups and their contents.

    Distinct from the WAF sanitization in ``opencorp_client._sanitize_query``,
    which replaces the bracket characters with a space and *keeps* the words
    inside. Under AND semantics those kept words become extra terms the
    registered name must also satisfy, which is what turns
    ``United Arab Shipping Company (S.A.G)`` into a zero-hit query while
    ``United Arab Shipping`` succeeds.

    Nested groups are removed by repeated innermost passes.

    Args:
        name: Company name, possibly containing parenthesised qualifiers.

    Returns:
        The name with every parenthesised group removed and whitespace
        collapsed. Returns the input unchanged if stripping would empty it.
    """
    stripped = name
    while "(" in stripped and ")" in stripped:
        reduced = _PARENTHETICAL_PATTERN.sub(" ", stripped)
        if reduced == stripped:
            break
        stripped = reduced
    collapsed = _WHITESPACE_PATTERN.sub(" ", stripped).strip(" ,;")
    return collapsed or name


def strip_trailing_role_clause(
    name: str, *, min_length: int | None = None
) -> str:
    """Drop everything from the first comma onward.

    Roster names routinely carry a trailing description that is not part of the
    registered name: a role (``Gebruder Junghans GmbH, clock and watch
    manufacturers``) or a town (``Solvay et Cie, Brussels``). Both are extra AND
    terms that no registry entry satisfies.

    Cutting at the *first* comma rather than the last is deliberate. This is a
    relaxation rung, so over-cutting merely broadens the query and the scorer
    filters the result; under-cutting leaves a term that returns nothing at all.

    Args:
        name: Company name, possibly with a trailing clause.
        min_length: Shortest result still worth searching for; defaults to
            ``settings.OPENCORP_MIN_RELAXED_QUERY_LENGTH``.

    Returns:
        The name up to the first comma, or the input unchanged when that would
        leave too little to search for.
    """
    floor = (
        settings.OPENCORP_MIN_RELAXED_QUERY_LENGTH if min_length is None
        else min_length
    )
    head, separator, _ = name.partition(",")
    if not separator:
        return name
    trimmed = head.strip(" ,;")
    if len(trimmed) < floor:
        return name
    return trimmed


def strip_legal_form(
    name: str, *, strip_leading: bool = False, min_length: int | None = None
) -> str:
    """Remove legal-form tokens such as ``GmbH & Co. KG`` from a company name.

    Tokens are dropped one at a time from the end for as long as each is a legal
    form or a connective between two of them, so compound suffixes collapse
    without needing to be enumerated. Stops before emptying the name or falling
    under ``min_length``.

    ``strip_leading`` extends the same treatment to the front of the name.
    Registered names carry the form on either side depending on the country --
    ``S.A. Cimenteries CBR`` and ``AB Volvo`` put it first, and a leading form is
    just as much an extra AND term as a trailing one. It is opt-in because the
    front of a name is where its identity usually lives, so a mistake there costs
    more than one at the end.

    Args:
        name: Company name, possibly carrying one or more legal forms.
        strip_leading: Whether to also drop leading legal-form tokens.
        min_length: Shortest result still worth searching for; defaults to
            ``settings.OPENCORP_MIN_RELAXED_QUERY_LENGTH``.

    Returns:
        The name without its legal form, or unchanged when stripping would leave
        too little to search for.
    """
    floor = (
        settings.OPENCORP_MIN_RELAXED_QUERY_LENGTH if min_length is None
        else min_length
    )
    strippable = _LEGAL_FORM_TOKENS | _LEGAL_FORM_CONNECTIVES
    tokens = name.split()
    while len(tokens) > 1:
        if _legal_form_key(tokens[-1]) not in strippable:
            break
        candidate = " ".join(tokens[:-1]).strip(" ,;&+")
        if len(candidate) < floor:
            break
        tokens = candidate.split()
    while strip_leading and len(tokens) > 1:
        if _legal_form_key(tokens[0]) not in strippable:
            break
        candidate = " ".join(tokens[1:]).strip(" ,;&+")
        if len(candidate) < floor:
            break
        tokens = candidate.split()
    stripped = " ".join(tokens).strip(" ,;&+")
    # A name that is nothing but legal forms ("SA") keeps its original text.
    if _legal_form_key(stripped) in _LEGAL_FORM_TOKENS:
        return name
    return stripped or name


def split_multi_company(name: str) -> list[str]:
    """Split a roster value that names several companies into its members.

    ``companies_investigated`` sometimes concatenates every addressee of a
    decision into one roster row -- ``OBB-Holding AG, Rail Cargo Austria AG,
    Rail Cargo Logistics - Austria GmbH and Express Interfracht Hellas A.E.`` is
    four companies sharing one id, one address and one country.

    The split is gated hard: it applies only when **every** resulting segment
    ends in a recognized legal form. That is what keeps ``Bischof + Klein`` and
    ``Solvay et Cie, Brussels`` whole -- guessing wrong here would fabricate
    companies that were never in the decision.

    Args:
        name: Roster company name, possibly naming several companies.

    Returns:
        The member names when the gate passes, otherwise an empty list. An empty
        list means "this is one company", not "nothing found".
    """
    segments = [
        segment.strip(" ,;")
        for segment in _MULTI_COMPANY_PATTERN.split(name)
        if segment.strip(" ,;")
    ]
    if len(segments) < 2:
        return []
    for segment in segments:
        tokens = segment.split()
        if len(tokens) < 2:
            return []
        if _legal_form_key(tokens[-1]) not in _LEGAL_FORM_TOKENS:
            return []
        # A trailing legal form is not enough on its own: 'S.A.B. de C.V.' is
        # the Mexican legal form of the company before it, not a second company,
        # and every one of its tokens is either a form or a particle. Require a
        # substantive token -- one that actually names something.
        if not any(
            len(token) >= _MIN_SUBSTANTIVE_TOKEN_LENGTH
            and _legal_form_key(token) not in _LEGAL_FORM_TOKENS
            for token in fold_match_key(segment).split()
        ):
            return []
    return segments


def relaxation_variants(
    name: str, *, policy: RelaxationPolicy = DEFAULT_POLICY
) -> list[tuple[str, str]]:
    """Build the ordered, deduplicated name variants for one company.

    Args:
        name: Roster company name, verbatim.
        policy: Relaxation knobs; supplies the minimum query length and whether
            leading legal forms are stripped.

    Returns:
        ``(rung slug, query text)`` pairs from most to least constrained.
        Variants that duplicate an earlier one (folded) or that fall under
        ``policy.min_query_length`` are dropped, so a clean name yields a single
        entry rather than four identical searches.
    """
    floor = policy.min_query_length
    no_paren = strip_parentheticals(name)
    no_role = strip_trailing_role_clause(no_paren, min_length=floor)
    core = strip_legal_form(
        no_role,
        strip_leading=policy.strip_leading_legal_form,
        min_length=floor,
    )

    candidates = [
        (RUNG_VERBATIM, name),
        (RUNG_NO_PAREN, no_paren),
        (RUNG_NO_ROLE, no_role),
        (RUNG_CORE, core),
    ]
    variants: list[tuple[str, str]] = []
    seen: set[str] = set()
    for rung, text in candidates:
        folded = _variant_key(text)
        if not folded or folded in seen:
            continue
        if len(text) < floor:
            continue
        seen.add(folded)
        variants.append((rung, text))
    return variants


def build_ladder(
    target: OpenCorpTarget,
    *,
    jurisdiction_code: str | None,
    country_jurisdiction_code: str | None = None,
    policy: RelaxationPolicy = DEFAULT_POLICY,
) -> list[ResolutionRung]:
    """Order the search attempts for one target, most constrained first.

    The ordering is the design. Name relaxation under the target's own
    jurisdiction is cheap and high precision, so it runs first. The unfiltered
    verbatim rung is placed *second* rather than last because it is the only one
    that reaches an offshore registration, and a target whose jurisdiction is
    simply wrong would otherwise burn every name rung before getting there.

    Args:
        target: The roster company to resolve.
        jurisdiction_code: OpenCorporates jurisdiction to search under, already
            translated from the roster's canonical form, or None when none was
            resolvable.
        country_jurisdiction_code: The country-level code to fall back to when
            ``jurisdiction_code`` is a subdivision, or None when there is no
            distinct country level to try.
        policy: Relaxation knobs; supplies the hit caps and everything
            :func:`relaxation_variants` needs.

    Returns:
        The rungs to attempt in order. Duplicate ``(query, jurisdiction)``
        pairs are removed, so a clean name under a country-level jurisdiction
        typically yields two rungs, not eight. A target with no jurisdiction at
        all still gets the two unfiltered rungs, which is how a roster row
        naming no country is resolved.
    """
    variants = relaxation_variants(target.company_name, policy=policy)
    if not variants:
        return []

    verbatim_text = variants[0][1]
    core_text = variants[-1][1]
    jurisdiction_cap = policy.jurisdiction_hit_cap
    unfiltered_cap = policy.unfiltered_hit_cap

    rungs: list[ResolutionRung] = []
    if jurisdiction_code:
        rungs.append(
            ResolutionRung(
                rung=RUNG_VERBATIM,
                query_text=verbatim_text,
                jurisdiction_code=jurisdiction_code,
                hit_cap=jurisdiction_cap,
                requires_scoring=False,
            )
        )

    # The offshore path: Halkis is stored GR and registered CY.
    rungs.append(
        ResolutionRung(
            rung=RUNG_VERBATIM_ANY,
            query_text=verbatim_text,
            jurisdiction_code=None,
            hit_cap=unfiltered_cap,
            requires_scoring=True,
        )
    )

    for member in split_multi_company(target.company_name):
        if len(member) < policy.min_query_length:
            continue
        rungs.append(
            ResolutionRung(
                rung=RUNG_SPLIT,
                query_text=member,
                jurisdiction_code=jurisdiction_code,
                hit_cap=jurisdiction_cap,
                requires_scoring=jurisdiction_code is None,
            )
        )

    if jurisdiction_code:
        for rung_slug, text in variants[1:]:
            rungs.append(
                ResolutionRung(
                    rung=rung_slug,
                    query_text=text,
                    jurisdiction_code=jurisdiction_code,
                    hit_cap=jurisdiction_cap,
                    requires_scoring=False,
                )
            )

    if country_jurisdiction_code and country_jurisdiction_code != jurisdiction_code:
        rungs.append(
            ResolutionRung(
                rung=RUNG_CORE_COUNTRY,
                query_text=core_text,
                jurisdiction_code=country_jurisdiction_code,
                hit_cap=jurisdiction_cap,
                requires_scoring=True,
            )
        )

    rungs.append(
        ResolutionRung(
            rung=RUNG_CORE_ANY,
            query_text=core_text,
            jurisdiction_code=None,
            hit_cap=unfiltered_cap,
            requires_scoring=True,
        )
    )

    deduplicated: list[ResolutionRung] = []
    seen: set[tuple[str, str | None]] = set()
    for rung in rungs:
        key = (_variant_key(rung.query_text), rung.jurisdiction_code)
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(rung)
    return deduplicated


def _address_tokens(address: str | None) -> set[str]:
    """Reduce a free-text address to comparable locating tokens.

    Args:
        address: Free-text address, or None.

    Returns:
        Folded tokens of at least ``_MIN_ADDRESS_TOKEN_LENGTH`` characters.
        ``Athens Tower GR-11527 ATHENS`` yields ``{athens, tower, 11527}``.
    """
    return {
        token
        for token in fold_match_key(address).split()
        if len(token) >= _MIN_ADDRESS_TOKEN_LENGTH
    }


def _name_score(target_name: str, hit_name: str) -> float:
    """Score how well a hit's registered name matches the searched name.

    Three tiers, because the registry name is normally *longer* than the roster
    name rather than equal to it: the roster's ``Halkis`` must reach
    ``HALKIS CEMENT TRANSPORTERS CO. LIMITED``. Prefix matching requires a whole
    word boundary, which is what separates that hit from ``GESTION HALKIS INC.``
    -- the latter contains the token but does not begin with it.

    Args:
        target_name: The name that was searched for.
        hit_name: The registered name OpenCorporates returned.

    Returns:
        1.0 exact, 0.8 whole-word prefix, 0.5 all tokens present, 0.0 otherwise.
    """
    folded_target = fold_match_key(target_name)
    folded_hit = fold_match_key(hit_name)
    if not folded_target or not folded_hit:
        return 0.0
    if folded_target == folded_hit:
        return _NAME_SCORE_EXACT
    if folded_hit.startswith(f"{folded_target} "):
        return _NAME_SCORE_PREFIX
    hit_tokens = set(folded_hit.split())
    if set(folded_target.split()) <= hit_tokens:
        return _NAME_SCORE_CONTAINS
    return 0.0


def score_hit(
    hit: OpenCorpCompany, target: OpenCorpTarget, *, query_text: str
) -> HitScore:
    """Score one OpenCorporates hit against the roster company.

    One veto, three boosts. The veto is temporal: a company incorporated after
    the decision cannot be one of its addressees. Everything geographic is a
    boost only, and no signal may ever be negative -- registration and
    headquarters legitimately diverge for exactly the companies this ladder
    exists to find, so a Cyprus registered office must not count against an
    Athens-headquartered party.

    Args:
        hit: One company from an OpenCorporates search response.
        target: The roster company being resolved.
        query_text: The relaxed name actually sent, which is what the hit's
            name is compared against rather than the polluted roster value.

    Returns:
        The :class:`HitScore`, with ``score`` zero when the temporal veto fires.
    """
    name_score = _name_score(query_text, hit.name)

    temporally_plausible = True
    if hit.incorporation_date is not None and target.decision_year is not None:
        incorporation = hit.incorporation_date
        if isinstance(incorporation, date):
            temporally_plausible = incorporation.year <= target.decision_year

    folded_hit_name = fold_match_key(hit.name)
    hit_address_tokens = _address_tokens(hit.registered_address_in_full)
    town_match = bool(hit_address_tokens & _address_tokens(target.address))
    industry_match = any(
        fold_match_key(industry) in folded_hit_name
        for industry in target.industries
        if fold_match_key(industry)
    )
    expected_jurisdiction = (target.jurisdication_code or "").replace("-", "_").lower()
    jurisdiction_match = bool(
        expected_jurisdiction
        and (hit.jurisdiction_code or "").lower() == expected_jurisdiction
    )

    score = name_score
    if score > 0.0:
        score += _BOOST_TOWN * town_match
        score += _BOOST_JURISDICTION * jurisdiction_match
        score += _BOOST_INDUSTRY * industry_match
    if not temporally_plausible:
        score = 0.0

    return HitScore(
        score=min(score, 1.0),
        name_score=name_score,
        town_match=town_match,
        industry_match=industry_match,
        jurisdiction_match=jurisdiction_match,
        temporally_plausible=temporally_plausible,
    )


def rank_and_filter(
    hits: list[OpenCorpCompany],
    target: OpenCorpTarget,
    *,
    query_text: str,
    policy: RelaxationPolicy = DEFAULT_POLICY,
    min_score: float | None = None,
    max_links: int | None = None,
) -> list[tuple[OpenCorpCompany, HitScore]]:
    """Rank hits and keep the ones confidently identifying the target.

    Args:
        hits: Companies returned by one rung.
        target: The roster company being resolved.
        query_text: The relaxed name that was sent.
        policy: Relaxation knobs; supplies the threshold and the ceiling.
        min_score: Acceptance threshold overriding ``policy.min_match_score``.
        max_links: Ceiling on accepted hits overriding ``policy.max_links``.

    Returns:
        Accepted ``(hit, score)`` pairs, best first, at most ``max_links`` long.
        Empty when nothing cleared the threshold -- which the caller must report
        as ambiguous rather than as an absence, since hits did come back.
    """
    threshold = policy.min_match_score if min_score is None else min_score
    ceiling = policy.max_links if max_links is None else max_links
    scored = [
        (hit, score_hit(hit, target, query_text=query_text)) for hit in hits
    ]
    accepted = [pair for pair in scored if pair[1].score >= threshold]
    accepted.sort(key=lambda pair: (-pair[1].score, pair[0].name))
    if len(accepted) > ceiling:
        logger.info(
            "Truncating %d accepted hit(s) to %d for %r",
            len(accepted),
            ceiling,
            target.company_name,
        )
    return accepted[:ceiling]
