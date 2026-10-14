import argparse
import html
import json
import logging
import os
import re
import sys
import time
from collections import deque
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
import gspread
from gspread.exceptions import APIError
from playwright.sync_api import sync_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

PROCESSED_TRACKER = "processed_pages.json"
OUTPUT_DIR = "output_pages"
BASE_HOST = "www.henrywalser.com"

SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".pdf", ".css",
            ".js", ".ico", ".zip", ".mp4", ".mp3", ".woff", ".woff2", ".ttf",
            ".xml", ".gz")
SKIP_PATH_PREFIXES = ("/obituaries", "/Administration", "/administration",
                      "/admin", "/login", "/signin", "/sign-in")
VIDEO_HOST_HINTS = ("youtube.com", "youtu.be", "youtube-nocookie.com", "vimeo.com",
                    "player.vimeo.com", "wistia", "tukios", "brightcove")
CHALLENGE_HINTS = ("just a moment", "attention required", "checking your browser",
                   "verify you are human")


def load_processed_ids():
    if os.path.exists(PROCESSED_TRACKER) and os.path.getsize(PROCESSED_TRACKER) > 0:
        try:
            with open(PROCESSED_TRACKER, "r", encoding="utf-8") as f:
                content = json.load(f)
                if isinstance(content, list):
                    return set(content)
        except json.JSONDecodeError:
            logging.warning("processed_pages.json was corrupted. Starting empty.")
            return set()
        except Exception as e:
            logging.warning(f"Could not read {PROCESSED_TRACKER}: {e}. Starting empty.")
            return set()
    return set()


def save_processed_ids(processed_ids):
    try:
        with open(PROCESSED_TRACKER, "w", encoding="utf-8") as f:
            json.dump(sorted(processed_ids), f, indent=2)
    except Exception as e:
        logging.error(f"Failed to save {PROCESSED_TRACKER}: {e}")


def same_host(url):
    host = urlparse(url).netloc.lower().replace("www.", "", 1)
    return host == BASE_HOST.replace("www.", "", 1)


def page_key(url):
    path = urlparse(url).path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return path


def section_of(url):
    parts = [p for p in urlparse(url).path.split("/") if p]
    return parts[0] if parts else "home"


def is_skippable_path(path):
    if any(path.startswith(p) for p in SKIP_PATH_PREFIXES):
        return True
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


