const notionTokenInput = document.getElementById("notionToken");
const databaseIdInput = document.getElementById("databaseId");
const filesPropertyInput = document.getElementById("filesProperty");
const notionVersionInput = document.getElementById("notionVersion");
const saveBtn = document.getElementById("saveBtn");
const statusSpan = document.getElementById("status");

function loadOptions() {
  chrome.storage.sync.get(
    ["notionToken", "databaseId", "filesProperty", "notionVersion"],
    (items) => {
      if (items.notionToken) notionTokenInput.value = items.notionToken;
      if (items.databaseId) databaseIdInput.value = items.databaseId;
      filesPropertyInput.value = items.filesProperty || "Description";
      notionVersionInput.value = items.notionVersion || "2022-06-28";
    }
  );
}

function saveOptions() {
  chrome.storage.sync.set(
    {
      notionToken: notionTokenInput.value.trim(),
      databaseId: databaseIdInput.value.trim(),
      filesProperty: filesPropertyInput.value.trim() || "Description",
      notionVersion: notionVersionInput.value.trim() || "2022-06-28",
    },
    () => {
      statusSpan.textContent = "Saved!";
      setTimeout(() => (statusSpan.textContent = ""), 1500);
    }
  );
}

document.addEventListener("DOMContentLoaded", loadOptions);
saveBtn.addEventListener("click", saveOptions);
