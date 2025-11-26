let activeTab = null;

const jobTitleInput = document.getElementById("jobTitle");
const companyInput = document.getElementById("company");
const loadingMessage = document.getElementById("loadingMessage");
const jobForm = document.getElementById("jobForm");
const refreshBtn = document.getElementById("refreshBtn");
const nextBtn = document.getElementById("nextBtn");
const saveBtn = document.getElementById("saveBtn");
const statusEl = document.getElementById("status");

const stepHeaders = document.querySelectorAll(".step-header");

// simple accordion behavior
stepHeaders.forEach((header) => {
  header.addEventListener("click", () => {
    const targetId = header.getAttribute("data-target");
    const body = document.getElementById(targetId);
    const isActive = body.classList.contains("active");

    if (isActive) {
      body.classList.remove("active");
      header.classList.add("collapsed");
    } else {
      body.classList.add("active");
      header.classList.remove("collapsed");
    }
  });
});

function setStatus(message, type = "") {
  statusEl.textContent = message || "";
  statusEl.className = "";
  if (type) statusEl.classList.add(type);
}

function setLoadingJobInfo(isLoading) {
  if (isLoading) {
    loadingMessage.style.display = "block";
    jobForm.style.display = "none";
  } else {
    loadingMessage.style.display = "none";
    jobForm.style.display = "block";
  }
}

// Request job info from background
function requestJobInfo() {
  if (!activeTab) return;
  setLoadingJobInfo(true);
  setStatus("");

  chrome.runtime.sendMessage(
    {
      type: "REQUEST_JOB_INFO",
      tabId: activeTab.id,
    },
    (resp) => {
      if (chrome.runtime.lastError) {
        console.error(chrome.runtime.lastError);
        setLoadingJobInfo(false);
        setStatus("Failed to detect job info.", "error");
        return;
      }
      if (!resp || !resp.ok) {
        console.error(resp?.error);
        setLoadingJobInfo(false);
        setStatus("Could not retrieve job info.", "error");
        return;
      }

      const info = resp.jobInfo || {};
      jobTitleInput.value = info.jobTitle || "";
      companyInput.value = info.company || "";

      setLoadingJobInfo(false);
    }
  );
}

document.addEventListener("DOMContentLoaded", () => {
  // Get active tab
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    activeTab = tabs[0];
    requestJobInfo();
  });

  refreshBtn.addEventListener("click", () => {
    requestJobInfo();
  });

  nextBtn.addEventListener("click", () => {
    // Collapse step 1, open step 2
    document.getElementById("step1-body").classList.remove("active");
    document.querySelector("#step1 .step-header").classList.add("collapsed");

    document.getElementById("step2-body").classList.add("active");
    document.querySelector("#step2 .step-header").classList.remove("collapsed");
  });

  saveBtn.addEventListener("click", () => {
    if (!activeTab) return;

    const jobTitle = jobTitleInput.value.trim();
    const company = companyInput.value.trim();

    if (!jobTitle) {
      setStatus("Job title is required.", "error");
      return;
    }

    setStatus("Saving to Notion…", "");
    saveBtn.disabled = true;
    refreshBtn.disabled = true;
    nextBtn.disabled = true;

    chrome.runtime.sendMessage(
      {
        type: "START_SAVE_JOB",
        tabId: activeTab.id,
        jobTitle,
        company,
      },
      (resp) => {
        saveBtn.disabled = false;
        refreshBtn.disabled = false;
        nextBtn.disabled = false;

        if (chrome.runtime.lastError) {
          console.error(chrome.runtime.lastError);
          setStatus("Failed: " + chrome.runtime.lastError.message, "error");
          return;
        }
        if (!resp || !resp.ok) {
          console.error(resp?.error);
          setStatus("Failed: " + (resp?.error || "Unknown error"), "error");
          return;
        }

        setStatus("Saved to Notion as full-page PDF ✅", "success");
      }
    );
  });
});
