"""Alabama bills and resolutions from the current Alison public API.

Run with ``python cli.py AL 2023_2026 scrape``. The term covers four session
years following the election; regular and special sessions in all years are
included. Bill numbers are unique only within a session.

The website's bill-search/modal GraphQL queries supply these fields. We omit
the modal's streaming @defer directive and fetch histories in counted pages.
No browser, login, or hardcoded current session is required.
"""

from __future__ import annotations

from scrape.reporting import scrape_run, write_manifest

from collections import defaultdict
import csv
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from utils.validate_term import term_years

BASE_URL = "https://alison.legislature.state.al.us"
API_URL = f"{BASE_URL}/graphql"
# The site opens bill details in a modal without changing this URL. The
# session abbreviation and bill number in each row identify the instrument.
BILL_URL = f"{BASE_URL}/bill-search?tab=3"
REPO_ROOT = Path(__file__).resolve().parents[2]
PAGE_SIZE = 500
CACHE_VERSION = 1

DETAILS_HEADER = [
    "bill_id", "sponsor", "session", "subject", "chamber", "status",
    "committee", "last_action_date", "temp_link", "short_title",
    "term", "session_year", "session_abbreviation", "instrument_id",
    "instrument_type", "all_committees", "prefiled_date", "first_read_date",
    "summary", "bill_url",
]
HISTORY_HEADER = [
    "bill_id", "sponsor", "session", "action_date", "chamber", "amd_sub",
    "action", "committee", "nay", "yea", "abstain", "vote_id",
    "term", "session_year", "session_abbreviation", "instrument_id",
    "history_id", "order", "amd_sub_url", "vote_type",
]

INSTRUMENT_FIELDS = """
    id sessionYear sessionName sessionAbbreviation instrumentNbr instrumentType
    sponsor body subject shortTitle currentStatus assignedCommittee allCommittees
    prefiledDate firstReadDate actSummary lastAction
"""
HISTORY_FIELDS = """
    id instrumentNbr sessionAbbreviation sessionName sessionYear calendarDate
    body matter amdSub amdSubFileUrl committee voteType voteTitle rollCallNbr
    yeas nays
"""


def parse_term(term: str) -> tuple[int, ...]:
    """Validate the analysis period before making requests or creating files."""
    years = term_years("AL", term)
    if years[-1] > date.today().year:
        raise ValueError(f"AL term {term} includes a future year")
    return years


def _make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3, backoff_factor=1, allowed_methods=frozenset({"POST"}),
        status_forcelist=[429, 500, 502, 503, 504],
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _query(http: requests.Session, query: str, variables: dict) -> dict:
    response = http.post(API_URL, json={"query": query, "variables": variables}, timeout=60)
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(f"Alabama API error: {payload['errors']}")
    if not isinstance(payload.get("data"), dict):
        raise RuntimeError("Alabama API returned no data object")
    return payload["data"]


def _fetch_records(http: requests.Session, kind: str, year: int, verbose: bool = False) -> list[dict]:
    """Fetch every counted page, checking identity, year, and pagination."""
    fields = {"instruments": INSTRUMENT_FIELDS, "instrumentHistories": HISTORY_FIELDS}[kind]
    query = """
        query($year: Int!, $offset: Int!, $limit: Int!) {
            %s(where: {sessionYear: {eq: $year}}, order: ["id", "ASC"],
               offset: $offset, limit: $limit) { count data { %s } }
        }
    """ % (kind, fields)
    rows, seen = [], set()
    expected = None
    while expected is None or len(rows) < expected:
        page = _query(http, query, {"year": year, "offset": len(rows), "limit": PAGE_SIZE})[kind]
        count, batch = page["count"], page["data"]
        if not isinstance(count, int) or count < 0 or not isinstance(batch, list):
            raise RuntimeError(f"Invalid Alabama {kind} response for {year}")
        if expected is None:
            expected = count
        if count != expected:
            raise RuntimeError(f"Alabama {year} {kind} count changed during fetch; retry with --force-fetch")
        if not batch and len(rows) < expected:
            raise RuntimeError(f"Alabama {year} {kind}: empty page before {expected} records")
        for row in batch:
            record_id = row.get("id")
            if not record_id or record_id in seen:
                raise RuntimeError(f"Alabama {year} {kind}: missing or repeated record ID {record_id}")
            if row.get("sessionYear") != year:
                raise RuntimeError(f"Alabama API returned a record outside requested year {year}")
            seen.add(record_id)
            rows.append(row)
        if len(rows) > expected:
            raise RuntimeError(f"Alabama {year} {kind}: received more than {expected} records")
        if verbose:
            print(f"  {year} {kind}: {len(rows)}/{expected}", flush=True)
        if len(rows) < expected:
            time.sleep(0.2)
    if not rows:
        raise ValueError(f"Alabama has no {kind} for {year}; check term availability")
    return rows


