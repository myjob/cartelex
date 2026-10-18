 `2026-08-02
I want to search for company details where my existing source fail. I was considering screen scraping google search in browser given the good results Evaluate the options including Brave, Mojeek, and Marginalia given criteria Index richness & cost per API call
-->
# The three, on your criteria

||Index richness|Cost/call|Notes|
|---|---|---|---|
|**Brave**|30B+ pages, 100M+ daily updates, genuinely independent|$5/1k for Search; Answers $4/1k + token charges. Some tiers quote ~$3/1k|Free tier gone for new signups — $5/month in credits ≈ 1,000 queries. Known to have long-tail blind spots vs Google on niche queries|
|**Mojeek**|Billions of pages; 4th-largest self-crawled English index after Google, Bing, Yandex — no Bing/Google backfill at all|Unpublished, quote-based. They position it well below Bing's $15/1k, and note Google's PSE at $5/1k isn't full-index access|Best fit for your access pattern: full advanced operators, country/language boosting, re-ranking explicitly permitted, authority/keyword/semantic scores on custom plans, and up to 40 results per query on standard plans|
|**Marginalia**|~300–400M documents, with a stated goal of reaching a billion|Free under non-commercial share-alike; commercial API licensing separately|Deliberately down-ranks ads, tracking, and commercial pages in favour of small non-commercial sites|

**Marginalia is disqualified on the merits, not on price.** Its ranking function is an inverse filter for exactly what you want — corporate registry pages, press releases, company websites are the "commercial web" it exists to suppress. Free is irrelevant if the index is anti-correlated with the target.

Between the other two, **Mojeek is the better instrument for entity resolution** despite the thinner index. Your problem is a recall task on names — the same recall-vs-precision distinction you already hit with FlashRanker. 40+ results per call with working `site:`/`intitle:` operators and permitted re-ranking beats 10-20 well-ranked results, and per-_entity_ cost matters more than per-_call_ cost when you're resolving ~12k canonical names. Brave wins if you want a published price and a fast start; Mojeek wins if you want to negotiate a bulk rate for a bounded one-off enrichment run.

# On scraping Google

Two problems, and the second is the one that should decide it. First, it violates Google's ToS, they have litigated against providers over it, and the feed can be throttled or cut at any time. Second, and more relevant to you: a scraped SERP snippet is a terrible provenance record. Your whole architecture is built on audit-grade lineage back to a citable source with hard verification gates — a non-reproducible snippet from a personalised, undated ranking can't pass that gate, and you'd end up treating it as a _hint_ to be confirmed elsewhere anyway. If that's the role, use it as a cheap candidate generator and put the verification burden on a registry, in which case you may as well generate candidates from a source you're allowed to use.

# The bigger issue

For company details specifically, general web search is a weak instrument at any price, and I'd rank these below several non-search options:

- **GLEIF Level 2** — you already have Level 1. Level 2 gives direct/ultimate parent relationships, and the Level 1 records carry `OtherEntityNames` including former legal names. Free, bulk-downloadable, no API budget at all. This is probably where a chunk of your misses actually live.
- **The LEI coverage gap is structural.** LEIs only exist from ~2012. A 1998 cartel decision names entities that dissolved or merged before the LEI system existed — no amount of web search fixes that, but Wikidata does surprisingly well: free SPARQL, models `followed by`/`replaced by`/`parent organization`/former names, and often cross-links to LEI where one exists.
- **National registers** — Zefix (CH), Handelsregister (DE), KVK (NL), INPI/RNE (FR), plus BRIS via the e-Justice portal for cross-border lookup. Authoritative, mostly free, and citable in a way a SERP is not.
- **OpenCorporates** is worth knowing about but disappointing here: the API caps at 500 calls/month, access starts around £2,250/year, and roughly half the registry sources are no longer actively updated.
- **Orbis (Moody's/Bureau van Dijk)** is the one competition economists actually use for cartel work — historical ownership structures and name changes are its whole point. Expensive commercially, but if you have any university affiliation in Zürich it's often already licensed, which would be far cheaper than any of the above.

If you tell me what the failure mode looks like — names too long/multi-company in one string, pre-2010 entities with no LEI, or foreign-language variants — I can be more specific about which of these closes the gap.



# issue with Deepseek search
what do you know about this company rg. jurisdication, state, company ID? 
Ets. Abel Falisse:

- **Company ID (Enterprise Number)**: `BE 0402.398.659`[](https://sentometrics.com/de/companies/be/0402398659/etablissements-abel-falisse)[](https://www.goudengids.be/bedrijf/Awans/L1162637/ETABLISSEMENT+ABEL+FALISSE/)[](https://b2bhint.com/zh/company/be/etablissement-abel-falisse--0402.398.659). This is the official number registered with the Crossroads Bank for Enterprises (CBE) in Belgium.
- **Jurisdiction & State**: **Belgium**[](https://sentometrics.com/de/companies/be/0402398659/etablissements-abel-falisse)[](https://be.linkedin.com/company/abelfalisse?trk=organization_guest_main-feed-card_feed-reaction-header)[](https://b2bhint.com/zh/company/be/etablissement-abel-falisse--0402.398.659). The company's headquarters are located in the Walloon Region[](https://be.linkedin.com/company/abelfalisse?trk=organization_guest_main-feed-card_feed-reaction-header)[](https://www.europages.se/ABEL-FALISSE/BEL091444-00101.html).
- **Legal Status**: Public limited company (**Société Anonyme / Naamloze Vennootschap**)[](https://sentometrics.com/de/companies/be/0402398659/etablissements-abel-falisse)[](https://b2bhint.com/zh/company/be/etablissement-abel-falisse--0402.398.659).
- **Date Founded**: October 23, 1948[](https://sentometrics.com/de/companies/be/0402398659/etablissements-abel-falisse)[](https://www.goudengids.be/bedrijf/Awans/L1162637/ETABLISSEMENT+ABEL+FALISSE/)[](https://crustdata.com/profiles/company/abel-falisse). The company is a family business, now in its third generation[](https://be.linkedin.com/company/abelfalisse?trk=organization_guest_main-feed-card_feed-reaction-header).    
- **Headquarters Address**: Rue de la Chaudronnerie 8, 4340 Awans, Belgium

this is simply wrong


That entity is a good stress test, and your web result is _right about the company but wrong about the case_. Here's what it actually is:

**Case:** Commission Decision 82/371/EEC of 17 December 1981, IV/29.995 — NAVEWA-ANSEAU, French and Dutch texts authentic, CELEX `31982D0371`. It concerns a December 1978 Brussels agreement making a conformity label compulsory on washing machines and dishwashers sold in Belgium from 1 January 1979. Household appliances — which matches Abel Falisse's sector exactly, so the identification is sound.



> [!attention] 
> **But look at the address in the decision itself:** "Éts. Abel Falisse, Av. Emile Digneffe 26, 4000 Liège". Your enrichment returned Rue de la Chaudronnerie 8, 4340 Awans — which is the current registered seat. Same legal person, forty-five years apart. 

For a provenance system, silently attaching the 2026 address to the 1981 addressee is a temporal-validity error of exactly the kind your HeidelTime layer exists to prevent, and it will not be flagged by any entailment check, because both statements are individually true.

Three more things in that paste that would fail an audit:

- **The enterprise number is anachronistic.** The Crossroads Bank for Enterprises was created in 2003. In 1981 this entity had a VAT number, not a CBE number. `0402.398.659` is a valid _current_ key for the entity, but it is not an identifier the decision could have used — same class of problem as the pre-2009 treaty article numbering you already hit.
- **The date conflicts across sources.** You have 23 October 1948, but the registry-derived date of constitution is given as 16-11-48. Two different dates, both from "search results," reconciled into one confident line.
- **"Third generation family business"** is unsourceable marketing copy that shouldn't be anywhere near a fact registry.

The checksum does validate, at least: 04023986 mod 97 = 38, and 97 − 38 = 59, matching the final two digits. That's a free, offline validity gate you can run across all 700 candidate Belgian numbers before any of them reach the registry — it won't catch a wrong-but-valid number, but it kills transcription and hallucination errors instantly. Every EU jurisdiction has an equivalent (NL RSIN 11-proof, FR SIREN Luhn, ES NIF check letter, IT codice fiscale).

**The actual fix for this class of miss is upstream of any search API.** Two things:

1. **Check whether you're parsing decision annexes.** NAVEWA-ANSEAU's addressee list is a table of ~50 undertakings _with their addresses at the time_. That's decision-time, authoritative, already inside your corpus, and already citable. If Falisse came back as unretrieved from your CELEX sources, the likeliest explanation is that a ~400-token chunker is shredding a 50-row annex table into fragments where "Éts. Abel Falisse" and "Av. Emile Digneffe 26" land in different chunks. Fix that and an unknown fraction of your 700 stops being a lookup problem at all.
    
2. **Bulk-load the national registers instead of querying them.** Belgium publishes the full CBE as monthly open data — enterprise, denomination (including former and abbreviated names), address, activity code — free, roughly 2M entities. Load it into `celex_facts` and your Falisse match becomes a local `pg_trgm` join, same primitive you already use for the GLEIF `lei2company_canonical2alias` view. France's INPI RNE, Denmark's CVR, and the UK's Companies House are equally downloadable. For 700 entities concentrated in a handful of jurisdictions, bulk beats per-call every time on both cost and reproducibility.
    

Web search is fine as a _hint generator_ for "which jurisdiction is this name in" — that's the one thing it did well here, narrowing an ambiguous French name to Belgium. Just don't let its output past the verification gate; route the hint to a register and let the register produce the fact.

Is the annex-parsing hypothesis plausible for your other misses — are the unretrieved 700 clustered in older decisions with tabular addressee lists?

```markdown
[!info] Here's a callout title
```


> [!attention] w
> Like this one.