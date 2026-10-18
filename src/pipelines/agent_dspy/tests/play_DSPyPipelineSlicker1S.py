"""
Key Improvements Made:
1. Reduced Latency (The "10s to 1s" fix)
By removing the PydanticAI agent.run() call, you stop the LLM from "thinking" about which tool to call.

Old way: LLM -> Thinks -> Calls SQL -> LLM -> Thinks -> Calls Vector -> LLM -> Returns.

New way: Python (SQL) -> If empty -> Python (Vector) -> DSPy (LLM).

Result: You likely shave 80% off your get_context time.

2. Signature-Driven Extraction
Instead of telling the PydanticAI agent "not to use your own knowledge" in a system prompt, we use a Typed Signature. DSPy will automatically format the prompt to enforce that the output matches your FactOutput model, which is much more reliable than natural language instructions.

3. Separation of Concerns
FactRagPipeline: Now acts as a pure Data Controller. Its only job is to check if a record exists in the DB before calling the DSPy module.

FactRagModule: Handles the logic of how to build the context and how to extract the answer.

4. Direct Tooling
Notice that select_fact4celex and retrieve_vector_context are no longer "tools" given to an agent; they are Methods used by the Module. In DSPy, "tools" are usually reserved for things the model needs to iterate on (like a calculator). For a linear retrieval pipeline, direct method calls are "slicker."

"""

import dspy
from typing import List, Optional, Dict
from pydantic import BaseModel, Field
import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from config.llm_thinking import thinking_kwargs
from config.settings import settings
from pipelines.rag.pgvector.db import FactTable
from pipelines.rag.pgvector.models import Facts
from tools.celex_tools import CelexDatabaseToolkit
from tools.PipelineFactIngestor import FactIngestor

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)
# logging.basicConfig(level=logging.INFO)



# -------------------- Protocols (Interfaces) --------------------

class SQLFactStore(Protocol):
    def select_fact4celex(self, celex_id: str, name: str) -> list: ...


class VectorStore(Protocol):
    def search_document_chunk4celex_id(self, celex_id: str, query: str, hits: int) -> list[str]: ...

# -------------------- Dependencies --------------------

@dataclass
class ProvisioningDeps:
    celex_id: str
    sql_db: Optional[SQLFactStore]
    vector_db: Optional[VectorStore]


# -------------------- Models--------------------
# 1. Define a clean Result Model

class FactExtraction(BaseModel):
    facts: List[str]
    source_used: str # metadata: 'sql' or 'vector'

class FactSignature(dspy.Signature):
    """Extract facts and their specific roles from the provided regulatory context."""
    context = dspy.InputField(desc="SQL facts or Vector chunks")
    question = dspy.InputField()
    output: FactExtraction = dspy.OutputField()

"""
class FactOutput(BaseModel):
    items: List[str]
    source: str  # "sql", "vector", or "none"
"""
class FactOutput(BaseModel):
    items: List[Dict[str, str]] = Field(
        description="List of extracted facts. For single values, this will be a list with one dict."
    )
    source: str

    @property
    def first_value(self) -> Optional[str]:
        """Helper to grab the first value if you know you only want one."""
        if self.items and len(self.items) > 0:
            # Returns the first value of the first dictionary
            return next(iter(self.items[0].values()))
        return None







class VerificationAudit(BaseModel):
    verified_items: List[str] = Field(
        description="Companies with confirmed fine amounts found in context"
    )
    hallucinations: List[str] = Field(
        description="Companies mentioned WITHOUT a fine, or companies_investigated not in the golden list"
    )
    is_valid: bool = Field(description="True if all extracted items have supporting fines")

class FactVerificationSignature(dspy.Signature):
    """
    Perform a strict auditing of extracted fines.
    
    Audit Rules:
    1. Lexical Match: The company name in the 'extracted_list' must clearly correspond 
       to a name in the 'golden_reference_list'.
    2. Contextual Support: The 'context' must explicitly state a monetary penalty 
       for that specific company.
    3. Exclusion: If a company appears in 'extracted_list' but NO fine is found in 
       the 'context', it is a hallucination.
    """
    context = dspy.InputField(desc="Raw CELEX text containing the decision")
    extracted_list = dspy.InputField(desc="The extracted 'Company: Fine' pairs")
    golden_reference_list = dspy.InputField(desc="The official list of parties/companies_investigated")
    
    # Updated output to force the reasoning
    output: VerificationAudit = dspy.OutputField(
        desc="Audit result. 'hallucinations' should include any company from "
             "the extracted list that does not have a fine mentioned in the context."
    )

