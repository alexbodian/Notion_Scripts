// Load pdf-lib into the service worker
importScripts("pdf-lib.min.js");
const { PDFDocument } = PDFLib;

async function getConfig() {
  return new Promise((resolve) => {
    chrome.storage.sync.get(
      ["notionToken", "databaseId", "filesProperty", "notionVersion"],
      (items) => {
        resolve({
          token: items.notionToken,
          databaseId: items.databaseId,
          filesProperty: items.filesProperty || "Description",
          notionVersion: items.notionVersion || "2022-06-28",
        });
      }
    );
  });
}

function dataUrlToBlob(dataUrl) {
  const [meta, base64] = dataUrl.split(",");
  const binary = atob(base64);
  const len = binary.length;
  const bytes = new Uint8Array(len);
  for (let i = 0; i < len; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  const mimeMatch = meta.match(/data:(.*);base64/);
  const mime = mimeMatch ? mimeMatch[1] : "image/png";
  return new Blob([bytes], { type: mime });
}

// PNGs → multi-page PDF using pdf-lib
async function pngBlobsToPdfBlob(pngBlobs) {
  const pdfDoc = await PDFDocument.create();

  for (const pngBlob of pngBlobs) {
    const pngBytes = new Uint8Array(await pngBlob.arrayBuffer());
    const pngImage = await pdfDoc.embedPng(pngBytes);
    const pngDims = pngImage.scale(1);

    const page = pdfDoc.addPage([pngDims.width, pngDims.height]);
    page.drawImage(pngImage, {
      x: 0,
      y: 0,
      width: pngDims.width,
      height: pngDims.height,
    });
  }

  const pdfBytes = await pdfDoc.save();
  return new Blob([pdfBytes], { type: "application/pdf" });
}

async function uploadFileToNotion({ blob, filename, config }) {
  const { token, notionVersion } = config;

  // Step 1: create file_upload
  const createResp = await fetch("https://api.notion.com/v1/file_uploads", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Notion-Version": notionVersion,
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify({
      filename,
      content_type: blob.type || "application/pdf",
    }),
  });

  if (!createResp.ok) {
    const text = await createResp.text();
    throw new Error(
      `Failed to create file_upload: ${createResp.status} ${text}`
    );
  }

  const createData = await createResp.json();
  const fileUploadId = createData.id;

  // Step 2: send file
  const formData = new FormData();
  formData.append("file", blob, filename);

  const sendResp = await fetch(
    `https://api.notion.com/v1/file_uploads/${fileUploadId}/send`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Notion-Version": notionVersion,
        Accept: "application/json",
        // DO NOT set Content-Type; browser sets boundary for multipart/form-data
      },
      body: formData,
    }
  );

  if (!sendResp.ok) {
    const text = await sendResp.text();
    throw new Error(`Failed to send file_upload: ${sendResp.status} ${text}`);
  }

  return fileUploadId;
}

async function getDatabaseProperties(config) {
  const { token, databaseId, notionVersion } = config;
  const resp = await fetch(
    `https://api.notion.com/v1/databases/${databaseId}`,
    {
      method: "GET",
      headers: {
        Authorization: `Bearer ${token}`,
        "Notion-Version": notionVersion,
        Accept: "application/json",
      },
    }
  );

  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`Failed to fetch DB schema: ${resp.status} ${text}`);
  }

  const data = await resp.json();
  return (data && data.properties) || {};
}

async function createNotionPage({
  jobTitle,
  company,
  url,
  fileUploadId,
  filename,
  config,
  dbProps,
}) {
  const { token, databaseId, filesProperty, notionVersion } = config;

  const properties = {
    Name: {
      title: [
        {
          type: "text",
          text: { content: jobTitle },
        },
      ],
    },
    Company: {
      rich_text: [
        {
          type: "text",
          text: { content: company },
        },
      ],
    },
    URL: {
      url,
    },
  };

  if (
    fileUploadId &&
    filesProperty &&
    dbProps[filesProperty]?.type === "files"
  ) {
    properties[filesProperty] = {
      type: "files",
      files: [
        {
          type: "file_upload",
          file_upload: { id: fileUploadId },
          name: filename || "Job listing PDF",
        },
      ],
    };
  }

  const resp = await fetch("https://api.notion.com/v1/pages", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Notion-Version": notionVersion,
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify({
      parent: { database_id: databaseId },
      properties,
    }),
  });

  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`Failed to create page: ${resp.status} ${text}`);
  }

  return resp.json();
}

