# Human index to facts reconciliation

#golden_set

Written 2026-11-15. Figures come from a read-only dry run on that day
(`Main_recon_human_index.py --dry-run`); they move with every extraction run.

## Purpose

The hand-maintained case index
(`celex_docs/cases_1964_2024_clean_20250103_long2_JoB.xlsx`, 699 rows, 45 columns)
is the only human-labelled material that covers most of the corpus. This
reconciliation compares four of its columns with the extracted facts and
stores the outcome in one table, `celex_facts.human_index_recon`.

It serves the gold set (`docs/qa/ARCHITECTURE_REVIEW-Fabel5.2.md`, section 11)
in two ways:

- **Agreement is a cheap seed.** The index and the extraction are independent
  and both noisy, so a value they agree on is very likely right.
- **Conflicts are the review list.** Each one is either a slip in the index,
  an extraction error, or a difference in what the two sides mean. A human
  decides which and records it in the `verdict` column.

The reconciliation never decides who is right, and no model is involved: it is
deterministic and runs in a few seconds.

## What maps

| Sheet column | Extracted fact(s) | Compared how |
| --- | --- | --- |
| Z `Formal Decision` | `case_commission_infringement` | Twelve spellings fold onto seven classes; only "infringement yes/no" is comparable |
| AD `Nature of Conduct` (AE `Indicator_Conduct` when AD is empty) | `infringement_category_hor_vert_lic_joint`, `infringement_category_abuse_of_dominance`, `infringement_category_procedural`, `infringement_category_member_states` | As sets of categories: every human category must be among the extracted ones |
| AO `Responsible Commissioner` | `case_commissioner` | Surname only, tolerant of truncation and OCR slips |
| I `firms` | `companies_investigated` | Coverage: every human firm must be matched by at least one roster entity |

Column AD is `Nature of Conduct`, not a second formal-decision column.

The index already had four columns loaded as facts in February 2026
(`human_index_year`, `human_index_decision_date`, `human_index_title`,
`human_index_nace`). Those stay as they are; this table adds the comparison,
which the facts table has no place for.

### Rules per target

**Formal decision.** `Infringement` expects `True`. Exemption, renewal of
exemption, negative clearance, non-applicability, commitments and the combined
"exemption and negative clearance" expect `False`. Interim measures (5
decisions) are `not_comparable`: the fact's question does not say how they
count, and the extraction answers both ways. No fact records exemption,
negative clearance or commitments as such. `exemption_case` is a keyword hit
that is True for 461 documents and cannot serve as a label.

**Nature of conduct.** The category facts are independent of one another, so
the extraction can assert several (Intel: vertical and dominance). The pair
agrees when the human category is among them; the surplus is kept in
`detail.extra`. For the 15 decisions with several natures the label cell is
empty and the codes in column AE are used (`1, 2` is horizontal and
dominance); a parenthesised code (`1 (4)`) marks a secondary aspect and is
ignored.

**Commissioner.** The role note (`(M)`, `(P)`, `(VP)`), casing and first names
are ignored. A surname that is a truncation of the other (`Malfa`) or within
0.8 similarity (`Andriesen`, `VOUUEL`) counts as the same person and is marked
`detail.match = fuzzy` (9 rows). The sheet says "responsible commissioner"
while the fact extracts the signatory of the decision; for the early years the
sheet records the President, who signed.

**Firms.** The index often names a group where the decision lists every
addressed legal entity (Carglass: 4 groups against 18 entities), so equal
counts are not a fair test. The statuses are:

- `agree`: every human firm is matched and no roster entity is left over.
- `extraction_superset`: every human firm is matched, the roster names more.
  This is expected for group-level index rows, but it also covers rosters
  polluted with complainants and third parties.
- `conflict`: at least one human firm has no match in the roster.