def _atomic_json(path: Path, value: dict):
    pending = path.with_suffix(path.suffix + ".tmp")
    pending.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    pending.replace(path)


def _load_year(http: requests.Session, year: int, cache_dir: Path,
               force_fetch: bool, verbose: bool) -> dict:
    path = cache_dir / f"{year}.json"
    # Older runs used two-year directories. Reuse annual snapshots while
    # preserving the partial outputs and their original cache files.
    pair_start = year if year % 2 else year - 1
    legacy = cache_dir.parent / f"{pair_start}_{pair_start + 1}" / path.name
    if not force_fetch:
        for candidate in (path, legacy):
            if not candidate.exists():
                continue
            cached = json.loads(candidate.read_text(encoding="utf-8"))
            if cached.get("version") == CACHE_VERSION and cached.get("year") == year:
                print(f"  {year}: using cached snapshot ({cached['fetched_at']})", flush=True)
                return cached
    result = {
        "version": CACHE_VERSION, "year": year,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "instruments": _fetch_records(http, "instruments", year, verbose),
        "histories": _fetch_records(http, "instrumentHistories", year, verbose),
    }
    # Only cache a year after both datasets have arrived completely.
    _atomic_json(path, result)
    return result


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _key(row: dict) -> tuple[str, str]:
    session, bill = row.get("sessionAbbreviation"), row.get("instrumentNbr")
    if not session or not bill:
        raise ValueError("Alabama record is missing its session or bill number")
    return session, bill


def parse_records(instruments: list[dict], histories: list[dict], term: str) -> tuple[list, list]:
    """Join by session AND bill number; retain original actions and vote totals."""
    years = parse_term(term)
    by_key, excluded = {}, set()
    for bill in instruments:
        key = _key(bill)
        if key in by_key or key in excluded:
            raise ValueError(f"Duplicate Alabama instrument: {key}")
        if bill["sessionYear"] not in years:
            raise ValueError(f"Alabama instrument outside term {term}: {key}")
        # The all-instruments endpoint also includes appointment confirmations.
        # Connor's bill/resolution scrape did not include those records.
        if bill["instrumentType"] == "CF":
            excluded.add(key)
            continue
        if bill["instrumentType"] not in ("B", "R"):
            raise ValueError(f"Unknown Alabama instrument type: {bill['instrumentType']}")
        if not _text(bill.get("shortTitle")):
            if bill["instrumentType"] != "R":
                raise ValueError(
                    f"Alabama non-resolution instrument has no title: {key}"
                )
        by_key[key] = bill
    grouped = defaultdict(list)
    history_ids = set()
    for action in histories:
        key = _key(action)
        if key in excluded:
            continue
        if key not in by_key:
            if re.fullmatch(r"BIR\d+", key[1]):
                continue
            raise ValueError(
                f"Alabama history has no matching instrument: {key}"
            )
        if action["sessionYear"] != by_key[key]["sessionYear"]:
            raise ValueError(f"Alabama history has wrong session year: {key}")
        if action["id"] in history_ids:
            raise ValueError(f"Duplicate Alabama history ID: {action['id']}")
        history_ids.add(action["id"])
        date.fromisoformat(action["calendarDate"])
        if not _text(action.get("matter")):
            if (
                term != "2023_2026"
                or action["sessionAbbreviation"] != "2024RS"
            ):
                raise ValueError(
                    f"Alabama history has no action text: {key}"
                )
        grouped[key].append(action)
    details, actions = [], []
    for key, bill in sorted(by_key.items()):
        history = sorted(grouped[key], key=lambda row: (row["calendarDate"], int(row["id"])))
        if not history and bill.get("firstReadDate"):
            raise ValueError(f"Alabama introduced instrument has no history: {key}")
        row = [
            bill["instrumentNbr"], bill.get("sponsor"), bill["sessionName"],
            bill.get("subject"), bill.get("body"), bill.get("currentStatus"),
            bill.get("assignedCommittee"), history[-1]["calendarDate"] if history else "",
            BILL_URL, (
                _text(bill.get("shortTitle"))
                or _text(bill.get("subject"))
            ), term, bill["sessionYear"], key[0], bill["id"],
            bill["instrumentType"], bill.get("allCommittees"), bill.get("prefiledDate"),
            bill.get("firstReadDate"), bill.get("actSummary"), BILL_URL,
        ]
        details.append([_text(value) for value in row])
        for order, action in enumerate(history, 1):
            row = [
                bill["instrumentNbr"], bill.get("sponsor"), bill["sessionName"],
                action["calendarDate"], action.get("body"), action.get("amdSub"),
                action["matter"], action.get("committee"), action.get("nays"),
                action.get("yeas"), "", action.get("rollCallNbr"), term,
                bill["sessionYear"], key[0], bill["id"], action["id"], order,
                action.get("amdSubFileUrl"), action.get("voteType"),
            ]
            actions.append([_text(value) for value in row])
    return details, actions

