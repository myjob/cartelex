"""
Us the flash_ranker.py function to classify the predefined company with the list of company strings given as 2nd argument.
Return per predefined company an alias if available 
Example 



input: 
1) list of predefined company names
[Google]
2)  json list of company strings 
["Google", "Google Ireland", "Google LLC", "Amazon", "eBay", "Fnac", "PriceMinister", "Conrad", "1PlusV", "Acheter moins cher", "BDZV", "VDZ", "BEUC", "Ciao", "Microsoft", "Company AC", "Deutsche Telekom", "ETTSA", "Euro-Cities", "Expedia", "FairSearch", "Foundem", "Getty", "HolidayCheck", "Hot Maps", "ICOMP", "Kelkoo", "MIH", "MoneySupermarket", "News Corp", "Nextag", "Guenstiger", "OCU", "Odigeo", "OIP", "TradeComet", "TripAdvisor", "Trivago", "Twenga", "VfT", "Visual Meta", "Yelp", "Axa", "Permira", "Opodo", "Go Voyages", "eDreams", "MIH", "MoneySupermarket", "News Corp", "Nextag", "Guenstiger", "OCU", "Odigeo", "OIP", "TradeComet", "TripAdvisor", "Trivago", "Twenga", "VfT", "Visual Meta", "Yelp", "1PlusV", "Acheter moins cher", "BDZV", "VDZ", "Amazon", "Apple", "ECC Köln", "Raymond James", "Seznam", "Yahoo", "America Online", "Bing."]

output:
[
    {
        "predefined_company": "Google",
        "alias": "Google Ireland",
        "score": 0.95
    },
    {
        "predefined_company": "Google",
        "alias": "Google LLC",
        "score": 0.95
    }
    
]

"""

import logging
import dspy
from pydantic import BaseModel, Field
from typing import List, Optional

from extract.rag.ranker.ranker_factory import RankerProtocol, create_ranker
from extract.rag.models.chunk_vo import ChunkVo

logger = logging.getLogger(__name__)


class CompanyAlias(BaseModel):
    predefined_company: str = Field(description="The primary company name being queried.")
    alias: str = Field(description="The alias or alternate company name identified.")
    score: float = Field(description="The matching score from FlashRanker.")

class AliasClassificationResult(BaseModel):
    aliases: List[str] = Field(description="List of strings from the candidates that are genuine aliases, subsidiaries, or legal alternate names of the predefined company.")

class ClassifyAliasSignature(dspy.Signature):
    """
    Identify genuine aliases, subsidiaries, or legal alternate names of the predefined company from a list of candidate strings.
    Exclude competitors or unrelated companies.
    """
    predefined_company: str = dspy.InputField(desc="The target corporate entity.")
    candidates: List[str] = dspy.InputField(desc="The top ranked candidate strings based on semantic similarity.")
    
    extracted: AliasClassificationResult = dspy.OutputField(desc="The genuine aliases identified from the candidates.")

class CorpRanker(dspy.Module):
    """
    A Ranker module that classifies a predefined company against a list of company strings,
    returning an alias per predefined company if available.
    """
    def __init__(self, ranker: Optional[RankerProtocol] = None, top_k: int = 25):
        super().__init__()
        # Provisioner Pattern
        self.ranker = ranker or create_ranker()
        self.top_k = top_k
        self.extractor = dspy.Predict(ClassifyAliasSignature)
        
    def forward(self, predefined_companies: List[str], company_strings: List[str]) -> List[CompanyAlias]:
        """
        Executes the "Slick Pipe" (FlashRank -> DSPy Extraction) to identify aliases.
        """
        if not company_strings or not predefined_companies:
            return []

        # Convert company strings to chunks for ranking (doc_pos=0 to prevent structural boosts)
        chunks = [
            ChunkVo(id=str(idx), celex_id="", chunk=comp_str, origin="company_list", similarity=0.5, doc_pos=0)
            for idx, comp_str in enumerate(company_strings)
        ]

        all_aliases = []

        for company in predefined_companies:
            # Step 1: Vector/Semantic Retrieval via FlashRanker
            ranked_chunks = self.ranker.rank_chunkvo(query=company, chunks=chunks, top_k=self.top_k)
            
            if not ranked_chunks:
                continue

            # Map chunk to score for fast lookup
            chunk_to_score = {rc.chunk: rc.score for rc in ranked_chunks}
            candidates = list(chunk_to_score.keys())
            
            # Step 2: DSPy Classification / Extraction
            try:
                extraction = self.extractor(
                    predefined_company=company,
                    candidates=candidates
                )
                
                # Format into final Output structure
                for matched_alias in extraction.extracted.aliases:
                    if matched_alias in chunk_to_score:
                        all_aliases.append(
                            CompanyAlias(
                                predefined_company=company,
                                alias=matched_alias,
                                score=round(chunk_to_score[matched_alias], 4)
                            )
                        )
            except Exception as e:
                logger.error(f"Failed to extract aliases for {company}: {e}")
                
        return all_aliases