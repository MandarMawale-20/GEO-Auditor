# GEO Auditor — Technical Design Document
---

## 1. Summary

Traditional search usually shows a list of links, while AI search tools try to generate a direct answer from the content they can read on a page. During my research, I noticed that a website can rank well on Google and still be hard for AI systems to understand and cite. GEO Auditor was built to check those AI-specific signals and turn them into a practical report that a business owner can act on.

---

## 2. Research Process

Before writing any code, I spent time exploring how AI search systems actually read, process, and cite web pages. Rather than relying on traditional SEO literature, I focused directly on RAG behavior, crawler access, and citation patterns in active AI answer engines.

I focused on a few areas:

* **Crawlability and Access:** How AI crawlers such as GPTBot, PerplexityBot, and ClaudeBot interpret `robots.txt` and `sitemap.xml`.
* **Structured Data and Entity Signals:** How Schema.org JSON-LD helps AI systems identify organizations and products more reliably.
* **Extraction and Chunking:** Why concise, self-contained paragraphs and structured tables are easier for retrieval systems to use.
* **Freshness Signals:** How explicit update dates affect the perceived relevance of a page.

I also reviewed early GEO tools and found that many simply repackaged traditional SEO metrics. The main thing I learned was that AI visibility depends much more on crawlability, clear business information, understandable content, and easily extractable text than on many legacy SEO metrics.

---

## 3. Why I Selected These Scoring Parameters

I selected ten scoring parameters that map directly to how AI retrieval systems work in practice.

| Parameter                           | Why it matters                                  |
| ----------------------------------- | ----------------------------------------------- |
| AI Crawler Accessibility            | AI crawlers must be allowed to access the site. |
| Structured Entity Data              | Provides machine-readable business information. |
| Entity Completeness & Trust Signals | Improves entity confidence and trust.           |
| Quote Readiness                     | Makes content easier to extract into answers.   |
| Query Coverage                      | Helps match user intent to page content.        |
| AI Citation Snippet Quality         | Provides clean citation candidates.             |
| Fact Density & Tables               | Increases information density for retrieval.    |
| Freshness Signals                   | Indicates content recency.                      |
| JavaScript Rendering Risk           | Ensures content is visible in raw HTML.         |
| Media-to-Text Balance               | Ensures enough crawlable text exists.           |

### Excluded Metrics

I intentionally excluded backlinks, domain authority, PageSpeed, keyword density, and social engagement because they are useful for traditional SEO but do not directly measure AI answerability.

---

## 4. Scoring Calibration

The initial draft summed to 110 points. I rebalanced it to a strict **100-point model** and adjusted the weights based on testing against real websites.

### Grade Scale

* **A (85–100):** Excellent AI answerability
* **B (70–84):** Strong AI visibility
* **C (55–69):** Moderate / partial visibility
* **D (40–54):** Weak AI visibility
* **F (<40):** High risk of AI invisibility

### Check-Based Scoring

The original implementation awarded points directly from each engine section. That made it easy for a presentation change to drift away from the score. The current model makes `CheckResult` the only scoring object:

* Each of the 10 categories is composed of one or more stable checks (`app/checks.py`, 21 total).
* A section's points are the sum of its checks; `total_score` is the sum of section points.
* Findings are generated from checks for presentation, not used as a second scoring path.
* Checks have explicit `pass`, `partial`, `fail`, `not_applicable`, and `unknown` statuses. Unknown is never converted into failure.
* `scoring_version` is stored on the report. Legacy payloads still decode with the v1 defaults.

For multipage audits, each check declares one aggregation strategy: `SITE_SINGLETON`, `BEST`, `COVERAGE`, `SITE_CORPUS`, `CONSISTENCY`, or `LATEST`. Coverage excludes `NOT_APPLICABLE` and `UNKNOWN` pages from the denominator, weights the homepage 2× and internal pages 1×, and discloses the evaluated points plus per-check uncertainty. A single-page audit is a strict parity mode: the page check becomes the site check unchanged.

