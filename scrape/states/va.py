"""
Virginia Bill Scraper — New LIS Website (Headless)

Scrapes bill details and histories from the new Virginia LIS SPA:
    https://lis.virginia.gov/bill-details/{session_code}/{bill_id}

Uses Playwright (headless Chromium) since the new site is a React SPA
that loads data dynamically via JavaScript.

Current entry point: python cli.py VA <term> scrape
Collects a complete configured term, caching bills by session and publishing
CSV pairs only after every listed instrument succeeds. Session IDs must be
verified before a new term is enabled; currently only 2024_2025 is configured.

URL structure:
    session_code = YYYY + session_number (e.g., 20251 = 2025 RS,
                                          20242 = 2024 SS1)
    bill_id = prefix + number (e.g., HB2021, SB1021)

Output format matches the old scraper (va.py) exactly so the
estimation pipeline can consume both interchangeably.
"""

from __future__ import annotations

from scrape.reporting import scrape_run, write_manifest

import base64
import csv
import json
import re
import time
from pathlib import Path
from utils.validate_term import term_years
from unicodedata import normalize

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Page

REPO_ROOT = Path(__file__).resolve().parents[2]

# CSV column headers (compatible with the preceding generation)
DETAILS_HEADER = [
    "bill_id",
    "term",
    "session_year",
    "session",
    "short_title",
    "sponsor",
    "house_sponsors",
    "senate_sponsors",
    "summary",
    "bill_url",
]

HISTORY_HEADER = [
    "bill_id",
    "term",
    "session_year",
    "session",
    "action_date",
    "chamber",
    "action",
    "order",
    "action_details_url",
]

# Map session_number to session name suffix
SESSION_NAMES = {
    1: "SESSION",
    2: "SPECIAL SESSION I",
    3: "SPECIAL SESSION II",
    4: "SPECIAL SESSION III",
    5: "SPECIAL SESSION IV",
}

# LIS internal session IDs (discovered via API interception).
# Map (year, session_num) -> LIS sessionID integer.
# These are needed for the search API. Add new entries as needed.
LIS_SESSION_IDS = {
    (2024, 1): 55,   # 2024 Regular Session
    (2024, 2): 56,   # 2024 Special Session I
    (2025, 1): 57,   # 2025 Regular Session
}

# Map API ActorType values to old-scraper chamber names
ACTOR_TYPE_MAP = {
    "House": "House",
    "Senate": "Senate",
    "Governor": "Governor",
}


def _session_code(year: int, session_num: int) -> str:
    """Build the LIS URL session code (e.g., '20251')."""
    return f"{year}{session_num}"


def _session_name(year: int, session_num: int) -> str:
    """Build the human-readable session name."""
    suffix = SESSION_NAMES.get(
        session_num, f"SPECIAL SESSION {session_num}"
    )
    return f"{year} {suffix}"


def _bill_id_pad(prefix: str, number: int) -> str:
    """Zero-pad a bill number to 4 digits with prefix."""
    return f"{prefix}{str(number).zfill(4)}"


def _parse_api_date(iso_date: str) -> str:
    """Convert ISO datetime (2025-01-06T13:48:00) to YYYY-MM-DD."""
    if not iso_date:
        return ""
    return iso_date[:10]


def _extract_summary_text(html_summary: str) -> str:
    """Extract plain text from HTML summary."""
    if not html_summary:
        return ""
    soup = BeautifulSoup(html_summary, "lxml")
    return (
        soup.get_text()
        .replace("\r\n", " ")
        .replace("\n", " ")
        .strip()
    )


