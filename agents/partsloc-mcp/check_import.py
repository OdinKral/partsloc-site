#!/usr/bin/env python3
"""Check a CSV against what the PartsLoc app's "Import parts from CSV" really accepts.

Usage:  python3 check_import.py parts.csv        (exit 0 = importable, 1 = errors found)

Standard library only. No network. Reads the file, changes nothing.

This mirrors the app's importer (lib/services/csv_import.dart in the PartsLoc source, store build:
Settings > Data > "Import parts from CSV"): same encoding fallback, same header matching, same
number parsing, same "what counts as a usable row". Where the app only warns, this prints WARNING;
where the app skips a row, ERROR; NOTE is something the app accepts silently but that usually
surprises people. The app also shows its own preview before writing anything, so this is a
pre-flight check, not a replacement.

Why the row numbers look the way they do: the app numbers rows the way its own preview does (header
is row 1; completely blank lines are skipped and not counted). Keep that, so a message here matches
the message the person sees on the phone.
"""
import csv, io, re, sys

# Exactly the importer's canonical columns (what the app's own export writes).
CANONICAL = ["id", "name", "sku", "category", "trade", "description", "count", "ideal_stock",
             "low_threshold", "unit_cost", "supplier", "supplier_url", "default_location",
             "photo_url", "manufacturer", "lifecycle_status", "last_updated"]
# Header aliases the importer accepts (keys are normalised: lowercase, letters and digits only).
ALIASES = {
    "partnumber": "sku", "partno": "sku", "part": "sku", "partid": "sku", "modelnumber": "sku",
    "model": "sku", "itemnumber": "sku", "partname": "name", "itemname": "name", "item": "name",
    "title": "name", "qty": "count", "quantity": "count", "onhand": "count", "stock": "count",
    "instock": "count", "cost": "unit_cost", "price": "unit_cost", "unitprice": "unit_cost",
    "eachcost": "unit_cost", "vendor": "supplier", "vendorname": "supplier",
    "suppliername": "supplier", "mfg": "manufacturer", "mfr": "manufacturer", "make": "manufacturer",
    "brand": "manufacturer", "type": "category", "group": "category", "notes": "description",
    "desc": "description", "reorderpoint": "low_threshold", "reorderat": "low_threshold",
    "minqty": "low_threshold", "min": "low_threshold", "maxqty": "ideal_stock",
    "target": "ideal_stock", "location": "default_location", "bin": "default_location",
    "shelf": "default_location", "discipline": "trade", "tradetype": "trade",
}
# The app reads these columns but does not apply them on import (see import-format.md).
IGNORED_ON_IMPORT = {"lifecycle_status", "last_updated"}


def norm(h):
    return re.sub(r"[^a-z0-9]", "", h.lower())


def decode(raw):
    """UTF-8 first, Latin-1 as the fallback (Latin-1 never fails), like the app."""
    note = None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
        note = "file is not UTF-8; read as Latin-1 (accented letters may look odd). Save as CSV UTF-8 to be safe."
    return text.lstrip("﻿"), note   # Excel's BOM would otherwise spoil the first header


def parse_rows(text):
    rows = list(csv.reader(io.StringIO(text, newline="")))
    # The app skips lines that are one empty cell.
    return [r for r in rows if len(r) > 1 or (r and r[0].strip())]


def int_ok(raw):
    """The app strips everything except digits and '-' then parses; None means 'not a number'."""
    try:
        return int(re.sub(r"[^0-9-]", "", raw))
    except ValueError:
        return None


def float_ok(raw):
    try:
        return float(re.sub(r"[^0-9.-]", "", raw))
    except ValueError:
        return None


def check(raw_bytes):
    """Return (errors, warnings, notes) as lists of strings."""
    errors, warnings, notes = [], [], []
    text, enc_note = decode(raw_bytes)
    if enc_note:
        notes.append(enc_note)
    rows = parse_rows(text)
    if not rows:
        return ["The file is empty."], warnings, notes

    headers = rows[0]
    colmap, unmapped, seen = {}, [], {}
    for i, h in enumerate(headers):
        n = norm(h.strip())
        if not n:
            continue
        canon = next((c for c in CANONICAL if norm(c) == n), None) or ALIASES.get(n)
        if not canon:
            unmapped.append(h.strip())
            continue
        if canon in seen:
            warnings.append(f"columns '{headers[seen[canon]].strip()}' and '{h.strip()}' both mean '{canon}'; the later one wins.")
        seen[canon] = i
        colmap[i] = canon
    if unmapped:
        notes.append("ignored columns (not recognised): " + ", ".join(unmapped))
    if "name" not in seen and "sku" not in seen:
        errors.append("row 1: no usable columns. Need at least a name or a part number column. Found: " + ", ".join(headers))
        return errors, warnings, notes
    for c in IGNORED_ON_IMPORT & set(seen):
        notes.append(f"column '{c}' is accepted but not applied by the import.")

    keys, names = {}, {}
    for r in range(1, len(rows)):
        line, cells = r + 1, rows[r]
        v = {colmap[i]: cells[i].strip() for i in colmap if i < len(cells)}
        name, sku = v.get("name", ""), v.get("sku", "")
        if len(cells) > len(headers):
            notes.append(f"row {line}: has {len(cells)} cells but the header has {len(headers)}; extra cells are ignored (an unquoted comma in a name?).")
        if not name and not sku:
            errors.append(f"row {line}: no name and no part number; this row would be skipped.")
            continue
        for col in ("count", "ideal_stock", "low_threshold"):
            raw = v.get(col, "")
            if raw and int_ok(raw) is None:
                warnings.append(f"row {line}: {col} '{raw}' is not a number; the app would use 0 (or keep the stored value).")
            elif raw and re.search(r"[.,]\d", raw) and col != "unit_cost":
                warnings.append(f"row {line}: {col} '{raw}' has a decimal or comma; the app drops non-digits and would read it as {int_ok(raw)}. Use whole numbers.")
        raw = v.get("unit_cost", "")
        if raw and float_ok(raw) is None:
            warnings.append(f"row {line}: unit_cost '{raw}' is not a number; the app would use 0.")
        if v.get("photo_url"):
            notes.append(f"row {line}: photo_url is set; the app downloads that address itself when the part is opened. Leave it blank unless you trust it.")
        key = (v.get("manufacturer", "").lower(), sku.lower()) if sku else None
        lname = name.lower()
        if (key and key in keys) or (lname and lname in names):
            first = keys.get(key) or names.get(lname)
            warnings.append(f"row {line}: same part as row {first}; the app does NOT merge rows within one file, so both would be created (a duplicate).")
        if key:
            keys.setdefault(key, line)
        if lname:
            names.setdefault(lname, line)
    if len(rows) == 1:
        errors.append("the file has a header row but no parts.")
    return errors, warnings, notes


def main(argv):
    if len(argv) != 2:
        print(__doc__.split("\n\n")[0]); return 2
    try:
        raw = open(argv[1], "rb").read()
    except OSError as e:
        print(f"ERROR cannot read {argv[1]}: {e}"); return 2
    errors, warnings, notes = check(raw)
    for m in errors:   print("ERROR   " + m)
    for m in warnings: print("WARNING " + m)
    for m in notes:    print("NOTE    " + m)
    n = len(parse_rows(decode(raw)[0])) - 1
    print(f"\n{max(n,0)} data row(s): {len(errors)} error(s), {len(warnings)} warning(s), {len(notes)} note(s).")
    print("Importable." if not errors else "Fix the errors above, then check again.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
