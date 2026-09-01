"""Entity & Trust engine: JSON-LD organization, schema fields, trust-page reachability.

Trust-page reachability goes through the SSRF-safe HTTP client (HEAD-then-GET,
default headers, response-size limits).
"""
from __future__ import annotations

import asyncio
from urllib.parse import urljoin

from app.config import CATEGORY_META, TRUSTED_SAMEAS_DOMAINS, settings
from app.models import AuditSection, EffortLevel, Finding, ImpactLevel, Severity
from app.services.html_utils import (
    ParsedPage,
    extract_sameas,
    find_entities_by_type,
)
from app.services.http_client import safe_head_or_get

ORG_TYPES = ("Organization", "Corporation", "LocalBusiness", "Product", "SoftwareApplication")


def score_structured_entity_data(parsed: ParsedPage) -> AuditSection:
    meta = CATEGORY_META["structured_entity_data"]
    section = AuditSection(
        key="structured_entity_data",
        label=meta["label"],
        group=meta["group"],
        points_possible=10,
    )

    entities = find_entities_by_type(parsed.json_ld_blocks, ORG_TYPES)

    if not entities:
        section.points_awarded = 0
        section.findings.append(
            Finding(
                category="structured_entity_data",
                dimension=None,
                severity=Severity.CRITICAL,
                title="No Organization or Product JSON-LD found",
                why_it_matters=(
                    "AI systems rely on structured data (JSON-LD) to build entity confidence. "
                    "Without it, the model has to infer everything from unstructured prose, "
                    "which is far less reliable for citation."
                ),
                evidence="No JSON-LD block with an Organization, Product, or SoftwareApplication type was found.",
                recommendation="Add an Organization JSON-LD block to your homepage <head>.",
                copy_paste_fix=(
                    '<script type="application/ld+json">\n'
                    "{\n"
                    '  "@context": "https://schema.org",\n'
                    '  "@type": "Organization",\n'
                    '  "name": "Your Company Name",\n'
                    '  "url": "https://yourdomain.com",\n'
                    '  "logo": "https://yourdomain.com/logo.png",\n'
                    '  "description": "One sentence describing what you do and for whom.",\n'
                    '  "sameAs": [\n'
                    '    "https://www.linkedin.com/company/yourcompany",\n'
                    '    "https://x.com/yourcompany"\n'
                    "  ]\n"
                    "}\n"
                    "</script>"
                ),
                impact=ImpactLevel.HIGH,
                effort=EffortLevel.LOW,
                points_awarded=0,
                points_possible=7,
            )
        )
        section.findings.append(
            Finding(
                category="structured_entity_data",
                dimension=None,
                severity=Severity.CRITICAL,
                title="No sameAs links (no JSON-LD entity to check)",
                why_it_matters="sameAs links let AI systems cross-reference your entity against trusted knowledge graphs.",
                evidence="No Organization entity exists to hold a sameAs array.",
                recommendation="Add the Organization JSON-LD above, including a sameAs array.",
                impact=ImpactLevel.MEDIUM,
                effort=EffortLevel.LOW,
                points_awarded=0,
                points_possible=3,
            )
        )
        return section

    entity = entities[0]
    section.findings.append(
        Finding(
            category="structured_entity_data",
            dimension=None,
            severity=Severity.PASS,
            title=f"Valid {entity.get('@type', 'Organization')} JSON-LD found",
            why_it_matters="Structured entity data gives AI systems a machine-readable identity to cite confidently.",
            evidence=f"Found JSON-LD with @type={entity.get('@type')!r} and name={entity.get('name')!r}.",
            recommendation="No action needed.",
            impact=ImpactLevel.LOW,
            effort=EffortLevel.LOW,
            points_awarded=7,
            points_possible=7,
        )
    )

    same_as_links = extract_sameas(entity)
    trusted_links = [
        link
        for link in same_as_links
        if any(domain in link.lower() for domain in TRUSTED_SAMEAS_DOMAINS)
    ]

    if trusted_links:
        section.findings.append(
            Finding(
                category="structured_entity_data",
                dimension=None,
                severity=Severity.PASS,
                title="sameAs links point to trusted knowledge sources",
                why_it_matters="sameAs links let AI systems verify your entity against Wikipedia, LinkedIn, Crunchbase, etc.",
                evidence=f"Found sameAs links: {', '.join(trusted_links)}.",
                recommendation="No action needed.",
                impact=ImpactLevel.LOW,
                effort=EffortLevel.LOW,
                points_awarded=3,
                points_possible=3,
            )
        )
        section.points_awarded = 10
    else:
        section.findings.append(
            Finding(
                category="structured_entity_data",
                dimension=None,
                severity=Severity.WARNING,
                title="No sameAs links to trusted sources",
                why_it_matters="Without sameAs links, AI systems have no independent way to verify your entity's identity.",
                evidence=f"sameAs array: {same_as_links if same_as_links else 'missing or empty'}.",
                recommendation="Add sameAs links to your LinkedIn, Crunchbase, Wikidata, or X/Twitter profile.",
                copy_paste_fix='"sameAs": ["https://www.linkedin.com/company/yourcompany", "https://www.crunchbase.com/organization/yourcompany"]',
                impact=ImpactLevel.MEDIUM,
                effort=EffortLevel.LOW,
                points_awarded=0,
                points_possible=3,
            )
        )
        section.points_awarded = 7

    return section


