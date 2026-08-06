// Small progressive-enhancement script for GEO Auditor.

document.addEventListener("DOMContentLoaded", () => {
  // Copy fix snippets to the clipboard.
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

  // Disable the form while the audit is running.
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
