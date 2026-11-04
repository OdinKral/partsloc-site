#!/usr/bin/env python3
"""PartsLoc helper: a small, LOCAL, READ-ONLY, stdio MCP server over a PartsLoc backup file.

An AI assistant (Claude Desktop or any MCP client) starts this program when it needs it and it
exits when the assistant does. There is no daemon, no port, no account and NO NETWORK ACCESS at
all: the only things it ever touches are the backup file you copied from your phone and, when you
ask for labels or an import file, a NEW file it writes next to it. Never overwrites, never edits
the backup. (Why so strict: the owner pays per gigabyte and wants every tool offline-first. A
helper that phoned home, or sat running all day "just because we built it", is exactly the trap.
There is not one `socket`, `urllib` or `http` import in this file; test_no_network_imports keeps
it that way.)

THE FILE IT READS (all of this was read from the app source, store build, 2026-11-04):

1. FULL BACKUP, JSON  (Settings > Data > "Full backup (JSON)", file `partsloc-backup-YYYY-MM-DD.json`)
   Written by InventoryRepo.exportBackupJson in lib/services/inventory_repo.dart, shared from
   lib/screens/settings_screen.dart `_backup`. It is a single UTF-8 JSON object (indent 2):
       {"format": 2, "taken_at": ISO-8601, "source": "partsloc-phone",
        "counts": {table: rowcount, ...},
        "items": [...], "audit": [...], "locations": [...], "stock": [...],
        "count_event": [...], "part_info": [...], "barcode_link": [...], ...more tables}
   Each table is a list of raw sqlite row dicts (LocalDb.dumpTable: "what is ON DISK"), so keys are
   column names. The ones used here, with the file that defines the columns:
     items         id name sku category trade description count ideal_stock low_threshold unit_cost
                   supplier supplier_url default_location photo_url manufacturer lifecycle_status
                   last_updated                                   (lib/models/item.dart toRow)
     locations     id building room kind name parent_id directions plan_image_url
                                                                  (lib/models/location.dart toRow)
     stock         id item_id location_id count                   (lib/models/stock.dart StockEntry)
     count_event   id item_id location_id number expected counted_at counted_by kind
                                                                  (lib/models/count_event.dart)
     audit         item_id timestamp action prev_count new_count delta notes user location_id
                                                                  (LocalDb.itemAuditSince)
     part_info     item_id mfr_number supplier_number lead_time_days critical
                                                                  (lib/models/part_info.dart)
     barcode_link  code item_id created_at                        (LocalDb v9 table)
   "Readers must treat every key as optional" (the app's own comment on backupTables): an older
   phone's file may lack a table, and a format-1 file has only items/audit/compatibility. So every
   table here defaults to empty and a missing one degrades the answer (no place names, no count
   dates) rather than failing. The two outbox tables and the photo image files are NOT in the file.

2. INVENTORY CSV  (Settings > Data > "Export inventory CSV", file `inventory_export.csv`)
   Written by InventoryRepo.exportCsv: header
   `id,name,sku,category,trade,description,count,ideal_stock,low_threshold,unit_cost,supplier,
   supplier_url,default_location,photo_url,manufacturer,lifecycle_status,last_updated`.
   Parts only: no shelves, no count dates, no history. Answers from it are always "Probably" (or
   "No" at zero), which is exactly what the app says for a part whose number was typed in and
   never counted.

THE VERDICT is a line-for-line port of lib/services/part_answer.dart (`buildAnswer`,
`CheckInterval`, `UsageMeasure`) fed the way `InventoryRepo.loadAnswer` feeds it. The wording is
the app's wording on purpose, so a person comparing the phone to the assistant sees one answer.
Known and accepted differences: (a) it is computed as of NOW from a SNAPSHOT, so every reply says
how old the backup is: the phone may already have moved on; (b) a part's id-alias merges
(part_alias) are not followed.

LABELS follow lib/services/label_pdf.dart and label_batch.dart: US Letter, Avery 5160 grid (30 part
or bin labels a page), 4 big shelf labels a page, QR text `partsloc://part/<id>` /
`partsloc://part/<id>?bin=<locationId>` / `partsloc://shelf/<locationId>` (lib/services/label_link.dart),
error-correction level M. The PDF is written here by hand (stdlib only). The QR matrix needs the
OPTIONAL `segno` package; without it the sheet is still produced but with NO codes, and says so,
because a label the app cannot scan is only a name tag. The app scans by decoding the text, so
any valid QR of that text works; segno may pick a different mask than the app's encoder, which is
still a valid, scannable code.

IMPORT FILES reuse agents/check_import.py (the site's checker, which mirrors the app's importer).
It is imported, never copied: one set of rules.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import importlib.util
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

__version__ = "1.0.0"

# --------------------------------------------------------------------------------------------
# Loading the backup
# --------------------------------------------------------------------------------------------

DEFAULT_DIR = Path.home() / "PartsLoc"
# A backup older than this gets a plain warning in every reply. 7 days is the app's own
# CheckInterval floor (part_answer.dart floorDays): after that, even the fastest-moving critical
# part's counts would already be "stale" by the app's own rule.
STALE_BACKUP_DAYS = 7


def parse_dt(s) -> datetime | None:
    """Dart's DateTime.tryParse, as naive LOCAL time (the app compares to DateTime.now()).

    Python < 3.11 fromisoformat rejects 'Z' and odd fractional lengths, and Dart emits microseconds,
    so both are normalised first. Anything unparseable is None, which the app treats as "date
    unknown" (never guessed).
    """
    if not s or not isinstance(s, str):
        return None
    t = s.strip().replace(" ", "T", 1).replace("Z", "+00:00")
    t = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], t)
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    return d.astimezone().replace(tzinfo=None) if d.tzinfo else d


def _int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


@dataclass
class Item:
    id: str
    name: str = ""
    sku: str = ""
    category: str = ""
    count: int = 0
    low_threshold: int = 0
    ideal_stock: int = 0
    unit_cost: float = 0.0
    supplier: str = ""
    manufacturer: str = ""
    description: str = ""
    default_location: str = ""
    last_updated: str = ""


@dataclass
class Location:
    id: str
    name: str = ""
    building: str = ""
    room: str = ""
    kind: str = ""
    parent_id: str | None = None
    directions: str = ""

    @property
    def label(self) -> str:  # lib/models/location.dart `label`
        br = " — ".join(s for s in (self.building, self.room) if s)
        return br or self.name or self.id

    @property
    def short_label(self) -> str:  # `shortLabel`
        return self.name or self.label


@dataclass
class Inventory:
    source: Path | None = None
    kind: str = "json"  # "json" | "csv"
    format: int | None = None
    taken_at: str = ""
    items: dict[str, Item] = field(default_factory=dict)
    locations: dict[str, Location] = field(default_factory=dict)
    stock: list[dict] = field(default_factory=list)
    count_events: list[dict] = field(default_factory=list)
    audit: list[dict] = field(default_factory=list)
    part_info: dict[str, dict] = field(default_factory=dict)
    barcodes: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    # -- derived, built lazily by place_index() --
    _paths: dict[str, str] | None = None

    @classmethod
    def from_bytes(cls, raw: bytes, source: Path | None = None) -> "Inventory":
        text = raw.decode("utf-8-sig", errors="replace")
        if text.lstrip().startswith("{"):
            return cls._from_json(json.loads(text), source)
        return cls._from_csv(text, source)

    @classmethod
    def _from_json(cls, d: dict, source) -> "Inventory":
        inv = cls(source=source, kind="json", format=d.get("format"), taken_at=str(d.get("taken_at") or ""))
        if not isinstance(d.get("items"), list):
            raise ValueError("this JSON has no 'items' list, so it is not a PartsLoc backup")
        if inv.format != 2:
            inv.notes.append(f"backup format is {inv.format!r}; this helper was written against format 2, "
                             "so some answers may be missing details.")
        for r in d["items"]:
            if not isinstance(r, dict) or not r.get("id"):
                continue
            inv.items[r["id"]] = Item(
                id=r["id"], name=r.get("name") or "", sku=r.get("sku") or "", category=r.get("category") or "",
                count=_int(r.get("count")), low_threshold=_int(r.get("low_threshold")),
                ideal_stock=_int(r.get("ideal_stock")), unit_cost=float(r.get("unit_cost") or 0),
                supplier=r.get("supplier") or "", manufacturer=r.get("manufacturer") or "",
                description=r.get("description") or "", default_location=r.get("default_location") or "",
                last_updated=r.get("last_updated") or "")
        for r in d.get("locations") or []:
            if isinstance(r, dict) and r.get("id"):
                inv.locations[r["id"]] = Location(
                    id=r["id"], name=r.get("name") or "", building=r.get("building") or "", room=r.get("room") or "",
                    kind=r.get("kind") or "", parent_id=r.get("parent_id") or None,
                    directions=r.get("directions") or "")
        inv.stock = [r for r in d.get("stock") or [] if isinstance(r, dict)]
        inv.count_events = [r for r in d.get("count_event") or [] if isinstance(r, dict)]
        inv.audit = [r for r in d.get("audit") or [] if isinstance(r, dict)]
        for r in d.get("part_info") or []:
            if isinstance(r, dict) and r.get("item_id"):
                inv.part_info[r["item_id"]] = r
        for r in d.get("barcode_link") or []:
            if isinstance(r, dict) and r.get("item_id") and r.get("code"):
                inv.barcodes.setdefault(r["item_id"], []).append(str(r["code"]))
        if "locations" not in d:
            inv.notes.append("this backup has no places table, so shelf names are unavailable.")
        return inv

    @classmethod
    def _from_csv(cls, text: str, source) -> "Inventory":
        inv = cls(source=source, kind="csv")
        rows = list(csv.DictReader(io.StringIO(text, newline="")))
        if not rows or "name" not in (rows[0] or {}):
            raise ValueError("this CSV has no 'name' column, so it is not a PartsLoc inventory export")
        for i, r in enumerate(rows):
            iid = (r.get("id") or "").strip() or f"row-{i + 2}"
            inv.items[iid] = Item(
                id=iid, name=r.get("name") or "", sku=r.get("sku") or "", category=r.get("category") or "",
                count=_int(r.get("count")), low_threshold=_int(r.get("low_threshold")),
                ideal_stock=_int(r.get("ideal_stock")), unit_cost=_float(r.get("unit_cost")),
                supplier=r.get("supplier") or "", manufacturer=r.get("manufacturer") or "",
                description=r.get("description") or "", default_location=r.get("default_location") or "",
                last_updated=r.get("last_updated") or "")
        inv.notes.append("this is the CSV export: parts only, with no shelves, count dates or history. "
                         "Every answer is therefore 'Probably' (or 'No' at zero). The full JSON backup "
                         "gives real Yes answers.")
        return inv

    # ---- places -------------------------------------------------------------------------
    def place_paths(self) -> dict[str, str]:
        """location id -> 'Shop > Shelf B' (lib/services/place_tree.dart buildPlaceIndex).

        Same rules: a parent_id pointing at nothing makes the place top-level; a parent cycle is broken
        by promoting the alphabetically-first id in the loop to a root (so nothing vanishes and the
        result is deterministic). Names are `short_label`, joined with ' > '.
        """
        if self._paths is not None:
            return self._paths
        parent = {i: (l.parent_id if l.parent_id and l.parent_id != i and l.parent_id in self.locations else None)
                  for i, l in self.locations.items()}
        for i in self.locations:
            seen: list[str] = []
            cur = i
            while cur is not None:
                if cur in seen:
                    loop = sorted(seen[seen.index(cur):])
                    parent[loop[0]] = None
                    break
                seen.append(cur)
                cur = parent[cur]
        paths: dict[str, str] = {}

        def path(i: str) -> str:
            if i not in paths:
                p = parent[i]
                n = self.locations[i].short_label
                paths[i] = n if p is None else f"{path(p)} > {n}"
            return paths[i]

        for i in self.locations:
            path(i)
        self._paths = paths
        return paths

    def children_of(self, loc_id: str) -> list[str]:
        paths = self.place_paths()
        return [i for i in self.locations if paths[i].startswith(paths[loc_id] + " > ")]

    def stock_rows(self, item_id: str) -> list[dict]:
        return [s for s in self.stock if s.get("item_id") == item_id]

    def placed_paths(self, item_id: str) -> list[tuple[str, int]]:
        """(path, count) for every KNOWN place holding > 0 of the part, alphabetical, like
        PlaceIndex.pathsFor (a zeroed bin is not 'stored there'; a place that no longer exists is skipped)."""
        paths = self.place_paths()
        out: dict[str, int] = {}
        for s in self.stock_rows(item_id):
            lid, c = s.get("location_id"), _int(s.get("count"))
            if lid in paths and c > 0:
                out[paths[lid]] = out.get(paths[lid], 0) + c
        return sorted(out.items())


def _float(v) -> float:
    try:
        return float(re.sub(r"[^0-9.-]", "", str(v or "")) or 0)
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------------------------
# Search: lib/services/item_search.dart
# --------------------------------------------------------------------------------------------

def _tokens(q: str) -> list[str]:
    return [t for t in re.split(r"\s+", re.sub(r"[*>]", " ", q.lower())) if t]


def item_matches(inv: Inventory, item: Item, query: str) -> bool:
    """itemMatchesQuery: every word must appear (substring, any order) in name, sku, category,
    manufacturer, any place path, or the part's other numbers (linked barcodes, maker number, supplier
    number). A purely numeric word also matches with leading zeros stripped (barcodes are stored that way).
    `*` and `>` are noise. Same rules so a query that finds a part on the phone finds it here."""
    tokens = _tokens(query)
    if not tokens:
        return True
    info = inv.part_info.get(item.id, {})
    numbers = list(inv.barcodes.get(item.id, [])) + [info.get("mfr_number") or "", info.get("supplier_number") or ""]
    places = [p for p, _ in inv.placed_paths(item.id)]
    hay = " ".join([item.name, item.sku, item.category, item.manufacturer, *places, *numbers]).lower()
    # The app does not put default_location in the haystack. We do, for CSV exports where it is the only
    # place there is (no shelves exist); in a JSON backup it only adds hits, never removes any.
    if inv.kind == "csv":
        hay += " " + item.default_location.lower()
    for t in tokens:
        if t in hay:
            continue
        if t.isdigit() and t.lstrip("0") and t.lstrip("0") != t and t.lstrip("0") in hay:
            continue
        return False
    return True


def find_items(inv: Inventory, query: str) -> list[Item]:
    hits = [i for i in inv.items.values() if item_matches(inv, i, query)]
    return sorted(hits, key=lambda i: (i.name.lower(), i.id))


# --------------------------------------------------------------------------------------------
# The verdict: lib/services/part_answer.dart, ported line for line
# --------------------------------------------------------------------------------------------

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MAX_WINDOW_DAYS, MIN_WINDOW_DAYS = 90, 7
DEFAULT_DAYS, FLOOR_DAYS, LONG_LEAD_DAYS = 90, 7, 14
# Actions that are not "use" (part_answer.dart UsageMeasure._notUse).
_NOT_USE = {"set", "move", "merge", "create", "bulk_import", "edit", "undo"}
# Actions movementsSince (local_db.dart) ignores when counting "taken / put back since the last count".
# Deliberately a DIFFERENT set from _NOT_USE (it counts 'move' and 'undo'); copied as the app has it.
_NOT_MOVEMENT = {"set", "create", "bulk_import", "edit", "merge"}


def _days(delta: timedelta) -> int:
    return int(delta.total_seconds() / 86400)  # Dart Duration.inDays truncates toward zero; // would floor


def format_date(iso: str, now: datetime) -> str:
    d = parse_dt(iso)
    if d is None:
        return "date unknown"
    base = f"{_MONTHS[d.month - 1]} {d.day}"
    return base if d.year == now.year else f"{base}, {d.year}"


def _ago(n: int) -> str:
    return "today" if n == 0 else "yesterday" if n == 1 else f"{n} days ago"


def _lower(s: str) -> str:
    return s[:1].lower() + s[1:]


def _place_or_none(p: str) -> str:
    return p or "no shelf recorded"


@dataclass
class LastCount:
    number: int
    counted_at: str | None
    counted_by: str
    taken: int = 0
    put_back: int = 0

    def since_text(self) -> str | None:  # LastCountLine.sinceText
        if self.counted_at is None:
            return None
        parts = []
        if self.taken > 0:
            parts.append(f"{self.taken} taken")
        if self.put_back > 0:
            parts.append(f"{self.put_back} put back")
        return "Nothing taken or put back since" if not parts else ", ".join(parts) + " since"


@dataclass
class ShelfLine:
    location_id: str | None
    place: str
    on_hand: int
    last: LastCount | None = None


@dataclass
class Answer:
    verdict: str
    headline: str
    reasons: list[str]
    shelves: list[ShelfLine]
    interval_days: int
    total: int


def last_count_for(inv: Inventory, item_id: str, location_id: str | None) -> LastCount | None:
    """InventoryRepo.lastCountFor + LocalDb.lastCountEvent/movementsSince."""
    evs = [e for e in inv.count_events if e.get("item_id") == item_id
           and (e.get("location_id") or None) == location_id]
    if not evs:
        return None
    # ORDER BY counted_at DESC, id DESC. SQLite sorts NULL lowest, so an undated count loses to any dated one.
    e = max(evs, key=lambda x: (x.get("counted_at") or "", str(x.get("id") or "")))
    at = e.get("counted_at")
    lc = LastCount(_int(e.get("number")), at, e.get("counted_by") or "")
    if at:
        for r in inv.audit:
            if r.get("item_id") != item_id or not (str(r.get("timestamp") or "") > at):
                continue
            if r.get("action") in _NOT_MOVEMENT:
                continue
            if r.get("location_id") not in (None, "", location_id or ""):
                continue
            d = _int(r.get("delta"))
            if d < 0:
                lc.taken += -d
            elif d > 0:
                lc.put_back += d
    return lc


def measure_usage(inv: Inventory, item_id: str, now: datetime) -> tuple[int, int, int]:
    """(takes, units, window_days): UsageMeasure.measure over the last 90 days of the audit log.
    Rows are filtered by ISO-string comparison exactly as LocalDb.itemAuditSince does."""
    since = (now - timedelta(days=MAX_WINDOW_DAYS)).isoformat()
    rows = sorted((r for r in inv.audit if r.get("item_id") == item_id and str(r.get("timestamp") or "") >= since),
                  key=lambda r: (str(r.get("timestamp") or ""), _int(r.get("id"))))
    takes = units = 0
    for r in rows:
        delta, action = _int(r.get("delta")), r.get("action")
        if action == "undo" and delta > 0:
            if takes > 0:
                takes -= 1
            units -= delta
        elif delta < 0 and action not in _NOT_USE:
            takes += 1
            units += -delta
    units = max(units, 0)
    first = min((str(r.get("timestamp")) for r in inv.audit
                 if r.get("item_id") == item_id and r.get("timestamp")), default=None)
    days = MAX_WINDOW_DAYS
    fd = parse_dt(first) if first else None
    if fd is not None:
        days = min(max(_days(now - fd), MIN_WINDOW_DAYS), MAX_WINDOW_DAYS)
    return takes, units, days


def check_interval(critical: bool, takes: int, window: int, lead: int | None) -> tuple[int, list[str], bool]:
    """CheckInterval.compute: 90 days, halved for critical / used more than weekly / lead time over
    2 weeks, floor 7. Each halving is kept as a sentence so the reason can be said in words."""
    days, why = DEFAULT_DAYS, []
    if critical:
        days //= 2
        why.append("it is marked critical")
    if window and takes / (window / 7) > 1:
        days //= 2
        why.append(f"it is used more than weekly (taken {takes} {'time' if takes == 1 else 'times'} in {window} days)")
    if lead is not None and lead > LONG_LEAD_DAYS:
        days //= 2
        why.append(f"it takes {lead} days to get one (over 2 weeks)")
    floored = days < FLOOR_DAYS
    return (FLOOR_DAYS if floored else days), why, floored


def _explain(days: int, why: list[str], floored: bool) -> str:
    if not why:
        return f"Counts of this part are trusted for {days} days (the standard {DEFAULT_DAYS})."
    halved = why[0] if len(why) == 1 else ", ".join(why[:-1]) + " and " + why[-1]
    tail = f", but never less than {FLOOR_DAYS} days" if floored else ""
    times = "once" if len(why) == 1 else ("twice" if len(why) == 2 else f"{len(why)} times")
    return f"Counts of this part are trusted for {days} days ({DEFAULT_DAYS} halved {times} because {halved}){tail}."


def build_answer(shelves: list[ShelfLine], now: datetime, critical=False, lead=None,
                 takes=0, units=0, window=0) -> Answer:
    """buildAnswer. Rule: No at zero or below; Yes when at least one shelf that HOLDS stock was counted
    inside the check interval; Probably otherwise (counted too long ago, date unknown, or never counted).
    Wording is the app's; do not 'improve' it here without changing part_answer.dart too."""
    days, why, floored = check_interval(critical, takes, window, lead)
    total = sum(s.on_hand for s in shelves)
    holding = [s for s in shelves if s.on_hand > 0]

    def age(s: ShelfLine):
        at = parse_dt(s.last.counted_at) if s.last and s.last.counted_at else None
        return None if at is None else _days(now - at)

    def fresh(s):
        a = age(s)
        return a is not None and a <= days

    def who(lc: LastCount) -> str:
        return f" by {lc.counted_by}" if lc.counted_by else ""

    reasons: list[str] = []
    if total <= 0:
        verdict = "No"
        headline = ("No — none on hand" if total == 0
                    else f"No — the app shows {total}, which means more was taken than counted")
        reasons.append("Every shelf shows 0." if not shelves or all(s.on_hand == 0 for s in shelves)
                       else f"The shelves add up to {total}.")
        for s in shelves:
            l = s.last
            if l is None:
                continue
            since = l.since_text()
            at = "on an unknown date" if l.counted_at is None else f"on {format_date(l.counted_at, now)}"
            reasons.append(f"{_place_or_none(s.place)}: last counted {l.number} {at}{who(l)}"
                           f"{'' if since is None else ', ' + _lower(since)}.")
        if all(s.last is None for s in shelves):
            reasons.append("It has never been counted, so this is only what was entered.")
    else:
        any_fresh = any(fresh(s) for s in holding)
        verdict = "Yes" if any_fresh else "Probably"
        stock = ", ".join(f"{s.on_hand} on {_place_or_none(s.place)}" for s in holding if not any_fresh or fresh(s))
        headline = f"{verdict} — {stock}"
        for s in holding:
            l, place, a = s.last, _place_or_none(s.place), age(s)
            if l is None:
                reasons.append(f"{place}: never counted, so {s.on_hand} is what was entered, not what was checked.")
                continue
            since = l.since_text()
            since_txt = "" if since is None else ", " + _lower(since)
            if a is None:
                reasons.append(f"{place}: counted {l.number}, but the date is unknown, "
                               f"so it cannot be trusted as recent{since_txt}.")
            else:
                when = f"{format_date(l.counted_at, now)}{who(l)} ({_ago(a)})"
                if a <= days:
                    reasons.append(f"{place}: counted {l.number} on {when}, inside the {days}-day check window{since_txt}.")
                else:
                    reasons.append(f"{place}: counted {l.number} on {when}, past the {days}-day check window{since_txt}. "
                                   "Worth a look before you rely on it.")
    reasons.append(_explain(days, why, floored))
    more_than_weekly = window > 0 and takes / (window / 7) > 1
    if window > 0 and not more_than_weekly and takes > 0:
        reasons.append(f"Taken {takes} {'time' if takes == 1 else 'times'} in the last {window} days.")
    elif takes == 0:
        reasons.append("No takes logged yet, so use is not measured.")
    return Answer(verdict, headline, reasons, shelves, days, total)


