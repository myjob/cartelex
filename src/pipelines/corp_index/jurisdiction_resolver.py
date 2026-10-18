"""
Resolve an investigated company's legal jurisdiction from the facts on its
roster row.

A roster row carries an ISO 3166-1 alpha-2 ``iso_country_code``, which is wrong
whenever the company is registered in a *subdivision* -- a US state, a Canadian
province, an emirate. Downstream consumers that need a jurisdiction (notably the
OpenCorporates search) cannot work from the country code alone: OpenCorporates
has no country-level jurisdiction for AE or US at all, so a bare ``ae`` search
returns HTTP 200 with zero hits, which reads as "not found" rather than "wrong
question".

This module derives the jurisdiction from data available at roster-load time
only: the company's address, its country, the ISO 3166-2 subdivisions in
``celex_facts.gleif_jurisdication2countrycode``, and -- as a weak secondary
signal -- the legal jurisdiction of a matched GLEIF LEI record. It never calls
or reads OpenCorporates, whose data is typically not available when the roster
is loaded.

Codes are handled in the ISO 3166-2 / GLEIF canonical form (``AE-DU``:
uppercase, hyphen-separated). :func:`to_opencorp_code` converts to the
OpenCorporates spelling (``ae_du``) at the point of the API call, and
:func:`normalize_jurisdiction_code` accepts any casing or separator so an
externally supplied ``ae_du`` / ``AE_DU`` / ``ae-du`` all land on ``AE-DU``.

It is plain deterministic logic: no LLM or DSPy pipeline is involved, so the
DSPy signature rules do not apply.

Exports:
    normalize_jurisdiction_code -- parse any casing/separator into canonical form.
    to_opencorp_code -- canonical form to the OpenCorporates spelling.
    country_part -- the ISO 3166-1 parent of a canonical code.
    fold_match_key -- case/accent/punctuation-insensitive match key, shared with
    the roster ETL so company names normalize identically on both sides.
    GleifSubdivision -- one subdivision reference row.
    JurisdictionResolver -- address / LEI / country precedence resolver.
"""

import logging
import re
import unicodedata

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Canonical (ISO 3166-2 / GLEIF) jurisdiction code: two uppercase letters,
# optionally a hyphen and a 1-3 character alphanumeric subdivision. Subdivisions
# such as PT-20 are numeric, so digits must be allowed.
CANONICAL_CODE_PATTERN = re.compile(r"^[A-Z]{2}(-[A-Z0-9]{1,3})?$")

# Separator in the canonical form, and the one OpenCorporates uses instead.
CANONICAL_SEPARATOR = "-"
OPENCORP_SEPARATOR = "_"

# Countries whose ISO 3166-2 subdivision codes are also postal abbreviations, so
# a bare uppercase token in an address ("St Louis, MO 63188") identifies the
# subdivision. Applied only here because a two-letter token means something else
# in most other countries' addresses.
POSTAL_ABBREVIATION_COUNTRIES: frozenset[str] = frozenset({"US", "CA"})

# GLEIF appends the parent country in parentheses to every subdivision label
# ("Dubai (United Arab Emirates)"); the label itself is the part before it.
_COUNTRY_SUFFIX_SEPARATOR = " ("

# Matches a standalone run of two uppercase letters in a raw (unfolded) address.
_UPPERCASE_TOKEN_PATTERN = re.compile(r"\b[A-Z]{2}\b")

# Every character that is not a letter or digit collapses to a single space, so
# punctuation and spacing differences between an address and a subdivision label
# cannot prevent a match.
_NON_ALPHANUMERIC_PATTERN = re.compile(r"[^a-z0-9]+")


def normalize_jurisdiction_code(raw: str | None) -> str | None:
    """Parse a jurisdiction code in any casing or separator into canonical form.

    Accepts the OpenCorporates spelling (``ae_du``), the ISO 3166-2 spelling
    (``AE-DU``) and any mixture of the two (``Ae_Du``, ``ae-du``), with
    surrounding whitespace, and returns the canonical ``AE-DU``. Anything that
    is not a well-formed code returns None rather than raising, so a malformed
    value stored on a roster row degrades to "no jurisdiction" instead of
    breaking the run.

    Args:
        raw: The code to parse, or None.

    Returns:
        The canonical uppercase, hyphen-separated code, or None when ``raw`` is
        empty or not a well-formed jurisdiction code.
    """
    if not raw:
        return None

    candidate = raw.strip().upper().replace(OPENCORP_SEPARATOR, CANONICAL_SEPARATOR)
    if not CANONICAL_CODE_PATTERN.match(candidate):
        return None
    return candidate


