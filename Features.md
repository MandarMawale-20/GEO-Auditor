## Multipage Crawl (Implemented)

### What it does

- Audits the homepage by default. Pass `pages=2..10` to opt into a bounded same-site crawl.
- Discovers priority internal pages from homepage links and `sitemap.xml`.
- Keeps only the target host plus its apex/`www` equivalent; other subdomains are excluded unless `CRAWL_INCLUDE_SUBDOMAINS=true`.
- Honors full `robots.txt` `Allow`/`Disallow` rules (longest match, wildcards, `$`), skips assets/queries/utility pages, validates canonicals, and excludes internal `noindex` pages from scoring.
- Fetches with bounded concurrency under a shared monotonic deadline. Per-page timeout, total budget exhaustion, and page exceptions are isolated and reported separately.
- Aggregates checks with the homepage weighted 2× and internal pages 1×. `NOT_APPLICABLE` and `UNKNOWN` pages are excluded from coverage denominators; unknown is never turned into failure.

### Scoring contract

- `CheckResult` is the only object that awards points.
- Each section sums its checks; `total_score = Σ section.points_awarded`; `Σ points_possible = 100`.
- Findings are presentation only.
- Checks use explicit `pass`, `partial`, `fail`, `not_applicable`, and `unknown` statuses.
- Reports carry `scoring_version`; legacy share payloads still decode and render.
- Single-page audits are a strict parity mode: with one page, the page check becomes the site check unchanged.

### Product behavior

- API default is single-page. `pages` accepts 1–10 and is capped by `CRAWL_MAX_PAGES`.
- Reports include page summaries, crawl counts, `points_evaluated`, and `partial` coverage disclosure.
- Shared URLs are stateless (gzip + base64url in the hash). A sizing guard compacts presentation-only duplicates if a URL would become impractically long.
- The roadmap sorts by impact/effort, points recoverable, affected-page importance (homepage 2×), then deterministic `check_id`.
- Unknown checks are excluded from the roadmap.
- PDF/HTML exports include the audited-page summary and per-page detail; shared payloads omit detailed internal evidence.