### Important Distinction

GEO Auditor measures **on-page GEO readiness**, not overall brand authority. A well-known brand may still score moderately if its page structure is not optimized for AI extraction.

---

## 5. Product Decisions and Scope Management

Given the limited time available, I focused on a reliable MVP and made several deliberate trade-offs:

* No authentication
* No database
* No background job system
* No headless browser
* Bounded same-site multipage crawl (opt-in, 1–10 pages)
* Optional LLM usage
* Local-first execution

These choices prioritized speed, simplicity, explainability, and ease of evaluation.

---

## 6. Technical Architecture

GEO Auditor is built with **FastAPI**, **httpx**, **BeautifulSoup**, **Pydantic**, and **ReportLab**. The API is a pure JSON backend — reports are rendered client-side by the static Cloudflare Pages frontend, and an optional standalone HTML export (`app/services/html_export.py`) is generated on demand for offline sharing.

### Core Flow

1. Fetch page HTML and `robots.txt` concurrently, then discover `sitemap.xml` (including robots.txt hints). Sitemap hints/children are capped and restricted to the audited site; a 5xx or transport failure for `robots.txt` is `UNKNOWN`, while 4xx/empty is treated as no restrictions. If the homepage redirects to a different origin, `robots.txt` is re-fetched from the final origin. Every fetch validates all resolved IPs and pins a validated address for the TCP connection; redirect targets are validated and pinned again.
2. Parse the HTML once.
3. Run independent scoring engines for the homepage.
4. For `pages>1`, build a deterministic crawl plan from homepage links and sitemap URLs, filter by same-host scope/robots.txt/assets/queries, fetch under a shared monotonic deadline, and audit each page in isolation.
5. Aggregate per-page checks and site-level checks into the 10 frozen sections.
6. Generate both HTML and PDF reports.

### Hybrid Deterministic + LLM Design

* **Deterministic layer:** Calculates the score using Python rules.
* **LLM layer:** Generates semantic recommendations and content rewrites.
* **Fallback behavior:** The audit remains fully functional without an API key.

---

## 7. Report Design Philosophy

I treated the report as the main product. Every finding is evidence-backed, written in business-friendly language, prioritized by impact, and accompanied by a copy-pasteable fix whenever possible.

---

## 8. Validation and Testing

I tested the system against public websites with different structures, including corporate sites, SaaS sites, content sites, and JavaScript-heavy pages. I also verified PDF export quality and fallback behavior when no LLM credentials were configured.

The repository now includes a regression suite (`python -m pytest -q`) covering:

* check-registry weight invariants (21 checks, 100 possible points)
* single-page parity (aggregated status/title/points match the page check)
* coverage weights, not-applicable exclusion, and unknown-never-failure behavior
* Organization-schema consistency (no carrier, one carrier, matching, conflicting)
* robots.txt Allow/Disallow longest-match and wildcard rules
* crawl host scope, canonical validation, budget/timeout distinction, and outage isolation
* DNS-pinned redirect handling and robots.txt refetch after a cross-origin homepage redirect
* legacy share-payload decoding, new report round-trips, and oversized-payload viewer fallback
* PDF and standalone HTML exports

## 9. What I Would Build Next

With additional time, I would add:

1. Recursive crawling with depth limits
2. Historical audit tracking
3. Competitive benchmarking
4. Headless rendering fallback
5. Real TTFB instrumentation

---

## 10. Conclusion

Building GEO Auditor helped me explore how AI search systems discover and use web content and turn that research into a practical product. The project also helped me practice making product decisions quickly and building a useful MVP within a limited time.

From my own experience using Google Search, Gemini, and Google AI Overviews, I came away with the view that GEO does not replace SEO. Traditional SEO signals still appear to matter, while GEO improves how easily AI systems can understand and cite content. In practice, strong visibility in modern search is likely to require a combination of both SEO and GEO.