def extract_main_text(soup):
    source = soup.find("main") or soup.find("article") or soup.body
    if not source:
        return ""
    body = BeautifulSoup(str(source), "html.parser")
    for tag in body.find_all(["script", "style", "noscript", "nav", "header",
                              "footer", "aside", "form", "svg", "iframe"]):
        tag.decompose()
    text = body.get_text(separator="\n")
    text = re.sub(r"Loading\.\.\.", " ", text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n\n".join(lines)


def extract_metadata(soup):
    meta = {}
    title_tag = soup.find("title")
    meta["title"] = html.unescape(title_tag.text.strip()) if title_tag else ""
    desc = soup.find("meta", attrs={"name": "description"}) or \
        soup.find("meta", attrs={"property": "og:description"})
    meta["description"] = html.unescape(desc["content"].strip()) if desc and desc.get("content") else ""
    if not meta["title"]:
        ogt = soup.find("meta", attrs={"property": "og:title"})
        if ogt and ogt.get("content"):
            meta["title"] = html.unescape(ogt["content"].strip())
    h1 = soup.find("h1")
    meta["h1"] = h1.get_text(strip=True) if h1 else ""
    return meta


def extract_links(soup, page_url):
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

    for a in soup.find_all(["a", "area"]):
        consider(a.get("href"))
    for el in soup.find_all(attrs={"data-href": True}):
        consider(el.get("data-href"))
    for el in soup.find_all(attrs={"data-url": True}):
        consider(el.get("data-url"))

    for frame in soup.find_all(["iframe", "embed", "source"]):
        src = frame.get("src")
        if src:
            full = urljoin(page_url, src)
            if any(h in urlparse(full).netloc.lower() for h in VIDEO_HOST_HINTS):
                videos.add(full)

    for m in re.findall(r"https?://[^\s\"'<>\\]+", str(soup)):
        if any(h in urlparse(m).netloc.lower() for h in VIDEO_HOST_HINTS):
            videos.add(m.split("\\")[0])

    return sorted(internal), sorted(external), sorted(videos)


def parse_page(html_content, page_url):
    soup = BeautifulSoup(html_content, "html.parser")
    meta = extract_metadata(soup)
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
    logging.info(f"Fetching sitemap structure from {sitemap_url}...")
    try:
        resp = session.get(sitemap_url, impersonate="chrome", timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logging.error(f"Failed to fetch sitemap {sitemap_url}: {e}")
        return []

    soup = BeautifulSoup(resp.content, "xml")
    sub_sitemaps = [loc.text.strip() for loc in soup.find_all("loc")
                    if "sitemap" in loc.text.lower()]
    if sub_sitemaps and soup.find("sitemapindex"):
        logging.info(f"Found {len(sub_sitemaps)} sub-sitemaps.")
        all_urls = []
        for idx, sub_url in enumerate(sub_sitemaps, start=1):
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
{record.get('text') or '(no text found)'}

{block('Video Links', record['video_links'])}
{block('Internal Page Links', record['internal_links'])}
{block('External Links', record['external_links'])}
"""


def save_locally(record):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    safe = record["key"].strip("/").replace("/", "_") or "home"
    filepath = os.path.join(OUTPUT_DIR, f"page_{safe}.md")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(build_markdown_document(record))
    return filepath


def render_html(page, url, settle_ms, nav_timeout_ms):
    resp = page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
    status = resp.status if resp else None
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass
    try:
        page.wait_for_function(
            "() => { const el = document.querySelector('main, article') || document.body;"
            " return el && el.innerText && el.innerText.replace(/Loading\\.\\.\\./g,'').trim().length > 150; }",
            timeout=settle_ms,
        )
    except Exception:
        pass
    page.wait_for_timeout(400)
    return page.content(), status


def is_challenge(title):
    t = (title or "").strip().lower()
    return any(t.startswith(c) for c in CHALLENGE_HINTS)


def safe_append_batch_with_retry(sheet, batch, max_retries=5):
    for attempt in range(1, max_retries + 1):
        try:
            sheet.append_rows(batch)
            time.sleep(1.5)
            return True
        except APIError as e:
            if "429" in str(e) or "Quota exceeded" in str(e):
                backoff = attempt * 10
                logging.warning(f"Sheets 429. Backing off {backoff}s ({attempt}/{max_retries})...")
                time.sleep(backoff)
            else:
                logging.error(f"Sheets API Error: {e}")
                time.sleep(5)
        except Exception as err:
            logging.error(f"Unexpected upload error: {err}")
            time.sleep(5)
    logging.critical("Failed to upload batch after max retries.")
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Crawl non-obituary pages of henrywalser.com (render JS, follow internal links) to Google Sheets."
    )
    parser.add_argument("--sitemap", default="https://www.henrywalser.com/sitemap.xml")
    parser.add_argument("--base-url", default="https://www.henrywalser.com/")
    parser.add_argument("--sheet-name", required=True)
    parser.add_argument("--worksheet", default="OtherPages")
    parser.add_argument("--credentials", default="service_account.json")
    parser.add_argument("--max-pages", type=int, default=1000,
                        help="Safety cap on total pages crawled.")
    parser.add_argument("--force", action="store_true",
                        help="Full refresh: clear the tab and re-write every reachable page.")
    parser.add_argument("--settle-ms", type=int, default=8000)
    parser.add_argument("--nav-timeout-ms", type=int, default=45000)
    args = parser.parse_args()

    logging.info("Connecting to Google Sheets API...")
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

    processed = load_processed_ids()
    logging.info(f"Loaded {len(processed)} previously processed page(s).")

    # --- Seed the crawl from the sitemap + the homepage ---
    session = cffi_requests.Session()
    seed_urls = get_urls_from_sitemap(args.sitemap, session)
    seed_urls.append(args.base_url)
    seen, frontier = set(), deque()
    for u in seed_urls:
        k = page_key(u)
        if k not in seen:
            seen.add(k)
            frontier.append(u)
    logging.info(f"Seeded crawl with {len(frontier)} URL(s) from sitemap + homepage.")

    rows, newly_done = [], set()
    challenges = 0
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    custom_header = os.environ.get("CRAWLER_KEY")
    extra_headers = {"X-Crawler-Key": custom_header} if custom_header else {}

    logging.info("Launching headless Chromium...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context(user_agent=ua, viewport={"width": 1366, "height": 900},
                                  extra_http_headers=extra_headers)
        ctx.route("**/*", lambda r: r.abort()
                  if r.request.resource_type in ("image", "media", "font") else r.continue_())
        page = ctx.new_page()

        visited = 0
        while frontier and visited < args.max_pages:
            url = frontier.popleft()
            visited += 1
            start = time.time()
            try:
                html_content, status = render_html(page, url, args.settle_ms, args.nav_timeout_ms)
                if is_challenge(page.title()):
                    challenges += 1
                    logging.error(f"[{visited}] Cloudflare challenge on {url}. Needs allowlisting.")
                    continue
                record = parse_page(html_content, url)
                elapsed = round(time.time() - start, 2)

                # Still follow this page's links for discovery...
                discovered = 0
                for link in record["internal_links"]:
                    k = page_key(link)
                    if k not in seen:
                        seen.add(k)
                        frontier.append(link)
                        discovered += 1

                # ...but DON'T store admin/login or empty pages.
                title_l = (record["title"] or "").lower()
                is_admin = ("log in" in title_l or "login" in title_l
                            or "administrator" in title_l or record["section"] == "admin")
                is_blank = not (record["title"] or "").strip() and not record["text"].strip()
                if is_admin or is_blank:
                    logging.info(f"[{visited}] skipped junk page ({'admin' if is_admin else 'blank'}): {url}")
                    continue

                save_locally(record)

                logging.info(f"[{visited}] ({elapsed}s) {record['title']} "
                             f"[text {len(record['text'])} chars, "
                             f"{len(record['video_links'])} video, +{discovered} new links, "
                             f"queue {len(frontier)}]")

                rows.append([
                    record["url"], record["title"], record["section"],
                    record.get("description", ""), record.get("text", ""),
                    "\n".join(record["video_links"]),
                    "\n".join(record["internal_links"]),
                    "\n".join(record["external_links"]),
                ])
                newly_done.add(record["key"])
            except Exception as exc:
                elapsed = round(time.time() - start, 2)
                logging.error(f"[{visited}] ({elapsed}s) {url}: {exc}")

        browser.close()

    if visited >= args.max_pages and frontier:
        logging.warning(f"Hit --max-pages ({args.max_pages}); {len(frontier)} still queued. "
                        f"Raise --max-pages if the site is larger.")
    if challenges:
        logging.warning(f"{challenges} page(s) hit a Cloudflare challenge. If most pages, "
                        f"the crawler needs allowlisting (IP + User-Agent + X-Crawler-Key).")

    logging.info(f"Crawl finished: {len(rows)} page(s) captured.")

    if not rows:
        logging.warning("No rows produced. If challenges were logged, allowlisting is needed.")
        return

    if args.force:
        # Full refresh: wipe the tab and write everything fresh (no duplicates).
        logging.info("Full refresh: clearing the worksheet and rewriting all rows...")
        sheet.clear()
        sheet.append_row(headers)
        to_upload = rows
        processed = set(newly_done)
    else:
        # Incremental: only add pages not already processed.
        if not sheet.row_values(1):
            sheet.append_row(headers)
        to_upload = [r for r, k in zip(rows, [page_key(x[0]) for x in rows])
                     if k not in processed]
        processed.update(newly_done)

    if to_upload:
        logging.info(f"Uploading {len(to_upload)} row(s)...")
        for i in range(0, len(to_upload), 500):
            batch = to_upload[i:i + 500]
            if safe_append_batch_with_retry(sheet, batch):
                logging.info(f"   Uploaded [{min(i + 500, len(to_upload))}/{len(to_upload)}].")
            else:
                logging.error(f"   Failed batch at index {i}.")
    else:
        logging.info("No new pages to upload.")

    save_processed_ids(processed)
    logging.info(f"Synced to '{args.sheet_name}' / '{args.worksheet}'. "
                 f"Total reachable pages: {len(rows)}.")


if __name__ == "__main__":
    main()