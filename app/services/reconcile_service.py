"""
Warehouse reconciliation: compare "my list" (Google Sheet export) against the
prep-center warehouse export.

Primary key:   (tracking_number, ASIN)  with quantity comparison.
Secondary key: ASIN only — for prep rows that have no tracking number
               (e.g. Origin ref = "return") and my rows without a tracking.

Both files are read directly from the uploaded bytes; nothing touches the DB.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

# Origin-ref values that are NOT tracking numbers → go to the ASIN-only bucket.
_NON_TRACKING = {"return", "returns", "повернення", "damaged", "lost", "n/a", "-", "—"}


# ── File loading ──────────────────────────────────────────────────────────────

def _load_rows(data: bytes, filename: str) -> list[list]:
    """Read an .xlsx or .csv upload into a list of rows (list of cell values)."""
    name = (filename or "").lower()
    if name.endswith(".csv"):
        text = data.decode("utf-8-sig", errors="replace")
        return [list(r) for r in csv.reader(io.StringIO(text))]

    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.active
    return [list(r) for r in ws.iter_rows(values_only=True)]


def _find_col(header: list, *candidate_groups: list[str]) -> Optional[int]:
    """
    Return the index of the first header cell matching a candidate, honouring
    group priority: all candidates in the first group are tried before the next.
    Matching is case-insensitive substring.
    """
    cells = [(str(c).strip().lower() if c is not None else "") for c in header]
    for group in candidate_groups:
        for cand in group:
            for i, cell in enumerate(cells):
                if cand in cell:
                    return i
    return None


def _as_int(val, default: int = 1) -> int:
    if val is None or val == "":
        return default
    try:
        return int(float(str(val).strip()))
    except (ValueError, TypeError):
        return default


def _norm_track(val) -> Optional[str]:
    """Normalise a tracking cell → uppercased string, or None if not a real track."""
    if val is None:
        return None
    s = str(val).strip().upper()
    if not s or s.lower() in _NON_TRACKING:
        return None
    return s


def _norm_asin(val) -> str:
    return str(val).strip().upper() if val is not None else ""


# ── Parsing each file into normalised entries ─────────────────────────────────

@dataclass
class _Parsed:
    tracked: dict = field(default_factory=lambda: defaultdict(int))    # (track, asin) -> qty
    untracked: dict = field(default_factory=lambda: defaultdict(int))  # asin -> qty
    titles: dict = field(default_factory=dict)                         # asin -> title
    row_count: int = 0
    header_found: dict = field(default_factory=dict)                   # label -> bool


def _parse(data: bytes, filename: str, is_prep: bool) -> _Parsed:
    rows = _load_rows(data, filename)
    result = _Parsed()
    if not rows:
        return result

    header = rows[0]
    if is_prep:
        track_i = _find_col(header, ["origin ref"], ["tracking", "track"])
        asin_i = _find_col(header, ["actual asin"], ["display asin"], ["sku asin"], ["asin"])
        qty_i = _find_col(header, ["qty", "quantity"])
        title_i = _find_col(header, ["title"], ["display"])
    else:
        track_i = _find_col(header, ["track", "трек", "origin ref"])
        asin_i = _find_col(header, ["asin", "асін"])
        qty_i = _find_col(header, ["qty", "quantity", "кільк", "count", "к-сть"])
        title_i = _find_col(header, ["title", "назва", "product"])

    result.header_found = {
        "tracking": track_i is not None,
        "asin": asin_i is not None,
        "qty": qty_i is not None,
    }

    for row in rows[1:]:
        if not any(c not in (None, "") for c in row):
            continue  # blank line

        def cell(idx):
            return row[idx] if (idx is not None and idx < len(row)) else None

        asin = _norm_asin(cell(asin_i))
        track = _norm_track(cell(track_i))
        qty = _as_int(cell(qty_i), default=1)
        if qty <= 0:
            qty = 1

        if not asin and not track:
            continue  # nothing usable
        result.row_count += 1

        if title_i is not None:
            t = cell(title_i)
            if asin and t and asin not in result.titles:
                result.titles[asin] = str(t).strip()

        if track:
            result.tracked[(track, asin)] += qty
        else:
            result.untracked[asin] += qty

    return result


# ── Reconciliation ────────────────────────────────────────────────────────────

def _status(mine: int, prep: int) -> str:
    if mine > 0 and prep > 0:
        return "match" if mine == prep else "qty_mismatch"
    if mine > 0 and prep == 0:
        return "missing_in_prep"   # I have it, warehouse doesn't
    return "missing_in_mine"       # warehouse has it, I don't


def reconcile(my_bytes: bytes, my_name: str,
              prep_bytes: bytes, prep_name: str) -> dict:
    """
    Compare the two uploads. Returns a dict ready for the template:
      {
        "tracked":   [ {tracking, asin, title, mine, prep, status}, ... ],
        "untracked": [ {asin, title, mine, prep, status}, ... ],
        "summary":   { match, qty_mismatch, missing_in_prep, missing_in_mine, ... },
        "meta":      { my_rows, prep_rows, headers... },
      }
    """
    mine = _parse(my_bytes, my_name, is_prep=False)
    prep = _parse(prep_bytes, prep_name, is_prep=True)

    titles = {**mine.titles, **prep.titles}

    # Primary: tracking + ASIN
    tracked_rows = []
    for key in set(mine.tracked) | set(prep.tracked):
        track, asin = key
        m, p = mine.tracked.get(key, 0), prep.tracked.get(key, 0)
        tracked_rows.append({
            "tracking": track,
            "asin": asin,
            "title": titles.get(asin, ""),
            "mine": m,
            "prep": p,
            "status": _status(m, p),
        })

    # Secondary: ASIN-only (no tracking on either side)
    untracked_rows = []
    for asin in set(mine.untracked) | set(prep.untracked):
        m, p = mine.untracked.get(asin, 0), prep.untracked.get(asin, 0)
        untracked_rows.append({
            "asin": asin,
            "title": titles.get(asin, ""),
            "mine": m,
            "prep": p,
            "status": _status(m, p),
        })

    # Sort: problems first, then by tracking/asin
    order = {"missing_in_prep": 0, "missing_in_mine": 1, "qty_mismatch": 2, "match": 3}
    tracked_rows.sort(key=lambda r: (order[r["status"]], r["tracking"], r["asin"]))
    untracked_rows.sort(key=lambda r: (order[r["status"]], r["asin"]))

    summary = defaultdict(int)
    for r in tracked_rows + untracked_rows:
        summary[r["status"]] += 1
    summary["total"] = len(tracked_rows) + len(untracked_rows)
    summary["ok"] = summary["match"]
    summary["problems"] = summary["total"] - summary["match"]

    return {
        "tracked": tracked_rows,
        "untracked": untracked_rows,
        "summary": dict(summary),
        "meta": {
            "my_rows": mine.row_count,
            "prep_rows": prep.row_count,
            "my_headers": mine.header_found,
            "prep_headers": prep.header_found,
        },
    }


# ── Template file for the user's "my list" upload ─────────────────────────────

def build_template_xlsx() -> bytes:
    """A minimal xlsx the user fills in with their expected-in-warehouse items."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "My list"

    headers = ["tracking", "asin", "qty"]
    fill = PatternFill("solid", fgColor="4F46E5")
    for i, h in enumerate(headers, start=1):
        c = ws.cell(row=1, column=i, value=h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = fill

    examples = [
        ["1Z14V49E0337961675", "B0BYFJD8XX", 1],
        ["1Z7R65990303240359", "B09J99GVXX", 4],
        ["", "B00V525TXX", 1],  # no tracking (e.g. a return) — matched by ASIN only
    ]
    for r in examples:
        ws.append(r)

    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 14
    ws.column_dimensions["C"].width = 8

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()