def discover_bill_numbers(
    page: Page,
    lis_session_id: int,
    verbose: bool = False,
) -> list[str]:
    """Get all bill numbers for a session via API interception.

    Navigates to the bill search page with a query that triggers
    GetLegislationIdsListAsync and captures the response.

    Returns list of bill numbers like ["HB9", "HB19", ..., "SB1495"].
    """
    captured = [None]

    def on_response(response):
        if "GetLegislationIdsListAsync" in response.url:
            ct = response.headers.get("content-type", "")
            if "json" in ct:
                try:
                    captured[0] = response.json()
                except Exception:
                    pass

    page.on("response", on_response)

    query = json.dumps({
        "selectedBillNumbers": "",
        "selectedKeywords": "",
        "selectedSession": lis_session_id,
        "selectedChapterNumber": "",
        "includeFailed": True,
        "SortBy": "Bill|ASC",
    })
    encoded = base64.b64encode(query.encode()).decode()
    url = f"https://lis.virginia.gov/bill-search?q={encoded}"

    try:
        page.goto(url, timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)
        page.wait_for_timeout(3000)
    finally:
        page.remove_listener("response", on_response)

    if not captured[0]:
        raise RuntimeError("VA listing response was not captured; no outputs published")

    data = captured[0]
    if not data.get("Success"):
        raise RuntimeError(f"VA listing API failed: {data.get('FailureMessage')}")

    ids = data.get("LegislationIds", [])
    bill_numbers = [
        entry["LegislationNumber"] for entry in ids
    ]

    if verbose:
        print(f"  Found {len(bill_numbers)} bills in session")

    return bill_numbers

def select_final_legislation(legislations, bill_number, session_code):
    """Select the final available summary for one legislative instrument."""
    if not isinstance(legislations, list) or not legislations:
        raise ValueError("VA missing legislation records")

    versions = {
        "SUMMARY AS INTRODUCED": 1,
        "SUMMARY AS PASSED HOUSE": 2,
        "SUMMARY AS PASSED SENATE": 2,
        "SUMMARY AS PASSED": 3,
        "SUMMARY AS ENACTED WITH GOVERNOR'S RECOMMENDATION": 4,
    }

    first = legislations[0]

    if not isinstance(first, dict):
        raise ValueError("VA malformed legislation record")

    identity = (
        first.get("LegislationID"),
        first.get("LegislationNumber"),
        first.get("SessionCode"),
    )

    if (
        identity[0] is None
        or identity[1] != bill_number
        or identity[2] != session_code
    ):
        raise ValueError("VA legislation identity mismatch")

    selected = None
    selected_rank = -1

    for record in legislations:
        if not isinstance(record, dict):
            raise ValueError("VA malformed legislation record")

        current_identity = (
            record.get("LegislationID"),
            record.get("LegislationNumber"),
            record.get("SessionCode"),
        )

        if current_identity != identity:
            raise ValueError("VA conflicting legislation identities")

        version = record.get("SummaryVersion")

        # A single unversioned record is permitted.
        if version is None and len(legislations) == 1:
            rank = 0
        elif version in versions:
            rank = versions[version]
        else:
            raise ValueError(
                f"VA unexpected summary version: {version!r}"
            )

        # Records for the same version must not conflict.
        if selected is not None and rank == selected_rank:
            if record != selected:
                raise ValueError(
                    "VA conflicting records at same summary stage"
                )

        if rank > selected_rank:
            selected = record
            selected_rank = rank

    return selected

