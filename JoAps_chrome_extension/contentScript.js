(function () {
  function getMeta(property) {
    const el = document.querySelector(`meta[property="${property}"]`);
    return el?.getAttribute("content")?.trim() || null;
  }

  function extractJobInfo() {
    const url = window.location.href;
    const hostname = window.location.hostname.replace(/^www\./, "");

    let titleText = getMeta("og:title") || document.title || "";

    // --- Job Title ---
    let jobTitle = null;

    // 1) <h1>
    const h1 = document.querySelector("h1");
    if (h1 && h1.textContent.trim()) {
      jobTitle = h1.textContent.trim();
    }

    // 2) Common ATS selectors
    if (!jobTitle) {
      const selectors = [
        '[data-qa*="job-title"]',
        ".job-title",
        ".posting-headline",
        ".job-header-title",
        ".job-title-text",
      ];
      for (const sel of selectors) {
        const el = document.querySelector(sel);
        if (el && el.textContent.trim()) {
          jobTitle = el.textContent.trim();
          break;
        }
      }
    }

    // 3) Heuristic from <title>
    if (!jobTitle && titleText) {
      let temp = titleText;
      if (temp.includes(" - ")) temp = temp.split(" - ")[0];
      if (temp.includes("|")) temp = temp.split("|")[0];
      jobTitle = temp.trim();
    }

    // --- Company ---
    let company = null;

    // 1) og:site_name
    company = getMeta("og:site_name") || company;

    // 2) schema.org hiringOrganization
    if (!company) {
      const org = document.querySelector('[itemprop="hiringOrganization"]');
      const nameEl = org?.querySelector('[itemprop="name"]');
      if (nameEl && nameEl.textContent.trim()) {
        company = nameEl.textContent.trim();
      }
    }

    // 3) Common company selectors
    if (!company) {
      const sels = [
        '[data-qa*="company-name"]',
        ".company-name",
        ".posting-company",
        ".job-header-company",
      ];
      for (const sel of sels) {
        const el = document.querySelector(sel);
        if (el && el.textContent.trim()) {
          company = el.textContent.trim();
          break;
        }
      }
    }

    // 4) From <title> right side
    if (!company && titleText && titleText.includes("|")) {
      let right = titleText.split("|").slice(-1)[0].trim();
      right = right
        .replace(/\b(Careers?|Jobs?|Hiring)\b/gi, "")
        .replace(/[-|]/g, "")
        .trim();
      if (right) company = right;
    }

    // 5) "Role at Company" pattern
    if (!company && titleText && titleText.includes(" at ")) {
      let after = titleText.split(" at ")[1].split("|")[0];
      const guess = after.replace(/[-|]/g, "").trim();
      if (guess) company = guess;
    }

    // 6) Hostname fallback
    if (!company) {
      const parts = hostname.split(".");
      let base =
        parts.length >= 2 ? parts[parts.length - 2] : parts[0] || "Unknown";
      base = base.charAt(0).toUpperCase() + base.slice(1);
      company = base;
    }

    if (!jobTitle) {
      jobTitle = `Job from ${hostname || "Unknown"}`;
    }

    return { jobTitle, company, url };
  }

  const info = extractJobInfo();

  chrome.runtime.sendMessage({ type: "JOB_INFO", payload: info });
})();