class FactExtractionSignature(dspy.Signature):
    """
    Extract a list of companies_investigated and their associated fine amounts.
    
    Rules:
    1. ONLY include companies_investigated where a specific fine amount or infringement is mentioned.
    2. If a company is mentioned but no fine or infringement is discussed, EXCLUDE it.
    3. If no fines are found at all, return an empty list.
    """
    context = dspy.InputField(desc="Regulatory document text")
    question = dspy.InputField(desc="Example: 'List companies_investigated and their fines'")
    output: FactOutput = dspy.OutputField()    

class GenericExtractionSignature(dspy.Signature):
    """
    Extract structured data from the context.
    
    Rules:
    1. Focus strictly on: {target_fact_name}.
    2. Format the output as a list of dictionaries.
    3. Use the following keys for the dictionary: {schema_description}.
    """
    context = dspy.InputField(desc="Regulatory document text")
    target_fact_name = dspy.InputField(desc="The data category (e.g. 'Fines')")
    schema_description = dspy.InputField(desc="The keys to use in the dict, e.g. 'company, amount'")
    question = dspy.InputField()
    output: FactOutput = dspy.OutputField()

class GenericVerificationSignature(dspy.Signature):
    """
    Cross-reference the 'extracted_list' against the 'context' and the 'golden_reference_list'.
    
    Audit Rules:
    1. Lexical Match: The entity in the extraction must exist in the 'golden_reference_list'.
    2. Support: The specific value/fact for that entity must be explicitly stated in the 'context'.
    3. Filter: Remove any items that are not explicitly supported.
    """
    context = dspy.InputField()
    extracted_list = dspy.InputField()
    golden_reference_list = dspy.InputField()
    target_fact_name = dspy.InputField()
    output: VerificationAudit = dspy.OutputField()

class UniversalFactModule(dspy.Module):
    def __init__(self, sql_store, vector_store, verify_fact_name=None):
        super().__init__()
        self.sql_store = sql_store
        self.vector_store = vector_store
        self.verify_fact_name = verify_fact_name
        self.extract = dspy.ChainOfThought(GenericExtractionSignature)

    def forward(self, celex_id: str, question: str, target_fact_name: str, schema: str):
        # 1. Provision Context (Logic stays same)
        # ... SQL -> Fallback -> Vector ...
        context = ""

        # 2. Extract with Dynamic Schema
        prediction = self.extract(
            context=context,
            target_fact_name=target_fact_name,
            schema_description=schema, # e.g., "company_name, fine_amount, currency"
            question=question
        )
        
        return prediction


# 3. The "Slick" DSPy Module; # 1. The Brain (Strictly DSPy)
# -------------------- DSPy Module (The Brain & The Pipe) --------------------

class FactRagModule(dspy.Module):
    """
    Rule 2 Compliance: This module encapsulates the fallback retrieval logic 
    (SQL -> Vector) and the extraction reasoning within the forward method.
    """
    def __init__(self, sql_store: SQLFactStore, vector_store: VectorStore ):
        super().__init__()
        # Rule 3: Dependency Injection (The Provisioners)
        self.sql_store = sql_store
        self.vector_store = vector_store
        # self.target_fact_name = target_fact_name
        
        self.extract = dspy.ChainOfThought(FactExtractionSignature)
        

    def forward(self, celex_id: str, question: str):
        """
        The Forward Dispatch: Orchestrates the data flow before reasoning.
        """
        # 1. SQL Attempt (Fast Path)
        facts = self.sql_store.select_fact4celex(celex_id, self.target_fact_name)
        
        source = "sql"
        context = "\n".join([f.value_md for f in facts if f.value_md]) if facts else ""

        # 2. Fallback to Vector (Discovery Path)
        if not context:
            source = "vector"
            chunks = self.vector_store.search_document_chunk4celex_id(
                celex_id, question, 5
            )
            context = "\n---\n".join(chunks)

        # 3. DSPy Reasoning/Extraction
        # Note: In DSPy, calling self.extract(...) is the standard way.
        prediction = self.extract(context=context or "No data found.", question=question)
        
        # Inject metadata into the result object
        prediction.output.source = source if context else "none"
        
        return prediction

