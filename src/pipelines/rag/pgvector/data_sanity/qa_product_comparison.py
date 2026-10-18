"""
Compare ``products`` vs ``products_name`` facts per celex_id.

Queries:
    SELECT celex_id, value_md FROM facts WHERE name = 'products'
    SELECT celex_id, value_md FROM facts WHERE name = 'products_name'

Markdown table formatting is stripped before comparison so that layout
differences do not mask semantic matches, e.g.:

    | products         |
    |:-----------------|
    | Cast iron rolls  |
    | Cast steel rolls |

Outputs a CSV file containing only rows where the two datasets diverge
significantly (semantically missing products):

    celex_id, product, product_name
"""

import csv
import logging
import re
import sys
from pathlib import Path
from typing import Any

import dspy
from pydantic import BaseModel, Field
from sqlalchemy import text

from config.llm_factory import batch_profile, create_lm
from config.settings import settings
from pipelines.rag.pgvector.session import make_engine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pydantic output models (Rule 3 — structured outputs)
# ---------------------------------------------------------------------------


class ProductDivergence(BaseModel):
    """A single product that is semantically absent or mismatched in products_name.

    Attributes:
        product: Item from the ``products`` fact dataset.
        product_name: Closest counterpart from ``products_name``, or empty string
            when no match was found at all.
    """

    product: str = Field(description="Product item from the 'products' fact.")
    product_name: str = Field(
        description=(
            "Closest match from 'products_name', or empty string when nothing "
            "semantically equivalent exists."
        )
    )


class ProductComparisonResult(BaseModel):
    """Collection of product divergences for a single celex_id.

    Attributes:
        divergences: Products from 'products' that are semantically missing or
            significantly different in 'products_name'.
    """

    divergences: list[ProductDivergence] = Field(
        description=(
            "Products from the 'products' list that are semantically absent or "
            "significantly different in 'products_name'. Return an empty list "
            "when the two datasets are equivalent."
        )
    )


# ---------------------------------------------------------------------------
# DSPy signature (Rule 1 — declarative, Rule 2 — logic-free)
# ---------------------------------------------------------------------------


class ProductSemanticComparisonSignature(dspy.Signature):
    """Compare two product lists and identify semantic gaps.

    Return only products from ``products_list`` that are semantically absent or
    significantly different in ``products_name_list``.  Ignore minor formatting
    differences (punctuation, case, spacing, abbreviations).  Focus on
    substantive semantic divergence — e.g. a product category present in one
    list but entirely missing from the other.
    """

    products_list: str = dspy.InputField(
        desc="Newline-separated product items from the 'products' fact."
    )
    products_name_list: str = dspy.InputField(
        desc="Newline-separated product items from the 'products_name' fact."
    )
    result: ProductComparisonResult = dspy.OutputField(
        desc=(
            "Semantic divergences: products from 'products_list' that are "
            "missing or significantly different in 'products_name_list'."
        )
    )


# ---------------------------------------------------------------------------
# DSPy module (Rule 4 — forward implements the pipe)
# ---------------------------------------------------------------------------


