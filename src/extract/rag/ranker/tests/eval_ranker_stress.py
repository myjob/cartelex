"""
Since you are using MS-Marco models (like ms-marco-MiniLM-L-12-v2 or ms-marco-TinyBERT-L-2-v2), the scores are typically unbounded logits (e.g., -10 to +10) or sigmoid probabilities (0 to 1).

If your scores are in the 0.0 to 1.0 range, your current 0.0005 boost is effectively a "dead" variable—it’s too small to even act as a tie-breaker for most floating-point comparisons.

The "Flipping" Stress Test
This script simulates a common legal search scenario: An Intro chunk that mentions the query (high semantic match) vs. a Conclusion chunk that actually contains the decision (high legal value).


How to interpret the results:
If intro_chunk is still #1: Your 0.0005 is too weak. In legal RAG, we usually want the "End of Doc" signal to be strong enough to overcome a ~10-15% difference in semantic relevance.

If the Scores are > 1.0: You are looking at raw logits. A boost of 0.0005 in logit-space is mathematically negligible (like adding a grain of sand to a brick).

My Recommendation for Legal Documents
To make the position-weighting meaningful without breaking the model's logic, try Logarithmic Scaling or a Power Curve. This ensures you don't over-boost "Chapter 2" vs "Chapter 1", but you heavily boost the "Final Order."
"""

from flashrank import Ranker, RerankRequest

# 1. Setup Mock Data
# We simulate a case where the 'Conclusion' is actually what the user needs,
# but the 'Intro' has higher keyword overlap/semantic similarity.
query = "What was the final court order regarding the injunction?"

passages = [
    {
        "id": "intro_chunk", 
        "text": "The plaintiff filed a motion for a preliminary injunction regarding the property dispute. This introduction outlines the history of the legal battle...",
        "meta": {"index": 1, "total": 100}
    },
    {
        "id": "conclusion_chunk", 
        "text": "IT IS HEREBY ORDERED that the preliminary injunction is GRANTED. The defendant must cease all construction activities immediately.",
        "meta": {"index": 98, "total": 100}
    }
]

# 2. Initialize Ranker (Using your MS-Marco model)
ranker = Ranker(model_name="ms-marco-MiniLM-L-12-v2") # or "tiny"
request = RerankRequest(query=query, passages=passages)
results = ranker.rerank(request)

# 3. Compare Scores
print(f"{'CHUNK ID':<20} | {'BASE SCORE':<12} | {'YOUR BOOST':<12} | {'NEW SCORE'}")
print("-" * 65)

BOOST_CONSTANT = 0.0005 # Your current value

for res in results:
    p_id = res['id']
    base_score = res['score']
    
    # Simulating your logic: find the original passage to get the index
    meta = next(p['meta'] for p in passages if p['id'] == p_id)
    
    # Calculate boost using your current logic
    boost = meta['index'] * BOOST_CONSTANT
    new_score = base_score + boost
    
    print(f"{p_id:<20} | {base_score:<12.5f} | {boost:<12.5f} | {new_score:.5f}")