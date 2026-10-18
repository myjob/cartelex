import logging
from typing import Any, Dict, List, Optional


from pipelines.agent_dspy.dspy_protocols import RegexRetriever as RegexRetrieverProtocol

logger = logging.getLogger(__name__)


class RegexRetriever:
    """
    A unified orchestration class exclusively using keyword-based RDBMS search.
    """

    def __init__(self, repo: RegexRetrieverProtocol):
        """
        Initialize KeywordSearch with a chunk repository and a reranker.

        Parameters
        ----------
        repo:
            Data-access object providing keyword search access.
        """
        self.repo = repo

    def regex_retriever(
        self,
        celex_id: str,
        regex: str,
        section_types: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Execute keyword search using SQL and return ranked chunks.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier to scope the search.
        regex:
            The regex string used for SQL full-text search.
        section_types:
            Optional decision-section filter (``SectionType`` values); the
            fact's ``section_scope``.

        Returns
        -------
        List[Dict[str, Any]]:
            List of distinct matches mapped to dictionaries, empty if no matches found.
        """
        logger.debug(
            f"Starting regex search for CELEX: '{celex_id}' with regex '{regex}'"
        )

        sql_matches = self.repo.regex_retriever(
            celex_id=celex_id, regex=regex, section_types=section_types
        )
        logger.debug(f"SQL regex search yielded {len(sql_matches)} matches.")

        if not sql_matches:
            return []

        return sql_matches
