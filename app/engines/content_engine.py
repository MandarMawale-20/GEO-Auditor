"""
Content Extractability Engine (deterministic).

Scores:
  * Quote Readiness / Answer Extractability (15 pts)
  * Fact Density & Structured Tables (10 pts)
  * Media-to-Text Balance (5 pts)

All engines operate on the immutable :class:`ParsedPage` data structure —
no BeautifulSoup tree traversal occurs here, so these functions are safe
to call from worker threads.
"""
from __future__ import annotations

from app.config import (
    CATEGORY_META,
    CITATION_MIN_WORDS,
    CITATION_MAX_WORDS,
    MEDIA_RATIO_CRITICAL,
    MEDIA_RATIO_HEALTHY,
    MIN_NUMERIC_DENSITY,
)
from app.models import AuditSection, EffortLevel, Finding, ImpactLevel, Severity
from app.services.html_utils import (
    ParsedPage,
    count_fluff_phrases,
    is_question_heading,
    numeric_density_per_500_words,
    word_to_image_ratio,
)


def score_quote_readiness(parsed: ParsedPage) -> AuditSection:
    meta = CATEGORY_META["quote_readiness"]
    section = AuditSection(
        key="quote_readiness",
        label=meta["label"],
        group=meta["group"],
        points_possible=15,
    )

    heading_pairs = parsed.heading_pairs
    question_headings = [(h, p) for h, p in heading_pairs if is_question_heading(h)]

    # --- Sub-check 1: question-style headings present (5 pts) ---
    question_heading_points = 5.0 if question_headings else 0.0
    section.findings.append(
        Finding(
            category="quote_readiness",
            dimension=None,
            severity=Severity.PASS if question_heading_points else Severity.WARNING,
            title=(
                f"Found {len(question_headings)} question-style heading(s)"
                if question_heading_points
                else "No question-style H2/H3 headings found"
            ),
            why_it_matters=(
                "AI answer engines match user questions against headings. Question-phrased "
                "headings ('What is X?', 'How does X work?') are directly extractable as answers."
            ),
            evidence=(
                f'Example: "{question_headings[0][0]}"'
                if question_headings
                else f"Checked {len(heading_pairs)} H2/H3 headings; none were phrased as questions."
            ),
            recommendation="Rewrite key section headings as direct questions your customers actually ask.",
            copy_paste_fix="<h2>What is [Your Product]?</h2>\n<p>[Your Product] is a ... that helps [audience] ... by ....</p>"
            if not question_heading_points
            else None,
            impact=ImpactLevel.HIGH,
            effort=EffortLevel.MEDIUM,
            points_awarded=question_heading_points,
            points_possible=5,
        )
    )

    # --- Sub-check 2: lead paragraph CITATION_MIN_WORDS..CITATION_MAX_WORDS after a heading (5 pts) ---
    valid_lead_paragraph = None
    for heading, para in question_headings or heading_pairs:
        word_count = len(para.split())
        if CITATION_MIN_WORDS <= word_count <= CITATION_MAX_WORDS:
            valid_lead_paragraph = (heading, para)
            break
    lead_points = 5.0 if valid_lead_paragraph else 0.0
    section.findings.append(
        Finding(
            category="quote_readiness",
            dimension=None,
            severity=Severity.PASS if lead_points else Severity.WARNING,
            title=(
                f"Found a well-sized lead paragraph ({CITATION_MIN_WORDS}-{CITATION_MAX_WORDS} words) following a heading"
                if lead_points
                else f"No heading is followed by a {CITATION_MIN_WORDS}-{CITATION_MAX_WORDS} word direct-answer paragraph"
            ),
            why_it_matters=(
                "Inverted-pyramid style — answering the question immediately in a concise "
                "paragraph — is exactly the shape AI systems extract for direct answers."
            ),
            evidence=(
                f'Under "{valid_lead_paragraph[0]}": "{valid_lead_paragraph[1][:160]}"'
                if valid_lead_paragraph
                else f"Checked paragraphs following headings; none fell in the {CITATION_MIN_WORDS}-{CITATION_MAX_WORDS} word range."
            ),
            recommendation=f"Directly answer each heading's question in the first paragraph, in {CITATION_MIN_WORDS}-{CITATION_MAX_WORDS} words.",
            impact=ImpactLevel.HIGH,
            effort=EffortLevel.MEDIUM,
            points_awarded=lead_points,
            points_possible=5,
        )
    )

    # --- Sub-check 3: low fluff language (5 pts) ---
    fluff_found = count_fluff_phrases(parsed.visible_text)
    fluff_points = 5.0 if not fluff_found else 0.0
    section.findings.append(
        Finding(
            category="quote_readiness",
            dimension=None,
            severity=Severity.PASS if fluff_points else Severity.WARNING,
            title=(
                "No generic marketing fluff detected"
                if fluff_points
                else f"Found {len(fluff_found)} marketing-fluff phrase(s)"
            ),
            why_it_matters=(
                "Fluff phrases ('cutting-edge solutions', 'world-class') carry no extractable "
                "information, so AI systems skip past them rather than citing them."
            ),
            evidence=(f"Detected phrases: {', '.join(fluff_found)}." if fluff_found else "No fluff phrases matched."),
            recommendation="Replace vague marketing language with concrete, specific claims and numbers.",
            impact=ImpactLevel.MEDIUM,
            effort=EffortLevel.MEDIUM,
            points_awarded=fluff_points,
            points_possible=5,
        )
    )

    section.points_awarded = question_heading_points + lead_points + fluff_points
    return section


