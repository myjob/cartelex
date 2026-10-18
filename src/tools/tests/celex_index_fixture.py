"""Shared test double for the ``CelexValidator`` protocol.

The gate's *detection* half is driven by the identifier registry, so a stub
that only answers ``check_celex_or_case_id`` can no longer stand in for the
production toolkit.  :class:`FakeCelexChecker` pairs an in-memory resolution
dict with a real :class:`~tools.celex_checker.CelexIdentifierIndex` built from
:data:`REGISTRY_SAMPLE`, giving tests the genuine detection behaviour with no
database connection.

Lives under ``tools/tests`` because it fakes ``tools.celex_checker``; imported
by the pipeline and UI suites alike.
"""

from typing import Iterable, Sequence

from tools.celex_checker import NOT_FOUND, CelexIdentifierIndex

# A representative slice of celex_facts.celex_case_id: one row per identifier
# family the corpus actually contains, so the shape vocabulary a test sees
# matches production.
REGISTRY_SAMPLE: tuple[tuple[str, str | None], ...] = (
    ("CELEX-31991D0562", "IV/31562"),
    ("CELEX-32002D0180", "IV/37859"),
    ("CELEX-31969D0216", "IV/25410"),
    ("37766_610_5", "IV/37766"),
    ("39226_8310_11", "IV/39226"),
    ("38344_5856_3", "IV/38344"),
    ("AT_40588_6339", "AT.40588"),
    ("AT_40760_9770404_1218_4", "AT.40760"),
)


def build_index(
    extra_rows: Iterable[tuple[str, str | None]] = (),
) -> CelexIdentifierIndex:
    """Return an index over :data:`REGISTRY_SAMPLE` plus *extra_rows*.

    Args:
        extra_rows: Additional ``(celex_id, case_id)`` rows to register.

    Returns:
        A DB-free :class:`CelexIdentifierIndex`.
    """
    return CelexIdentifierIndex.from_pairs(list(extra_rows) + list(REGISTRY_SAMPLE))


class FakeCelexChecker:
    """In-memory ``CelexValidator``: registry detection, dict resolution.

    Args:
        db: Maps an identifier spelling to the canonical celex_id it resolves
            to.  Anything absent resolves to ``"NA"``, which is what makes a
            detected-but-unknown token trip the ``invalid`` branch.
        register: Whether *db* entries also join the detection vocabulary.
            Left True so a test's own ids are recognised; set False to model an
            identifier that exists in the DB but not in the cached index.
    """

    def __init__(self, db: dict[str, str], *, register: bool = True) -> None:
        self.db = db
        self.calls: list[str] = []
        extra = (
            [(canonical, alias) for alias, canonical in db.items()]
            if register
            else []
        )
        self._index = build_index(extra)

    def check_celex_or_case_id(self, identifier: str) -> str:
        """Resolve *identifier* against the in-memory dict.

        Args:
            identifier: The token to resolve.

        Returns:
            The canonical celex_id, or ``"NA"``.
        """
        self.calls.append(identifier)
        return self.db.get(identifier, NOT_FOUND)

    def find_candidates(self, question: str) -> list[str]:
        """Return the case-reference candidates in *question*.

        Args:
            question: The prompt to scan.

        Returns:
            Candidate tokens in order of appearance.
        """
        return self._index.find_candidates(question)

    def suggest(self, identifier: str, limit: int = 3) -> Sequence[str]:
        """Return no suggestions -- tests assert on the plain rejection text.

        Args:
            identifier: The unresolved token.
            limit: Ignored.

        Returns:
            An empty sequence.
        """
        return []
