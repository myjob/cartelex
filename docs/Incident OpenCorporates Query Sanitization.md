# Incident Report — OpenCorporates API v0.4 `/companies/search`

## Summary

The `/companies/search` endpoint silently rejects a class of syntactically
valid, real company names, and does so via error signaling that is either
overloaded to the point of being misleading, or absent entirely (a dropped
connection with no HTTP response). Three distinct rejection patterns have
been identified — matched bracket/quote punctuation, a repeated
dotted-abbreviation-plus-comma pattern, and a scare-quoted nickname/trade name
— and nothing in the published API documentation describes any of them.

- **Endpoint**: `GET https://api.opencorporates.com/v0.4/companies/search`
- **Auth**: `X-API-TOKEN` header (per documented recommendation)
- **Client**: internal Python/`requests` integration, ~1 req/sec throttle,
  standard retry/backoff on `429`/`5xx`
- **Suggested severity**: Medium–High — not an outage, but a systematic
  false-negative on legitimate queries, concentrated in jurisdictions (BE,
  NL, and likely other continental European registries) that commonly format
  company names with comma-separated entity-type abbreviations (e.g.
  `"X N.V., Y S.A., City"`), parenthetical detail (`"Chambers (W. & R.)
  Ltd"`), or a quoted trade name (`"DMV 'Campina' BV"`).

---

## Issue 1 — Misleading / overloaded error signaling on HTTP 403

`/companies/search` returns HTTP 403 for at least **three unrelated
conditions**, and in a fourth related condition returns **no HTTP response at
all**. None of this is distinguishable from the documented API contract; it
can only be inferred by sniffing the response `Content-Type` and body — an
undocumented, informal signal our client had to reverse-engineer.

**Observed conditions, all surfaced identically or worse:**

| Condition | Status | Content-Type | Body |
|---|---|---|---|
| Invalid/rejected API token | 403 | `application/json` | JSON error body |
| Genuine IP-level rate throttle | 403 | `text/html` | Generic HTML page ("accessing OpenCorporates at a higher than expected rate") |
| Content-based query rejection (see Issue 2) | 403 | `text/html` | **Same generic HTML page**, containing a Google Tag Manager snippet — i.e. a marketing/website deny page, not an API error page, suggesting this is enforced at a shared edge/CDN layer (Cloudflare or similar) in front of both the website and the API, not in API application code |
| Same content-based rejection, observed elsewhere in the same run | *(none)* | — | TCP connection reset: `('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))` |

**Why this is a problem:**
- A client cannot reliably tell "your token is bad" from "you're being
  rate-limited" from "this specific query was rejected" — all three collapse
  to HTTP 403 with a generic HTML page. We had to build a heuristic
  (content-type sniffing) client-side to separate "valid token, just
  throttled" from "token rejected," and even that heuristic cannot
  distinguish rate-throttling from content-rejection, since both return the
  identical HTML page.
- The fourth condition — a bare dropped connection with no status code and
  no body — is strictly worse than "misleading": it is **no signal at all**.
  A retrying client cannot apply the correct backoff strategy (token
  problems shouldn't be retried; throttling should back off long; a
  malformed/rejected query should never be retried) because it cannot tell
  which situation it's in.
- Because the deny page carries a Google Tag Manager tag, it appears to be
  the same general-purpose website firewall page served for the main site,
  applied to API traffic without an API-appropriate error body (e.g. a JSON
  error with a distinguishing `error` code).