def answer_for(inv: Inventory, item: Item, now: datetime) -> Answer:
    """InventoryRepo.loadAnswer: shelves from stock rows (place = the location's NAME, not its path,
    exactly as the app's answer screen shows it); no stock rows at all = one line from the part's own
    count and its free-text default_location."""
    info = inv.part_info.get(item.id, {})
    rows = inv.stock_rows(item.id)
    shelves: list[ShelfLine] = []
    if not rows:
        shelves.append(ShelfLine(None, item.default_location, item.count, last_count_for(inv, item.id, None)))
    else:
        for s in rows:
            loc = inv.locations.get(s.get("location_id"))
            shelves.append(ShelfLine(s.get("location_id"), loc.name if loc else "", _int(s.get("count")),
                                     last_count_for(inv, item.id, s.get("location_id"))))
        shelves.sort(key=lambda s: s.place)
    takes, units, window = measure_usage(inv, item.id, now) if inv.audit else (0, 0, 0)
    return build_answer(shelves, now, critical=bool(_int(info.get("critical"))),
                        lead=(None if info.get("lead_time_days") is None else _int(info.get("lead_time_days"))),
                        takes=takes, units=units, window=window)


# --------------------------------------------------------------------------------------------
# The tools (plain functions over an Inventory; the MCP wrappers at the bottom are one line each)
# --------------------------------------------------------------------------------------------

