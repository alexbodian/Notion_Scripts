import argparse
import json
import os
import re
import shutil
from datetime import datetime
from typing import Optional, Dict, Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image
from playwright.sync_api import sync_playwright

# -------------------------------------------------
# Load configuration from .env
# -------------------------------------------------
load_dotenv()

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
NOTION_VERSION = os.getenv("NOTION_VERSION", "2022-06-28")

# Default to "Description" if NOTION_FILES_PROPERTY_NAME not set
FILES_PROPERTY_NAME = os.getenv("NOTION_FILES_PROPERTY_NAME", "Description") or None

MAX_PDF_MB = float(os.getenv("MAX_PDF_MB", "5"))
MAX_PDF_BYTES = int(MAX_PDF_MB * 1024 * 1024)

NOTION_API_BASE = "https://api.notion.com/v1"

BASE_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}" if NOTION_TOKEN else "",
    "Notion-Version": NOTION_VERSION,
    "accept": "application/json",
}


# -------------------------------------------------
# Helpers
# -------------------------------------------------
def sanitize_for_filename(text: str) -> str:
    """
    Make a string safe for filenames:
    - Normalize whitespace
    - Remove forbidden characters
    - Replace spaces with underscores
    - Truncate to a reasonable length
    """
    if not text:
        return "Unknown"

    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r'[\\/:*?"<>|]', "", text)  # forbidden chars
    text = text.replace(" ", "_")
    return text[:80] or "Unknown"


def prompt_with_default(label: str, current: str) -> str:
    """
    Ask user to confirm or override a field.
    If user just presses Enter, keep the current value.
    """
    print(f"{label}: {current}")
    new_val = input(f"Enter correct {label} (or press Enter to keep): ").strip()
    return new_val or current


# -------------------------------------------------
# 1. Capture full-page screenshot
# -------------------------------------------------
def capture_fullpage_screenshot(url: str, out_dir: str = "captures"):
    """
    Returns (png_path, html) where:
      - png_path is the full-page screenshot
      - html is the page content (for parsing job info)
    """
    os.makedirs(out_dir, exist_ok=True)

    safe_url_part = re.sub(r"[^a-zA-Z0-9]+", "_", url)[:50] or "job"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    png_path = os.path.join(out_dir, f"{safe_url_part}_{timestamp}.png")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})

        page.goto(url, wait_until="networkidle", timeout=60_000)
        page.wait_for_timeout(2000)

        page.screenshot(path=png_path, full_page=True)
        html = page.content()
        browser.close()

    return png_path, html


def compress_png_to_pdf_under_size(png_path: str, pdf_path: str, max_bytes: int) -> str:
    """
    Convert PNG to PDF, iteratively downscaling until file is <= max_bytes.
    Uses an intermediate JPEG to keep file size smaller.
    Final PDF is written to pdf_path.
    """
    img = Image.open(png_path).convert("RGB")
    width, height = img.size

    scale = 1.0
    min_width = 800
    temp_jpeg = png_path.replace(".png", "_tmp.jpg")
    tmp_pdf = pdf_path + ".tmp"

    for _ in range(7):
        target_w = max(min_width, int(width * scale))
        target_h = int(height * scale)
        resized = img.resize((target_w, target_h), Image.LANCZOS)

        resized.save(temp_jpeg, "JPEG", quality=85)

        with Image.open(temp_jpeg) as jpg_img:
            jpg_img.save(tmp_pdf, "PDF", resolution=72)

        size = os.path.getsize(tmp_pdf)
        if size <= max_bytes or scale <= 0.4:
            shutil.move(tmp_pdf, pdf_path)
            if os.path.exists(temp_jpeg):
                os.remove(temp_jpeg)
            return pdf_path

        scale *= 0.8

    shutil.move(tmp_pdf, pdf_path)
    if os.path.exists(temp_jpeg):
        os.remove(temp_jpeg)
    return pdf_path