// 🔍 Get page height + viewport height from the tab
async function getPageMetrics(tabId) {
  const [result] = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      const body = document.body;
      const html = document.documentElement;
      const height = Math.max(
        body.scrollHeight,
        body.offsetHeight,
        html.clientHeight,
        html.scrollHeight,
        html.offsetHeight
      );
      const viewportHeight = window.innerHeight;
      return {
        height,
        viewportHeight,
      };
    },
  });

  return result.result;
}

// 📸 Capture full page by scrolling and taking multiple screenshots
async function captureFullPagePngBlobs(tab) {
  const tabId = tab.id;

  const { height, viewportHeight } = await getPageMetrics(tabId);
  const positions = [];
  let y = 0;
  while (y < height) {
    positions.push(y);
    y += viewportHeight;
  }

  const dataUrls = [];

  for (const scrollY of positions) {
    // Scroll page
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (yPos) => {
        window.scrollTo(0, yPos);
      },
      args: [scrollY],
    });

    // Give the page a moment to render
    await new Promise((resolve) => setTimeout(resolve, 300));

    // Capture visible area
    const dataUrl = await new Promise((resolve, reject) => {
      chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" }, (url) => {
        if (chrome.runtime.lastError || !url) {
          reject(
            chrome.runtime.lastError ||
              new Error("captureVisibleTab returned empty URL")
          );
        } else {
          resolve(url);
        }
      });
    });

    dataUrls.push(dataUrl);
  }

  // Scroll back to top
  await chrome.scripting.executeScript({
    target: { tabId },
    func: () => window.scrollTo(0, 0),
  });

  return dataUrls.map(dataUrlToBlob);
}

// Get job info from the tab by injecting contentScript.js and waiting for JOB_INFO
async function getJobInfoFromTab(tabId) {
  return new Promise((resolve, reject) => {
    const listener = (message, sender) => {
      if (
        sender.tab &&
        sender.tab.id === tabId &&
        message?.type === "JOB_INFO"
      ) {
        chrome.runtime.onMessage.removeListener(listener);
        resolve(message.payload);
      }
    };
    chrome.runtime.onMessage.addListener(listener);

    chrome.scripting.executeScript(
      {
        target: { tabId },
        files: ["contentScript.js"],
      },
      () => {
        if (chrome.runtime.lastError) {
          chrome.runtime.onMessage.removeListener(listener);
          reject(chrome.runtime.lastError);
        }
      }
    );
  });
}

// Message handler for popup <-> background
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "REQUEST_JOB_INFO") {
    (async () => {
      try {
        const config = await getConfig();
        if (!config.token || !config.databaseId) {
          sendResponse({
            ok: false,
            error: "Notion token or database ID not configured.",
          });
          return;
        }

        const jobInfo = await getJobInfoFromTab(message.tabId);
        sendResponse({ ok: true, jobInfo });
      } catch (err) {
        console.error("REQUEST_JOB_INFO error:", err);
        sendResponse({ ok: false, error: err.message || String(err) });
      }
    })();
    return true; // keep message channel open
  }

  if (message.type === "START_SAVE_JOB") {
    (async () => {
      try {
        const config = await getConfig();
        if (!config.token || !config.databaseId) {
          sendResponse({
            ok: false,
            error: "Notion token or database ID not configured.",
          });
          return;
        }

        const tab = await chrome.tabs.get(message.tabId);
        const jobTitle = message.jobTitle || "Untitled job";
        const company = message.company || "";
        const pageUrl = tab.url;

        // Capture full page → PDF
        const pngBlobs = await captureFullPagePngBlobs(tab);
        const pdfBlob = await pngBlobsToPdfBlob(pngBlobs);

        const todayStr = new Date().toISOString().slice(0, 10); // yyyy-mm-dd
        const safeCompany = (company || "Unknown")
          .replace(/[\\/:*?"<>|]/g, "")
          .replace(/\s+/g, "_")
          .slice(0, 80);
        const safeTitle = (jobTitle || "Job")
          .replace(/[\\/:*?"<>|]/g, "")
          .replace(/\s+/g, "_")
          .slice(0, 80);
        const filename = `${todayStr}-${safeCompany}-${safeTitle}.pdf`;

        const dbProps = await getDatabaseProperties(config);
        const fileUploadId = await uploadFileToNotion({
          blob: pdfBlob,
          filename,
          config,
        });

        await createNotionPage({
          jobTitle,
          company,
          url: pageUrl,
          fileUploadId,
          filename,
          config,
          dbProps,
        });

        sendResponse({ ok: true });
      } catch (err) {
        console.error("START_SAVE_JOB error:", err);
        sendResponse({ ok: false, error: err.message || String(err) });
      }
    })();
    return true; // keep message channel open
  }

  // for other message types, do nothing
});