def audit_source_exceptions(instruments, histories, term):
    """Audit known source irregularities without modifying source records."""
    indexed = {_key(row): row for row in instruments}

    title_fallbacks = []
    missing_titles = []
    unmatched_bir = []
    missing_actions = []

    for bill in instruments:
        if bill["instrumentType"] == "CF":
            continue

        if not _text(bill.get("shortTitle")):
            entry = {
                "session": bill["sessionAbbreviation"],
                "bill": bill["instrumentNbr"],
                "id": bill["id"],
            }
            if _text(bill.get("subject")):
                title_fallbacks.append(entry)
            else:
                missing_titles.append(entry)

    for action in histories:
        key = _key(action)
        bill = indexed.get(key)

        if bill is None:
            if not re.fullmatch(r"BIR\d+", key[1]):
                raise ValueError(
                    f"Unexpected unmatched Alabama history: {key}"
                )
            unmatched_bir.append({
                "session": key[0],
                "bill": key[1],
                "history_id": action["id"],
            })

        elif bill["instrumentType"] != "CF":
            if not _text(action.get("matter")):
                missing_actions.append({
                    "session": key[0],
                    "bill": key[1],
                    "history_id": action["id"],
                    "date": action["calendarDate"],
                })

    if term == "2023_2026":
        expected = {
            "title_fallbacks": 11,
            "missing_titles": 3,
            "unmatched_bir": 809,
            "missing_actions": 67,
        }

        actual = {
            "title_fallbacks": len(title_fallbacks),
            "missing_titles": len(missing_titles),
            "unmatched_bir": len(unmatched_bir),
            "missing_actions": len(missing_actions),
        }

        if actual != expected:
            raise ValueError(
                f"Alabama source exception counts changed: {actual}"
            )

        if any(
            entry["session"] != "2024RS"
            for entry in missing_actions
        ):
            raise ValueError(
                "Alabama missing action outside verified session"
            )

    return {
        "source_title_fallbacks": title_fallbacks,
        "source_missing_titles": missing_titles,
        "source_unmatched_bir_histories": unmatched_bir,
        "source_missing_action_text": missing_actions,
    }

@scrape_run
def scrape(state: str, term: str, verbose: bool = False, force_fetch: bool = False):
    """Write one complete details/history pair for the requested term.

    Year snapshots allow interrupted runs to resume. --force-fetch refreshes
    cached snapshots and existing outputs. Final CSVs are staged only after
    all four years and validation succeed; a completion manifest is last.
    """
    if state.upper() != "AL":
        raise ValueError("Alabama scraper requires state AL")
    years = parse_term(term)
    bill_dir = REPO_ROOT / ".data" / "AL" / "bill"
    detail_path = bill_dir / f"AL_Bill_Details_{term}.csv"
    history_path = bill_dir / f"AL_Bill_Histories_{term}.csv"
    manifest_path = bill_dir / f".AL_scrape_{term}.json"
    if all(p.exists() for p in (detail_path, history_path, manifest_path)) and not force_fetch:
        print(f"Skipping AL {term}: completed outputs exist (use --force-fetch to refresh)")
        return
    cache_dir = bill_dir / ".cache" / term
    cache_dir.mkdir(parents=True, exist_ok=True)
    with _make_session() as http:
        snapshots = [_load_year(http, year, cache_dir, force_fetch, verbose) for year in years]
    instruments = [row for snap in snapshots for row in snap["instruments"]]
    histories = [row for snap in snapshots for row in snap["histories"]]
    details, actions = parse_records(instruments, histories, term)
    source_exceptions = audit_source_exceptions(
        instruments, histories, term
    )
    if not details or not actions:
        raise RuntimeError("Refusing to publish empty Alabama output")
    staged = []
    try:
        for path, header, rows in ((detail_path, DETAILS_HEADER, details), (history_path, HISTORY_HEADER, actions)):
            pending = path.with_suffix(".csv.tmp")
            staged.append((pending, path))
            with pending.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(header)
                writer.writerows(rows)
        # A missing manifest marks an interrupted publication, which is rebuilt.
        manifest_path.unlink(missing_ok=True)
        for pending, path in staged:
            pending.replace(path)
        write_manifest(manifest_path, {
            "term": term, "details": len(details), "histories": len(actions),
            "excluded_confirmations": sum(row["instrumentType"] == "CF" for row in instruments),
            "sessions": sorted({row["sessionAbbreviation"] for row in instruments}),
            "source_snapshots": {str(s["year"]): s["fetched_at"] for s in snapshots},
            **source_exceptions,
        })
    finally:
        for pending, _ in staged:
            pending.unlink(missing_ok=True)
    print(f"AL {term}: saved {len(details):,} instruments and {len(actions):,} history rows")
    print(f"  {detail_path}\n  {history_path}")
