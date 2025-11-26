import os
import time
import textwrap
from typing import Optional, Dict, Any, List

import requests
from dotenv import load_dotenv

# -------------------------------------------------
# Load config from .env
# -------------------------------------------------
load_dotenv()

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
NOTION_VERSION = os.getenv("NOTION_VERSION", "2022-06-28")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-70b-versatile")

if not NOTION_TOKEN or not NOTION_DATABASE_ID:
    raise RuntimeError("NOTION_TOKEN and NOTION_DATABASE_ID must be set in .env")

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY must be set in .env")

NOTION_API_BASE = "https://api.notion.com/v1"

BASE_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
    "Accept": "application/json",
}

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_HEADERS = {
    "Authorization": f"Bearer {GROQ_API_KEY}",
    "Content-Type": "application/json",
}


# -------------------------------------------------
# Helpers for Notion properties
# -------------------------------------------------
def get_text_property(properties: Dict[str, Any], prop_name: str) -> str:
    """Return plain text from a title or rich_text property."""
    prop = properties.get(prop_name)
    if not prop:
        return ""

    p_type = prop.get("type")

    if p_type == "title":
        parts = prop.get("title", [])
    elif p_type == "rich_text":
        parts = prop.get("rich_text", [])
    else:
        # handle select / multi_select generically
        if p_type == "select" and prop.get("select"):
            return prop["select"]["name"]
        if p_type == "multi_select" and prop.get("multi_select"):
            return ", ".join([opt["name"] for opt in prop["multi_select"]])
        return ""

    texts = []
    for part in parts:
        if part.get("type") == "text" and part.get("plain_text"):
            texts.append(part["plain_text"])
        elif part.get("plain_text"):
            texts.append(part["plain_text"])

    return "".join(texts).strip()


def get_status_value(
    properties: Dict[str, Any], status_prop_name: str = "Status"
) -> Optional[str]:
    """
    Return the Status value if it exists.
    Works for both:
      - type == 'status'
      - type == 'select'
    """
    prop = properties.get(status_prop_name)
    if not prop:
        return None

    p_type = prop.get("type")

    if p_type == "status" and prop.get("status"):
        return prop["status"].get("name")

    if p_type == "select" and prop.get("select"):
        return prop["select"].get("name")

    return None


def has_nonempty_rich_text(properties: Dict[str, Any], prop_name: str) -> bool:
    prop = properties.get(prop_name)
    if not prop or prop.get("type") != "rich_text":
        return False
    parts = prop.get("rich_text", [])
    return any((part.get("plain_text") or "").strip() for part in parts)


# -------------------------------------------------
# Company description generation (Groq)
# -------------------------------------------------
def generate_company_description(company_name: str, max_chars: int = 400) -> Optional[str]:
    """
    Use Groq (free-tier LLM) to generate a short 1–2 sentence description
    of what this company does.

    If the model cannot confidently identify the company, it should output a
    generic but honest sentence, not fabricated facts.
    """
    if not company_name:
        return None

    system_msg = (
        "You help a user maintain a personal job applications tracker.\n"
        "You write short, neutral, factual-sounding descriptions of companies.\n"
        "If you cannot confidently identify the company from its name alone, "
        "you must say something generic like:\n"
        "'A business or organization named <name>; specific public details are not readily available.'\n"
        "Do NOT invent specific details such as revenue, exact employee counts, or specific product names.\n"
    )

    user_msg = (
        f"Company name: {company_name}\n\n"
        "Write a concise 1–2 sentence description of what this company does.\n"
        "Return only the description text, no bullet points or extra commentary."
    )

    body = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.3,
        "max_tokens": 256,
    }

    try:
        resp = requests.post(GROQ_API_URL, headers=GROQ_HEADERS, json=body, timeout=20)
    except requests.RequestException as e:
        print(f"   ❌ Groq request error: {e}")
        return None

    if not resp.ok:
        print(f"   ❌ Groq API error {resp.status_code}: {resp.text}")
        return None

    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        print("   ❌ Groq response has no choices.")
        return None

    content = choices[0].get("message", {}).get("content", "")
    desc = (content or "").strip()
    if not desc:
        return None

    if len(desc) > max_chars:
        desc = desc[: max_chars].rsplit(" ", 1)[0].rstrip() + "…"

    return desc


