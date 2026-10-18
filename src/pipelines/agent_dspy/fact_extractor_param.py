from typing import List, Optional, Any
from pydantic import BaseModel, Field, field_validator
import yaml
import logging

from extract.rag.models.chunk_vo import SectionType

logger = logging.getLogger(__name__)

class FactExtractorParamVO(BaseModel):
    """
    Parameter Value Object for fact extraction pipelines bringing type safety.
    """
    target_fact_name: str = Field(description="The name of the fact being targeted")
    question: str = Field(description="The question to guide the extraction")
    run_state_active: bool = Field(default=True, description="Whether this property extraction is active")
    celex_id: str = Field(description="The CELEX identifier for the document")
    output_schema: str = Field(description="The schema mapping for the output")
    output_type: str = Field(default="dict", description="Output shape hint: 'dict' (default, multi-key structured rows) or 'str' (single free-text answer wrapped in {'value': ...})")
    pipeline: str = Field(default="universal", description="The pipeline type to use: 'universal' or 'hybrid_search'")
    verify_fact_name: Optional[str] = Field(default=None, description="Optional fact name to verify against golden list")
    predecessors: List[str] = Field(default_factory=list, description="A list of target_fact_names to be fetched and used as context")
    force_override: bool = Field(default=False, description="Whether to bypass the existing presence check")
    condition: Optional[str] = Field(default=None, description="A condition to evaluate before running the pipeline")
    dependency_condition: Optional[str] = Field(default=None, description="An inline expression to evaluate against context_data (e.g. 'fact_name == \\'True\\'')")
    section_scope: Optional[List[str]] = Field(default=None, description="Optional decision-section filter for retrieval; SectionType values, e.g. ['fines', 'operative']")
    normalize_iso_country: bool = Field(default=False, description="When true, the extracted 'country' field of each row is normalized to its ISO 3166-1 alpha-2 code after extraction")
    context_facts: List[str] = Field(default_factory=list, description="Ordered fact names read best-effort as LLM context (e.g. by case_summary). Unlike predecessors, a missing entry never skips the fact and never pulls the entry into a --filter-fact run")

    @field_validator("section_scope")
    @classmethod
    def _validate_section_scope(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        """Coerce each entry through ``SectionType`` (YAML is a system boundary).

        Parameters
        ----------
        value:
            Raw section names from the YAML config, or ``None``.

        Returns
        -------
        Optional[List[str]]
            Canonical ``SectionType`` values, or ``None`` when unset.

        Raises
        ------
        ValueError
            If an entry is not a valid ``SectionType`` value.
        """
        if value is None:
            return None
        return [SectionType(entry).value for entry in value]

    def __init__(self, target_fact_name: str, question: str, celex_id: str, output_schema: str, output_type: str = "dict", pipeline: str = "universal", run_state_active: bool = True, verify_fact_name: Optional[str] = None, force_override: bool = False, predecessors: Optional[List[str]] = None, condition: Optional[str] = None, dependency_condition: Optional[str] = None, section_scope: Optional[List[str]] = None, normalize_iso_country: bool = False, context_facts: Optional[List[str]] = None, **data: Any):
        super().__init__(
            target_fact_name=target_fact_name,
            question=question,
            run_state_active=run_state_active,
            celex_id=celex_id,
            output_schema=output_schema,
            output_type=output_type,
            pipeline=pipeline,
            verify_fact_name=verify_fact_name,
            predecessors=predecessors or [],
            force_override=force_override,
            condition=condition,
            dependency_condition=dependency_condition,
            section_scope=section_scope,
            normalize_iso_country=normalize_iso_country,
            context_facts=context_facts or [],
            **data
        )

    @classmethod
    def from_yaml_struct(cls, celex_id: str, struct: dict, force_override: bool = False) -> 'FactExtractorParamVO':
        """
        Loads a single instance from a parsed YAML dictionary structure.
        Injects the current `celex_id` and `force_override` values runtime.
        """
        return cls(
            target_fact_name=struct.get("target_fact_name", ""),
            question=struct.get("question", ""),
            celex_id=celex_id,
            output_schema=struct.get("output_schema", ""),
            output_type=struct.get("output_type", "dict"),
            pipeline=struct.get("pipeline", "universal"),
            run_state_active=struct.get("run_state_active", True),
            verify_fact_name=struct.get("verify_fact_name"),
            predecessors=struct.get("predecessors", []),
            force_override=force_override or struct.get("force_override", False),
            condition=struct.get("condition"),
            dependency_condition=struct.get("dependency_condition"),
            section_scope=struct.get("section_scope"),
            normalize_iso_country=struct.get("normalize_iso_country", False),
            context_facts=struct.get("context_facts", []),
        )

    @classmethod
    def load_from_yaml_file(cls, celex_id: str, yaml_path: str, force_override: bool = False) -> List['FactExtractorParamVO']:
        """
        Loads a list of FactExtractorParamVO instances from a YAML file.
        """
        try:
            with open(yaml_path, 'rt') as f:
                data = yaml.safe_load(f)
            
            # Accommodate both list architectures and single-dict architectural structures in the YAML
            structs = data if isinstance(data, list) else [data]
            
            return [cls.from_yaml_struct(celex_id, struct, force_override) for struct in structs]
        except Exception as e:
            logger.error(f"Failed to load YAML from {yaml_path}: {e}")
            return []

    def to_yaml_struct(self) -> dict:
        """
        Returns a dictionary representation suitable for YAML export.
        Excludes celex_id and force_override since they are typically runtime parameters.
        """
        struct = {
            "target_fact_name": self.target_fact_name,
            "question": self.question,
            "run_state_active": self.run_state_active,
            "output_schema": self.output_schema,
            "output_type": self.output_type,
            "pipeline": self.pipeline,
            "predecessors": self.predecessors,
        }
        if self.verify_fact_name:
            struct["verify_fact_name"] = self.verify_fact_name
        if self.force_override:
            struct["force_override"] = True
        if self.condition:
            struct["condition"] = self.condition
        if self.section_scope:
            struct["section_scope"] = self.section_scope
        if self.context_facts:
            struct["context_facts"] = self.context_facts
        return struct

    @classmethod
    def dump_to_yaml_file(cls, vos: List['FactExtractorParamVO'], yaml_path: str) -> None:
        """
        Dumps a list of FactExtractorParamVO instances to a YAML file.
        """
        try:
            structs = [vo.to_yaml_struct() for vo in vos]
            with open(yaml_path, 'wt') as f:
                yaml.dump(structs, f, sort_keys=False, default_flow_style=False, indent=2)
            logger.info(f"Successfully dumped {len(vos)} configs to {yaml_path}")
        except Exception as e:
            logger.error(f"Failed to dump YAML to {yaml_path}: {e}")