def scrape_bill(
    page: Page,
    session_code: str,
    bill_number_raw: str,
    year: int,
    session_num: int,
    term: str,
    verbose: bool = False,
) -> tuple[list, list[list]] | str:
    """Scrape a single bill via API interception.

    Navigates to the bill detail page and intercepts the JSON API
    responses for bill details, patrons, and history.

    Returns (details_row, history_rows) or "Error" on failure.
    """
    prefix = re.sub(r"\d.*", "", bill_number_raw)
    number = int(re.sub(r"^[A-Z]+", "", bill_number_raw))
    bill_id = _bill_id_pad(prefix, number)
    session_name_str = _session_name(year, session_num)

    url = (
        f"https://lis.virginia.gov"
        f"/bill-details/{session_code}/{bill_number_raw}"
    )

    # Set up API interception
    api = {"bill": None, "patrons": None, "events": None,"event_types": None}

    def on_response(response):
        resp_url = response.url
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        try:
            if "GetLegislationListAsync" in resp_url:
                api["bill"] = response.json()
            elif "GetLegislationPatronsByIdAsync" in resp_url:
                api["patrons"] = response.json()
            elif (
                "GetPublicLegislationEventHistoryListAsync"
                in resp_url
            ):
                api["events"] = response.json()
            elif (
                "getlegislationeventtypereferencesasync"
                in resp_url.lower()
            ):
                api["event_types"] = response.json()
        except Exception:
            pass

    page.on("response", on_response)

    try:
        page.goto(url, timeout=20000)
        page.wait_for_load_state("networkidle", timeout=15000)
        page.wait_for_timeout(1500)
    except Exception as e:
        page.remove_listener("response", on_response)
        if verbose:
            print(f"    Error loading {url}: {e}")
        return "Error"

    page.remove_listener("response", on_response)

    # Check if valid bill page loaded (not search redirect)
    title = page.title()
    if "Bill Search" in title:
        return "Error"

    # --- Extract bill details from API ---
    bill_data = api.get("bill")

    if not isinstance(bill_data, dict):
        return "Error"

    if bill_data.get("Success") is not True:
        return "Error"

    legislations = bill_data.get("Legislations")

    if not isinstance(legislations, list) or not legislations:
        if verbose:
            print(f"    No bill API data for {bill_id}")
        return "Error"

    try:
        leg = select_final_legislation(
            legislations,
            bill_number_raw,
            session_code,
        )
    except ValueError as error:
        if verbose:
            print(f"    {bill_id}: {error}")
        return "Error"

    for key, field in (("patrons", "Patrons"), ("events", "LegislationEvents")):
        payload = api[key]
        if (not isinstance(payload, dict) or payload.get("Success") is False
                or not isinstance(payload.get(field), list)):
            return "Error"
    returned_number = leg.get("LegislationNumber")
    if returned_number and re.sub(r"\s+", "", returned_number) != bill_number_raw:
        return "Error"
    if not (leg.get("Description") or "").strip():
        return "Error"

    # Short title: "HB 1876 <description>"
    description = (leg.get("Description") or "").strip()
    short_title = (
        f"{prefix} {number} {description}"
    )

    # Summary
    summary = _extract_summary_text(
        leg.get("LegislationSummary", "")
    )

    # --- Extract patrons from API ---
    patron_data = api.get("patrons")
    patrons = (
        patron_data.get("Patrons", []) if patron_data else []
    )

    # Find introducing sponsor (chief patron)
    sponsor = ""
    for p in patrons:
        if p.get("IsIntroducing") or p.get("Name") == "Chief Patron":
            name = normalize(
                "NFKD", p.get("MemberDisplayName", "")
            )
            display = p.get("DisplayName", "")
            if display:
                sponsor = f"{name} ({p['Name']})"
            else:
                sponsor = name
            break

    # If no introducing patron found, use first patron
    if not sponsor and patrons:
        p = patrons[0]
        name = normalize(
            "NFKD", p.get("MemberDisplayName", "")
        )
        sponsor = f"{name} ({p.get('Name', '')})"

    # Build house and senate sponsor lists
    house_sponsors = []
    senate_sponsors = []
    for p in patrons:
        name = normalize(
            "NFKD", p.get("MemberDisplayName", "")
        )
        display_name = p.get("DisplayName", "")
        if display_name:
            full = f"{name} {display_name}"
        else:
            full = name

        chamber_code = p.get("ChamberCode", "")
        if chamber_code == "H":
            house_sponsors.append(full)
        elif chamber_code == "S":
            senate_sponsors.append(full)

    house_sponsors_str = "; ".join(house_sponsors)
    senate_sponsors_str = "; ".join(senate_sponsors)

    # --- Extract history from API ---
    bill_history = []
    event_data = api.get("events")
    events = (
        event_data.get("LegislationEvents", [])
        if event_data else []
    )

    # Official LIS event-type descriptions provide a fallback
    # when individual history events lack Description.
    event_type_data = api.get("event_types")
    event_descriptions = {}

    if (
        isinstance(event_type_data, dict)
        and event_type_data.get("Success") is True
        and isinstance(event_type_data.get("EventTypes"), list)
    ):
        for reference in event_type_data["EventTypes"]:
            if not isinstance(reference, dict):
                continue

            code = reference.get("EventCode")
            description = (
                reference.get("LegislationDescription") or ""
            ).strip()

            if not code or not description:
                continue

            # Track conflicting definitions without selecting one.
            if code in event_descriptions:
                if event_descriptions[code] != description:
                    event_descriptions[code] = None
            else:
                event_descriptions[code] = description

    # Sort events by date and sequence
    events.sort(
        key=lambda e: (
            e.get("EventDate", ""),
            e.get("Sequence", 0),
        )
    )

    order = 1
    for evt in events:
        if not evt.get("IsPublic", True):
            continue

        action_date = _parse_api_date(
            evt.get("EventDate", "")
        )
        chamber = ACTOR_TYPE_MAP.get(
            evt.get("ActorType", ""), evt.get("ActorType", "")
        )
        action = (
            (evt.get("Description") or "")
            .replace("\r\n", " ")
            .replace("\n", " ")
            .strip()
        )

        if not action:
            code = evt.get("EventCode")
            action = event_descriptions.get(code, "")

        if not action:
            raise ValueError(
                f"VA missing history action: "
                f"{bill_number_raw}, "
                f"event {evt.get('LegislationEventID')}, "
                f"code {evt.get('EventCode')}"
            )

        # Build action detail URL from references if available
        action_url = ""
        refs = evt.get("EventReferences", [])
        for ref in refs:
            ref_type = ref.get("ActionReferenceType", "")
            ref_id = ref.get("ReferenceID")
            if ref_type == "Committee" and ref_id:
                action_url = (
                    f"https://lis.virginia.gov"
                    f"/session-details/{session_code}"
                    f"/committee-information"
                )
                break
            elif ref_type == "VoteTally" and ref_id:
                action_url = (
                    f"https://lis.virginia.gov"
                    f"/bill-details/{session_code}"
                    f"/{bill_number_raw}"
                )
                break

        bill_history.append([
            bill_id,
            term,
            year,
            session_name_str,
            action_date,
            chamber,
            action,
            order,
            action_url,
        ])
        order += 1

    details_row = [
        bill_id,
        term,
        year,
        session_name_str,
        short_title,
        sponsor,
        house_sponsors_str,
        senate_sponsors_str,
        summary,
        url,
    ]

    return (details_row, bill_history)


