"""Standalone, self-contained HTML report renderer for offline sharing / download.

The output is a single HTML document with all CSS embedded inline and no
external asset references, so it can be saved as a file or pasted into an
email and will render identically. It mirrors the design system colors and
typography while remaining print-friendly.
"""
from __future__ import annotations

from html import escape as _escape

from app.models import AuditReport


def _e(value: object) -> str:
    """Escape page-derived/report-derived text for standalone HTML output."""
    return _escape("" if value is None else str(value), quote=True)

_CSS = """
  html { font-size: 16px; color-scheme: light; }
  body { margin: 0; padding: 2rem;
    font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
    background: #f5efe2; color: #211c12; line-height: 1.7; }
  h1 { font-size: 1.75rem; margin: 0 0 .25rem; color: #211c12; }
  h2 { font-size: 1.25rem; margin: 1.5rem 0 .5rem; border-bottom: 1px solid #ddcda3; padding-bottom: .25rem; color: #211c12; }
  h3 { font-size: 1.05rem; margin: 1rem 0 .35rem; color: #211c12; }
  p { margin: 0 0 1rem 0; color: #5c5236; }
  a { color: #2c6b62; }
  table { border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: .9rem; }
  th, td { border: 1px solid #ddcda3; padding: .5rem .75rem; text-align: left; vertical-align: top; }
  th { background: #efe6d2; color: #8c8163; font-size: .72rem; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; }
  td { color: #5c5236; }
  code, pre { font-family: "IBM Plex Mono", "SFMono-Regular", Consolas, "Liberation Mono", Menlo, Monaco, monospace; }
  code { background: #efe6d2; padding: .15rem .4rem; border-radius: 4px; font-size: .82rem; color: #5c5236; }
  pre { background: #efe6d2; padding: 1rem; border-radius: 6px; overflow-x: auto; border: 1px solid #ddcda3; }
  .score-circle { width: 110px; height: 110px; border-radius: 50%;
    background: conic-gradient(from 0deg, #2c6b62 0deg, #ddcda3 0deg);
    display: inline-flex; align-items: center; justify-content: center; border: 3px solid #ddcda3; }
  .score-inner { background: #f5efe2; border-radius: 50%; width: 74px; height: 74px;
    display: flex; align-items: center; justify-content: center; flex-direction: column; }
  .badge { display: inline-block; padding: .2rem .6rem; border-radius: 6px; font-size: .72rem; font-weight: 600; }
  .badge-yes { background: rgba(74,122,78,.12); color: #4a7a4e; }
  .badge-partial { background: rgba(176,106,31,.12); color: #b06a1f; }
  .badge-no { background: rgba(161,74,53,.12); color: #a14a35; }
  .grade-a { background: rgba(74,122,78,.12); color: #4a7a4e; }
  .grade-b { background: rgba(44,107,98,.12); color: #2c6b62; }
  .grade-c { background: rgba(176,106,31,.12); color: #b06a1f; }
  .grade-d { background: rgba(184,90,21,.12); color: #b85a15; }
  .grade-f { background: rgba(161,74,53,.12); color: #a14a35; }
  .finding { border: 1px solid #ddcda3; border-left: 3px solid #ddcda3; border-radius: 10px; padding: 1rem 1.25rem; margin-bottom: 1rem; background: #f5efe2; }
  .finding-critical { border-left-color: #a14a35; }
  .finding-warning { border-left-color: #b06a1f; }
  .finding-info { border-left-color: #2c6b62; }
  .finding-pass { border-left-color: #4a7a4e; }
  .evidence { background: #efe6d2; border-left: 3px solid #2c6b62; padding: .75rem; font-size: .82rem; border-radius: 8px; }
  .disclaimer { font-size: .78rem; color: #8c8163; border-top: 1px solid #ddcda3; margin-top: 2rem; padding-top: 1rem; }
  .container { max-width: 900px; margin: 0 auto; }
  @media print { body { background: #fff; padding: 0; } .finding { break-inside: avoid; } }
"""

_STATUS_LABELS = {
    "yes": ("Yes", "badge-yes"),
    "partial": ("Partial", "badge-partial"),
    "no": ("No", "badge-no"),
}


def _grade_class(grade: str) -> str:
    g = (grade or "").lower()
    if g in ("a", "b", "c", "d", "f"):
        return f"grade-{g}"
    return "grade-c"


