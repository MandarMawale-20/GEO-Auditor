// GEO Auditor — progressive-enhancement script (no framework needed).
// Handles copy-paste fix buttons, audit form loading state, and
// smooth scroll for anchor links.

document.addEventListener("DOMContentLoaded", () => {
  // ---- Copy-paste buttons -------------------------------------------------
  document.querySelectorAll(".copy-btn").forEach((button) => {
    button.addEventListener("click", async () => {
      const pre = button.parentElement.querySelector("pre code");
      if (!pre) return;
      try {
        await navigator.clipboard.writeText(pre.textContent);
        const originalLabel = button.textContent;
        button.textContent = "Copied!";
        button.classList.add("copied");
        setTimeout(() => {
          button.textContent = originalLabel;
          button.classList.remove("copied");
        }, 2000);
      } catch (err) {
        console.error("Copy failed:", err);
      }
    });
  });

  // ---- Audit form loading state ----------------------------------------
  const form = document.getElementById("audit-form");
  const submitBtn = document.getElementById("audit-submit");
  if (form && submitBtn) {
    form.addEventListener("submit", () => {
      submitBtn.disabled = true;
      submitBtn.textContent = "Auditing…";
      submitBtn.classList.add("opacity-60", "cursor-not-allowed");
    });
  }
});