def sessions_for_term(term: str) -> list[tuple[int, int, int]]:
    """Require a verified session map for both years before starting."""
    years = term_years("VA", term)
    if any((year, 1) not in LIS_SESSION_IDS for year in years):
        raise ValueError(
            f"VA {term}: LIS session IDs are not verified for this term. "
            "Configure its regular and special sessions in "
            "scrape/states/va.py before running."
        )
    return [(year, number, sid) for (year, number), sid
            in sorted(LIS_SESSION_IDS.items()) if year in years]


def _atomic_json(path: Path, payload: dict):
    pending = path.with_suffix(path.suffix + ".tmp")
    try:
        pending.write_text(json.dumps(payload), encoding="utf-8")
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def _validate_record(record, raw_number, year, session_num, term):
    if not isinstance(record, dict):
        raise ValueError(f"VA {year}/{session_num} {raw_number}: invalid record")
    details, history = record.get("details"), record.get("history")
    match = re.fullmatch(r"([A-Z]+)(\d+)", raw_number)
    if not match:
        raise ValueError(f"Unexpected VA bill number: {raw_number}")
    expected = [_bill_id_pad(match[1], int(match[2])), term.replace("_", "-"),
                year, _session_name(year, session_num)]
    if (not isinstance(details, list) or len(details) != len(DETAILS_HEADER)
            or details[:4] != expected):
        raise ValueError(f"VA {raw_number}: wrong details identity or schema")
    if not isinstance(history, list) or not history:
        raise ValueError(f"VA {raw_number}: no validated history")
    for row in history:
        if (not isinstance(row, list) or len(row) != len(HISTORY_HEADER)
                or row[:4] != expected):
            raise ValueError(f"VA {raw_number}: wrong history identity or schema")
    return details, history