# -------------------------------------------------
# Notion API helpers
# -------------------------------------------------
def query_database_excluding_resources() -> List[Dict[str, Any]]:
    """
    Query the database for all pages whose Status != 'Resources'.
    Uses the 'status' filter type (because your Status property is a Status-type).
    Handles pagination.
    """
    all_results: List[Dict[str, Any]] = []

    payload: Dict[str, Any] = {
        "filter": {
            "property": "Status",
            "status": {
                "does_not_equal": "Resources",
            },
        }
    }

    next_cursor = None

    while True:
        if next_cursor:
            payload["start_cursor"] = next_cursor
        else:
            payload.pop("start_cursor", None)

        resp = requests.post(
            f"{NOTION_API_BASE}/databases/{NOTION_DATABASE_ID}/query",
            headers=BASE_HEADERS,
            json=payload,
        )
        if not resp.ok:
            raise RuntimeError(
                f"Failed to query database: {resp.status_code} {resp.text}"
            )

        data = resp.json()
        results = data.get("results", [])
        all_results.extend(results)

        if data.get("has_more") and data.get("next_cursor"):
            next_cursor = data["next_cursor"]
        else:
            break

    return all_results


def update_company_description(page_id: str, description: str):
    """Update the 'Company Description' rich_text property on a page."""
    payload = {
        "properties": {
            "Company Description": {
                "type": "rich_text",
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": description},
                    }
                ],
            }
        }
    }

    resp = requests.patch(
        f"{NOTION_API_BASE}/pages/{page_id}",
        headers=BASE_HEADERS,
        json=payload,
    )
    if not resp.ok:
        raise RuntimeError(
            f"Failed to update page {page_id}: {resp.status_code} {resp.text}"
        )


# -------------------------------------------------
# Main logic
# -------------------------------------------------
def main():
    print("🔎 Querying Notion database (excluding Status = 'Resources')...")
    pages = query_database_excluding_resources()
    print(f"   → Found {len(pages)} pages to inspect.")

    updated_count = 0
    skipped_no_company = 0
    skipped_already_has_desc = 0
    skipped_groq_fail = 0

    for idx, page in enumerate(pages, start=1):
        page_id = page["id"]
        properties = page.get("properties", {})

        company = get_text_property(properties, "Company")
        status_val = get_status_value(properties, "Status") or "(none)"
        title = get_text_property(properties, "Name") or "(untitled)"

        print(f"\n[{idx}/{len(pages)}] Page {page_id}")
        print(f"   Name:    {title}")
        print(f"   Status:  {status_val}")
        print(f"   Company: {company or '(empty)'}")

        if not company:
            print("   → Skipping: no Company value.")
            skipped_no_company += 1
            continue

        if has_nonempty_rich_text(properties, "Company Description"):
            print("   → Skipping: Company Description already populated.")
            skipped_already_has_desc += 1
            continue

        print("   → Generating company description via Groq...")
        desc = generate_company_description(company)
        if not desc:
            print("   ⚠️  Groq returned no description; skipping.")
            skipped_groq_fail += 1
            continue

        wrapped = "\n       ".join(textwrap.wrap(desc, width=70))
        print("   → Description generated:")
        print(f"       {wrapped}")

        try:
            update_company_description(page_id, desc)
        except RuntimeError as e:
            print(f"   ❌ Failed to update Notion: {e}")
            continue

        updated_count += 1
        print("   ✅ Company Description updated.")

        # Tiny delay to avoid hammering APIs
        time.sleep(0.3)

    print("\n🏁 Done.")
    print(f"   Updated pages:                {updated_count}")
    print(f"   Skipped (no Company):         {skipped_no_company}")
    print(f"   Skipped (already has desc):   {skipped_already_has_desc}")
    print(f"   Skipped (Groq fail):          {skipped_groq_fail}")


if __name__ == "__main__":
    main()