# -------------------------------------------------
# 2. Extract job title + company name (Workday-optimized)
# -------------------------------------------------
def extract_job_info_from_html(html: str, url: str):
    soup = BeautifulSoup(html, "html.parser")
    parsed_url = urlparse(url)
    hostname = (parsed_url.hostname or "").lower()
    is_workday = "myworkdayjobs.com" in hostname

    job_title = None
    company = None

    # --- 1) JSON-LD JobPosting (esp. strong on Workday) ---
    def try_extract_jobposting_from_obj(obj: Any):
        if not isinstance(obj, dict):
            return None
        t = obj.get("@type")
        if isinstance(t, list):
            is_job = "JobPosting" in t
        else:
            is_job = t == "JobPosting"
        if not is_job:
            return None

        jt = obj.get("title") or (obj.get("identifier") or {}).get("name")
        org = obj.get("hiringOrganization") or {}
        comp = None
        if isinstance(org, dict):
            comp = org.get("name")
        return (
            jt.strip() if isinstance(jt, str) else None,
            comp.strip() if isinstance(comp, str) else None,
        )

    try:
        for script in soup.find_all("script", type="application/ld+json"):
            text = script.string or script.get_text(strip=True)
            if not text:
                continue
            try:
                data = json.loads(text)
            except Exception:
                continue

            candidate = None
            if isinstance(data, list):
                for item in data:
                    candidate = try_extract_jobposting_from_obj(item)
                    if candidate:
                        break
            else:
                candidate = try_extract_jobposting_from_obj(data)

            if candidate:
                jt, comp = candidate
                if jt:
                    job_title = jt
                if comp:
                    company = comp
                break
    except Exception:
        pass

    # --- 2) Workday host → brand for company (manulife, leidos, etc.) ---
    if is_workday:
        host = hostname.replace("www.", "")
        brand = host.split(".")[0]  # "manulife" in "manulife.wd3.myworkdayjobs.com"
        if brand:
            brand = brand.replace("-", " ")
            brand = " ".join(w.capitalize() for w in brand.split())
            company = brand

    # --- 3) Generic fallbacks (only if still missing) ---
    title_text = None
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title_text = og_title["content"].strip()
    elif soup.title and soup.title.string:
        title_text = soup.title.string.strip()

    # Job title: try <h1> then common class patterns, then title heuristics
    if not job_title:
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            job_title = h1.get_text(strip=True)

    if not job_title:
        possible_title_selectors = [
            ("data-qa", "job-title"),
            ("class", "job-title"),
            ("class", "posting-headline"),
            ("class", "job-header-title"),
            ("class", "job-title-text"),
        ]
        for attr, value in possible_title_selectors:
            el = soup.find(attrs={attr: re.compile(value, re.I)})
            if el and el.get_text(strip=True):
                job_title = el.get_text(strip=True)
                break

    if not job_title and title_text:
        temp = title_text
        if " - " in temp:
            temp = temp.split(" - ", 1)[0]
        if "|" in temp:
            temp = temp.split("|", 1)[0]
        job_title = temp.strip()

    # Company: if still none, use og:site_name / schema / title patterns / hostname
    if not company:
        og_site_name = soup.find("meta", property="og:site_name")
        if og_site_name and og_site_name.get("content"):
            company = og_site_name["content"].strip()

    if not company:
        org_el = soup.find(attrs={"itemprop": "hiringOrganization"})
        if org_el:
            name_el = org_el.find(attrs={"itemprop": "name"})
            if name_el and name_el.get_text(strip=True):
                company = name_el.get_text(strip=True)

    if not company and title_text and "|" in title_text:
        right_part = title_text.split("|")[-1].strip()
        right_part = re.sub(
            r"\b(Careers?|Jobs?|Hiring)\b", "", right_part, flags=re.IGNORECASE
        ).strip(" -|")
        if right_part:
            company = right_part

    if title_text and " at " in title_text and not company:
        _, after = title_text.split(" at ", 1)
        after = after.split("|")[0]
        company_guess = after.strip(" -|")
        if company_guess:
            company = company_guess

    if not company:
        host = hostname.replace("www.", "")
        parts = host.split(".")
        if len(parts) >= 2:
            base = parts[-2]
        elif parts:
            base = parts[0]
        else:
            base = "Unknown"
        company = base.capitalize()

    if not job_title:
        job_title = f"Job from {hostname or 'Unknown'}"

    return job_title, company