Cleaning of the cell before matching: parenthesised notes are removed (they
contain semicolons, as in `ArcelorMittal (L;F;B;I)`), list items that only
name a country are dropped (`Mueller;USA; IMI;UK`), a one-word legal form
joins the name before it (`Citibank; N.A`), and group aliases joined by a
slash are tried separately (`Asahi/AGC Flat Glass`). Two names match when
their squashed forms contain one another (`InBev NV` / `InBevNV`), when the
first distinctive word of the human name occurs in the entity name, when a
one-word entity occurs in the human name, or when one is the acronym of the
other (`SNCF`). The matcher is conservative on purpose: a missed match becomes
a conflict somebody reviews, a false match would pass as agreement.

## What "big" means

Size is the amount of text the extraction had to work with: the tokens stored
for the decision in `pg_embedding_qwen` (`content_tokens`). The bands are the
ones the gold-set proposal uses, set in `settings.SIZE_BAND_MEDIUM_FROM_TOKENS`
and `settings.SIZE_BAND_LARGE_FROM_TOKENS`:

| Band | Tokens | Decisions in the index | With human labels |
| --- | --- | --- | --- |
| S | up to 12,000 | 379 | 351 |
| M | 12,001 to 40,000 | 199 | 142 |
| L | above 40,000 | 119 | 83 |

One indexed decision (`CELEX-32002D0758`) has no chunks and no facts.

## The table

`celex_facts.human_index_recon`, one row per (decision, target), so 2,792 rows
for the 698 indexed decisions. DDL:
`src/pipelines/rag/pgvector/migrate_human_index_recon.sql`.

| Column | Meaning |
| --- | --- |
| `celex_id`, `target` | Primary key. `target` is `formal_decision`, `nature_of_conduct`, `responsible_commissioner` or `firms` |
| `sheet_column`, `fact_names` | Where the two values come from |
| `human_raw`, `human_norm` | The cell as written, and normalised |
| `extracted_raw`, `extracted_norm` | The stored fact value(s), and normalised |
| `status` | `agree`, `conflict`, `extraction_superset`, `human_blank`, `extraction_blank`, `not_comparable` |
| `detail` | JSON: `missing` / `extra` lists, firm counts, fuzzy surname, reason |
| `title`, `decision_year` | From the index, repeated on each row of a decision |
| `content_tokens`, `size_band` | Content size at reconciliation time |
| `source_file`, `source_row` | Workbook file name and worksheet row |
| `verdict`, `verdict_note` | Human adjudication: `human_wrong`, `extraction_wrong`, `both_wrong`, `both_acceptable` |
| `reconciled_at` | Time of the last run that wrote the row |

A re-run upserts every row. `verdict` and `verdict_note` are kept while
`human_raw` and `extracted_raw` are both unchanged and cleared otherwise: a
verdict on a value that has since changed says nothing about the new value.

## Running it

```bash
# once: create the table, then refresh the in-database comments
psql -h localhost -p 5433 -U celex_facts -d celex \
     -f src/pipelines/rag/pgvector/migrate_human_index_recon.sql
psql -h localhost -p 5433 -U celex_facts -d celex \
     -f src/pipelines/rag/pgvector/schema_comments.sql

# compare and log the summary, write nothing (works without the table)
PYTHONPATH=src .venv/bin/python src/batch/Main_recon_human_index.py --dry-run

# compare and fill the table; repeat after any re-extraction
PYTHONPATH=src .venv/bin/python src/batch/Main_recon_human_index.py
```

`--celex-id` limits the run to one decision, `--xlsx` points at another
workbook (default `settings.HUMAN_INDEX_XLSX_PATH`).

The workbook is read once per run, with openpyxl in streaming mode. Its stored
dimension record claims a single cell and it references a drawing part that is
missing from the archive, so polars' Excel reader and openpyxl's normal mode
both fail on it.

### Queries