class FactRagVerifyModule(dspy.Module):
    """  1. Provision, 2. Extract, 3. Cross-Reference."""
    def __init__(self, 
                 sql_store: SQLFactStore, 
                 vector_store: VectorStore, 
                 # target_fact_name: str, 
                 verify_fact_name: Optional[str] = None):
        super().__init__()
        # Rule 3: Dependency Injection
        self.sql_store = sql_store
        self.vector_store = vector_store
        # self.target_fact_name = target_fact_name
        self.verify_fact_name = verify_fact_name # The Golden List (e.g., "CompanyNames")
        
        self.extract = dspy.ChainOfThought(FactExtractionSignature)
        self.verify = dspy.ChainOfThought(FactVerificationSignature)

    def forward(self, celex_id: str, question: str, target_context_name: str):
        """
        The Forward Dispatch: Orchestrates the data flow before reasoning.
        a Triple-Hop: 1. Provision, 2. Extract, 3. Cross-Reference.
        """
        # 1. SQL Attempt (Fast Path)
        facts = self.sql_store.select_fact4celex(celex_id, target_context_name)
        source = "sql"
        context = "\n".join([f.value_md for f in facts if f.value_md]) if facts else ""

        if not context:
            source = "vector"
            chunks = self.vector_store.search_document_chunk4celex_id(celex_id, question, 5)
            context = "\n---\n".join(chunks)

        if not context:
            return dspy.Prediction(output=FactOutput(items=[], source="none"))
        ## !! FactOutput should be table 

        # --- 2. PROVISION VERIFICATION LIST (The Golden List) ---
        golden_list = []
        if self.verify_fact_name:
            v_facts = self.sql_store.select_fact4celex(celex_id, self.verify_fact_name)
            # Flatten the result into a simple list of names for the LLM to use as a lookup
            golden_list = [f.value_md or f.value_json for f in v_facts]

        # --- 3. EXTRACTION ---
        prediction = self.extract(context=context, question=question)

       # --- 3. VERIFICATION (The Audit) ---
        # Pass the list to the auditor
        audit_result = self.verify(
            context=context, 
            extracted_list=", ".join(prediction.output.items)
        )

        # --- 4. CONDITIONAL VERIFICATION ---
        if self.verify_fact_name and golden_list:
            audit_result = self.verify(
                context=context, 
                extracted_list=", ".join(prediction.output.items),
                golden_reference_list=", ".join(golden_list)
            )
            
            # If the auditor moved everything to hallucinations because no fines existed:
            if not audit_result.output.verified_items:
                logger.info(f"Verified: No legitimate fines found for {celex_id}.")
                return dspy.Prediction(output=FactOutput(items=[], source=source))

            return dspy.Prediction(
                output=FactOutput(
                    items=audit_result.output.verified_items,
                    source=f"{source}_verified"
                )
            )

        # --- 4. SUCCESS ---
        prediction.output.source = source
        return prediction

# -------------------- Pipeline Wrapper (The Ingestor & Controller) --------------------

class FactRagPipeline:
    """
    Separation of Concerns: This class is a PURE Controller. 
    It manages DB state (Existence) and Post-Processing (Saving).
    """
    def __init__(self, target_fact_name: str):
        self.target_fact_name = target_fact_name
        self.ingestor = FactIngestor(target_fact_name)

    async def process_pipeline(self, celex_id: str, question: str, dspy_agent: dspy.Module):
        # 1. High-speed Existence Check (Avoids the 'Brain' entirely if possible)
        with self.ingestor.Session() as session:
            existing = session.query(Facts).filter(
                Facts.celex_id == celex_id, 
                Facts.name == self.target_fact_name
            ).first()
            if existing:
                logger.info(f"Skipping pipeline for {celex_id}: Fact exists.")
                return {"status": "skipped", "celex_id": celex_id}

        # 2. Execute the DSPy Module (The Brain performs the SQL/Vector fallbacks)
        # Using asyncio.to_thread keeps the sync DSPy Module from blocking the event loop
        prediction = await asyncio.to_thread(
            dspy_agent, 
            celex_id=celex_id, 
            question=question,
            target_context_name=self.target_fact_name # Passed dynamically
        )
        
        # 3. Save result back to infrastructure
        self.ingestor.save_fact(celex_id, prediction.output.items)
        
        return {"status": "success", "celex_id": celex_id, "data": prediction.output}

