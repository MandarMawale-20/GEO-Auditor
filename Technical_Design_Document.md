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

* **A (90–100):** Excellent AI answerability
* **B (80–89):** Strong AI visibility
* **C (70–79):** Moderate visibility
* **D (60–69):** Weak visibility
* **F (<60):** High risk of AI invisibility

### Important Distinction

GEO Auditor measures **on-page GEO readiness**, not overall brand authority. A well-known brand may still score moderately if its page structure is not optimized for AI extraction.

---

## 5. Product Decisions and Scope Management

Given the limited time available, I focused on a reliable MVP and made several deliberate trade-offs:

* No authentication
* No database
* No background job system
* No headless browser
* Single-page auditing
* Optional LLM usage
* Local-first execution

These choices prioritized speed, simplicity, explainability, and ease of evaluation.

---

## 6. Technical Architecture

GEO Auditor is built with **FastAPI**, **httpx**, **BeautifulSoup**, **Pydantic**, and **ReportLab**. The API is a pure JSON backend — reports are rendered client-side by the static Cloudflare Pages frontend, and an optional standalone HTML export (`app/services/html_export.py`) is generated on demand for offline sharing.

### Core Flow

1. Fetch page HTML, `robots.txt`, and `sitemap.xml` concurrently.
2. Parse the HTML once.
3. Run independent scoring engines.
4. Aggregate results into a single audit model.
5. Generate both HTML and PDF reports.

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

---

## 9. What I Would Build Next

With additional time, I would add:

1. Multi-page crawling
2. Historical audit tracking
3. Competitive benchmarking
4. Headless rendering fallback
5. Real TTFB instrumentation

---

## 10. Conclusion

Building GEO Auditor helped me explore how AI search systems discover and use web content and turn that research into a practical product. The project also helped me practice making product decisions quickly and building a useful MVP within a limited time.

From my own experience using Google Search, Gemini, and Google AI Overviews, I came away with the view that GEO does not replace SEO. Traditional SEO signals still appear to matter, while GEO improves how easily AI systems can understand and cite content. In practice, strong visibility in modern search is likely to require a combination of both SEO and GEO.
