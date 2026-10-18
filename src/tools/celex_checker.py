"""
CELEX / case-ID validation toolkit.

Resolution and *detection* are both driven by an in-memory identifier index
built from ``celex_facts.celex_case_id`` (authoritative, CSV-loaded) with
``celex_facts.case_celex_id`` unioned in as a recall supplement, so identifiers
that exist only in the noisier scanned table keep resolving.  The index is
loaded once per process and cached for the lifetime of the container -- see
:func:`load_identifier_index`.

Detection replaces the former hand-maintained ``CELEX_CANDIDATE_RE``: a token
counts as a case reference when the registry either contains it outright or
contains identifiers of the same *shape*.  That is what lets a mistyped id such
as ``02D0180`` be rejected at the gate instead of being mistaken for prose and
passed to the planner.

Database access is delegated to
:class:`~pipelines.rag.pgvector.celex_identity_repository.CelexIdentityRepository`.

Satisfies the ``CelexValidator`` protocol defined in
``pipelines.agent_dspy.dspy_protocols``.

Exports:
    CelexIdentifierIndex     — DB-free alias/shape vocabulary plus detection
    CelexCheckerToolkit      — CelexValidator implementation over the index
    load_identifier_index    — process-lifetime cached loader
    reset_identifier_index_cache — test hook that drops the cached index
"""

import logging
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from pipelines.rag.pgvector.celex_identity_repository import CelexIdentityRepository
from pipelines.rag.pgvector.vector_config import DATABASE_URL

logger = logging.getLogger(__name__)

# Sentinel returned to callers when an identifier does not resolve.
NOT_FOUND = "NA"

# Prefix carried by CELEX-form ids in every storage table.  Users routinely
# type the bare number, so both forms are registered as aliases.
_CELEX_PREFIX = "CELEX-"

# Punctuation stripped from a token before it is looked up.  Sentence-final
# marks and quoting characters only; separators that are part of an identifier
# (``_ / - .``) are preserved inside the token.
_TOKEN_TRIM = "\"'`()[]{}<>,;:!?"

# Characters that may join identifier segments.  Everything else is treated as
# a token boundary.
_ID_SEPARATORS = "-_/."

# A shape signature keeps separators verbatim but collapses each run of digits
# to "9" and each run of letters to "A".
_DIGIT_RUN_RE = re.compile(r"\d+")
_ALPHA_RUN_RE = re.compile(r"[A-Z]+")

# Repeated "_9" groups collapse to one "_9+" unit so that AT_40588_6339 and
# AT_38700_8179861_2920_3 share a single family.
_REPEATED_GROUP_RE = re.compile(r"(_9)(?:_9)+")

# A token is only worth testing when it carries at least one digit -- no case
# identifier in the registry is purely alphabetic.
_HAS_DIGIT_RE = re.compile(r"\d")


def _normalise(identifier: str) -> str:
    """Return the case-folded, trimmed lookup key for *identifier*.

    Args:
        identifier: A raw identifier token.

    Returns:
        The upper-cased token with surrounding punctuation removed.
    """
    return identifier.strip().strip(_TOKEN_TRIM).upper()


def _leading_alpha_run(identifier: str) -> str:
    """Return the letters *identifier* opens with, or "" when it starts numeric.

    The registry's prefixes (CELEX, IV, AT, COMP, ...) are learned from the data
    rather than hardcoded; a token opening with any other word is prose.

    Args:
        identifier: A normalised identifier token.

    Returns:
        The leading run of letters, upper-cased, possibly empty.
    """
    match = _ALPHA_RUN_RE.match(identifier)
    return match.group(0) if match else ""


def shape_signature(identifier: str) -> str:
    """Return the collapsed shape family of *identifier*.

    Digit runs become ``9``, letter runs become ``A``, separators are kept, and
    repeated ``_9`` groups collapse to ``_9+``.  Identifiers that differ only in
    how many digit groups they carry therefore share one family::

        CELEX-32002D0180        -> A-9A9
        32002D0180              -> 9A9
        02D0180                 -> 9A9      (same family: a truncated CELEX)
        AT_40588_6339           -> A_9+
        AT_38700_8179861_2920_3 -> A_9+
        1994                    -> 9        (no registry family: not an id)

    Args:
        identifier: A normalised identifier token.

    Returns:
        The shape signature string.
    """
    signature = _DIGIT_RUN_RE.sub("9", identifier)
    signature = _ALPHA_RUN_RE.sub("A", signature)
    return _REPEATED_GROUP_RE.sub(r"\1+", signature)