```sql
-- conflicts to adjudicate, biggest decisions first
SELECT celex_id, title, target, human_raw, extracted_raw, detail
FROM celex_facts.human_index_recon
WHERE status = 'conflict' AND verdict IS NULL
ORDER BY content_tokens DESC NULLS LAST, celex_id, target;

-- outcome per target and size band
SELECT target, size_band, status, count(*)
FROM celex_facts.human_index_recon
GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;

-- gold-set seeds: decisions where all four targets agree
SELECT celex_id, max(title) AS title, max(content_tokens) AS content_tokens
FROM celex_facts.human_index_recon
GROUP BY celex_id
HAVING bool_and(status = 'agree')
ORDER BY 3 DESC;

-- record a verdict
UPDATE celex_facts.human_index_recon
SET verdict = 'human_wrong', verdict_note = 'date line read: signed by Kroes'
WHERE celex_id = '37533_43_1' AND target = 'responsible_commissioner';
```

## Results (dry run, 2026-11-15)

698 decisions, of which 577 carry human labels in columns Z, AD and AO; 666
have a firms cell.

| Target | agree | conflict | extraction superset | extraction blank | not comparable | human blank |
| --- | --- | --- | --- | --- | --- | --- |
| Formal decision | 535 | 14 | | 23 | 5 | 121 |
| Nature of conduct | 371 | 115 | | 91 | | 121 |
| Responsible commissioner | 379 | 6 | | 192 | | 121 |
| Firms | 334 | 111 | 220 | 1 | | 32 |

112 of the 577 labelled decisions agree on all four targets; 198 do when a
firms superset is accepted.

### By size band

Share of the labelled decisions in each band (firms: of the decisions with a
firms cell).

| Measure | S | M | L |
| --- | --- | --- | --- |
| Formal decision: agree | 93% | 94% | 93% |
| Formal decision: conflict | 12 | 2 | 0 |
| Nature of conduct: agree | 58% | 70% | 83% |
| Nature of conduct: extraction blank | 19% | 11% | 8% |
| Commissioner: agree | 69% | 70% | 45% |
| Commissioner: extraction blank | 30% | 30% | 53% |
| Firms: conflict | 15% | 16% | 22% |
| All four targets agree | 21% | 19% | 13% |

Reading:

- Small decisions are where decision type and conduct go wrong: 12 of the 14
  formal-decision conflicts are in band S and none in band L, and conduct
  agreement rises from 58% to 83% with size.
- The commissioner goes the other way. In large decisions the fact is empty in
  53% of cases. The signature line sits at the very end of the document, so
  the likely cause is that retrieval does not reach it in a long text; this
  has not been checked chunk by chunk.
- Firms conflicts are somewhat more frequent in large decisions, because long
  addressee lists are where the roster loses entities (see below).

### The 25 largest labelled decisions

Roster column: firms in the index / entities in the extracted roster.

