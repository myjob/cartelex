- [ ] q 20260730: would I need an orchestrator such as n4n to run the sequence of Main* batch runs?
- [x] q 20260730: the cli need enabled config in FACT_Extractor -- would prefer a CLI over yaml here
- [ ] q 20260728: a german doc loaded (39452_3336_8) is somewhat well processed and and seems in a few facts stronger than the english version 39452_3384_13; see 
```
      select * from (
		select * from celex_facts.facts f where celex_id = '39452_3336_8' --deutsch
		union
		select * from celex_facts.facts f where celex_id = '39452_3384_13'
	) x order by name
```
- [ ] q 20260716 : why is flashranker stillin charge rather than BGE ranker?
- [ ] q 20260629: what does this in the landmark chapter of the book mean? - Cases IV/30.979, 31.394 -- to cases?
- [ ] q 20260613: how does git local work tree prevent parallel agent conflicts?
- [ ] q 20260610: could I benefit from https://github.com/gastownhall/beads ?
- [ ] q 20260610: could beads  integrate with Obsidian tasks? there may be a community plugin?
- [ ] q 20260610: could beads steer my celex ingestion and verifier?
- [ ] q 20260608: what can pi agentic and MoE do for me ?
- [x] q 20260705: whats the diff between FlashRanker and BGEReranker?
- [ ] why not llama.cpp as ollama alternative? add to ADR
 