@dataclass(frozen=True)
class CelexIdentifierIndex:
    """Alias and shape vocabulary of every known case identifier.

    Pure data with no database handle, so the retrieval gate can be unit-tested
    without a connection.

    Attributes:
        aliases: Normalised alias to canonical ``celex_id``.  Each registry row
            contributes its ``celex_id``, its bare (prefix-less) CELEX number,
            and its ``case_id``.
        shape_families: Every shape signature present among the aliases.  A
            prompt token matching one of these is a case-reference *attempt*
            even when it resolves to nothing.
    """

    aliases: Mapping[str, str]
    shape_families: frozenset[str]
    alpha_prefixes: frozenset[str]

    @classmethod
    def from_pairs(
        cls, pairs: Iterable[tuple[str, str | None]]
    ) -> "CelexIdentifierIndex":
        """Build an index from ``(celex_id, case_id)`` rows.

        Args:
            pairs: Registry rows; ``case_id`` may be None.  Earlier rows win on
                conflict, so the authoritative table must be supplied first.

        Returns:
            The populated index.
        """
        aliases: dict[str, str] = {}
        for celex_id, case_id in pairs:
            if not celex_id:
                continue
            canonical = celex_id.strip()
            for alias in cls._row_aliases(canonical, case_id):
                aliases.setdefault(alias, canonical)

        families = frozenset(shape_signature(alias) for alias in aliases)
        prefixes = frozenset(
            prefix for alias in aliases if (prefix := _leading_alpha_run(alias))
        )
        return cls(
            aliases=aliases, shape_families=families, alpha_prefixes=prefixes
        )

    @staticmethod
    def _row_aliases(canonical: str, case_id: str | None) -> list[str]:
        """Return every spelling of one registry row a user might type.

        Args:
            canonical: The row's ``celex_id``.
            case_id: The row's ``case_id``, when present.

        Returns:
            Normalised alias keys for the row.
        """
        aliases = [_normalise(canonical)]
        if canonical.upper().startswith(_CELEX_PREFIX):
            # Users routinely omit the prefix; the DB never stores it that way.
            aliases.append(_normalise(canonical[len(_CELEX_PREFIX):]))
        if case_id and case_id.strip():
            aliases.append(_normalise(case_id))
        return [alias for alias in aliases if alias]

    def resolve(self, identifier: str) -> str | None:
        """Return the canonical ``celex_id`` for *identifier*, or None.

        Args:
            identifier: A raw identifier token in any registered spelling.

        Returns:
            The canonical ``celex_id``, or None when the token is unknown.
        """
        return self.aliases.get(_normalise(identifier))

    def is_identifier_attempt(self, token: str) -> bool:
        """Return True when *token* is meant to be a case reference.

        A token carrying digits qualifies when:

        1. the registry knows it outright; or
        2. it opens with a registry prefix (CELEX, IV, AT, COMP, ...) -- those
           words introduce a case number and nothing else, so even a malformed
           ``CELEX-37766_610_5`` is an attempt; or
        3. it starts numeric and its shape matches a family the registry does
           contain -- which is how the truncated ``02D0180`` is caught.

        Everything else is prose: bare numbers ("1994"), decimals ("2.5"),
        section numbers ("9.1.5") and citations under a foreign prefix
        ("No.17") all fall through to the planner untouched.

        Args:
            token: A whitespace-delimited token from the user's prompt.

        Returns:
            True when the token must be validated rather than ignored.
        """
        normalised = _normalise(token)
        if not normalised or not _HAS_DIGIT_RE.search(normalised):
            return False
        if normalised in self.aliases:
            return True
        prefix = _leading_alpha_run(normalised)
        if prefix:
            return prefix in self.alpha_prefixes
        return shape_signature(normalised) in self.shape_families

    def find_candidates(self, question: str) -> list[str]:
        """Return ordered, deduplicated case-reference candidates in *question*.

        Replaces the former regex scan.  Tokens are split on whitespace and on
        characters that never join identifier segments, so an inline context tag
        such as ``[context: celex_id=39226_8310_11]`` still yields its id.

        Args:
            question: The user's free-form prompt.

        Returns:
            Candidate tokens in order of appearance, without duplicates.
        """
        seen: set[str] = set()
        out: list[str] = []
        for token in self._tokenise(question or ""):
            if not self.is_identifier_attempt(token):
                continue
            normalised = _normalise(token)
            if normalised in seen:
                continue
            seen.add(normalised)
            out.append(token.strip(_TOKEN_TRIM))
        return out

    @staticmethod
    def _tokenise(question: str) -> list[str]:
        """Split *question* into identifier-preserving tokens.

        Args:
            question: The raw prompt.

        Returns:
            Tokens that keep internal ``- _ / .`` separators intact.
        """
        tokens: list[str] = []
        for raw in question.split():
            # An inline tag like "celex_id=39226_8310_11" or a trailing full
            # stop must not hide the identifier, so split on "=" and strip
            # sentence punctuation from both ends.
            for part in raw.split("="):
                cleaned = part.strip(_TOKEN_TRIM).strip(_ID_SEPARATORS)
                if cleaned:
                    tokens.append(cleaned)
        return tokens