# -------------------------------------------------
# 3. Notion helpers
# -------------------------------------------------
def get_database_properties() -> Dict[str, Any]:
    if not NOTION_TOKEN or not NOTION_DATABASE_ID:
        raise RuntimeError("NOTION_TOKEN and NOTION_DATABASE_ID must be set in .env")

    resp = requests.get(
        f"{NOTION_API_BASE}/databases/{NOTION_DATABASE_ID}",
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": NOTION_VERSION,
        },
    )
    if not resp.ok:
        raise RuntimeError(
            f"Failed to fetch database schema: {resp.status_code} {resp.text}"
        )
    data = resp.json()
    return data.get("properties", {})


def upload_pdf_to_notion(pdf_path: str) -> str:
    """
    Notion Direct Upload:
    1. Create file_upload
    2. Send file contents
    3. Return file_upload ID
    """
    if not NOTION_TOKEN:
        raise RuntimeError("NOTION_TOKEN is not set")

    filename = os.path.basename(pdf_path)

    payload = {
        "filename": filename,
        "content_type": "application/pdf",
    }

    headers_json = {
        **BASE_HEADERS,
        "Content-Type": "application/json",
    }

    create_resp = requests.post(
        f"{NOTION_API_BASE}/file_uploads",
        json=payload,
        headers=headers_json,
    )
    if not create_resp.ok:
        raise RuntimeError(
            f"Failed to create file_upload: {create_resp.status_code} {create_resp.text}"
        )

    file_upload = create_resp.json()
    file_upload_id = file_upload["id"]

    with open(pdf_path, "rb") as f:
        files = {
            "file": (filename, f, "application/pdf"),
        }
        send_resp = requests.post(
            f"{NOTION_API_BASE}/file_uploads/{file_upload_id}/send",
            headers=BASE_HEADERS,
            files=files,
        )

    if not send_resp.ok:
        raise RuntimeError(
            f"Failed to send file_upload: {send_resp.status_code} {send_resp.text}"
        )

    return file_upload_id


def create_notion_page(
    job_title: str,
    company: str,
    url: str,
    pdf_upload_id: Optional[str],
    pdf_name: Optional[str],
    db_properties: Dict[str, Any],
):
    if not NOTION_TOKEN or not NOTION_DATABASE_ID:
        raise RuntimeError("NOTION_TOKEN and NOTION_DATABASE_ID must be set in .env")

    headers_json = {
        **BASE_HEADERS,
        "Content-Type": "application/json",
    }

    properties: Dict[str, Any] = {
        "Name": {
            "title": [
                {
                    "type": "text",
                    "text": {"content": job_title},
                }
            ]
        },
        "Company": {
            "rich_text": [
                {
                    "type": "text",
                    "text": {"content": company},
                }
            ]
        },
        "URL": {
            "url": url,
        },
    }

    if FILES_PROPERTY_NAME and pdf_upload_id:
        prop = db_properties.get(FILES_PROPERTY_NAME)
        if prop and prop.get("type") == "files":
            print(f"   → Attaching PDF to files property '{FILES_PROPERTY_NAME}'")
            properties[FILES_PROPERTY_NAME] = {
                "type": "files",
                "files": [
                    {
                        "type": "file_upload",
                        "file_upload": {"id": pdf_upload_id},
                        "name": pdf_name or "Job_listing_PDF",
                    }
                ],
            }
        else:
            print(
                f"⚠️ Not attaching PDF: property '{FILES_PROPERTY_NAME}' "
                f"not found or not type 'files' in this database."
            )

    payload = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": properties,
    }

    resp = requests.post(
        f"{NOTION_API_BASE}/pages",
        json=payload,
        headers=headers_json,
    )

    if not resp.ok:
        raise RuntimeError(
            f"Failed to create Notion page: {resp.status_code} {resp.text}"
        )

    data = resp.json()
    return data["id"]