| Decision | Title | Year | Tokens | Decision | Conduct | Commissioner | Firms | Roster |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `37990_3581_18` | Intel | 2009 | 277,087 | agree | agree | blank | superset | 1 / 15 |
| `39092_7226_4` | Bathroom Equipment Manufacturers | 2010 | 243,574 | blank | agree | blank | superset | 16 / 73 |
| `38344_5856_3` | Prestressing Steel Producers | 2010 | 207,936 | blank | agree | blank | superset | 15 / 87 |
| `CELEX-31994D0815` | Ciment Cement | 1994 | 182,256 | agree | agree | blank | CONFLICT | 42 / 47 |
| `37507_193_6` | AstraZeneca | 2005 | 164,023 | agree | agree | blank | superset | 2 / 5 |
| `AT_39258_8068894_9177_3` | Airfreight | 2017 | 158,459 | blank | agree | blank | CONFLICT | 19 / 19 |
| `38784_311_12` | Wanadoo España vs. Telefónica | 2007 | 140,266 | agree | agree | blank | superset | 2 / 4 |
| `39525_1916_7` | Telekomunikacja Polska | 2011 | 138,144 | agree | CONFLICT | blank | superset | 1 / 48 |
| `37792_4177_3` | Microsoft | 2004 | 136,465 | agree | agree | agree | superset | 1 / 11 |
| `38069_33_4` | Copper plumbing tubes | 2004 | 129,014 | agree | agree | blank | superset | 14 / 33 |
| `39125_2726_3` | Carglass | 2008 | 115,195 | agree | agree | blank | superset | 4 / 18 |
| `34579_1889_2` | MasterCard, EuroCommerce and Commercial Cards | 2007 | 111,719 | agree | CONFLICT | blank | superset | 3 / 18 |
| `39181_1908_8` | Candle Waxes | 2008 | 110,075 | agree | CONFLICT | blank | superset | 10 / 28 |
| `38589_4669_7` | Heat Stabilisers | 2009 | 108,660 | agree | agree | blank | CONFLICT | 26 / 21 |
| `38233_87_1` | Wanadoo Interactive | 2003 | 105,215 | agree | agree | blank | superset | 1 / 2 |
| `CELEX-32003D0002` | Vitamins | 2001 | 103,950 | agree | agree | agree | agree | 13 / 13 |
| `38823_1340_4` | Elevators and Escalators | 2007 | 96,994 | blank | agree | blank | superset | 25 / 32 |
| `37152_72_1` | Plasterboard | 2002 | 96,858 | agree | agree | agree | agree | 4 / 5 |
| `CELEX-31994D0601` | Cartonboard | 1994 | 96,390 | agree | agree | blank | CONFLICT | 23 / 19 |
| `38606_611_1` | Groupement des Cartes Bancaires | 2007 | 95,466 | agree | agree | blank | superset | 1 / 10 |
| `39168_757_6` | Fasteners | 2007 | 94,786 | agree | agree | agree | agree | 7 / 12 |
| `CELEX-32004D0337` | Carbonless Paper | 2001 | 90,590 | agree | agree | agree | agree | 10 / 11 |
| `38543_1810_3` | International Removal Services | 2008 | 86,190 | agree | agree | blank | CONFLICT | 11 / 23 |
| `38113_250_8` | Prokent-Tomra | 2006 | 84,051 | agree | agree | agree | agree | 7 / 7 |
| `39188_2291_2` | Bananas | 2008 | 83,187 | agree | CONFLICT | agree | agree | 3 / 8 |

Five of the 25 agree on everything (Vitamins, Plasterboard, Fasteners,
Carbonless Paper, Prokent-Tomra). The commissioner is blank in 18.

## Conflicts

### Responsible commissioner (6)

| Decision | Title | Year | Tokens | Index | Extraction | Assessment |
| --- | --- | --- | --- | --- | --- | --- |
| `37533_43_1` | Choline Chloride | 2004 | 42,615 | Mario Monti | Neelie Kroes | Index contradicts itself: its own decision date is 8 December 2004, after Kroes took office in November 2004 |
| `CELEX-31972D0237` | Davidson Rubber Co | 1972 | 6,433 | Franco M. Malfatti | S.L. Mansholt | Index contradicts itself: its own decision date is 9 June 1972, after Mansholt succeeded Malfatti in March 1972 |
| `AT_38700_8179861_2920_3` | Hellenic Republic | 2008 | 9,259 | Nelie Kroes | Margrethe Vestager | Different documents: the index describes the decision of 5 March 2008, the stored decision date is 17 April 2018 |
| `CELEX-31964D0233` | Grosfillex & Fillistorf | 1964 | 1,341 | Walter Hallstein | European Economic Community Commission | Extraction wrong: it returned the institution |
| `CELEX-32000D0475` | CECED | 1999 | 7,078 | Karel van Miert | Mario Monti | Open. Index and stored date both say 24 January 1999, when van Miert was in office; the CELEX number is from 2000 |
| `38456_792_2` | Bitumen NL | 2006 | 70,589 | Nelie Kroes | Charlie McCreevy | Open; may be a decision signed by another member, which is the signatory / responsible gap |