# ---------------------------------------------------------------------------
# Process-lifetime cache
# ---------------------------------------------------------------------------

# Populated on the first lookup and never invalidated: the container restart
# that reloads the process is the refresh mechanism.  Rows added to the registry
# while the server runs are therefore invisible until the next restart.
_INDEX_CACHE: CelexIdentifierIndex | None = None


def load_identifier_index(
    repo: CelexIdentityRepository | None = None,
) -> CelexIdentifierIndex:
    """Return the cached identifier index, building it on first call.

    The single bulk read replaces one DB round trip per prompt token.  The
    authoritative ``celex_case_id`` rows are loaded first so they win over the
    ``case_celex_id`` supplement on any conflicting alias.

    Args:
        repo: Identity repository to read from.  A default instance is created
            when omitted.

    Returns:
        The process-wide :class:`CelexIdentifierIndex`.
    """
    global _INDEX_CACHE
    if _INDEX_CACHE is not None:
        return _INDEX_CACHE

    repo = repo or CelexIdentityRepository()
    rows: list[tuple[str, str | None]] = list(repo.select_identifier_index())
    supplement = _load_supplement(repo)
    _INDEX_CACHE = CelexIdentifierIndex.from_pairs(rows + supplement)
    logger.info(
        "Identifier index cached: %d aliases, %d shape families "
        "(%d authoritative rows + %d supplementary).",
        len(_INDEX_CACHE.aliases),
        len(_INDEX_CACHE.shape_families),
        len(rows),
        len(supplement),
    )
    return _INDEX_CACHE


def _load_supplement(
    repo: CelexIdentityRepository,
) -> list[tuple[str, str | None]]:
    """Return ``case_celex_id`` rows, or an empty list when unreadable.

    ``celex_case_id`` covers only part of the corpus (it holds no COMP/ case
    numbers and misses roughly fifty served celex_ids), so the scanned table is
    unioned in for recall.  A failure here degrades detection rather than the
    whole gate, hence the swallowed exception.

    Args:
        repo: Identity repository to read from.

    Returns:
        Supplementary ``(celex_id, case_id)`` rows.
    """
    try:
        return [
            (row["celex_id"], row["case_id"])
            for row in repo.select_scanned_identifier_index()
        ]
    except Exception as exc:
        logger.warning("Supplementary identifier rows unavailable: %s", exc)
        return []


def reset_identifier_index_cache() -> None:
    """Drop the cached index so the next call reloads it.

    Test hook -- mirrors ``ranker_factory.reset_ranker_cache``.  Production code
    relies on the container restart instead.
    """
    global _INDEX_CACHE
    _INDEX_CACHE = None


class CelexCheckerToolkit:
    """Validate case references against the cached identifier index.

    Args:
        db_url: PostgreSQL connection string for the identity repository.
    """

    def __init__(self, db_url: str = DATABASE_URL) -> None:
        self._repo = CelexIdentityRepository(db_url=db_url)

    def identifier_index(self) -> CelexIdentifierIndex:
        """Return the process-wide identifier index, loading it on first use.

        Returns:
            The cached :class:`CelexIdentifierIndex`.
        """
        return load_identifier_index(self._repo)

    def check_celex_or_case_id(self, identifier: str) -> str:
        """Resolve *identifier* to its canonical ``celex_id``.

        Answers from the cached index; falls back to a single-row query only
        when the index misses, so an identifier added to the registry after the
        process started still resolves.

        Args:
            identifier: A CELEX id, a bare CELEX number, or a case number.

        Returns:
            The canonical ``celex_id``, or ``"NA"`` when unknown.
        """
        resolved = self.identifier_index().resolve(identifier)
        if resolved:
            return resolved

        fallback = self._repo.check_celex_or_case_id(identifier)
        return fallback if fallback else NOT_FOUND

    def find_candidates(self, question: str) -> list[str]:
        """Return the case-reference candidates present in *question*.

        Args:
            question: The user's free-form prompt.

        Returns:
            Candidate tokens in order of appearance.
        """
        return self.identifier_index().find_candidates(question)

    def suggest(self, identifier: str, limit: int = 3) -> Sequence[str]:
        """Return registry identifiers that *identifier* was probably meant to be.

        Matches on a shared tail (``02D0180`` -> ``CELEX-32002D0180``), which is
        the truncation users actually make.

        Args:
            identifier: The unresolved token.
            limit: Maximum number of suggestions.

        Returns:
            Up to *limit* canonical ``celex_id`` values, ordered alphabetically.
        """
        normalised = _normalise(identifier)
        if len(normalised) < 4:
            return []
        index = self.identifier_index()
        hits = {
            canonical
            for alias, canonical in index.aliases.items()
            if alias.endswith(normalised) or normalised.endswith(alias)
        }
        return sorted(hits)[:limit]