# -------------------------------------------------
# CLI entrypoint
# -------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Archive a job listing: screenshot, PDF, and create Notion page."
    )
    parser.add_argument("url", help="Job listing URL")
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Don't upload PDF to Notion, only create page with title/company/url.",
    )

    args = parser.parse_args()
    url = args.url

    print(f"🔗 Processing URL: {url}")
    print(f"📁 Using Notion database: {NOTION_DATABASE_ID}")
    print(f"📎 Files property configured as: {FILES_PROPERTY_NAME!r}")

    png_path = None
    pdf_path = None
    captures_dir = "captures"

    try:
        # 0) DB schema
        print("🔍 Fetching Notion database schema...")
        db_properties = get_database_properties()
        print("   → Available properties:", ", ".join(db_properties.keys()))

        # 1) Screenshot
        print("📸 Capturing full-page screenshot (PNG)...")
        png_path, html = capture_fullpage_screenshot(url, out_dir=captures_dir)
        print(f"   → PNG saved at: {png_path}")

        # 2) Extract job info + confirm
        print("🧠 Extracting job title and company...")
        job_title, company = extract_job_info_from_html(html, url)
        print(f"   → Detected Job Title: {job_title}")
        print(f"   → Detected Company:   {company}")

        print("\n🔧 Confirm or edit the extracted values:")
        job_title = prompt_with_default("Job Title", job_title)
        company = prompt_with_default("Company", company)
        print(f"\n✅ Final Job Title: {job_title}")
        print(f"✅ Final Company:   {company}\n")

        # 3) PDF
        today_str = datetime.now().strftime("%Y-%m-%d")
        safe_company = sanitize_for_filename(company)
        safe_title = sanitize_for_filename(job_title)
        pdf_filename = f"{today_str}-{safe_company}-{safe_title}.pdf"
        pdf_path = os.path.join(os.getcwd(), pdf_filename)

        print(f"🧾 Converting screenshot to PDF (target ≤ {MAX_PDF_MB} MB)...")
        pdf_path = compress_png_to_pdf_under_size(png_path, pdf_path, MAX_PDF_BYTES)
        pdf_size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
        print(f"   → PDF saved at: {pdf_path} ({pdf_size_mb:.2f} MB)")

        pdf_upload_id: Optional[str] = None
        if not args.no_upload:
            print("☁️ Uploading PDF to Notion (Direct Upload)...")
            pdf_upload_id = upload_pdf_to_notion(pdf_path)
            print(f"   → file_upload ID: {pdf_upload_id}")
        else:
            print("ℹ️ --no-upload set, skipping PDF upload.")

        print("📄 Creating Notion page in your Kanban database...")
        page_id = create_notion_page(
            job_title, company, url, pdf_upload_id, pdf_filename, db_properties
        )
        print(f"✅ Done! Notion page ID: {page_id}")

    finally:
        # Cleanup local files after we're done
        print("\n🧹 Cleaning up local files...")
        if pdf_path and os.path.exists(pdf_path):
            try:
                os.remove(pdf_path)
                print(f"   → Deleted PDF: {pdf_path}")
            except Exception as e:
                print(f"   ⚠️ Could not delete PDF {pdf_path}: {e}")

        if png_path and os.path.exists(png_path):
            try:
                os.remove(png_path)
                print(f"   → Deleted PNG: {png_path}")
            except Exception as e:
                print(f"   ⚠️ Could not delete PNG {png_path}: {e}")

        if os.path.isdir(captures_dir):
            try:
                if not os.listdir(captures_dir):
                    os.rmdir(captures_dir)
                    print(f"   → Removed empty folder: {captures_dir}")
            except Exception as e:
                print(f"   ⚠️ Could not remove folder {captures_dir}: {e}")


if __name__ == "__main__":
    main()