def score_fact_density(parsed: ParsedPage) -> AuditSection:
    meta = CATEGORY_META["fact_density"]
    section = AuditSection(
        key="fact_density",
        label=meta["label"],
        group=meta["group"],
        points_possible=10,
    )

    table_count = parsed.table_count
    list_count = parsed.list_count
    density = numeric_density_per_500_words(parsed.visible_text)

    table_points = 5.0 if table_count > 0 else 0.0
    section.findings.append(
        Finding(
            category="fact_density",
            dimension=None,
            severity=Severity.PASS if table_points else Severity.WARNING,
            title=f"{table_count} <table> element(s) found" if table_count else "No <table> elements found",
            why_it_matters="Tables are highly extractable structured facts AI systems can quote directly (pricing, comparisons, specs).",
            evidence=f"Found {table_count} <table> tag(s) in the DOM.",
            recommendation="Present comparable data (pricing tiers, feature comparisons, specs) in an HTML <table>.",
            copy_paste_fix="<table>\n  <tr><th>Plan</th><th>Price</th><th>Key Feature</th></tr>\n  <tr><td>Starter</td><td>$0</td><td>...</td></tr>\n</table>"
            if not table_points
            else None,
            impact=ImpactLevel.MEDIUM,
            effort=EffortLevel.MEDIUM,
            points_awarded=table_points,
            points_possible=5,
        )
    )

    facts_points = 5.0 if (density >= MIN_NUMERIC_DENSITY or list_count > 0) else 0.0
    section.findings.append(
        Finding(
            category="fact_density",
            dimension=None,
            severity=Severity.PASS if facts_points else Severity.WARNING,
            title=f"Numeric density: {density}/500 words, {list_count} list(s) found",
            why_it_matters="Specific numbers, stats, and structured lists are the raw material AI systems quote as facts.",
            evidence=f"Measured {density} numeric tokens per 500 words and {list_count} <ol>/<ul> elements.",
            recommendation="Add concrete statistics (percentages, counts, benchmarks) or convert prose into structured lists.",
            impact=ImpactLevel.MEDIUM,
            effort=EffortLevel.MEDIUM,
            points_awarded=facts_points,
            points_possible=5,
        )
    )

    section.points_awarded = table_points + facts_points
    return section


def score_media_to_text_balance(parsed: ParsedPage) -> AuditSection:
    meta = CATEGORY_META["media_to_text_balance"]
    section = AuditSection(
        key="media_to_text_balance",
        label=meta["label"],
        group=meta["group"],
        points_possible=5,
    )

    image_count = parsed.image_count
    ratio = word_to_image_ratio(parsed.word_count, image_count)

    # Reuse the same thresholds from config for consistency.
    if image_count == 0 or (ratio is not None and ratio >= MEDIA_RATIO_HEALTHY):
        points, severity = 5.0, Severity.PASS
        title = "Text-to-image balance is healthy"
        recommendation = "No action needed."
    elif ratio is not None and ratio < MEDIA_RATIO_CRITICAL:
        points, severity = 0.0, Severity.WARNING
        title = "Page is image-heavy — key info may be trapped in graphics"
        recommendation = "Ensure any information conveyed only in images (pricing, stats, feature lists) also exists as real text."
    else:
        points, severity = 2.5, Severity.WARNING
        title = "Text-to-image ratio is borderline"
        recommendation = "Consider adding more crawlable text alongside visual content."

    section.points_awarded = points
    section.findings.append(
        Finding(
            category="media_to_text_balance",
            dimension=None,
            severity=severity,
            title=title,
            why_it_matters=(
                "AI crawlers read text, not images. Information locked inside graphics "
                "(infographics, pricing screenshots) is invisible to AI answer systems."
            ),
            evidence=f"{parsed.word_count} words across {image_count} <img> tags"
            + (f" (ratio {ratio}:1)." if ratio is not None else " (no images found)."),
            recommendation=recommendation,
            impact=ImpactLevel.MEDIUM,
            effort=EffortLevel.MEDIUM,
            points_awarded=points,
            points_possible=5,
        )
    )
    return section