MAX_MATCHES = 5


def provenance(inv: Inventory, now: datetime) -> str:
    """Said under EVERY answer. A backup is a snapshot; the phone is the truth. The assistant must not
    present a week-old file as 'what is on the shelf right now' (the over-assertion this guards)."""
    src = inv.source.name if inv.source else "backup"
    if inv.kind == "csv":
        # A CSV export carries no timestamp inside it; the file's save time is the best (and honest) proxy.
        t = datetime.fromtimestamp(inv.source.stat().st_mtime) if inv.source and inv.source.exists() else None
        taken = f"file saved {t:%Y-%m-%d}" if t else "export date unknown"
    else:
        t = parse_dt(inv.taken_at)
        taken = f"taken {inv.taken_at[:16].replace('T', ' ')}" if t else "taken date unknown"
    line = f"[From {src}, {taken}. This is a copy; the phone may have changed since.]"
    if t and _days(now - t) > STALE_BACKUP_DAYS:
        line += f" WARNING: this copy is {_days(now - t)} days old. Copy a fresh backup from the phone before relying on it."
    for n in inv.notes:
        line += f"\nNote: {n}"
    return line


def _part_title(i: Item) -> str:
    bits = [b for b in (i.sku, i.manufacturer) if b]
    return i.name + (f" ({', '.join(bits)})" if bits else "")