def to_opencorp_code(canonical_code: str) -> str:
    """Convert a canonical jurisdiction code to the OpenCorporates spelling.

    Args:
        canonical_code: A canonical code such as ``AE-DU`` or ``GB``.

    Returns:
        The OpenCorporates form: lowercase, underscore-separated (``ae_du``).
    """
    return canonical_code.lower().replace(CANONICAL_SEPARATOR, OPENCORP_SEPARATOR)


def country_part(canonical_code: str) -> str:
    """Return the ISO 3166-1 alpha-2 parent country of a canonical code.

    Args:
        canonical_code: A canonical code such as ``AE-DU`` or ``GB``.

    Returns:
        The two-letter country part (``AE``, ``GB``).
    """
    return canonical_code.split(CANONICAL_SEPARATOR, 1)[0]


def fold_match_key(text: str | None) -> str:
    """Reduce text to a case-, accent- and punctuation-insensitive match key.

    Decomposes to NFKD and drops non-ASCII marks so ``Ajman`` matches
    ``'Ajman`` and ``Regiao Autonoma dos Acores`` matches its accented form,
    then casefolds and collapses every non-alphanumeric run to a single space.
    This mirrors the normalization ``_handelsregister_match_key`` performs in SQL
    for company names; it is not a similarity metric, only a way to make
    spelling-variant strings compare equal.

    Args:
        text: The value to fold, or None.

    Returns:
        The folded key, or an empty string when there is nothing to fold.
    """
    decomposed = unicodedata.normalize("NFKD", text or "")
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return _NON_ALPHANUMERIC_PATTERN.sub(" ", ascii_only.casefold()).strip()


class GleifSubdivision(BaseModel):
    """One ISO 3166-2 subdivision from the GLEIF reference table.

    Attributes
    ----------
    jurisdiction_code:
        Canonical subdivision code (``AE-DU``).
    jurisdiction_name:
        GLEIF's label, including the parenthesised parent country
        (``Dubai (United Arab Emirates)``).
    iso_country_code:
        ISO 3166-1 alpha-2 parent country (``AE``).
    """

    model_config = {"frozen": True}

    jurisdiction_code: str = Field(description="Canonical subdivision code (AE-DU).")
    jurisdiction_name: str = Field(
        description="GLEIF label, e.g. 'Dubai (United Arab Emirates)'."
    )
    iso_country_code: str = Field(description="ISO 3166-1 alpha-2 parent country.")

    @property
    def label(self) -> str:
        """The subdivision name with GLEIF's parenthesised country stripped."""
        return self.jurisdiction_name.split(_COUNTRY_SUFFIX_SEPARATOR, 1)[0].strip()

    @property
    def subdivision_part(self) -> str:
        """The ISO 3166-2 part of the code (``DU`` for ``AE-DU``)."""
        _, _, subdivision = self.jurisdiction_code.partition(CANONICAL_SEPARATOR)
        return subdivision


