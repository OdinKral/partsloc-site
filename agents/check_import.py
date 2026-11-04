#!/usr/bin/env python3
"""Check a CSV against what the PartsLoc app's "Import parts from CSV" really accepts.

Usage:  python3 check_import.py parts.csv        (exit 0 = importable, 1 = errors found)

Standard library only. No network. Reads the file, changes nothing.

This mirrors the app's importer (lib/services/csv_import.dart in the PartsLoc source, store build:
Settings > Data > "Import parts from CSV"): same encoding fallback, same header matching, same
number parsing, same "what counts as a usable row". Where the app only warns, this prints WARNING;
where the app skips a row, ERROR; NOTE is something the app accepts silently but that usually
surprises people. (Behaviour as of the 2026-11-04 importer: non-whole counts and in-file duplicates
are row ERRORS, blank text cells leave existing values alone, photo_url is ignored.) The app also shows its own preview before writing anything, so this is a
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
TEXT_COLS = ("category", "description", "supplier", "supplier_url", "default_location", "trade")
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


# Whole numbers as the app reads them (since 2026-11-04): "40", "-3", "1,200" (thousands
# separators) and "40.0" (what a spreadsheet makes of a whole number). Everything else is a ROW
# ERROR in the app, not a guess: "1.5", "a few", "12 pcs", and "1.200" (one point two or one
# thousand two hundred? the app will not choose). The old rule (drop every non-digit, so "1.5"
# became 15) was a silent 10x stock error; this must stay identical to csv_import.dart.
WHOLE = re.compile(r"^-?(\d+|\d{1,3}(,\d{3})+)(\.0+)?$")


def int_ok(raw):
    """True if the app accepts this as a whole number."""
    return bool(WHOLE.match(raw))


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

    idents, photo_rows, blank_cells = {}, 0, False
    for r in range(1, len(rows)):
        line, cells = r + 1, rows[r]
        v = {colmap[i]: cells[i].strip() for i in colmap if i < len(cells)}
        name, sku = v.get("name", ""), v.get("sku", "")
        if len(cells) > len(headers):
            notes.append(f"row {line}: has {len(cells)} cells but the header has {len(headers)}; extra cells are ignored (an unquoted comma in a name?).")
        if not name and not sku:
            errors.append(f"row {line}: no name and no part number; this row would be skipped.")
            continue
        bad = False
        for col in ("count", "ideal_stock", "low_threshold"):
            raw = v.get(col, "")
            if raw and not int_ok(raw):
                errors.append(f"row {line}: {col} '{raw}' is not a whole number; the app skips this row.")
                bad = True
        if bad:
            continue
        raw = v.get("unit_cost", "")
        if raw and float_ok(raw) is None:
            warnings.append(f"row {line}: unit_cost '{raw}' is not a number; the app would use 0.")
        if v.get("photo_url"):
            photo_rows += 1
        for col in TEXT_COLS:
            if col in seen and not v.get(col, ""):
                blank_cells = True
        # One part, one row: the later row is an ERROR in the app (the first is kept). Identity
        # is (manufacturer, sku), else name, as in csv_import.dart. A typed id is not an identity (the
        # app's own template repeats one placeholder id on every example row).
        if sku:
            ident = ("sku", v.get("manufacturer", "").lower(), sku.lower())
        else:
            ident = ("name", name.lower())
        if ident in idents:
            errors.append(f"row {line}: same part as row {idents[ident]} (duplicate); the app skips this row. Keep one of the two rows.")
            continue
        idents[ident] = line
    if photo_rows:
        warnings.append(f"photo_url ignored on {photo_rows} rows: the app does not download pictures from web addresses on import. Add photos in the app.")
    if blank_cells:
        notes.append("blank text cells on parts already in the app leave those fields unchanged (a spreadsheet cannot clear a text field).")
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
