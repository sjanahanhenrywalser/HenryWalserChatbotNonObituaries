"""
Record when a daily job last ran, and whether it worked, on a status tab in the
HenryWalserChatbotInfo spreadsheet.

Run by each GitHub workflow as its last step (even if the scrape failed):
    python write_status.py --sheet-name "HenryWalserChatbotInfo" \
        --job "Obituaries" --data-tab "Obituaries" --result success

The status tab is "Sheet1" if that tab is empty or already holds this status
table. If Sheet1 holds anything else (for example old data), it is left alone
and a "Sync Status" tab is used instead.

Each job has one row, updated in place:
    Job | Last run (Kitchener time) | Result | Rows in tab | Run details
"""
import argparse
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
from gspread.exceptions import APIError, WorksheetNotFound

HEADER = ["Job", "Last run (Kitchener time)", "Result", "Rows in tab", "Run details"]
PREFERRED_TAB = "Timestamps"
FALLBACK_TAB = "Sync Status"


def with_retry(fn, tries=5):
    """Retry on Google Sheets rate limits (429) with a growing pause."""
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except APIError as e:
            if "429" in str(e) and attempt < tries:
                time.sleep(attempt * 10)
                continue
            raise


def status_tab(spreadsheet):
    """Sheet1 if it is free or already ours; otherwise the Sync Status tab."""
    try:
        ws = spreadsheet.worksheet(PREFERRED_TAB)
        first_row = with_retry(lambda: ws.row_values(1))
        if not any(cell.strip() for cell in first_row) or first_row[:1] == ["Job"]:
            return ws
        print(f'"{PREFERRED_TAB}" already holds other data, so it was left untouched; '
              f'writing to "{FALLBACK_TAB}" instead.')
    except WorksheetNotFound:
        return with_retry(lambda: spreadsheet.add_worksheet(title=PREFERRED_TAB, rows=20, cols=len(HEADER)))
    try:
        return spreadsheet.worksheet(FALLBACK_TAB)
    except WorksheetNotFound:
        return with_retry(lambda: spreadsheet.add_worksheet(title=FALLBACK_TAB, rows=20, cols=len(HEADER)))


def rows_in(spreadsheet, tab):
    """Data rows in a tab (not counting its header), or blank if it can't be read."""
    if not tab:
        return ""
    try:
        ws = spreadsheet.worksheet(tab)
        return max(len(with_retry(lambda: ws.col_values(1))) - 1, 0)
    except Exception:
        return ""


def main():
    ap = argparse.ArgumentParser(description="Record a job's last run on the status tab.")
    ap.add_argument("--sheet-name", required=True)
    ap.add_argument("--job", required=True, help='Row label, e.g. "Obituaries" or "Website pages"')
    ap.add_argument("--data-tab", default="", help="Tab whose row count to show")
    ap.add_argument("--result", default="success", help="success, failure or cancelled")
    ap.add_argument("--credentials", default="service_account.json")
    args = ap.parse_args()

    gc = gspread.service_account(filename=args.credentials)
    spreadsheet = gc.open(args.sheet_name)
    ws = status_tab(spreadsheet)

    now = datetime.now(ZoneInfo("America/Toronto")).strftime("%Y-%m-%d %I:%M %p %Z")
    server, repo, run_id = (os.environ.get(k, "") for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    run_link = f"{server}/{repo}/actions/runs/{run_id}" if server and repo and run_id else ""
    row = [args.job, now, args.result, rows_in(spreadsheet, args.data_tab), run_link]

    values = with_retry(lambda: ws.get_all_values())
    if not values or values[0][:1] != ["Job"]:
        with_retry(lambda: ws.update(range_name="A1", values=[HEADER]))
        values = [HEADER]
    target = next((i + 1 for i, r in enumerate(values) if r[:1] == [args.job]), len(values) + 1)
    with_retry(lambda: ws.update(range_name=f"A{target}", values=[row]))
    print(f'Recorded on "{ws.title}" row {target}: {row}')


if __name__ == "__main__":
    main()