class ProductComparisonModule(dspy.Module):
    """Fetch paired product facts from the DB and detect semantic divergences.

    Parameters
    ----------
    engine:
        SQLAlchemy engine connected to the celex_facts schema.
    """

    def __init__(self, engine: Any) -> None:
        super().__init__()
        self._engine = engine
        self._comparator = dspy.Predict(ProductSemanticComparisonSignature)

    def forward(self) -> list[dict[str, str]]:
        """Run the full comparison pipeline.

        Returns
        -------
        list[dict[str, str]]
            Rows with keys ``celex_id``, ``product``, ``product_name``
            for every detected semantic divergence.
        """
        paired_rows = self._fetch_paired_facts()
        logger.info("Fetched %d celex_ids with both product facts.", len(paired_rows))

        results: list[dict[str, str]] = []

        for celex_id, products_md, products_name_md in paired_rows:
            products_items = _parse_md_table(products_md)
            products_name_items = _parse_md_table(products_name_md)

            if not products_items or not products_name_items:
                logger.debug(
                    "Skipping %s — one or both fact lists are empty after parsing.",
                    celex_id,
                )
                continue

            try:
                prediction = self._comparator(
                    products_list="\n".join(products_items),
                    products_name_list="\n".join(products_name_items),
                )
            except Exception:
                logger.exception("DSPy prediction failed for celex_id=%s", celex_id)
                continue

            for div in prediction.result.divergences:
                results.append(
                    {
                        "celex_id": celex_id,
                        "product": div.product,
                        "product_name": div.product_name,
                    }
                )

        return results

    def _fetch_paired_facts(self) -> list[tuple[str, str, str]]:
        """Return rows where both 'products' and 'products_name' facts exist.

        Returns
        -------
        list[tuple[str, str, str]]
            Each tuple is ``(celex_id, products_value_md, products_name_value_md)``.
        """
        query = text("""
            SELECT
                p.celex_id,
                p.value_md  AS products_md,
                pn.value_md AS products_name_md
            FROM celex_facts.facts p
            JOIN celex_facts.facts pn
                ON p.celex_id = pn.celex_id
               AND pn.name    = 'products_name'
            WHERE p.name = 'products'
              AND p.value_md   IS NOT NULL
              AND pn.value_md  IS NOT NULL
            ORDER BY p.celex_id
        """)

        with self._engine.connect() as conn:
            rows = conn.execute(query).fetchall()

        return [(row[0], row[1], row[2]) for row in rows]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_md_table(value_md: str) -> list[str]:
    """Extract cell values from a markdown table, stripping header and separators.

    Handles tables of the form::

        | products         |
        |:-----------------|
        | Cast iron rolls  |
        | Cast steel rolls |

    Parameters
    ----------
    value_md:
        Raw markdown string from the ``value_md`` database column.

    Returns
    -------
    list[str]
        Cleaned item strings with markdown syntax removed.  The header row
        and separator rows are excluded.
    """
    lines = value_md.strip().splitlines()
    items: list[str] = []
    header_consumed = False

    for line in lines:
        stripped = line.strip()

        if not stripped:
            continue

        if not stripped.startswith("|"):
            # Plain-text line outside a markdown table
            items.append(stripped)
            continue

        # Separator rows: |:---| or |---|
        if re.match(r"^\|[-: |]+\|$", stripped):
            continue

        cells = [c.strip() for c in stripped.split("|") if c.strip()]
        if not cells:
            continue

        if not header_consumed:
            # First content row is the column header — skip it
            header_consumed = True
            continue

        items.extend(cells)

    return [item for item in items if item]


def _configure_dspy() -> None:
    """Configure DSPy from the shared batch profile (cached: a QA replay is free)."""
    dspy.configure(lm=create_lm(batch_profile(cache=True)), adapter=dspy.JSONAdapter())


def _write_csv(rows: list[dict[str, str]], output_path: Path) -> None:
    """Write comparison results to a CSV file.

    Parameters
    ----------
    rows:
        List of dicts with keys ``celex_id``, ``product``, ``product_name``.
    output_path:
        Destination file path.
    """
    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["celex_id", "product", "product_name"])
        writer.writeheader()
        writer.writerows(rows)

    logger.info("Wrote %d divergence rows to %s", len(rows), output_path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the product comparison pipeline and emit a CSV report."""
    output_path = Path(settings.REPORT_PATH) / "qa_product_comparison.csv"

    _configure_dspy()

    engine = make_engine()
    module = ProductComparisonModule(engine=engine)

    logger.info("Starting product comparison pipeline.")
    divergences = module.forward()

    if not divergences:
        logger.info("No semantic divergences found — datasets appear consistent.")
    else:
        logger.info("Found %d divergence rows.", len(divergences))

    _write_csv(divergences, output_path)
    print(f"Report written to: {output_path}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    main()

"""

The -m flag tells Python to run a module (a .py file located somewhere on the Python path) rather than a file path.


.venv/bin/python -m pipelines.rag.pgvector.data_sanity.qa_product_comparison
Python translates the dotted module name into a file path (src/pipelines/rag/.../qa_product_comparison.py), sets __name__ = "__main__", and runs it — which is what triggers the if __name__ == "__main__": main() block at the bottom of the file.

The practical difference vs. python path/to/file.py:

-m adds the current directory to sys.path, so relative imports like from config.settings import settings resolve correctly.
Running a file path directly does not do that, so those imports would fail with ModuleNotFoundError
"""
# .venv/bin/python -m pipelines.rag.pgvector.data_sanity.qa_product_comparison