def tool_find_part(inv: Inventory, query: str, now: datetime | None = None) -> str:
    """Yes / Probably / No for each part matching `query`, with the app's reasons."""
    now = now or datetime.now()
    hits = find_items(inv, query)
    if not hits:
        return f"No part matches {query!r}. That is not the same as 'none on hand': it may be spelled " \
               f"differently, or not in this copy.\n{provenance(inv, now)}"
    out = []
    for i in hits[:MAX_MATCHES]:
        a = answer_for(inv, i, now)
        out.append(f"{_part_title(i)}\n  {a.headline}\n" + "\n".join(f"  - {r}" for r in a.reasons))
    if len(hits) > MAX_MATCHES:
        out.append(f"{len(hits) - MAX_MATCHES} more match; narrow the query. Next: " +
                   "; ".join(_part_title(i) for i in hits[MAX_MATCHES:MAX_MATCHES + 8]))
    return "\n\n".join(out) + "\n\n" + provenance(inv, now)


def tool_where_is(inv: Inventory, part: str, now: datetime | None = None) -> str:
    """Every place that holds the part, with counts. No verdict; use find_part for trust."""
    now = now or datetime.now()
    hits = find_items(inv, part)
    if not hits:
        return f"No part matches {part!r}.\n{provenance(inv, now)}"
    out = []
    for i in hits[:MAX_MATCHES]:
        placed = inv.placed_paths(i.id)
        lines = [f"  {p}: {c}" for p, c in placed]
        if not placed:
            lines = [f"  noted as '{i.default_location}' (text only, no shelf set up), count {i.count}"
                     if i.default_location else "  no place recorded"]
        dirs = [inv.locations[s["location_id"]].directions for s in inv.stock_rows(i.id)
                if s.get("location_id") in inv.locations and inv.locations[s["location_id"]].directions
                and _int(s.get("count")) > 0]
        out.append(f"{_part_title(i)}\n" + "\n".join(lines) + "".join(f"\n  how to get there: {d}" for d in dirs))
    if len(hits) > MAX_MATCHES:
        out.append(f"{len(hits) - MAX_MATCHES} more match; narrow the query.")
    return "\n\n".join(out) + "\n\n" + provenance(inv, now)


