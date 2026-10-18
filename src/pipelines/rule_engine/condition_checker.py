# https://zerosteiner.github.io/rule-engine/
import rule_engine
from datetime import date

from pipelines.legal.infringement_concept import InfringementConcept, concepts_for

# The article surface every concept condition falls back to: a regex over
# the whole decision text, present for every competition document.
_DEFAULT_ARTICLE_FACT = "law_article_all_relevant"
# Stored values that carry no article tokens; a preferred source holding one
# of these defers to the fallback fact.
_PLACEHOLDER_VALUES: frozenset[str] = frozenset({"", "na", "n/a", "none", "null", "[]"})


class ConditionValidator:
    def __init__(self):
        return

    def evaluate(self, condition: str | None, fact_context: dict) -> bool:
        """Evaluate a rule-engine condition against a fact context dictionary.

        Supports ``exists("key")`` calls in the condition string to test for
        key presence without raising on missing keys.

        Parameters
        ----------
        condition:
            A rule-engine expression string, or ``None`` / empty string to
            unconditionally return ``True``.
        fact_context:
            The dictionary of facts to evaluate the condition against.

        Returns
        -------
        bool
            ``True`` when the condition matches (or is absent), ``False``
            otherwise.
        """
        if not condition:
            return True

        context = rule_engine.Context(resolver=self.unified_custom_resolver)
        # context = rule_engine.Context(resolver=self.custom_resolver)
        rule = rule_engine.Rule(condition, context=context)
        return rule.matches(fact_context)

    def custom_resolver(self, thing, name):
        # Intercept the symbol "exists" and return a Python function (callable)
        if name == "exists":
            # This lambda executes when the rule evaluates `exists("...")`
            return lambda key_to_check: key_to_check in thing

        # For all other symbols, use the standard dictionary lookup
        return rule_engine.resolve_item(thing, name)

    def auto_date_resolver(self, thing, name):
        # Get the raw value from the dictionary
        value = rule_engine.resolve_item(thing, name)

        # If the string looks like a YYYY-MM-DD date, convert it automatically
        if isinstance(value, str) and len(value) == 10:
            try:
                return date.fromisoformat(value)
            except ValueError:
                pass  # Not a valid date string, return as-is

    @staticmethod
    def _article_surface(thing, source_fact: str | None) -> str:
        """Return the article text a concept condition should read.

        Prefers ``source_fact`` (e.g. ``law_article_operative``, the articles
        the decision actually applies) and falls back to
        ``law_article_all_relevant`` when that fact is absent or holds only a
        placeholder, so a document without a classified operative section is
        still gated on its narrative mentions rather than silently never fired.

        Parameters
        ----------
        thing:
            The fact-context mapping.
        source_fact:
            Preferred fact name, or ``None`` for the default surface.

        Returns
        -------
        str
            The article text to canonicalize (may be empty).
        """
        if source_fact:
            preferred = thing.get(source_fact)
            if preferred is not None and str(preferred).strip().lower() not in _PLACEHOLDER_VALUES:
                return str(preferred)
        return str(thing.get(_DEFAULT_ARTICLE_FACT, "") or "")

    def _matches_concept(
        self, thing, concept_slug: str, source_fact: str | None = None
    ) -> bool:
        """Return True when the case's articles canonicalize to ``concept_slug``.

        Decouples the YAML infringement conditions from raw article-number text:
        the case's article surface (see :meth:`_article_surface`) is mapped to
        treaty-era-correct concepts (using the decision date, falling back to
        the CELEX year), and the membership of ``concept_slug`` in that set is
        returned. An unknown slug yields ``False`` rather than raising, so a
        typo in a condition can never crash the batch.

        Parameters
        ----------
        thing:
            The fact-context mapping the rule is evaluated against.
        concept_slug:
            An :class:`InfringementConcept` value (e.g. ``"abuse_of_dominance"``).
        source_fact:
            Optional fact holding the preferred article surface
            (``law_article_operative``); falls back to
            ``law_article_all_relevant`` when absent or a placeholder.

        Returns
        -------
        bool
            Whether the case's canonical concepts include ``concept_slug``.
        """
        try:
            target = InfringementConcept(concept_slug)
        except ValueError:
            return False
        concepts = concepts_for(
            self._article_surface(thing, source_fact),
            thing.get("eu_commisssion_decision_date", ""),
            thing.get("celex_id", ""),
        )
        return target in concepts

    def unified_custom_resolver(self, thing, name):
        # 1. Intercept custom functions/symbols FIRST
        if name == "exists":
            # Returns a callable that the rule engine can execute
            return lambda key_to_check: key_to_check in thing

        # Canonical infringement-concept membership, decoupled from the literal
        # article numbering (see pipelines.legal.infringement_concept). The
        # optional second argument names the fact to read the articles from.
        if name == "same_concept":
            return lambda concept_slug, source_fact=None: self._matches_concept(
                thing, concept_slug, source_fact
            )

        # 2. For all other symbols, perform the standard dictionary lookup
        # Note: If 'name' doesn't exist in 'thing', this raises a SymbolResolutionError automatically
        value = rule_engine.resolve_item(thing, name)

        # 3. Apply automatic type conversions to the resolved value

        # 3a. Boolean-string coercion — DB stores all values as strings,
        #     but rule conditions use the boolean literal ``true`` / ``false``.
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered == "true":
                return True
            if lowered == "false":
                return False

        # 3b. Date coercion
        if isinstance(value, str) and len(value) == 10:
            try:
                return date.fromisoformat(value)
            except ValueError:
                pass  # Not a valid date string, move on

        # 4. Return the final value (either the converted date or the raw value)
        return value
