"""Shared successful-scrape manifests and latest-attempt reporting.

State parsers own source-specific checks. This module gives their findings a
common shape and keeps a failed attempt separate from the last successful run.
"""
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import inspect
import json
from pathlib import Path
import tempfile
import uuid

_RUN = ContextVar("scrape_run", default=None)

# State fields preserved verbatim below; this list identifies their exceptions.
EXCEPTION_FIELDS = frozenset({
    "not_introduced", "unintroduced_drafts", "excluded_confirmations",
    "excluded_appointments", "excluded_administrative_carryovers",
    "empty_source_placeholders", "missing_name_lookups",
    "index_link_discrepancies", "unavailable_source_fields",
    "excluded_appendices", "excluded_substitute_versions_and_articles",
    "source_blank_actions", "source_empty_histories", "source_empty_sponsors",
    "source_invalid_placeholders", "source_missing_measures",
    "source_missing_primary_authors", "source_missing_text", "source_not_found",
    "source_without_primary_sponsor", "source_xml_fallbacks","source_title_mismatches"
})


def _now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def normalize_exceptions(payload):
    """Preserve state evidence without guessing severity or whether it is a bug."""
    result = []
    for code, records in payload.items():
        if code not in EXCEPTION_FIELDS:
            continue
        if isinstance(records, (list, dict)):
            count = len(records)
        elif isinstance(records, int):
            count = records
        else:
            count = int(bool(records))
        if count:
            result.append({"code": code, "count": count, "records": records})
    return result


def write_manifest(path, payload):
    """Publish common metadata plus the state's original fields after CSV writes."""
    path = Path(path)
    run = _RUN.get()
    state = run["state"] if run else path.parent.parent.name
    term = payload["term"]
    suffixes = term.split("_") if state == "CO" else [term]
    outputs = []
    for suffix in suffixes:
        for kind in ("Bill_Details", "Bill_Histories"):
            output = path.parent / f"{state}_{kind}_{suffix}.csv"
            outputs.append({"file": output.name, "bytes": output.stat().st_size})
    manifest = dict(payload)
    manifest.update({
        "schema_version": 1, "state": state, "term": term,
        "run_id": run["run_id"] if run else uuid.uuid4().hex,
        "status": "succeeded", "started_at": run["started_at"] if run else None,
        "completed_at": _now(), "outputs": outputs,
        "exceptions": normalize_exceptions(payload),
    })
    atomic_json(path, manifest)
    if run is not None:
        run["published"] = True


def scrape_run(function):
    """Record normal completion, skips, exceptions, and keyboard interrupts.

    A hard process kill leaves status=running; it is never called a success.
    Decorating the state entry point also covers callers outside the CLI.
    """
    signature = inspect.signature(function)

    @wraps(function)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        state, term = bound.arguments["state"].upper(), bound.arguments["term"]
        # Invalid path components must never create files outside the bill dir.
        from utils.validate_term import term_years
        term_years(state, term)
        root = Path(function.__globals__["__file__"]).resolve().parents[2]
        folder = root / ".data" / state / "bill"
        attempt = folder / f".{state}_scrape_{term}.attempt.json"
        run = {"schema_version": 1, "state": state, "term": term,
               "run_id": uuid.uuid4().hex, "started_at": _now(),
               "status": "running", "force_fetch": bound.arguments.get("force_fetch", False)}
        atomic_json(attempt, run)
        token = _RUN.set(run)
        try:
            result = function(*args, **kwargs)
        except BaseException as error:
            run.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                       completed_at=_now(), error={"type": type(error).__name__, "message": str(error)})
            try:
                atomic_json(attempt, {k: v for k, v in run.items() if k != "published"})
            except OSError as reporting_error:
                # Never mask the scrape error with a secondary reporting failure.
                error.add_note(f"Could not update scrape attempt report: {reporting_error}")
            raise
        else:
            run.update(status="succeeded" if run.get("published") else "skipped", completed_at=_now())
            atomic_json(attempt, {k: v for k, v in run.items() if k != "published"})
            return result
        finally:
            _RUN.reset(token)

    return wrapped
