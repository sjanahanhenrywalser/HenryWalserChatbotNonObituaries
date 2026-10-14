import argparse
import html
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
import gspread
from gspread.exceptions import APIError

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

PROCESSED_TRACKER = "processed_pages.json"
OUTPUT_DIR = "output_pages"
BASE_HOST = "www.henrywalser.com"

# Links to these file types / paths are not "pages"; we skip them as page links.
SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".pdf", ".css",
            ".js", ".ico", ".zip", ".mp4", ".mp3", ".woff", ".woff2", ".ttf",
            ".xml", ".gz")
# These sections are obituary content; this script is EVERYTHING ELSE.
SKIP_PATH_PREFIXES = ("/obituaries", "/Administration")
VIDEO_HOST_HINTS = ("youtube.com", "youtu.be", "youtube-nocookie.com", "vimeo.com",
                    "player.vimeo.com", "wistia", "tukios", "brightcove")


def load_processed_ids():
    """Safely reads the tracker, handling empty or missing files gracefully."""
    if os.path.exists(PROCESSED_TRACKER) and os.path.getsize(PROCESSED_TRACKER) > 0:
        try:
            with open(PROCESSED_TRACKER, "r", encoding="utf-8") as f:
                content = json.load(f)
                if isinstance(content, list):
                    return set(content)
        except json.JSONDecodeError:
            logging.warning("⚠️ processed_pages.json was corrupted. Starting empty.")
            return set()
        except Exception as e:
            logging.warning(f"⚠️ Could not read {PROCESSED_TRACKER}: {e}. Starting empty.")
            return set()
    return set()


def save_processed_ids(processed_ids):
    """Safely writes processed page keys as a JSON array."""
    try:
        with open(PROCESSED_TRACKER, "w", encoding="utf-8") as f:
            json.dump(sorted(processed_ids), f, indent=2)
    except Exception as e:
        logging.error(f"❌ Failed to save {PROCESSED_TRACKER}: {e}")


def same_host(url):
    host = urlparse(url).netloc.lower().replace("www.", "", 1)
    return host == BASE_HOST.replace("www.", "", 1)


def page_key(url):
    """Stable identity for a page: its path, no query/fragment, no trailing slash."""
    path = urlparse(url).path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return path