class JurisdictionResolver:
    """Resolves a roster row's jurisdiction from its address, LEI and country.

    Follows the Provisioner pattern: the GLEIF subdivision reference rows are
    injected, so the resolver holds no DB handle and unit-tests without one.

    Resolution precedence, first signal that yields a code wins:

    1. **Address** -- the country's subdivision label appears in the address as a
       whole folded phrase; failing that, for
       :data:`POSTAL_ABBREVIATION_COUNTRIES`, the subdivision code appears as a
       standalone uppercase token. An ambiguous match (two subdivisions of the
       same country hit) is discarded rather than guessed.
    2. **LEI legal jurisdiction** -- the ISO 3166-2 code from a matched GLEIF LEI
       record, accepted only when its country part agrees with the row's own
       country. The check matters: the roster's LEI is assigned by a substring
       name match that frequently lands on a same-named entity in another
       country, and such a record's jurisdiction says nothing about this company.
    3. **Country code** -- when the country has no subdivisions at all, the
       country *is* the jurisdiction.
    4. **None** -- the country has subdivisions but nothing identified one.
       Deliberately unresolved rather than guessed, so a consumer can decline to
       search instead of recording a false negative.

    Parameters
    ----------
    subdivisions:
        Every ISO 3166-2 subdivision row from
        ``celex_facts.gleif_jurisdication2countrycode``.
    """

    def __init__(self, subdivisions: list[GleifSubdivision]) -> None:
        self._by_country: dict[str, list[GleifSubdivision]] = {}
        for subdivision in subdivisions:
            self._by_country.setdefault(subdivision.iso_country_code, []).append(
                subdivision
            )
        # Folded label per subdivision, computed once per resolver rather than
        # per row: the roster is resolved in one bulk pass over thousands of rows.
        self._folded_labels: dict[str, str] = {
            subdivision.jurisdiction_code: fold_match_key(subdivision.label)
            for subdivision in subdivisions
        }

    def resolve(
        self,
        iso_country_code: str | None,
        address: str | None,
        lei_jurisdiction: str | None = None,
    ) -> str | None:
        """Resolve one roster row's canonical jurisdiction code.

        Args:
            iso_country_code: The row's ISO 3166-1 alpha-2 country code. A value
                that is not a well-formed country code (the roster holds some
                full country names) yields None.
            address: The row's registered address, or None.
            lei_jurisdiction: ``legal_jurisdiction`` of the LEI record matched to
                this row, or None. Used only as the secondary signal and only
                when its country agrees with ``iso_country_code``.

        Returns:
            The canonical jurisdiction code (``AE-DU``, ``GB``), or None when the
            country has subdivisions but none could be identified.
        """
        country = normalize_jurisdiction_code(iso_country_code)
        if country is None or CANONICAL_SEPARATOR in country:
            # Not a plain country code -- either junk or already a subdivision,
            # neither of which this method is given.
            return None

        subdivisions = self._by_country.get(country)
        if not subdivisions:
            return country

        from_address = self._resolve_from_address(subdivisions, address)
        if from_address is not None:
            return from_address

        from_lei = self._resolve_from_lei(country, lei_jurisdiction)
        if from_lei is not None:
            return from_lei

        return None

    def _resolve_from_address(
        self, subdivisions: list[GleifSubdivision], address: str | None
    ) -> str | None:
        """Identify a subdivision from an address, or None when it is unclear.

        Args:
            subdivisions: Candidate subdivisions of the row's country.
            address: The row's registered address, or None.

        Returns:
            The canonical subdivision code, or None when no single candidate
            matched.
        """
        if not address or not address.strip():
            return None

        padded = f" {fold_match_key(address)} "
        matches = [
            subdivision.jurisdiction_code
            for subdivision in subdivisions
            if self._folded_labels[subdivision.jurisdiction_code]
            and f" {self._folded_labels[subdivision.jurisdiction_code]} " in padded
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.debug(
                "Ambiguous subdivision labels %s in address %r; leaving "
                "unresolved.",
                matches,
                address[:120],
            )
            return None

        return self._resolve_from_postal_abbreviation(subdivisions, address)

    @staticmethod
    def _resolve_from_postal_abbreviation(
        subdivisions: list[GleifSubdivision], address: str
    ) -> str | None:
        """Identify a US/CA subdivision from a postal abbreviation in an address.

        Reads standalone uppercase two-letter tokens off the *raw* address, not
        the folded key, because the casing is what distinguishes a state code
        from an ordinary word.

        Args:
            subdivisions: Candidate subdivisions of the row's country.
            address: The row's registered address, verbatim.

        Returns:
            The canonical subdivision code, or None when no single candidate
            matched.
        """
        if subdivisions[0].iso_country_code not in POSTAL_ABBREVIATION_COUNTRIES:
            return None

        tokens = set(_UPPERCASE_TOKEN_PATTERN.findall(address))
        if not tokens:
            return None

        matches = [
            subdivision.jurisdiction_code
            for subdivision in subdivisions
            if subdivision.subdivision_part in tokens
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.debug(
                "Ambiguous postal abbreviations %s in address %r; leaving "
                "unresolved.",
                matches,
                address[:120],
            )
        return None

    def _resolve_from_lei(
        self, country: str, lei_jurisdiction: str | None
    ) -> str | None:
        """Take the subdivision from an LEI record, if it agrees on the country.

        Args:
            country: The row's canonical country code.
            lei_jurisdiction: ``legal_jurisdiction`` from the matched LEI record.

        Returns:
            The canonical subdivision code, or None when the LEI carries no
            subdivision or belongs to a different country.
        """
        candidate = normalize_jurisdiction_code(lei_jurisdiction)
        if candidate is None or CANONICAL_SEPARATOR not in candidate:
            return None
        if country_part(candidate) != country:
            logger.debug(
                "Discarding LEI jurisdiction %s for a %s row: country mismatch.",
                candidate,
                country,
            )
            return None
        return candidate