The first three assessments rest on dates recorded in the index and the facts
table and on the dates of office, not on the decision text; nobody has read
the date lines yet.

### Formal decision (14)

Eight old decisions the index calls an infringement while the extraction says
`False` (none carries a fine), and six the index calls an exemption or
negative clearance while the extraction says `True`.

| Decision | Title | Year | Tokens | Index | Extraction |
| --- | --- | --- | --- | --- | --- |
| `CELEX-31971D0257` | Albra-Brasserie Espérance | 1971 | 2,133 | Infringement | False |
| `CELEX-31971D0259` | Brasserie MAES | 1971 | 2,237 | Infringement | False |
| `CELEX-31971D0268` | Asphaltoïd-Keller | 1971 | 1,001 | Infringement | False |
| `CELEX-31978D0516` | RAI / UNITEL | 1978 | 1,872 | Infringement | False |
| `CELEX-31978D0922` | Zanussi | 1978 | 3,944 | Infringement | False |
| `CELEX-31979D0253` | FIDES | 1979 | 1,248 | Infringement | False |
| `CELEX-31979D0670` | AM&S Europe Limited | 1979 | 2,317 | Infringement | False |
| `CELEX-31980D1074` | Industrieverband Solnhofener Natursteinplatten | 1980 | 6,258 | Infringement | False |
| `CELEX-31965D0426` | Hummel-Isbecque | 1965 | 2,238 | Exemption | True |
| `CELEX-31973D0323` | Prym-Beka | 1973 | 3,320 | Exemption | True |
| `CELEX-31984D0380` | Synthetic Fibres | 1984 | 7,531 | Exemption | True |
| `CELEX-31988D0477` | British Dental Trade Association | 1988 | 8,494 | Exemption | True |
| `CELEX-31999D0421` | P&O Stena Line | 1999 | 21,515 | Negative Clearance | True |
| `37396_45_3` | Revised TACA | 2002 | 12,397 | Negative Clearance | True |

Six of the first eight are procedural by the index's own columns (Article
`Reg. 17/62 (Articles 11-14)`, Nature of Conduct `Procedural`): all but
Zanussi and Solnhofener. Calling them an infringement of the competition rules
is doubtful and `False` from the extraction is defensible. In the second
group the index contradicts itself on British Dental Trade Association:
`Exemption` in column Z, a fine of 100,000 in column AN. All 14 need a
reader.

### Nature of conduct (115)

The disagreement is mostly horizontal against vertical and cartel against
joint venture:

| Index | Extraction | Decisions |
| --- | --- | --- |
| horizontal | vertical | 26 |
| vertical | horizontal | 17 |
| horizontal | joint venture | 13 |
| procedural | horizontal | 10 |
| joint venture | horizontal | 9 |
| license | horizontal | 5 |
| vertical | license | 5 |
| other pairs | | 30 |

"Horizontal filed as joint venture" includes plain cartels (Candle Waxes, Raw
Tobacco Spain, Needles), which points at the fact's question: "joint ventures
and alliances involving coordination of activities" describes a cartel too.
In the procedural rows `infringement_category_procedural` has no stored value
(the fact only runs under a catalogue condition) while the agreement category
fact answered anyway. In 91 further decisions no category fact has a value at
all.

### Firms (111 conflicts, 220 supersets)

The conflicts fall into four groups.

**Roster recall misses.** The index lists many more addressees than the
roster holds. These are the most valuable finds, all in large decisions:

| Decision | Title | Tokens | Index | Roster | Not matched |
| --- | --- | --- | --- | --- | --- |
| `AT_40135_8399817_5458_10` | (Forex, no index title) | 120,431 | 22 | 3 | 19 |
| `39824_6567_14` | Trucks | 62,077 | 18 | 4 | 15 |
| `39914_8910_5` | Euro Interest Rate Derivatives | 130,975 | 21 | 8 | 12 |
| `39861_4230_3` | Yen Interest Rate Derivatives | 54,274 | 13 | 3 | 11 |
| `40018_2611_3` | Car battery recycling | 64,199 | 11 | 4 | 6 |

The same pattern exists in smaller decisions (`39563_7279_3`: 40 firms
against 3; `39965_1823_5`: 11 against 2). The Trucks roster also contains the
literal entry `[Industry association]`.

**Wrong document behind the identifier.** The index lists `36756_68_16`
(Sodium Gluconate) twice, as cases 36756-1 (2001) and 36756-2 (2004). The
stored document under that identifier is the decision addressed to
Jungbunzlauer alone, while the cartel roster the index describes sits under
`36756_74_4`, which the index does not list.

**Translated names.** For old decisions extracted from a translated text the
roster holds translated company names: `Comptoir belge de l'azote` became
`Belgian Nitrogen Counter`, `Houillères d'Anderlues SA` became
`Anderlues Coal Mines, S.A.` (`CELEX-31968D0375`, 10 of 19 firms unmatched).

**Noise in the index.** Addresses and remarks as list items (`Lankro Way`,
`Woluwe Garden`, `Frankfurt a.M`, `jointly and severally`), misspellings
(`Quantas`, `Mersä-Botnia`) and acronyms the roster spells out differently.

The 220 supersets are mostly group-level index rows. In abuse-of-dominance
decisions they are roster pollution instead: Intel has 1 firm in the index and
15 in the roster (AMD, Media-Saturn, NEC and others), Microsoft 1 against 11
(Sun Microsystems, industry associations), Telekomunikacja Polska 1 against
48. Complainants and third parties are being extracted as parties.

## Limits

- **Agreement is not proof.** Both sides can be wrong in the same way, and the
  firm matcher can pair two different firms that share a first word within
  one decision (`British Airways` / `British Midland`). Treat an agreed value
  as a seed to confirm, not as gold.
- **Three of the four targets compare less than the column says.** Formal
  decision is reduced to infringement yes/no; conduct agreement tolerates
  extra extracted categories; commissioner compares the signatory.
- **121 indexed decisions have no labels** in columns Z, AD and AO (78 of them
  marked `new` in the index), mostly the recent ones. They are stored as
  `human_blank`.
- **90 decisions in the facts table are not in the index** and do not appear
  in this table.
- The index lists `36756_68_16` twice; the first row is kept and the run logs
  a warning.

## Code

| File | Role |
| --- | --- |
| `src/pipelines/legal/human_index_recon.py` | Typed rows and results, normalisers, the four comparisons (pure, no DB) |
| `src/pipelines/rag/pgvector/human_index_recon_repository.py` | Reads the facts and content sizes, upserts the table |
| `src/pipelines/rag/pgvector/migrate_human_index_recon.sql` | DDL |
| `src/batch/Main_recon_human_index.py` | Reads the workbook, runs the comparison, writes the table |
| `src/pipelines/legal/tests/test_human_index_recon.py`, `src/pipelines/rag/pgvector/tests/test_human_index_recon_repository.py`, `src/batch/tests/test_Main_recon_human_index.py` | Unit tests, no live database |

## Next steps

1. Apply the migration and load the table (run book at the top of `ISSUES.md`).
2. Adjudicate the 6 commissioner and 14 formal-decision conflicts; they are
   few and each needs only the operative part or the date line.
3. Decide whether to add further columns: `Fines` (AN) against `fines`,
   `Leniency` (Y) against `leniency_case`, `Article` (AC) against
   `law_article_operative`, and `decision_date` (H) against
   `eu_commission_decision_date`.
4. Feed agreed values into the gold-set pre-fill (review section 11, phase 3).