def section_of(url):
    """Top-level section, e.g. /our-services/what-is-a-funeral -> our-services."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    return parts[0] if parts else "home"


def is_skippable_path(path):
    if any(path.startswith(p) for p in SKIP_PATH_PREFIXES):
        return True
    # crawler-trap guard: a path segment repeated more than twice
    segs = [s for s in path.split("/") if s]
    for i in range(len(segs)):
        run = 1
        for j in range(i + 1, len(segs)):
            if segs[j] == segs[i]:
                run += 1
                if run > 2:
                    return True
            else:
                break
    return False


def clean_text(raw_html_or_text):
    if not raw_html_or_text:
        return ""
    text = html.unescape(html.unescape(raw_html_or_text))
    soup = BeautifulSoup(text, "html.parser")
    lines = soup.get_text(separator="\n").splitlines()
    cleaned = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("http"):
            cleaned.append(s)
    return "\n\n".join(cleaned)


def extract_main_text(soup):
    """Best-effort main-content text from raw HTML. Strips nav/header/footer/etc.
    NOTE: these pages are JavaScript-rendered, so raw HTML may contain little body
    text until the site has allowlisted this scraper (or a rendered fetch is used)."""
    source = soup.find("main") or soup.find("article") or soup.body
    if not source:
        return ""
    # work on a copy so link/video extraction (done earlier) isn't affected
    body = BeautifulSoup(str(source), "html.parser")
    for tag in body.find_all(["script", "style", "noscript", "nav", "header",
                              "footer", "aside", "form", "svg", "iframe"]):
        tag.decompose()
    text = body.get_text(separator="\n")
    # remove the JS placeholder wherever it appears (inline or standalone)
    text = re.sub(r"Loading\.\.\.", " ", text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n\n".join(lines)


def extract_metadata(soup):
    meta = {}
    title_tag = soup.find("title")
    meta["title"] = html.unescape(title_tag.text.strip()) if title_tag else ""
    # meta description
    desc = soup.find("meta", attrs={"name": "description"}) or \
        soup.find("meta", attrs={"property": "og:description"})
    meta["description"] = html.unescape(desc["content"].strip()) if desc and desc.get("content") else ""
    # og:title fallback
    if not meta["title"]:
        ogt = soup.find("meta", attrs={"property": "og:title"})
        if ogt and ogt.get("content"):
            meta["title"] = html.unescape(ogt["content"].strip())
    # first H1
    h1 = soup.find("h1")
    meta["h1"] = h1.get_text(strip=True) if h1 else ""
    return meta


def extract_links(soup, page_url):
    """Return (internal_page_links, external_links, video_links)."""
    internal, external, videos = set(), set(), set()

    def consider(raw):
        if not raw:
            return
        raw = raw.strip()
        if raw.startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
            return
        full = urljoin(page_url, raw)
        if not full.startswith("http"):
            return
        host = urlparse(full).netloc.lower()
        if any(h in host for h in VIDEO_HOST_HINTS):
            videos.add(full)
            return
        if same_host(full):
            path = page_key(full)
            low = path.lower()
            if low.endswith(SKIP_EXT) or is_skippable_path(path):
                return
            internal.add(full.split("#")[0])
        else:
            external.add(full)

    # <a href>, <area href>, and common data-* link attrs
    for a in soup.find_all(["a", "area"]):
        consider(a.get("href"))
    for el in soup.find_all(attrs={"data-href": True}):
        consider(el.get("data-href"))
    for el in soup.find_all(attrs={"data-url": True}):
        consider(el.get("data-url"))

    # iframes / embeds often carry the video
    for frame in soup.find_all(["iframe", "embed", "source"]):
        src = frame.get("src")
        if src:
            full = urljoin(page_url, src)
            if any(h in urlparse(full).netloc.lower() for h in VIDEO_HOST_HINTS):
                videos.add(full)

    # bare video URLs sometimes sit in scripts/JSON
    for m in re.findall(r"https?://[^\s\"'<>\\]+", str(soup)):
        if any(h in urlparse(m).netloc.lower() for h in VIDEO_HOST_HINTS):
            videos.add(m.split("\\")[0])

    return sorted(internal), sorted(external), sorted(videos)


def parse_page(html_content, page_url):
    soup = BeautifulSoup(html_content, "html.parser")
    meta = extract_metadata(soup)
    # Extract links/videos FIRST, before text extraction decomposes iframes etc.
    internal, external, videos = extract_links(soup, page_url)
    text = extract_main_text(soup)

    title = meta["title"] or meta["h1"] or page_key(page_url)
    return {
        "key": page_key(page_url),
        "url": page_url.split("#")[0],
        "title": title,
        "section": section_of(page_url),
        "description": meta["description"],
        "text": text,
        "internal_links": internal,
        "external_links": external,
        "video_links": videos,
    }


def get_urls_from_sitemap(sitemap_url, session):
    """Walk the sitemap index; return NON-obituary page URLs."""
    logging.info(f"🔍 Fetching sitemap structure from {sitemap_url}...")
    try:
        resp = session.get(sitemap_url, impersonate="chrome", timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logging.error(f"❌ Failed to fetch sitemap {sitemap_url}: {e}")
        return []

    soup = BeautifulSoup(resp.content, "xml")
    sub_sitemaps = [loc.text.strip() for loc in soup.find_all("loc")
                    if "sitemap" in loc.text.lower()]
    if sub_sitemaps and soup.find("sitemapindex"):
        logging.info(f"Found {len(sub_sitemaps)} sub-sitemaps.")
        all_urls = []
        for idx, sub_url in enumerate(sub_sitemaps, start=1):
            # Skip obituary sitemaps entirely — this script is everything else.
            if "obituar" in sub_url.lower():
                logging.info(f"   [{idx}/{len(sub_sitemaps)}] Skipping obituary sitemap: {sub_url}")
                continue
            logging.info(f"   [{idx}/{len(sub_sitemaps)}] Reading: {sub_url}")
            all_urls.extend(get_urls_from_sitemap(sub_url, session))
        return list(set(all_urls))

    urls = []
    for loc in soup.find_all("loc"):
        u = loc.text.strip()
        if not same_host(u):
            continue
        path = page_key(u)
        if path.lower().endswith(SKIP_EXT) or is_skippable_path(path):
            continue
        urls.append(u.split("#")[0])
    return list(set(urls))


def build_markdown_document(record):
    def block(title, items):
        if not items:
            return f"## {title}\n(none)\n"
        return f"## {title}\n" + "\n".join(f"- {i}" for i in items) + "\n"

    return f"""---
url: "{record['url']}"
title: "{record['title']}"
section: "{record['section']}"
---

# {record['title']}

## Description
{record.get('description') or '(none)'}

## Page Text
{record.get('text') or '(no text found in raw HTML — page may be JavaScript-rendered)'}