def _match_places(inv: Inventory, q: str) -> list[str]:
    """Location ids for a place query: an exact (case-blind) name or path wins; else every place whose
    path contains all the words."""
    paths = inv.place_paths()
    ql = q.strip().lower()
    exact = [i for i, p in paths.items() if p.lower() == ql or inv.locations[i].short_label.lower() == ql
             or inv.locations[i].label.lower() == ql]
    if exact:
        return sorted(exact, key=lambda i: paths[i])
    toks = _tokens(q)
    return sorted((i for i, p in paths.items() if toks and all(t in p.lower() for t in toks)), key=lambda i: paths[i])


def tool_list_place(inv: Inventory, place: str, now: datetime | None = None) -> str:
    """What is stored in a place (and in places inside it). Counts above zero only, like the app's
    By-place browser."""
    now = now or datetime.now()
    paths = inv.place_paths()
    ids = _match_places(inv, place)
    out = []
    for lid in ids[:MAX_MATCHES]:
        inside = {lid, *inv.children_of(lid)}
        rows: dict[str, list[tuple[str, int]]] = {}
        for s in inv.stock:
            if s.get("location_id") in inside and _int(s.get("count")) > 0 and s.get("item_id") in inv.items:
                rows.setdefault(s["item_id"], []).append((paths[s["location_id"]], _int(s["count"])))
        head = f"{paths[lid]} ({len(rows)} part{'s' if len(rows) != 1 else ''})"
        body = [f"  {_part_title(inv.items[i])}: " + ", ".join(f"{c} in {p}" if len(inside) > 1 else str(c) for p, c in v)
                for i, v in sorted(rows.items(), key=lambda kv: inv.items[kv[0]].name.lower())]
        out.append(head + ("\n" + "\n".join(body) if body else "\n  nothing counted here"))
    if not out:
        # CSV exports and unplaced parts: the only "place" is the free-text default_location.
        pl = place.strip().lower()
        loose = [i for i in inv.items.values() if i.default_location and pl in i.default_location.lower()
                 and not inv.placed_paths(i.id)]
        if loose:
            out.append(f"Parts noted as '{place}' (text only, no shelf set up):\n" +
                       "\n".join(f"  {_part_title(i)}: {i.count}" for i in sorted(loose, key=lambda i: i.name.lower())))
    if not out:
        known = sorted(set(paths.values()))[:15]
        return f"No place matches {place!r}." + (f" Places in this copy: {'; '.join(known)}." if known else "") + \
               "\n" + provenance(inv, now)
    if len(ids) > MAX_MATCHES:
        out.append(f"{len(ids) - MAX_MATCHES} more places match; be more specific.")
    return "\n\n".join(out) + "\n\n" + provenance(inv, now)


def low_stock_items(inv: Inventory) -> list[tuple[str, Item]]:
    """StockStatus: OUT at count <= 0; LOW when a threshold is set (> 0) and count <= it. Threshold 0
    means 'never set one' and is never LOW. This is the app's `needsAttention` chip, uses the part's own
    count (what the badge shows), and sorts OUT first then lowest count, then name."""
    res = []
    for i in inv.items.values():
        if i.count <= 0:
            res.append(("OUT", i))
        elif i.low_threshold > 0 and i.count <= i.low_threshold:
            res.append(("LOW", i))
    return sorted(res, key=lambda t: (t[0] != "OUT", t[1].count, t[1].name.lower(), t[1].id))


