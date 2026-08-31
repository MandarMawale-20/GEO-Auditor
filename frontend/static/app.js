/*
 * GEO Auditor — Frontend (static, deployed on Cloudflare Pages)
 *
 * Talks to the FastAPI backend on Render via its JSON API (/api/audit).
 * Reports are rendered client-side from the JSON response (no Jinja on Pages).
 * Shared reports are decoded from the URL hash fragment (gzip + base64url).
 */
(function () {
  "use strict";

  const API_BASE =
    window.API_BASE ||
    (document.querySelector('meta[name="api-base"]') &&
      document.querySelector('meta[name="api-base"]').content) ||
    "https://geo-auditor-api-xx47.onrender.com";

  /* ----------------------------- helpers ----------------------------- */
  function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function formatNum(n) {
    const num = Number(n);
    if (Number.isNaN(num)) return "0";
    return (Math.round(num * 10) / 10).toString();
  }

  function pctOf(report) {
    const max = report.max_score || 100;
    return max ? Math.round((report.total_score / max) * 100) : 0;
  }

  function formatDate(iso) {
    if (!iso) return "Unknown";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso).slice(0, 10);
    const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return d.getUTCDate() + " " + months[d.getUTCMonth()] + " " + d.getUTCFullYear();
  }

  function severityBadge(sev) {
    const map = { critical: "badge-danger", warning: "badge-warning", pass: "badge-success", info: "badge-info" };
    const cls = map[sev] || "badge-ghost";
    return '<span class="badge ' + cls + '" style="font-size:0.62rem;">' + esc(String(sev || "").toUpperCase()) + "</span>";
  }

  /* ------------------------- finding renderers ------------------------- */
  function findingCard(f) {
    return (
      '<div class="finding ' + f.severity + '">' +
        '<div class="finding-header">' +
          severityBadge(f.severity) +
          '<h3 class="finding-title">' + esc(f.title) + "</h3>" +
          '<div class="finding-meta">Impact: ' + esc(f.impact) + "</div>" +
        "</div>" +
        '<div class="finding-body">' +
          '<div class="why-it-matters">' + esc(f.why_it_matters) + "</div>" +
          (f.copy_paste_fix
            ? '<div class="code-block-wrapper"><pre class="code-block"><code>' + esc(f.copy_paste_fix) + '</code></pre><button class="copy-btn no-print" onclick="copyToClipboard(this)">Copy fix</button></div>'
            : "") +
          '<p class="muted" style="font-size:0.82rem; margin-top:0.5rem;"><span style="color:var(--text);">Fix:</span> ' + esc(f.recommendation) + "</p>" +
        "</div>" +
      "</div>"
    );
  }

  function findingAccordion(f) {
    if (f.severity !== "pass" && f.severity !== "info") {
      return (
        '<div class="finding ' + f.severity + '" style="padding:0.75rem; margin-bottom:0.5rem;">' +
          '<div class="finding-header">' +
            severityBadge(f.severity) +
            '<span class="finding-title" style="font-size:0.92rem;">' + esc(f.title) + "</span>" +
          "</div>" +
          "<details><summary>Evidence</summary><div class=\"evidence\">" + esc(f.evidence) + "</div></details>" +
          '<p class="muted" style="font-size:0.82rem; margin-top:0.4rem;"><span style="color:var(--text);">Recommendation:</span> ' + esc(f.recommendation) + "</p>" +
          (f.copy_paste_fix
            ? '<div class="code-block-wrapper" style="margin-top:0.5rem;"><pre class="code-block"><code>' + esc(f.copy_paste_fix) + '</code></pre><button class="copy-btn no-print" onclick="copyToClipboard(this)">Copy</button></div>'
            : "") +
        "</div>"
      );
    }
    return (
      '<div class="finding ' + f.severity + '" style="padding:0.75rem; margin-bottom:0.5rem;">' +
        '<div class="finding-header">' +
          severityBadge(f.severity) +
          '<span class="finding-title" style="font-size:0.92rem;">' + esc(f.title) + "</span>" +
        "</div>" +
        '<p class="muted" style="font-size:0.82rem;">' + esc(f.evidence) + "</p>" +
      "</div>"
    );
  }

  function roadmapItem(f, idx) {
    const dotClass = f.severity === "critical" ? "critical" : "warning";
    return (
      '<div class="roadmap-item">' +
        '<div class="roadmap-dot ' + dotClass + '">' + idx + "</div>" +
        '<div class="roadmap-content">' +
          '<div class="roadmap-item-header">' +
            '<div style="display:flex; align-items:center; gap:0.6rem;">' +
              severityBadge(f.severity) +
              '<h3 class="roadmap-item-title">' + esc(f.title) + "</h3>" +
            "</div>" +
            '<div class="finding-meta">Impact: ' + esc(f.impact) + " · Effort: " + esc(f.effort) + "</div>" +
          "</div>" +
          '<p class="muted" style="font-size:0.85rem; margin-bottom:0.5rem;"><span style="color:var(--text);">Why it matters:</span> ' + esc(f.why_it_matters) + "</p>" +
          "<details><summary>Evidence</summary><div class=\"evidence\">" + esc(f.evidence) + "</div></details>" +
          '<p class="muted" style="font-size:0.85rem; margin:0.4rem 0;"><span style="color:var(--text);">Recommendation:</span> ' + esc(f.recommendation) + "</p>" +
          (f.copy_paste_fix
            ? '<div class="code-block-wrapper"><pre class="code-block"><code>' + esc(f.copy_paste_fix) + '</code></pre><button class="copy-btn no-print" onclick="copyToClipboard(this)">Copy</button></div>'
            : "") +
        "</div>" +
      "</div>"
    );
  }

  function passCheck(f) {
    return (
      '<div class="finding pass" style="padding:0.75rem;">' +
        '<div style="display:flex; align-items:flex-start; gap:0.6rem;">' +
          '<span style="color:var(--success); font-size:1.1rem; margin-top:0.1rem;">✓</span>' +
          "<div>" +
            '<p style="color:var(--text); font-size:0.9rem; margin-bottom:0.2rem;">' + esc(f.title) + "</p>" +
            '<div class="muted" style="font-family:var(--font-mono); font-size:0.75rem;">' + formatNum(f.points_awarded) + "/" + formatNum(f.points_possible) + " pts</div>" +
          "</div>" +
        "</div>" +
      "</div>"
    );
  }

  function matrixRow(row) {
    const s = row.status;
    const cls = s === "yes" ? "badge-success" : s === "partial" ? "badge-warning" : "badge-danger";
    const th =
      'padding:0.6rem 0.75rem; color:var(--text-muted); font-family:var(--font-mono); font-size:0.7rem; font-weight:600; text-transform:uppercase; letter-spacing:0.06em;';
    return (
      "<tr style=\"border-bottom:1px solid var(--border-soft);\">" +
        '<td class="matrix-q" style="padding:0.55rem 0.75rem; font-weight:500; color:var(--text);">' + esc(row.question) + "</td>" +
        '<td style="padding:0.55rem 0.75rem;"><span class="badge ' + cls + '">' + esc(String(s).toUpperCase()) + "</span></td>" +
        '<td class="muted" style="padding:0.55rem 0.75rem; font-size:0.85rem;">' + esc(row.evidence || "") + "</td>" +
      "</tr>"
    );
  }

  /* --------------------------- report render --------------------------- */
  function renderReport(report) {
    const root = document.getElementById("report-root");
    if (!root || !report) return;

    const pct = pctOf(report);
    const finalUrl = report.final_url || report.url;
    const reportId = report.report_id;
    const circleOffset = 276.46 * (1 - pct / 100);

    const findings = report.all_findings || [];
    const criticalCount = findings.filter((f) => f.severity === "critical").length;
    const warningCount = findings.filter((f) => f.severity === "warning").length;
    const passCount = findings.filter((f) => f.severity === "pass").length;
    const sectionCount = (report.sections || []).length;

    const summaryLines = [];
    for (const s of report.sections || []) {
      for (const f of s.findings || []) {
        if (f.severity === "critical" || f.severity === "warning") {
          summaryLines.push("[" + String(f.severity).toUpperCase() + "] " + f.title);
        }
      }
    }
    const summaryText =
      "GEO Audit — " + finalUrl +
      "\nScore " + Math.floor(report.total_score) + "/" + report.max_score + " (" + report.grade + ")" +
      "\n\nFindings:\n" + summaryLines.join("\n");

    const shareUrl = (window.location.hash || "").replace(/^#/, "");

    const topPrioritiesHtml = (report.top_priorities || []).map(findingCard).join("");

    const sectionsHtml = (report.sections || [])
      .map(function (section, i) {
        const sp = Math.round(section.points_possible > 0 ? (section.points_awarded / section.points_possible) * 100 : 0);
        const barColor = sp >= 75 ? "var(--success)" : sp >= 40 ? "var(--warning)" : "var(--danger)";
        const findingsHtml = (section.findings || []).map(findingAccordion).join("");
        const aiBadge = section.llm_enhanced
          ? '<span class="badge badge-info" style="font-size:0.62rem;">AI</span>'
          : "";
        return (
          '<div class="accordion-item">' +
            '<div class="accordion-header" onclick="toggleSection(this)" role="button" tabindex="0" aria-expanded="false" aria-controls="section-' + (i + 1) + '">' +
              '<div class="accordion-header-row">' +
                '<div style="display:flex; align-items:center; gap:0.75rem;">' +
                  '<span class="strong" style="font-family:var(--font-mono); font-size:1.05rem; min-width:38px;">' + formatNum(section.points_awarded) + "</span>" +
                  '<span class="muted" style="font-family:var(--font-mono); font-size:0.8rem; min-width:30px;">/ ' + formatNum(section.points_possible) + "</span>" +
                  "<span>" + esc(section.label) + "</span>" +
                  aiBadge +
                "</div>" +
                '<div style="display:flex; align-items:center; gap:0.75rem;">' +
                  '<span class="muted" style="font-family:var(--font-mono); font-size:0.8rem;">' + sp + "%</span>" +
                  '<span class="accordion-chevron" role="presentation">▸</span>' +
                "</div>" +
              "</div>" +
              '<div class="progress-bar"><div class="progress-fill" style="width:' + sp + "%; background:" + barColor + ';"></div></div>' +
            "</div>" +
            '<div class="accordion-content" id="section-' + (i + 1) + '">' +
              '<div class="accordion-content-inner">' +
                '<div class="muted" style="font-family:var(--font-mono); font-size:0.74rem; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:0.6rem; padding-top:0.75rem;">' + esc(section.group || "") + "</div>" +
                findingsHtml +
              "</div>" +
            "</div>" +
          "</div>"
        );
      })
      .join("");

    const matrixHtml = (report.answerability_matrix || []).length
      ? '<section class="reveal" style="margin-bottom:2.5rem;">' +
        "<h2>AI answerability matrix</h2>" +
        '<p class="muted" style="margin-bottom:1.25rem; font-size:0.9rem;">Whether your content answers common AI-search questions.</p>' +
        '<div class="audit-table" style="margin:0;"><table style="width:100%; border-collapse:collapse;">' +
        "<thead><tr>" +
        '<th style="text-align:left; padding:0.6rem 0.75rem; color:var(--text-muted); font-family:var(--font-mono); font-size:0.7rem; font-weight:600; text-transform:uppercase; letter-spacing:0.06em;">Question</th>' +
        '<th style="text-align:left; padding:0.6rem 0.75rem; color:var(--text-muted); font-family:var(--font-mono); font-size:0.7rem; font-weight:600; text-transform:uppercase; letter-spacing:0.06em;">Status</th>' +
        '<th style="text-align:left; padding:0.6rem 0.75rem; color:var(--text-muted); font-family:var(--font-mono); font-size:0.7rem; font-weight:600; text-transform:uppercase; letter-spacing:0.06em;">Evidence</th>' +
        "</tr></thead><tbody>" +
        report.answerability_matrix.map(matrixRow).join("") +
        "</tbody></table></div></section>"
      : "";

    const roadmapFindings = findings.filter(function (f) {
      return f.severity === "critical" || f.severity === "warning";
    });
    const roadmapHtml = roadmapFindings.length
      ? '<section class="reveal" style="margin-bottom:2.5rem;">' +
        "<h2>Priority roadmap</h2>" +
        '<p class="muted" style="margin-bottom:1.25rem; font-size:0.9rem;">Sorted by impact vs. effort — fix top of list first.</p>' +
        '<div class="roadmap">' + roadmapFindings.map(function (f, idx) { return roadmapItem(f, idx + 1); }).join("") + "</div>" +
        "</section>"
      : '<section class="reveal" style="margin-bottom:2.5rem;"><h2>Priority roadmap</h2>' +
        '<div class="empty-state"><div class="empty-icon">✓</div><p class="muted">No critical or warning findings. Your site is well-optimized for AI search.</p></div></section>';

    const passFindings = findings.filter(function (f) { return f.severity === "pass"; });
    const passingHtml = passFindings.length
      ? '<section class="reveal" style="margin-bottom:2.5rem;"><h2>Passing checks (' + passFindings.length + ")</h2>" +
        '<div style="display:grid; grid-template-columns:repeat(2, 1fr); gap:0.75rem;">' + passFindings.map(passCheck).join("") + "</div></section>"
      : "";

    const fh = report.fetch_health;
    const diagHtml = fh
      ? '<section class="reveal" style="margin-bottom:2.5rem;"><h2>Fetch diagnostics</h2>' +
        '<div class="stats-row" style="margin-top:0;">' +
        '<div class="stat-item"><div class="stat-value" style="font-size:1.25rem;">' + esc(fh.http_status != null ? fh.http_status : "—") + '</div><div class="stat-label">HTTP Status</div></div>' +
        '<div class="stat-item"><div class="stat-value">' + (fh.fetch_time_ms != null ? Math.floor(fh.fetch_time_ms) + " ms" : "—") + '</div><div class="stat-label">Fetch Time</div></div>' +
        '<div class="stat-item"><div class="stat-value">' + (fh.ttfb_ms != null ? Math.floor(fh.ttfb_ms) + " ms" : "—") + '</div><div class="stat-label">TTFB</div></div>' +
        '<div class="stat-item"><div class="stat-value">' + esc(fh.redirect_count != null ? fh.redirect_count : 0) + '</div><div class="stat-label">Redirects</div></div>' +
        "</div></section>"
      : "";

    const actionsHtml = reportId
      ? '<div class="no-print" style="border-bottom:1px solid var(--border); position:sticky; top:69px; z-index:10; background:var(--surface-sticky);">' +
        '<div class="container" style="padding:0.75rem 0;"><div class="report-actions">' +
        '<a href="' + API_BASE + "/audit/pdf/" + encodeURIComponent(reportId) + '" class="btn btn-primary">Download PDF</a>' +
        '<a href="' + API_BASE + "/audit/html/" + encodeURIComponent(reportId) + '" class="btn btn-ghost-sm">Download HTML</a>' +
        '<button type="button" id="copy-summary" class="btn btn-ghost-sm" data-summary="' + esc(summaryText) + '">Copy summary</button>' +
        '<button type="button" class="share-report-btn btn btn-ghost-sm" data-share-url="' + esc(shareUrl) + '">Share report</button>' +
        "</div></div></div>"
      : "";

    const llmErrorHtml = report.llm_enhanced && report.llm_error
      ? '<div class="banner banner-warning" style="margin-bottom:2rem;"><span class="banner-icon">⚠</span><div class="banner-content"><div class="banner-title">AI analysis unavailable</div><div class="banner-text">' + esc(report.llm_error) + "</div></div></div>"
      : "";

    const html =
      actionsHtml +
      '<div style="padding-top:2.5rem;">' +
        '<div class="report-header anim-in" style="margin-bottom:2rem;">' +
          '<div class="report-header-inner"><div>' +
            '<span class="report-label">Audited URL</span>' +
            '<div class="report-url">' + esc(finalUrl) + "</div>" +
          "</div><div class=\"stat-value-container\">" +
            '<span class="report-label">Score</span>' +
            '<div style="font-family:var(--font-display); font-size:1.6rem; font-weight:500; color:var(--text); line-height:1;">' + Math.floor(report.total_score) + " / " + report.max_score + "</div>" +
          "</div></div>" +
        "</div>" +
        "<section>" +
          '<div class="anim-in" style="display:flex; align-items:center; gap:2rem; flex-wrap:wrap; padding:2.5rem 0; border-bottom:1px solid var(--border); margin-bottom:2.5rem; animation-delay:0.08s;">' +
            '<div style="display:flex; align-items:center; gap:1.5rem;">' +
              '<div class="score-circle">' +
                '<svg viewBox="0 0 100 100" data-pct="' + pct + '">' +
                  '<circle cx="50" cy="50" r="42" fill="none" stroke="var(--border)" stroke-width="4"/>' +
                  '<circle class="progress-ring__circle" cx="50" cy="50" r="42" fill="none" stroke="var(--accent)" stroke-width="4" stroke-dasharray="276.46 276.46" stroke-dashoffset="276.46" data-target-offset="' + circleOffset.toFixed(2) + '" style="transition: stroke-dashoffset 1100ms cubic-bezier(.16,1,.3,1) 200ms;"></circle>' +
                "</svg>" +
                '<div class="score-text"><span class="score-num" data-count-to="' + Math.floor(report.total_score) + '">0</span><span class="score-max">/ ' + report.max_score + "</span></div>" +
              "</div>" +
              '<div><span class="stamp stamp-lg grade-' + esc(report.grade.toLowerCase()) + ' anim-in" style="animation-delay:0.3s;">' + esc(report.grade) + "</span></div>" +
              "<div>" +
                '<div class="grade-label" style="font-size:1.05rem; color:var(--text); font-weight:600;">' + esc(report.grade_label) + "</div>" +
                '<div class="muted" style="font-size:0.85rem; margin-top:0.2rem;">' + pct + "% of AI answerability signals met</div>" +
              "</div>" +
            "</div>" +
            '<div style="margin-left:auto; min-width:200px; text-align:right;">' +
              '<div class="muted" style="font-size:0.8rem; font-family:var(--font-mono);">' +
                '<div style="margin-bottom:0.3rem;">Audited ' + esc(formatDate(report.generated_at)) + "</div>" +
                '<div style="margin-bottom:0.3rem;">Completed in ' + Math.floor(report.audit_duration_ms) + " ms</div>" +
                "<div>" + (report.llm_enhanced ? "AI-enhanced analysis" : "Deterministic scoring") + "</div>" +
              "</div>" +
            "</div>" +
          "</div>" +
          '<div class="banner banner-info reveal" style="margin-bottom:2rem;">' +
            '<span class="banner-icon">ⓘ</span>' +
            '<div class="banner-content"><div class="banner-title">What this measures</div>' +
            '<div class="banner-text">This audit measures <strong>on-page GEO readiness</strong>. Large brands may receive lower scores than their real-world AI visibility because off-page authority signals (Wikipedia, Knowledge Graph, backlinks, brand mentions) are intentionally excluded from this scoring model.</div>' +
            "</div></div>" +
          llmErrorHtml +
          '<div class="stats-row reveal" style="margin-bottom:2.5rem;">' +
            '<div class="stat-item"><div class="stat-value">' + sectionCount + '</div><div class="stat-label">Categories</div></div>' +
            '<div class="stat-item"><div class="stat-value" style="color:var(--danger);">' + criticalCount + '</div><div class="stat-label">Critical</div></div>' +
            '<div class="stat-item"><div class="stat-value" style="color:var(--warning);">' + warningCount + '</div><div class="stat-label">Warnings</div></div>' +
            '<div class="stat-item"><div class="stat-value" style="color:var(--success);">' + passCount + '</div><div class="stat-label">Passing</div></div>' +
          "</div>" +
        "</section>" +
        (report.top_priorities && report.top_priorities.length
          ? '<section class="reveal" style="margin-bottom:2.5rem;"><h2>Top 3 priorities for Monday morning</h2><p class="muted" style="margin-bottom:1.25rem; font-size:0.9rem;">Highest impact, lowest effort. Fix these first.</p><div class="grid-2">' + topPrioritiesHtml + "</div></section>"
          : "") +
        '<section class="reveal" style="margin-bottom:2.5rem;"><h2>Score breakdown</h2>' +
          '<p class="muted" style="margin-bottom:1.25rem; font-size:0.9rem;">10 categories across ' + report.max_score + " possible points.</p>" +
          '<div style="display:flex; flex-direction:column; gap:0.5rem;">' + sectionsHtml + "</div></section>" +
        matrixHtml +
        roadmapHtml +
        passingHtml +
        diagHtml +
      "</div>";

    root.innerHTML = html;
    document.title = "GEO Report — " + finalUrl;
    initReportInteractions();
  }

  /* --------------------- shared report (hash) decode --------------------- */
  async function decodeShareHash(hash) {
    const raw = hash.replace(/^#/, "").replace(/^report#/, "");
    if (!raw) return null;
    try {
      // The server encodes the payload with base64url (RFC 4648 §5), which
      // uses '-' and '_' instead of standard base64's '+' and '/'. atob() only
      // accepts standard base64, so translate back before decoding — otherwise
      // atob() throws InvalidCharacterError and the report fails to open.
      const clean = raw.replace(/-/g, "+").replace(/_/g, "/");
      const padLen = (4 - (clean.length % 4)) % 4;
      const padding = "=".repeat(padLen);
      const binary = atob(clean + padding);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      const ds = new DecompressionStream("gzip");
      const reader = new Response(new Blob([bytes]).stream()).body.pipeThrough(ds).getReader();
      const decoder = new TextDecoder();
      // NOTE: must capture the WHOLE {value, done} object from read(); the
      // previous pattern `done = (await reader.read()).done` assigned the
      // boolean `.done` property to `done`, making `done.value` always
      // `undefined` and silently dropping every chunk (the loop ran but
      // decoded zero bytes — hence "Failed to decode shared report").
      let result;
      let chunks = "";
      while (!(result = await reader.read()).done) {
        chunks += decoder.decode(result.value, { stream: true });
      }
      chunks += decoder.decode();
      return JSON.parse(chunks);
    } catch (e) {
      return null;
    }
  }

  async function initSharedReport(root) {
    const hash = window.location.hash;
    if (!hash || hash.length < 2) {
      root.innerHTML = '<div class="empty-state"><div class="empty-icon">🔗</div><p>No report data found in this link.</p></div>';
      return;
    }
    const data = await decodeShareHash(hash);
    if (!data) {
      root.innerHTML = '<div class="empty-state"><div class="empty-icon">⚠</div><p>Failed to decode shared report. Please ensure you are using a modern browser.</p></div>';
      return;
    }
    renderReport(data);
  }

  /* --------------------------- interactions --------------------------- */
  function initReportInteractions() {
    const copySummaryBtn = document.getElementById("copy-summary");
    if (copySummaryBtn) {
      copySummaryBtn.addEventListener("click", async function () {
        const text = copySummaryBtn.dataset.summary;
        if (!text) return;
        try {
          await navigator.clipboard.writeText(text);
          copySummaryBtn.textContent = "✓ Copied!";
          setTimeout(function () { copySummaryBtn.textContent = "Copy summary"; }, 2000);
        } catch (e) { /* ignore */ }
      });
    }
    document.querySelectorAll(".share-report-btn").forEach(function (btn) {
      btn.addEventListener("click", async function () {
        const shareUrl = btn.dataset.shareUrl;
        if (!shareUrl) return;
        const fullUrl = window.location.origin + window.location.pathname + "#" + shareUrl;
        try {
          await navigator.clipboard.writeText(fullUrl);
          btn.textContent = "✓ Link copied!";
          setTimeout(function () { btn.textContent = "Share report"; }, 2000);
        } catch (e) { /* ignore */ }
      });
    });
    initMotion();
  }

  /* ----------------------------- motion ----------------------------- */
  function initMotion() {
    const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const revealEls = document.querySelectorAll(".reveal, .reveal-stagger");
    if (revealEls.length) {
      if (prefersReducedMotion || !("IntersectionObserver" in window)) {
        /* leave visible */
      } else {
        revealEls.forEach(function (el) { el.classList.add("reveal-pending"); });
        const obs = new IntersectionObserver(function (entries) {
          entries.forEach(function (entry) {
            if (entry.isIntersecting) {
              entry.target.classList.remove("reveal-pending");
              entry.target.classList.add("in-view");
              obs.unobserve(entry.target);
            }
          });
        }, { threshold: 0.12, rootMargin: "0px 0px -40px 0px" });
        revealEls.forEach(function (el) { obs.observe(el); });
      }
    }

    function animateCount(el) {
      const target = parseInt(el.dataset.countTo, 10);
      if (Number.isNaN(target)) return;
      if (prefersReducedMotion) {
        if (el.firstChild) el.firstChild.textContent = String(target);
        else el.textContent = String(target);
        return;
      }
      const duration = 900;
      const start = performance.now();
      const suffix = el.querySelector("span") ? el.querySelector("span").outerHTML : "";
      function tick(now) {
        const progress = Math.min((now - start) / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 3);
        el.innerHTML = Math.round(eased * target) + suffix;
        if (progress < 1) requestAnimationFrame(tick);
      }
      requestAnimationFrame(tick);
    }

    const countEls = document.querySelectorAll("[data-count-to]");
    if (countEls.length) {
      if (!("IntersectionObserver" in window)) {
        countEls.forEach(animateCount);
      } else {
        const cobs = new IntersectionObserver(function (entries) {
          entries.forEach(function (e) {
            if (e.isIntersecting) { animateCount(e.target); cobs.unobserve(e.target); }
          });
        }, { threshold: 0.4 });
        countEls.forEach(function (el) { cobs.observe(el); });
      }
    }

    const ring = document.querySelector(".progress-ring__circle[data-target-offset]");
    if (ring) {
      const target = ring.dataset.targetOffset;
      if (prefersReducedMotion) {
        ring.style.strokeDashoffset = target;
      } else {
        requestAnimationFrame(function () {
          requestAnimationFrame(function () { ring.style.strokeDashoffset = target; });
        });
      }
    }
  }

  /* ----------------------------- audit form ----------------------------- */
  function showFormError(msg) {
    const formError = document.getElementById("form-error");
    if (formError) { formError.textContent = msg; formError.style.display = "block"; }
    const submitBtn = document.getElementById("audit-submit");
    if (submitBtn) { submitBtn.disabled = false; submitBtn.innerHTML = "Run audit →"; }
  }

  function initAuditForm(auditForm) {
    auditForm.addEventListener("submit", async function (e) {
      e.preventDefault();
      const urlInput = document.getElementById("url");
      const submitBtn = document.getElementById("audit-submit");
      const errorBanner = document.getElementById("url-error");
      const formError = document.getElementById("form-error");

      if (!urlInput || !urlInput.value.trim()) {
        if (errorBanner) { errorBanner.textContent = "Please enter a URL."; errorBanner.style.display = "block"; }
        if (urlInput) urlInput.focus();
        return;
      }
      if (errorBanner) errorBanner.style.display = "none";
      if (formError) formError.style.display = "none";

      const urlValue = urlInput.value;
      const llmChecked = document.getElementById("llm-enhanced-toggle") &&
        document.getElementById("llm-enhanced-toggle").checked;

      submitBtn.disabled = true;
      submitBtn.innerHTML = '<span class="spinner"></span><span>Auditing…</span>';

      try {
        const params = new URLSearchParams({
          url: urlValue,
          llm_enhanced: llmChecked ? "true" : "false",
        });
        const resp = await fetch(API_BASE + "/api/audit?" + params.toString(), { method: "GET" });
        if (resp.ok) {
          const data = await resp.json();
          if (data && data.ok && data.report) {
            // share_url is now an absolute URL (…/report.html#<encoded>).
            // Handle a bare hash fragment too (older API responses) by
            // resolving it relative to the report viewer.
            let share = data.share_url || "";
            if (share && !/^https?:/i.test(share)) {
              share = "report.html#" + share.replace(/^#/, "");
            }
            window.location.href = share;
            return;
          }
          showFormError((data && (data.detail || data.message)) || "Audit failed. Please try again.");
        } else {
          let msg = "Something went wrong. Please try again.";
          try { const j = await resp.json(); msg = j.detail || j.message || msg; } catch (e) {}
          if (resp.status === 429) msg = "Rate limit exceeded. Please try again in an hour.";
          if (resp.status === 400) msg = msg || "Invalid URL or unable to audit that site.";
          showFormError(msg);
        }
      } catch (err) {
        showFormError("Network error. Please check your connection and try again.");
      }
    });
  }

  async function syncLlmToggle() {
    const row = document.getElementById("llm-toggle-row");
    const toggle = document.getElementById("llm-enhanced-toggle");
    if (!row || !toggle) return;
    try {
      const r = await fetch(API_BASE + "/health", { method: "GET" });
      if (r.ok) {
        const h = await r.json();
        row.style.display = "block";
        toggle.disabled = !h.llm_enabled;
        toggle.checked = h.llm_enabled && toggle.checked;
        const badge = toggle.closest(".toggle-switch") && toggle.closest(".toggle-switch").querySelector(".toggle-badge");
        if (badge) {
          badge.textContent = h.llm_enabled ? (toggle.checked ? badge.dataset.on : badge.dataset.off) : "OFF";
          badge.style.opacity = h.llm_enabled ? "1" : "0.6";
        }
        if (!h.llm_enabled) {
          toggle.title = "AI enhancement is not configured on this deployment.";
        } else {
          toggle.title = "Use AI-enhanced analysis when available.";
        }
      } else {
        row.style.display = "block";
        toggle.disabled = true;
        toggle.title = "AI enhancement is unavailable.";
      }
    } catch (e) {
      row.style.display = "block";
      toggle.disabled = true;
      toggle.title = "AI enhancement is unavailable.";
    }
  }

  /* --------------------------- global onclick --------------------------- */
  window.toggleSection = function (header) {
    const content = header.nextElementSibling;
    const chevron = header.querySelector(".accordion-chevron");
    if (!content || !content.classList) return;
    const isOpen = content.classList.contains("open");
    content.classList.toggle("open", !isOpen);
    if (chevron) chevron.classList.toggle("rotate", !isOpen);
    header.setAttribute("aria-expanded", String(!isOpen));
  };

  window.copyToClipboard = async function (button) {
    const codeEl = button.closest(".code-block-wrapper") && button.closest(".code-block-wrapper").querySelector("code");
    if (!codeEl) return;
    const text = codeEl.textContent;
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = "✓ Copied!";
      button.classList.add("copied");
      setTimeout(function () { button.textContent = "Copy"; button.classList.remove("copied"); }, 2000);
    } catch (err) {
      console.error("Copy failed:", err);
      button.textContent = "✗ Failed";
      setTimeout(function () { button.textContent = "Copy"; }, 2000);
    }
  };

  /* ------------------------------ init ------------------------------ */
  document.addEventListener("DOMContentLoaded", function () {
    const menuToggle = document.getElementById("menu-toggle");
    if (menuToggle) {
      const navLinks = document.querySelectorAll(".nav a");
      navLinks.forEach(function (link) {
        link.addEventListener("click", function () { menuToggle.checked = false; });
      });
    }

    const llmToggle = document.getElementById("llm-enhanced-toggle");
    if (llmToggle) {
      const badge = llmToggle.closest(".toggle-switch") && llmToggle.closest(".toggle-switch").querySelector(".toggle-badge");
      if (badge) {
        const updateBadge = function () { badge.textContent = llmToggle.checked ? badge.dataset.on : badge.dataset.off; };
        updateBadge();
        llmToggle.addEventListener("change", updateBadge);
      }
    }

    // Sample reports are hosted as static files on the SAME origin as this
    // frontend (Cloudflare Pages), under /samples/{id}.html. Linking to the
    // API base would open them on the Render backend domain (and they'd be
    // unstyled there because /static isn't mounted). Keeping them on the
    // Pages origin means /static/style.css resolves and styling renders.
    document.querySelectorAll("[data-sample]").forEach(function (a) {
      a.href = "/samples/" + a.dataset.sample + ".html";
      a.target = "_blank";
      a.rel = "noopener";
    });
    const sampleLink = document.getElementById("sample-link");
    if (sampleLink) { sampleLink.href = "/samples/github.html"; sampleLink.target = "_blank"; sampleLink.rel = "noopener"; }

    const auditForm = document.getElementById("audit-form");
    if (auditForm) { initAuditForm(auditForm); syncLlmToggle(); }

    const root = document.getElementById("report-root");
    if (root) { initSharedReport(root); }

    initMotion();
  });
})();