- **This is not theoretical**: in one production run, four names later
  identified as sharing the scare-quote pattern (Issue 2c) were logged by our
  own client as `OpenCorpRateLimitError` — "throttled this IP... retry
  later" — because that classification is based solely on seeing `403` with
  an HTML body, which is indistinguishable from a content-based rejection. We
  had, in effect, misdiagnosed a content-rejection as a rate-limit because
  the API gives no way to tell them apart. Any historical log line reading
  `OpenCorpRateLimitError` in our systems (or presumably any other
  integrator's) may equally be masking an unrelated, permanently-failing
  content issue rather than a transient one worth retrying.

**Requested action:** Return a distinct, documented status/body for each
condition — at minimum, a JSON error body (even on a WAF-level rejection)
with a machine-readable reason code, and never a bare connection drop for a
condition the API intends to enforce deliberately.

---

## Issue 2 — No documentation on query sanitization / escaping requirements for `q`

Nothing in the `/companies/search` documentation describes which characters
or patterns in the `q` parameter are rejected, or how a client should
escape/sanitize a free-text company name before sending it. Three distinct
rejection patterns have been found empirically, at varying confidence
levels — one long-observed and used as the original basis for this client's
existing sanitization (2a, still not independently re-isolated), and two
confirmed with systematic live A/B probing run outside any batch load: the
repeated dotted-abbreviation-comma pattern (2b) and the scare-quoted
nickname/trade-name pattern (2c). All three
need vendor documentation; only 2a is still not fully characterized.

### 2a. Matched bracket / quoting punctuation (originally observed; pattern not yet fully isolated)

This is the example that first motivated sanitizing outgoing queries, and is
reproducible:

```bash
# Refused — HTTP 403, HTML body
curl -sS -D - --get "https://api.opencorporates.com/v0.4/companies/search" \
  --data-urlencode "q=Chambers (W. & R.) Ltd" \
  --data-urlencode "api_token=$opencorporates_api_token"

# Succeeds — HTTP 200
curl -sS -D - --get "https://api.opencorporates.com/v0.4/companies/search" \
  --data-urlencode "q=Chambers W. & R. Ltd" \
  --data-urlencode "api_token=$opencorporates_api_token"
```

A single-character sweep (every character in our client's blocklist tested
alone, e.g. `"Acme(Corp"`, `"Acme)Corp"`, `"Acme<Corp"`, `"Acme"Corp"` — one
character per request, no partner character present) came back **200/JSON
for every one**, including `(` and `)` individually. That rules out a *lone,
unmatched* bracket character as the trigger, but the known-bad example above
contains a **complete matched pair** enclosing text (`(W. & R.)`), which the
single-character sweep never tested. Given the comma finding below turned
out to hinge on a specific repeated pattern rather than "any occurrence of
the character," the same may be true here — e.g. it may take a full matched
pair, or a matched pair combined with an ampersand, or a repeated matched
pair, rather than any single bracket character. **We have not yet re-run the
same systematic isolation against this example that produced the confirmed
finding in 2b; that follow-up is recommended before treating "matched
brackets" as fully characterized.**

### 2b. Repeated dotted-abbreviation-plus-comma (fully isolated and confirmed)

**Reproduction:**
```bash
# Succeeds — HTTP 200
curl -sS -D - --get "https://api.opencorporates.com/v0.4/companies/search" \
  --data-urlencode "q=André Dumont N.V. S.A. Brissel" \
  --data-urlencode "api_token=$opencorporates_api_token"

# Fails — HTTP 403, HTML body
curl -sS -D - --get "https://api.opencorporates.com/v0.4/companies/search" \
  --data-urlencode "q=André Dumont N.V., S.A., Brissel" \
  --data-urlencode "api_token=$opencorporates_api_token"
```

**Systematic isolation** (ten minimal variants of the same string, one
request every 5s to rule out rate-limiting as a confound):

| Variant | Result |
|---|---|
| `André Dumont N.V., S.A., Brissel` (both commas, accented) | **403, HTML** |
| `André Dumont N.V. S.A. Brissel` (no commas) | 200, JSON |
| `Andre Dumont N.V., S.A., Brissel` (both commas, **no** accent) | **403, HTML** |
| `André Dumont N.V., S.A. Brissel` (only 1st comma) | 200, JSON |
| `André Dumont N.V. S.A., Brissel` (only 2nd comma) | 200, JSON |
| `Acme, Corp` (plain comma, no abbreviation) | 200, JSON |
| `Acme N.V., Corp` (one dotted-abbreviation-comma) | 200, JSON |
| `Acme N.V., S.A., Corp` (two dotted-abbreviation-commas) | **403, HTML** |
| `N.V., S.A., N.V., S.A.` (four dotted-abbreviation-commas) | **403, HTML** |
| `Andre Dumont NV SA Brissel Extra` (length-matched control, no punctuation) | 200, JSON |

We also tested every individual character we suspected might be blocklisted
(`( ) [ ] { } < > " \ | ; ,`) alone, embedded in an otherwise clean name
(`Acme<char>Corp`) — every single one, comma included, returned a normal
200/JSON response. **The rejection is not triggered by any individual
character.**

**Confirmed finding:** the trigger is the literal two-character substring
`.,` (a period immediately followed by a comma — i.e. a dotted abbreviation
like `N.V.` or `S.A.` immediately followed by a comma) occurring **twice or
more** in one query. Accents are irrelevant; comma count elsewhere in the
string is irrelevant; a single occurrence is accepted.

This specifically penalizes a **standard, extremely common continental
European corporate-naming convention** — chaining multiple legal-form
abbreviations and a locality with commas (`"X N.V., Y S.A., City"`, common in
Belgian, Dutch, French, and Luxembourgish company records) — meaning real,
correctly-formatted company names from exactly the registries where
OpenCorporates' data is most valuable are silently unsearchable via this
pattern, with no documented way to know that in advance.

### 2c. Scare-quoted nickname / trade name (fully isolated and confirmed)

A production run surfaced four consecutive NL/BE roster names that all
failed identically, all sharing one shape — a word or short phrase wrapped in
single quotes, used as a nickname or trade name rather than a contraction:

- `Coöperatieve Biol. Zuivelveren 'Skylge' v/h Coöperatieve Zuivelfabriek Terscheling`
- `Coöperatieve Melkproduktenbedrijven 'Domo-(Bedum)'`
- `DMV 'Campina' BV`
- `Kartonfabriek 'de Eendracht' NV (trading as BPB de Eendracht)`

All four 403'd. Two of the four also contain a matched parenthesis pair
(`(Bedum)`, `(trading as BPB de Eendracht)`), but our client already strips
brackets before sending (2a), so those reached the wire with the brackets
already removed — the surviving, unexplained variable in all four is the
`'...'` delimited word or phrase. This had not been in our sanitizer's scope:
we deliberately keep single quotes as a category, since they are also the
only way to represent a genuine mid-word apostrophe (`O'Brien`, `Child's
Play`, `d'Obourg`).

Because the production failures had been logged as `OpenCorpRateLimitError`
during a 4985-call/day run, and we already know that label can't distinguish
genuine throttling from a content rejection (Issue 1), the pattern needed the
same live A/B confirmation as 2b, run in isolation with no batch load
running:

```bash
# Refused — HTTP 403, HTML body
curl -sS -D - --get "https://api.opencorporates.com/v0.4/companies/search" \
  --data-urlencode "q=DMV 'Campina' BV" \
  --data-urlencode "api_token=$opencorporates_api_token"

# Succeeds — HTTP 200, 5 real hits
curl -sS -D - --get "https://api.opencorporates.com/v0.4/companies/search" \
  --data-urlencode "q=DMV Campina BV" \
  --data-urlencode "api_token=$opencorporates_api_token"

# Refused — HTTP 403, HTML body (unrelated words, rules out "Campina" specifically)
curl -sS -D - --get "https://api.opencorporates.com/v0.4/companies/search" \
  --data-urlencode "q=Acme 'Nickname' Corp" \
  --data-urlencode "api_token=$opencorporates_api_token"

# Succeeds — HTTP 200, 0 hits (valid empty result, not a rejection)
curl -sS -D - --get "https://api.opencorporates.com/v0.4/companies/search" \
  --data-urlencode "q=Acme Nickname Corp" \
  --data-urlencode "api_token=$opencorporates_api_token"

# Refused — HTTP 403, HTML body (a genuine contraction alongside a scare-quote
# still 403s -- the delimiter quote is what matters, not quotes-in-general)
curl -sS -D - --get "https://api.opencorporates.com/v0.4/companies/search" \
  --data-urlencode "q=O'Brien 'Nickname' Ltd" \
  --data-urlencode "api_token=$opencorporates_api_token"
```

The result alternated 403/200/403/200/403 exactly on cue across two unrelated
word pairs, run outside any batch load — ruling out simple volume-based
throttling as an explanation. **Confirmed finding:** a single quote used as a
delimiter around a standalone word or phrase (space/boundary on the outside,
no partner letter immediately across the quote) triggers the same HTML-403 as
brackets and the comma pattern; a mid-word apostrophe with a letter on both
sides does not, and does not protect a query that also contains a delimiter
quote elsewhere. We now strip a single quote only when it is *not* flanked by
a letter on both sides, which resolves all four production cases without
affecting genuine apostrophes.

**Requested action (2a, 2b, and 2c):**
1. Document, in the `/companies/search` reference, any character or pattern
   restrictions on `q`, ideally with the exact rule (not just "avoid special
   characters").
2. If this is an automated WAF heuristic rather than an intentional API
   restriction, consider excluding the API path from that rule, or scoping
   the rule so it doesn't fire on ordinary abbreviation punctuation, a single
   parenthetical aside, or a quoted trade name.
3. Provide an official client-side escaping/sanitization guideline (or a
   dedicated query-normalization parameter) so integrators don't have to
   reverse-engineer WAF behavior, as we did here.

---

## Business impact

Our integration now defensively strips all bracket/quote punctuation
(`()[]{}<>"\|;`), all commas, and scare-quoted (non-contraction) single
quotes from outgoing queries as a client-side workaround. For commas this is
a strict superset guard (removing every comma guarantees `.,` can never
occur, regardless of how the real rule is eventually documented). This
resolves the symptom but:
- Discards genuine word-boundary and parenthetical information from the
  query, which could reduce match quality server-side beyond what's
  necessary — a narrower server-side rule would let us keep more of the
  original name intact.
- Is fragile: it depends on our own reverse-engineering holding true going
  forward with no documented contract to rely on, and (for brackets
  specifically, 2a) is still based on one historical example rather than a
  fully isolated rule.
- Required correcting our own error handling: `OpenCorpRateLimitError` had
  been assumed to mean genuine IP throttling, but its detection (any `403`
  with an HTML body) is identical to the signature of a content-based
  rejection, and all four names in 2c were originally logged under that
  label — and were only correctly attributed to the scare-quote pattern
  after an isolated live A/B probe ruled out throttling as the cause. We
  cannot currently tell, from the response alone, which of the two actually
  occurred for any past or future `RateLimitError` — see Issue 1.

## Supporting artifacts available on request

Full request/response logs (headers redacted of token), and the probe
scripts used to isolate these patterns (a full blocklist character-by-
character sweep, the ten-variant comma isolation, and an in-progress
bracket-pattern isolation still pending for 2a), are available if your
engineering team wants to reproduce this directly.