# ------------------------------------------------------------------------------------------------------------
        

async def main():
    # 1. Initialize DB Connections (The "Provisioners")
    logger.debug("Initializing DB Connections")
    fact_table = FactTable()
    vector_store = CelexDatabaseToolkit()



    # 2. Configure the Global LM for DSPy
    # We use the new dspy.LM interface which is more robust for async
    logger.debug("Initializing LM")
    model_name = f"openai/{settings.LLM_MODEL_ID}"  # or "ollama/..." if using local
    lm = dspy.LM(
        model_name,
        api_base=f"{settings.OLLAMA_HOST}/v1",
        api_key="ollama",
        cache=True,  # Enable caching to save costs/time on repeated runs
        **thinking_kwargs(model_name),
    )
    dspy.configure(lm=lm)

    # 3. Setup the Pipeline and Module
    # We use the refactored 'FactRagModule' we discussed earlier


    """
    the celex_id should remain an input to the method, while the target_fact_name belongs in the initialization.

    Think of it like a factory line:

    target_fact_name is how you configure the machines (e.g., "Today this line extracts Commissioners").

    celex_id is the raw material you feed into the line (e.g., "Process Document A, then Document B").

    Why this distinction matters in DSPy
    Batch Processing: By keeping celex_id in the forward (or process_pipeline) call, you can process 1,000 documents using a single instance of your pipeline. If it were in __init__, you would have to destroy and recreate the object for every single document, which is inefficient.

    DSPy Optimization: When you eventually use a DSPy Optimizer (like BootstrapFewShot), the optimizer needs to pass a variety of different inputs (different CELEX IDs) through the same module to see how it performs. If the ID is hardcoded in the constructor, the optimizer cannot "see" it as an input variable to optimize against.

    Separation of Concerns:

    Pipeline Setup: target_fact_name, sql_conn, vector_conn.

    Pipeline Execution: celex_id, question.
    """
    logger.debug("Initializing Pipeline")
    """ The Pipeline owns the target_fact_name because it manages the lifecycle of that specific data point in the DB."""
    pipeline = FactRagPipeline(
        target_fact_name="fines"
    )
   
    # Initialize the DSPy Module with the store connections
    # dspy_agent = FactRagModule(
    dspy_agent = FactRagVerifyModule(
        sql_store=fact_table,
        vector_store=vector_store,
        # target_fact_name="fines",
        verify_fact_name="companies_investigated"
    )

    # 4. Execute for the specific CELEX ID
    target_celex = "37956_1607_5" # "39612_12448_6" # "CELEX-31972D0022" # "CELEX-32000D0146" # "CELEX-31994D0272"
    # question = "Identify the specific committees or groups mentioned in this decision and their roles."
    # question = "list of companies_investigated and their fines, including the fine amount if any"
    question = "List only the companies_investigated that were actually ordered to pay a fine, and the exact amount. If a company was not fined, do not list it."

    logger.info(f"--- Processing {target_celex} ---")
    # We use process_pipeline which handles existence checks + ingestion
    result = await pipeline.process_pipeline(
        celex_id=target_celex,
        question=question,
        dspy_agent=dspy_agent
    )

# !! TODO: performance, verification step, 

    # 5. Handle and Display Results
    if result["status"] == "skipped":
        logger.info(f"✅ Fast-path: Result already exists in database for {target_celex}.")
    else:
        logger.info(f"🚀 Pipeline Complete. Extracted {len(result['data'].items)} items.")
        for idx, item in enumerate(result['data'].items, 1):
            logger.info(f"{idx}. {item}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass        

"""
batch_tasks = [
    {"fact": "fines", "q": "List fines per company", "verify": "companies_investigated"},
    {"fact": "country", "q": "Where is each company based?", "verify": "companies_investigated"}
]

for task in batch_tasks:
    # Use the SAME dspy_agent for everything!
    result = await pipeline.process_pipeline(celex_id, task["q"], dspy_agent)

    """    