{block('Video Links', record['video_links'])}
{block('Internal Page Links', record['internal_links'])}
{block('External Links', record['external_links'])}
"""


def save_locally(record):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    safe = record["key"].strip("/").replace("/", "_") or "home"
    filepath = os.path.join(OUTPUT_DIR, f"page_{safe}.md")
    content = build_markdown_document(record)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    return filepath, content


def fetch_and_process(url, session, current_index, total_count, timeout=15):
    start = time.time()
    try:
        resp = session.get(url, impersonate="chrome", timeout=timeout)
        resp.raise_for_status()
        record = parse_page(resp.text, url)
        elapsed = round(time.time() - start, 2)
        save_locally(record)
        logging.info(f"✅ [{current_index}/{total_count}] ({elapsed}s) {record['title']} "
                     f"[{len(record['video_links'])} video, {len(record['internal_links'])} links]")
        return record["key"], record
    except Exception as exc:
        elapsed = round(time.time() - start, 2)
        logging.error(f"❌ [{current_index}/{total_count}] ({elapsed}s) {url}: {exc}")
        return None, None


def safe_append_batch_with_retry(sheet, batch, max_retries=5):
    for attempt in range(1, max_retries + 1):
        try:
            sheet.append_rows(batch)
            time.sleep(1.5)
            return True
        except APIError as e:
            if "429" in str(e) or "Quota exceeded" in str(e):
                backoff = attempt * 10
                logging.warning(f"⚠️ Sheets 429. Backing off {backoff}s ({attempt}/{max_retries})...")
                time.sleep(backoff)
            else:
                logging.error(f"❌ Sheets API Error: {e}")
                time.sleep(5)
        except Exception as err:
            logging.error(f"❌ Unexpected upload error: {err}")
            time.sleep(5)
    logging.critical("❌ Failed to upload batch after max retries.")
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Scrape non-obituary pages of henrywalser.com (text + links + video links) to Google Sheets."
    )
    parser.add_argument("--sitemap", default="https://www.henrywalser.com/sitemap.xml")
    parser.add_argument("--sheet-name", required=True,
                        help="Exact title of your Google Sheet document")
    parser.add_argument("--worksheet", default="Pages",
                        help="Worksheet/tab name to write pages into (default: Pages)")
    parser.add_argument("--credentials", default="service_account.json")
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--max", type=int, default=None)
    parser.add_argument("--force", action="store_true",
                        help="Re-scrape all pages even if already processed (content changes).")
    args = parser.parse_args()

    logging.info("🔑 Connecting to Google Sheets API...")
    gc = gspread.service_account(filename=args.credentials)
    spreadsheet = gc.open(args.sheet_name)
    try:
        sheet = spreadsheet.worksheet(args.worksheet)
    except gspread.WorksheetNotFound:
        logging.info(f"Creating worksheet '{args.worksheet}'...")
        sheet = spreadsheet.add_worksheet(title=args.worksheet, rows=200, cols=10)
    logging.info(f"Connected to '{args.sheet_name}' / tab '{args.worksheet}'")

    headers = ["url", "title", "section", "description", "text",
               "video_links", "internal_links", "external_links"]
    if not sheet.row_values(1):
        logging.info("Setting up header row...")
        sheet.append_row(headers)

    processed = load_processed_ids()
    logging.info(f"Loaded {len(processed)} previously processed page(s).")

    session = cffi_requests.Session()
    all_urls = get_urls_from_sitemap(args.sitemap, session)
    logging.info(f"📊 {len(all_urls)} non-obituary page URL(s) in sitemap.")

    if args.force:
        new_urls = all_urls
    else:
        new_urls = [u for u in all_urls if page_key(u) not in processed]
    total = len(new_urls)
    logging.info(f"📊 {total} page(s) to process"
                 f"{' (forced full re-scrape)' if args.force else ' (new only)'}.")

    if not new_urls:
        logging.info("🎉 Pages are up to date. Nothing to do.")
        return

    if args.max:
        new_urls = new_urls[: args.max]
        total = len(new_urls)
        logging.info(f"Limit applied: first {total} page(s).")

    rows, newly_done = [], set()
    logging.info(f"🚀 Scraping with {args.workers} workers...")
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(fetch_and_process, u, session, i, total): u
                   for i, u in enumerate(new_urls, start=1)}
        done = 0
        for fut in as_completed(futures):
            done += 1
            key, record = fut.result()
            if key and record:
                rows.append([
                    record["url"],
                    record["title"],
                    record["section"],
                    record.get("description", ""),
                    record.get("text", ""),
                    "\n".join(record["video_links"]),
                    "\n".join(record["internal_links"]),
                    "\n".join(record["external_links"]),
                ])
                newly_done.add(key)
            if done % 25 == 0 or done == total:
                logging.info(f"📈 Progress: {done}/{total}")

    if rows:
        logging.info(f"☁️ Uploading {len(rows)} page row(s) to Google Sheets...")
        for i in range(0, len(rows), 500):
            batch = rows[i:i + 500]
            if safe_append_batch_with_retry(sheet, batch):
                logging.info(f"   Uploaded [{min(i + 500, len(rows))}/{len(rows)}].")
            else:
                logging.error(f"   Failed batch at index {i}.")
        processed.update(newly_done)
        save_processed_ids(processed)
        logging.info(f"🎉 Synced {len(rows)} page(s) to '{args.sheet_name}' / '{args.worksheet}'.")


if __name__ == "__main__":
    main()