async def _check_page_reachable(base_url: str, href: str | None) -> bool:
    """Whether a trust-page link resolves to a live page.

    Routes through ``safe_head_or_get`` which validates the URL on every
    redirect, applies default headers, and caps response size. Never raises —
    any failure is treated as "not reachable".
    """
    if not href:
        return False

    target = urljoin(base_url, href)
    result = await safe_head_or_get(target, max_bytes=settings.max_html_bytes)
    return bool(result.ok and result.status_code is not None and result.status_code < 400)


async def score_entity_completeness_trust(parsed: ParsedPage, base_url: str) -> AuditSection:
    meta = CATEGORY_META["entity_completeness_trust"]
    section = AuditSection(
        key="entity_completeness_trust",
        label=meta["label"],
        group=meta["group"],
        points_possible=10,
    )

    entities = find_entities_by_type(parsed.json_ld_blocks, ORG_TYPES)
    entity = entities[0] if entities else {}

    required_fields = ("name", "logo", "description")
    present_fields = [f for f in required_fields if entity.get(f)]
    missing_fields = [f for f in required_fields if f not in present_fields]

    schema_points = 5.0 if len(present_fields) == len(required_fields) else 0.0
    section.findings.append(
        Finding(
            category="entity_completeness_trust",
            dimension=None,
            severity=Severity.PASS if schema_points else Severity.WARNING,
            title=(
                "Organization schema includes name, logo, and description"
                if schema_points
                else f"Organization schema is missing: {', '.join(missing_fields) or 'the schema itself'}"
            ),
            why_it_matters=(
                "AI systems use name/logo/description as baseline entity confidence signals "
                "before deciding whether to cite a source."
            ),
            evidence=f"Present fields: {present_fields or 'none'}. Missing: {missing_fields or 'none'}.",
            recommendation="Ensure your Organization JSON-LD includes name, logo, and description fields.",
            impact=ImpactLevel.MEDIUM,
            effort=EffortLevel.LOW,
            points_awarded=schema_points,
            points_possible=5,
        )
    )

    about_href = parsed.about_href
    contact_href = parsed.contact_href

    (about_ok, contact_ok) = await asyncio.gather(
        _check_page_reachable(base_url, about_href),
        _check_page_reachable(base_url, contact_href),
    )

    trust_points = 5.0 if (about_ok or contact_ok) else 0.0
    found_pages = [
        label for label, ok in (("About", about_ok), ("Contact", contact_ok)) if ok
    ]

    section.findings.append(
        Finding(
            category="entity_completeness_trust",
            dimension=None,
            severity=Severity.PASS if trust_points else Severity.WARNING,
            title=(
                f"Trust pages found: {', '.join(found_pages)}"
                if trust_points
                else "No working /about or /contact page found"
            ),
            why_it_matters=(
                "About/Contact pages are trust signals AI systems use to gauge whether a "
                "business is real, established, and citable."
            ),
            evidence=(
                f"about link: {about_href or 'not found'} (200 OK: {about_ok}); "
                f"contact link: {contact_href or 'not found'} (200 OK: {contact_ok})."
            ),
            recommendation="Add a linked /about and/or /contact page that returns HTTP 200.",
            impact=ImpactLevel.MEDIUM,
            effort=EffortLevel.MEDIUM,
            points_awarded=trust_points,
            points_possible=5,
        )
    )

    section.points_awarded = schema_points + trust_points
    return section