def _sev_class(severity: str) -> str:
    value = getattr(severity, "value", severity)
    return f"finding-{value}" if value else "finding-info"


def _findings_badge(severity: str) -> str:
    value = getattr(severity, "value", severity)
    if value == "pass":
        return _STATUS_LABELS["yes"][1]
    if value == "info":
        return _STATUS_LABELS["partial"][1]
    return _STATUS_LABELS["no"][1]


def render(report: AuditReport) -> str:
    """Build a fully self-contained HTML document for the given report."""
    display_url = report.final_url or report.url
    pct = int(round(report.total_score / report.max_score * 100)) if report.max_score else 0

    score_table_rows = "".join(
        f"<tr><td>{_e(s.label)}</td><td>{_e(s.group)}</td>"
        f"<td><strong>{s.points_awarded:g}</strong> / {s.points_possible:g}</td>"
        f"<td>{s.percentage:g}%</td></tr>"
        for s in report.sections
    ) or '<tr><td colspan="4" style="color:#8c8163;">No data</td></tr>'

    matrix_rows = "".join(
        f"<tr><td>{_e(row.question)}</td>"
        f'<td><span class="badge {label_class}">{label_text}</span></td>'
        f"<td>{_e(row.evidence or '')}</td></tr>"
        for row in report.answerability_matrix
        for label_text, label_class in [_STATUS_LABELS.get(row.status.value, _STATUS_LABELS["no"])]
    ) or '<tr><td colspan="3" style="color:#8c8163;">No matrix data available</td></tr>'

    findings_html = []
    for f in report.all_findings:
        sev = getattr(f.severity, "value", f.severity)
        findings_html.append(
            f'<div class="finding {_sev_class(sev)}">'
            f'<div style="display:flex; align-items:center; gap:.6rem; margin-bottom:.4rem;">'
            f'<span class="badge {_findings_badge(sev)}">{_e(sev.upper())}</span>'
            f'<h3 style="margin:0;">{_e(f.title)}</h3></div>'
            f'<p style="margin:0 0 .5rem;">{_e(f.why_it_matters)}</p>'
            f'<div class="evidence">{_e(f.evidence)}</div>'
            f'<p style="margin:.5rem 0 0;"><span style="color:#211c12; font-weight:600;">Fix:</span> {_e(f.recommendation)}</p>'
            f"</div>"
        )
    findings_html = "".join(findings_html) or (
        '<p style="color:#8c8163;">No detailed findings available.</p>'
    )

    roadmap_items = report.priority_roadmap or []

    pages_rows = "".join(
        f"<tr><td>{_e(page.final_url or page.url)}</td>"
        f"<td>{_e(page.role)}</td>"
        f"<td>{page.points_awarded:g} / {page.points_possible:g}</td>"
        f"<td>{_e('Excluded (noindex)' if page.error == 'noindex_excluded' else ('Audited' if not page.error else 'Not evaluated: ' + page.error))}</td></tr>"
        for page in report.pages
    )
    pages_section = (
        "<h2>Audited Pages</h2><table><thead><tr><th>Page</th><th>Role</th>"
        "<th>Page Checks</th><th>Status</th></tr></thead><tbody>"
        + pages_rows
        + "</tbody></table>"
        if pages_rows and report.audit_scope == "site"
        else ""
    )
    scope_bits = ""
    if report.audit_scope == "site" or report.partial:
        scope_bits = (
            '<strong style="color:#5c5236;">Scope:</strong> '
            + ("Site crawl" if report.audit_scope == "site" else "Single page")
            + ' · <strong style="color:#5c5236;">Pages:</strong> '
            + f"{report.pages_audited} · "
            + '<strong style="color:#5c5236;">Points evaluated:</strong> '
            + f"{report.points_evaluated:g}/{report.max_score} · "
        )

    page_detail_html = []
    if report.audit_scope == "site":
        for page in report.pages:
            if not page.sections:
                continue
            rows = "".join(
                f"<tr><td>{_e(section.label)}</td>"
                f"<td>{section.points_awarded:g} / {section.points_possible:g}</td>"
                f"<td>{_e(', '.join(sorted({check.status.value for check in section.checks})))}</td></tr>"
                for section in page.sections
            )
            page_detail_html.append(
                f"<h3>{_e(page.final_url or page.url)} ({_e(page.role)})</h3>"
                "<table><thead><tr><th>Category</th><th>Score</th><th>Status</th>"
                f"</tr></thead><tbody>{rows}</tbody></table>"
            )
    page_detail_html = "".join(page_detail_html)
    if page_detail_html:
        page_detail_html = "<h2>Per-Page Detail</h2>" + page_detail_html

    coverage_note = (
        '<p style="color:#b06a1f;font-size:.85rem;"><strong>Partial coverage:</strong> '
        f"{report.points_evaluated:g} of {report.max_score} points could be evaluated. "
        "Unknown or unreachable checks are disclosed, not counted as failures.</p>"
        if report.partial
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>GEO Audit Report — {_e(display_url)}</title>
  <style>{_CSS}</style>
</head>
<body>
  <div class="container">
    <h1>GEO Audit Report</h1>
    <p><a href="{_e(display_url)}">{_e(display_url)}</a></p>

    <div style="display:flex;align-items:center;gap:1.5rem;margin:1.5rem 0;">
      <div class="score-circle">
        <div class="score-inner">
          <span style="font-size:1.75rem;font-weight:700;color:#211c12;">{int(round(report.total_score))}</span>
          <span style="font-size:.8rem;color:#8c8163;">/ {report.max_score}</span>
        </div>
      </div>
      <div>
        <span class="badge {_grade_class(report.grade)}" style="font-size:1.1rem;padding:.3rem .7rem;">Grade {_e(report.grade)}</span>
        <p style="margin-top:.4rem;color:#5c5236;">{_e(report.grade_label)}</p>
        <p style="font-size:.82rem;color:#8c8163;margin-top:.2rem;">{pct}% of AI answerability signals met.</p>
      </div>
    </div>

    <p style="color:#8c8163;font-size:.85rem;">
      <strong style="color:#5c5236;">Generated:</strong> {report.generated_at} ·
      <strong style="color:#5c5236;">Audited in</strong> {report.audit_duration_ms:.0f} ms ·
      {scope_bits}<strong style="color:#5c5236;">Mode:</strong> {"AI-enhanced" if report.llm_enhanced else "Deterministic"}
    </p>
    {coverage_note}
    {pages_section}

    <h2>Score Breakdown</h2>
    <table>
      <thead><tr><th>Category</th><th>Group</th><th>Score</th><th>%</th></tr></thead>
      <tbody>{score_table_rows}</tbody>
    </table>

    <h2>AI Answerability Matrix</h2>
    <table>
      <thead><tr><th>Question</th><th>Status</th><th>Evidence</th></tr></thead>
      <tbody>{matrix_rows}</tbody>
    </table>

    <h2>Detailed Findings</h2>
    {findings_html}

    {page_detail_html}

    <h2>Top Priority Recommendations</h2>
    {''.join(
        f'<p style="margin-bottom:.6rem;">• <strong style="color:#211c12;">{_e(f.title)}</strong>: '
        f'{_e(f.recommendation)} <em style="color:#8c8163;">(impact: {_e(f.impact.value)}, effort: {_e(f.effort.value)})</em></p>'
        for f in report.top_priorities
    ) if report.top_priorities else '<p style="color:#8c8163;">No priority recommendations.</p>'}

    <h2>Priority Roadmap</h2>
    {''.join(
        f'<p style="margin-bottom:.4rem;">• <strong style="color:#211c12;">{_e(f.title)}</strong> '
        f'<em style="color:#8c8163;">(impact: {_e(f.impact.value)}, effort: {_e(f.effort.value)}, '
        f'points: {f.points_awarded:.1f}/{f.points_possible:.1f})</em></p>'
        for f in roadmap_items
    ) if roadmap_items else '<p style="color:#8c8163;">No actionable findings.</p>'}

    <div class="disclaimer">
      <p>GEO Auditor measures on-page AI answerability signals. It is not a guarantee of
         ranking or citation in any AI search engine. Large brands may score lower than
         their real-world visibility because off-page authority signals are excluded.</p>
      <p>Report generated by <a href="https://geoauditor.pages.dev">GEO Auditor</a> — built by Mandar Mawale.</p>
    </div>
  </div>
</body>
</html>"""