@scrape_run
def scrape(state: str, term: str, verbose: bool = False,
           force_fetch: bool = False):
    """Collect a configured term using Playwright; resume from session caches.

    Existing exports from Nick are not treated as completed current runs.
    They remain untouched unless a complete new run succeeds.
    """
    if state.upper() != "VA":
        raise ValueError("Virginia scraper requires state VA")
    sessions = sessions_for_term(term)
    folder = REPO_ROOT / ".data/VA/bill"
    outputs = [folder / f"VA_Bill_{kind}_{term}.csv"
               for kind in ("Details", "Histories")]
    manifest = folder / f".VA_scrape_{term}.json"
    if not force_fetch and manifest.exists() and all(p.exists() for p in outputs):
        completed = json.loads(manifest.read_text())
        if (completed.get("generation") == "headless-v1"
                and completed.get("sessions") == [list(s) for s in sessions]):
            print(f"Skipping VA {term}: complete outputs exist (use --force-fetch)")
            return
    cache = folder / ".cache" / term
    cache.mkdir(parents=True, exist_ok=True)
    details, histories, counts = [], [], {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            for year, session_num, session_id in sessions:
                code = _session_code(year, session_num)
                numbers = discover_bill_numbers(page, session_id, verbose)
                if not numbers or len(numbers) != len(set(numbers)):
                    raise ValueError(f"VA {code}: empty or duplicate bill listing")
                if any(not re.fullmatch(r"[A-Z]+\d+", n) for n in numbers):
                    raise ValueError(f"VA {code}: invalid bill listing")
                counts[code] = len(numbers)
                for raw in numbers:
                    # Bill numbers repeat across years and special sessions.
                    path = cache / f"{code}_{raw}.json"
                    if path.exists() and not force_fetch:
                        record = json.loads(path.read_text(encoding="utf-8"))
                    else:
                        result = scrape_bill(page, code, raw, year, session_num,
                                             term.replace("_", "-"), verbose)
                        if result == "Error":
                            raise RuntimeError(f"VA {code} {raw}: fetch failed; rerun to resume")
                        record = {"details": result[0], "history": result[1]}
                        _validate_record(record, raw, year, session_num, term)
                        _atomic_json(path, record)
                        time.sleep(0.3)
                    bill, actions = _validate_record(record, raw, year, session_num, term)
                    details.append(bill)
                    histories.extend(actions)
                print(f"VA {code}: {len(numbers)} instruments", flush=True)
        finally:
            browser.close()
    staged = []
    try:
        for path, header, rows in zip(outputs, (DETAILS_HEADER, HISTORY_HEADER),
                                      (details, histories)):
            pending = path.with_suffix(".csv.tmp")
            staged.append((pending, path))
            with pending.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(header)
                writer.writerows(rows)
        manifest.unlink(missing_ok=True)
        for pending, target in staged:
            pending.replace(target)
        write_manifest(manifest, {
            "generation": "headless-v1", "term": term, "sessions": sessions,
            "counts": counts, "details": len(details), "histories": len(histories),
            })
    finally:
        for pending, _ in staged:
            pending.unlink(missing_ok=True)
    print(f"VA {term}: wrote {len(details)} details and {len(histories)} histories")