def tool_low_stock(inv: Inventory, now: datetime | None = None) -> str:
    now = now or datetime.now()
    rows = low_stock_items(inv)
    if not rows:
        return "Nothing is LOW or OUT in this copy.\n" + provenance(inv, now)
    lines = []
    for state, i in rows:
        placed = inv.placed_paths(i.id)
        where = placed[0][0] if placed else (i.default_location or "no place recorded")
        want = f", ideal {i.ideal_stock} (short by {i.ideal_stock - i.count})" if i.ideal_stock > i.count else ""
        sup = f", buy from {i.supplier}" if i.supplier else ""
        reorder = f", reorder at {i.low_threshold}" if i.low_threshold > 0 else ""
        lines.append(f"{state}: {_part_title(i)}, {i.count} on hand{reorder}{want}{sup} ({where})")
    return f"{len(rows)} part(s) LOW or OUT:\n" + "\n".join(lines) + "\n\n" + provenance(inv, now)


# ---- labels ---------------------------------------------------------------------------------

def label_uri(kind: str, item_id: str = "", loc_id: str = "") -> str:
    """lib/services/label_link.dart. Dart's Uri percent-encodes path segments and query values; so do we."""
    from urllib.parse import quote  # pure string work; no network in urllib.parse
    if kind == "shelf":
        return f"partsloc://shelf/{quote(loc_id, safe='')}"
    base = f"partsloc://part/{quote(item_id, safe='')}"
    return base + (f"?bin={quote(loc_id, safe='')}" if kind == "bin" else "")


@dataclass
class LabelSpec:
    kind: str  # part | bin | shelf
    payload: str
    title: str
    number: str = ""
    place: str = ""

    @property
    def big(self) -> bool:
        return self.kind == "shelf"


def label_specs(inv: Inventory, parts: list[str] | None, place: str | None) -> tuple[list[LabelSpec], list[str]]:
    """label_batch.dart: forPart (a part label names every shelf holding it, else its default_location);
    forShelf (ONE shelf header then a BIN label per part with a stock row there, sorted by name)."""
    specs, problems = [], []
    for q in parts or []:
        hits = find_items(inv, q)
        if not hits:
            problems.append(f"no part matches {q!r}")
            continue
        if len(hits) > 1:
            problems.append(f"{q!r} matches {len(hits)} parts; labelled the first ({_part_title(hits[0])}). "
                            "Use a more specific name to pick another.")
        i = hits[0]
        names = [inv.locations[s["location_id"]].label for s in inv.stock_rows(i.id) if s.get("location_id") in inv.locations]
        specs.append(LabelSpec("part", label_uri("part", i.id), i.name, i.sku, ", ".join(names) or i.default_location))
    if place:
        ids = _match_places(inv, place)
        if not ids:
            problems.append(f"no place matches {place!r}")
        if len(ids) > 1:
            problems.append(f"{place!r} matches {len(ids)} places; labelled the first ({inv.place_paths()[ids[0]]}).")
        for lid in ids[:1]:
            loc = inv.locations[lid]
            specs.append(LabelSpec("shelf", label_uri("shelf", loc_id=lid), loc.label))
            held = [inv.items[s["item_id"]] for s in inv.stock if s.get("location_id") == lid and s.get("item_id") in inv.items]
            for i in sorted({i.id: i for i in held}.values(), key=lambda i: i.name.lower()):
                specs.append(LabelSpec("bin", label_uri("bin", i.id, lid), i.name, i.sku, loc.label))
    return specs, problems


# Geometry in points (1/72 in), copied from label_pdf.dart. If the app's numbers change, change these.
PT = 72.0
PAGE_W, PAGE_H = 8.5 * PT, 11 * PT
SMALL_COLS, SMALL_ROWS = 3, 10
SMALL_PER_SHEET = SMALL_COLS * SMALL_ROWS
SMALL_W, SMALL_H = 2.625 * PT, 1.0 * PT
SMALL_LEFT, SMALL_TOP = 0.1875 * PT, 0.5 * PT
SMALL_PITCH_X, SMALL_PITCH_Y = 2.75 * PT, 1.0 * PT
SMALL_QR = 0.75 * PT
BIG_COLS, BIG_ROWS = 2, 2
BIG_PER_SHEET = BIG_COLS * BIG_ROWS
BIG_LEFT, BIG_TOP = 0.5 * PT, 0.5 * PT
BIG_W, BIG_H, BIG_QR = 3.75 * PT, 5.0 * PT, 3.0 * PT
QUIET = 2  # modules of quiet zone drawn around each code (kLabelQuiet)
INSET = 0.08 * PT


def layout(specs: list[LabelSpec]) -> list[tuple[int, float, float, float, float, float, LabelSpec]]:
    """LabelSheet.layout: shelf labels first (4 a page), then small ones on a fresh page, 30 a page,
    left to right then top to bottom. Returns (page, x, y_from_top, w, h, qr_side, spec)."""
    big = [s for s in specs if s.big]
    small = [s for s in specs if not s.big]
    cells, page = [], 0
    for n, s in enumerate(big):
        if n > 0 and n % BIG_PER_SHEET == 0:
            page += 1
        slot = n % BIG_PER_SHEET
        cells.append((page, BIG_LEFT + (slot % BIG_COLS) * BIG_W, BIG_TOP + (slot // BIG_COLS) * BIG_H, BIG_W, BIG_H, BIG_QR, s))
    if big and small:
        page += 1
    for n, s in enumerate(small):
        p, slot = page + n // SMALL_PER_SHEET, n % SMALL_PER_SHEET
        cells.append((p, SMALL_LEFT + (slot % SMALL_COLS) * SMALL_PITCH_X, SMALL_TOP + (slot // SMALL_COLS) * SMALL_PITCH_Y,
                      SMALL_W, SMALL_H, SMALL_QR, s))
    return cells


def _qr_matrix(text: str):
    """Rows of booleans, or None when the optional `segno` package is not installed. Error level M like
    the app (about 15% damage survivable; a scuffed shelf-edge label is the use case)."""
    try:
        import segno
    except ImportError:
        return None
    q = segno.make(text, error="m", micro=False, boost_error=False)
    return [[bool(c) for c in row] for row in q.matrix]


def _pdf_text(s: str, max_len: int) -> str:
    """label_pdf.dart `_fit`: the 14 built-in PDF fonts are Latin-1 only, so anything else becomes '?'
    (visibly imperfect beats silently missing), then truncate with '...'. Escaped for a PDF string."""
    clean = "".join(c if 32 <= ord(c) <= 255 else "?" for c in s)
    if len(clean) > max_len:
        clean = clean[:max_len - 1] + "..."
    return clean.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_label_pdf(specs: list[LabelSpec], cut_lines: bool = True) -> tuple[bytes, bool]:
    """A hand-written PDF (stdlib only). Returns (bytes, qr_included). One filled rectangle per run of
    dark modules, like the app, which avoids hairline seams between adjacent squares in some viewers."""
    cells = layout(specs)
    pages = (max(c[0] for c in cells) + 1) if cells else 1
    have_qr = True
    streams = []
    for p in range(pages):
        ops = []
        for (pg, x, ytop, w, h, qr, spec) in cells:
            if pg != p:
                continue
            Y = lambda yt, hh=0: PAGE_H - yt - hh  # PDF origin is bottom-left; the layout is top-left
            if cut_lines:
                ops.append(f"0.5 G 0.4 w {x:.2f} {Y(ytop, h):.2f} {w:.2f} {h:.2f} re S")
            m = _qr_matrix(spec.payload)
            qx, qy = (x + (w - qr) / 2, ytop + 0.45 * PT) if spec.big else (x + INSET, ytop + (h - qr) / 2)
            if m is None:
                have_qr = False
            else:
                unit = qr / (len(m) + 2 * QUIET)
                ops.append("0 g")
                for r, row in enumerate(m):
                    c = 0
                    while c < len(row):
                        if row[c]:
                            n = 1
                            while c + n < len(row) and row[c + n]:
                                n += 1
                            ops.append(f"{qx + (c + QUIET) * unit:.3f} {Y(qy + (r + QUIET + 1) * unit):.3f} {n * unit:.3f} {unit:.3f} re f")
                            c += n
                        else:
                            c += 1
            ops.append("0 g")
            if spec.big:
                ty = qy + qr + 0.15 * PT
                for txt, font, size, off in (("SHELF", "F1", 12, 12), (spec.title, "F2", 30, 42)):
                    t = _pdf_text(txt, 28)
                    width = len(t) * size * 0.55
                    ops.append(f"BT /{font} {size} Tf {x + (w - min(width, w - 2 * INSET)) / 2:.2f} {Y(ty + off):.2f} Td ({t}) Tj ET")
            else:
                tx = x + INSET + qr + INSET
                ops.append(f"BT /F2 9 Tf {tx:.2f} {Y(ytop + INSET + 9):.2f} Td ({_pdf_text(spec.title, 40)}) Tj ET")
                if spec.number:
                    ops.append(f"BT /F3 8 Tf {tx:.2f} {Y(ytop + INSET + 20):.2f} Td ({_pdf_text(spec.number, 24)}) Tj ET")
                if spec.place:
                    isbin = spec.kind == "bin"
                    ops.append(f"BT /{'F2 10' if isbin else 'F1 8'} Tf {tx:.2f} {Y(ytop + h - INSET - 8):.2f} Td "
                               f"({_pdf_text(('BIN ' + spec.place) if isbin else spec.place, 22)}) Tj ET")
        streams.append("\n".join(ops).encode("latin-1"))
    objs: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{6 + 2 * i} 0 R" for i in range(pages))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    for name in ("Helvetica", "Helvetica-Bold", "Courier"):
        objs.append(f"<< /Type /Font /Subtype /Type1 /BaseFont /{name} /Encoding /WinAnsiEncoding >>".encode())
    for i, s in enumerate(streams):
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W:.0f} {PAGE_H:.0f}] "
                    f"/Resources << /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >> >> /Contents {7 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(s) + s + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offs = []
    for n, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{n} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for o in offs:
        out += f"{o:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out), have_qr and bool(cells)


def safe_new_path(path: str | None, default_dir: Path, stem: str, suffix: str) -> Path:
    """The ONLY write this program makes: a brand-new file. Never overwrites (refuse instead), and a
    relative or missing path lands in the backup's own folder, not wherever the assistant happened to start."""
    if path:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = default_dir / p
    else:
        p = default_dir / f"{stem}-{datetime.now():%Y%m%d-%H%M%S}{suffix}"
    if p.suffix.lower() != suffix:
        p = p.with_name(p.name + suffix)
    if p.exists():
        raise FileExistsError(f"{p} already exists; this helper never overwrites. Pick another name.")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def tool_labels_for(inv: Inventory, parts: list[str] | None = None, place: str | None = None,
                    output_path: str | None = None, out_dir: Path | None = None) -> str:
    if not parts and not place:
        return "Say which labels: a place (shelf label plus a bin label for each part on it) and/or a list of parts."
    specs, problems = label_specs(inv, parts, place)
    if not specs:
        return "No labels made. " + "; ".join(problems)
    pdf, have_qr = build_label_pdf(specs)
    dest = safe_new_path(output_path, out_dir or (inv.source.parent if inv.source else Path.cwd()), "partsloc-labels", ".pdf")
    dest.write_bytes(pdf)
    small = sum(1 for s in specs if not s.big)
    big = len(specs) - small
    msg = (f"Wrote {dest}: {small} part/bin label(s) and {big} shelf label(s), US Letter, Avery 5160 layout "
           f"(30 small labels a page; shelf labels 4 a page, printed first). Print at 100% / actual size; "
           f"cut lines are drawn for plain paper.")
    if not have_qr:
        msg += ("\nWARNING: NO QR CODES in this PDF, because the optional 'segno' package is not installed "
                "(pip install segno). The sheet shows names only and the app cannot scan it. Install segno and ask again.")
    if inv.kind == "csv" and place:
        msg += "\nNote: a CSV export has no shelves, so shelf and bin labels need the full JSON backup."
    if problems:
        msg += "\nNotes: " + "; ".join(problems)
    return msg


# ---- import files ---------------------------------------------------------------------------

def load_check_import():
    """Import the site's agents/check_import.py WITHOUT copying its rules. Looked up, in order: the
    PARTSLOC_CHECK_IMPORT env var, next to this file (the gifted bundle ships a copy placed there by
    build-pages.py), then the crib repo's own store/pages-site/agents/. If it cannot be found we refuse
    to write an import file rather than write one nobody checked."""
    here = Path(__file__).resolve().parent
    cands = [os.environ.get("PARTSLOC_CHECK_IMPORT"), here / "check_import.py",
             here.parent / "pages-site" / "agents" / "check_import.py"]
    for c in cands:
        if c and Path(c).is_file():
            spec = importlib.util.spec_from_file_location("check_import", c)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise FileNotFoundError("check_import.py not found next to this helper; re-download the helper bundle.")


def rows_to_csv(rows: list[dict], ci) -> bytes:
    """Headers are normalised to the importer's canonical names via check_import's own ALIASES, so
    'Part #' and 'Qty' work. Columns keep first-seen order. Every cell is written as text, quoted where
    needed by csv.writer; numbers are left to the checker to judge (the app drops non-digits)."""
    cols: list[str] = []
    canon_of: dict[str, str] = {}
    for r in rows:
        for k in r:
            if k not in canon_of:
                n = ci.norm(str(k))
                canon_of[k] = next((c for c in ci.CANONICAL if ci.norm(c) == n), None) or ci.ALIASES.get(n) or str(k)
                if canon_of[k] not in cols:
                    cols.append(canon_of[k])
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(cols)
    for r in rows:
        cells = {canon_of[k]: ("" if v is None else str(v)) for k, v in r.items()}
        w.writerow([cells.get(c, "") for c in cols])
    return buf.getvalue().encode("utf-8")


def tool_make_import_file(rows: list[dict], output_path: str | None = None, out_dir: Path | None = None) -> str:
    """Write a PartsLoc import CSV, but only if the site's checker finds no ERRORS. Warnings are
    reported and the file is still written (the app only warns on those too)."""
    ci = load_check_import()
    if not rows or not all(isinstance(r, dict) for r in rows):
        return "Give a list of rows, each a mapping of column name to value, e.g. [{'name': 'Belt A42', 'count': 12}]."
    data = rows_to_csv(rows, ci)
    errors, warnings, notes = ci.check(data)
    report = "\n".join([*(f"ERROR   {m}" for m in errors), *(f"WARNING {m}" for m in warnings), *(f"NOTE    {m}" for m in notes)])
    if errors:
        return "NOT WRITTEN: the importer would skip or reject rows.\n" + report + "\nFix these and call again."
    dest = safe_new_path(output_path, out_dir or DEFAULT_DIR, "partsloc-import", ".csv")
    dest.write_bytes(data)
    return (f"Wrote {dest}: {len(rows)} row(s), importable. On the phone: Settings, Data, Import parts from CSV "
            f"(it shows a preview before changing anything).\n" + report).rstrip()


# --------------------------------------------------------------------------------------------
# Finding the file, and the MCP wiring
# --------------------------------------------------------------------------------------------

def find_backup(spec: str | None = None) -> Path:
    """spec = a file, or a folder (the newest backup in it wins). Default: $PARTSLOC_BACKUP, then ~/PartsLoc.
    Re-resolved on every call, so dropping a fresh backup in the folder needs no restart and no config edit.
    Newest JSON backup wins over a CSV export, because the CSV cannot give a Yes."""
    p = Path(spec or os.environ.get("PARTSLOC_BACKUP") or DEFAULT_DIR).expanduser()
    if p.is_file():
        return p
    if p.is_dir():
        js = sorted(p.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        js = [f for f in js if f.name.startswith("partsloc-backup") or "backup" in f.name.lower()] or js
        cs = sorted(p.glob("*.csv"), key=lambda f: f.stat().st_mtime, reverse=True)
        for f in js + cs:
            return f
    raise FileNotFoundError(f"No PartsLoc backup found at {p}. Copy the backup file from the phone into that folder "
                            "(see INSTALL.md), or set PARTSLOC_BACKUP to the file.")


_cache: dict = {}


def load_inventory(spec: str | None = None) -> Inventory:
    """Cached on (path, mtime, size): re-read only when the file changed. Read-only; never writes it."""
    f = find_backup(spec)
    st = f.stat()
    key = (str(f), st.st_mtime_ns, st.st_size)
    if _cache.get("key") != key:
        _cache["key"], _cache["inv"] = key, Inventory.from_bytes(f.read_bytes(), f)
    return _cache["inv"]


def build_server(backup: str | None = None):
    """FastMCP over stdio. The `mcp` import is here, not at module top, so everything above is testable
    (and importable) without the SDK installed."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("partsloc", instructions=(
        "Read-only helper over a PartsLoc backup file copied from the user's phone. Answers are from a COPY: "
        "say how old it is (each reply states it). Verdicts are the app's own Yes / Probably / No with reasons; "
        "do not upgrade a Probably to a Yes. Nothing here uses the network."))

    @mcp.tool()
    def find_part(query: str) -> str:
        """Do I have this part? Search by name, part number, maker, place or any linked number. Returns the app's Yes / Probably / No, the count, the shelf and the reasons in words."""
        return tool_find_part(load_inventory(backup), query)

    @mcp.tool()
    def where_is(part: str) -> str:
        """Where is this part kept? Every place that holds it, with counts and any directions saved on the place."""
        return tool_where_is(load_inventory(backup), part)

    @mcp.tool()
    def list_place(place: str) -> str:
        """What is stored in this shelf, bin, room or truck (including places inside it)?"""
        return tool_list_place(load_inventory(backup), place)

    @mcp.tool()
    def low_stock() -> str:
        """Which parts are LOW or OUT (the app's own rule), with ideal stock and supplier to help reorder."""
        return tool_low_stock(load_inventory(backup))

    @mcp.tool()
    def labels_for(parts: list[str] | None = None, place: str | None = None, output_path: str | None = None) -> str:
        """Make a printable label sheet PDF (US Letter, Avery 5160 layout, QR codes the PartsLoc app scans). Give a place (shelf label + a bin label per part on it) and/or a list of part names. Writes a NEW file next to the backup; never overwrites."""
        return tool_labels_for(load_inventory(backup), parts, place, output_path)

    @mcp.tool()
    def make_import_file(rows: list[dict], output_path: str | None = None) -> str:
        """Write a PartsLoc import CSV from rows (column name to value; name or sku required) after checking it with the same rules the app's importer uses. Writes a NEW file; refuses if the checker finds errors."""
        try:
            d = find_backup(backup).parent
        except FileNotFoundError:
            d = DEFAULT_DIR  # making an import file does not need a backup to exist
        return tool_make_import_file(rows, output_path, out_dir=d)

    return mcp


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="PartsLoc read-only MCP helper (stdio, no network).")
    ap.add_argument("--backup", help="backup file, or a folder (newest backup wins). Default: $PARTSLOC_BACKUP or ~/PartsLoc")
    ap.add_argument("--check", action="store_true", help="load the backup, print a summary and exit (for setup)")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args(argv)
    if a.check:
        try:
            inv = load_inventory(a.backup)
        except Exception as e:  # setup aid: say what is wrong in words, not a traceback
            print(f"Problem: {e}")
            return 1
        print(f"OK: {inv.source} ({inv.kind}), {len(inv.items)} parts, {len(inv.locations)} places.")
        print(provenance(inv, datetime.now()))
        return 0
    build_server(a.backup).run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
