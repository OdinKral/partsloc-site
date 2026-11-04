# PartsLoc parts import: the CSV format

<!-- Source of truth: lib/services/csv_import.dart (CsvReader, columnAliases, CsvImport.plan/apply)
     and lib/screens/csv_import_screen.dart in the PartsLoc source, store build. Read 2026-11-04.
     The store build has the import (Settings > Data > "Import parts from CSV"). Only the PARTS
     importer is reachable there: the places/demand/stock importers in lib/services are not wired
     to any screen in the store build, so this file does not describe them. If csv_import.dart
     changes, change this file and agents/check_import.py in the same commit. -->

This is exactly what the PartsLoc Android app accepts when someone taps **Settings, Data, Import parts from CSV**. It is useful even without the app: it is a plain parts-list layout any spreadsheet can hold. To check a file before it goes to the phone, run `python3 check_import.py yourfile.csv` (same folder, no installs).

## The short version

- A CSV file (comma separated, `.csv`). UTF-8 is best; Excel's UTF-8 marker is fine; if the file is not UTF-8 the app reads it as Latin-1.
- First row is the header. Column order does not matter; columns are matched by name.
- **Every row needs a `name` or a `sku` (part number).** Everything else is optional.
- Numbers are whole numbers for quantities. Costs may have `$` and commas.
- The app shows a preview of what will change before anything is written.

## Columns

Header matching ignores case, spaces and punctuation, so `Part #`, `part_number` and `Part Number` are the same.

| Column | Meaning | Notes |
|---|---|---|
| `name` | what people call the part | Required unless `sku` is given. If only `sku` is given, the name becomes the sku. |
| `sku` | part or model number | Required unless `name` is given. |
| `count` | quantity on hand | Whole number. Blank means 0 for a new part. |
| `manufacturer` | who makes it | Used with `sku` to recognise a part that is already on the phone. |
| `category` | free text, e.g. Sensor | |
| `description` | free text | Notes, sizes, voltage. May hold commas or line breaks if quoted. |
| `ideal_stock` | the quantity you want to keep | Whole number, optional. |
| `low_threshold` | at or below this the part shows LOW | Whole number. 0 means never show LOW. |
| `unit_cost` | cost each | `$1,250.00` is read as 1250. |
| `supplier` | where it is bought | free text |
| `supplier_url` | supplier web page | free text; opens in the phone's browser when tapped |
| `default_location` | where it lives, as text | see Places below |
| `trade` | HVAC, Electrical, etc. | free text; kept on parts that already exist, not on brand-new ones |
| `photo_url` | web address of a picture | Leave blank. The app downloads that address itself when the part is opened. |
| `id` | the app's own part id | Leave blank for new parts. A matching id updates that part. |
| `lifecycle_status`, `last_updated` | exist in the app's own export | accepted, but not applied on import |

**Aliases** (a header the app also understands):
- sku: `part number`, `part no`, `part`, `part id`, `model number`, `model`, `item number`
- name: `part name`, `item name`, `item`, `title`
- count: `qty`, `quantity`, `on hand`, `stock`, `in stock`
- unit_cost: `cost`, `price`, `unit price`, `each cost`
- supplier: `vendor`, `vendor name`, `supplier name`
- manufacturer: `mfg`, `mfr`, `make`, `brand`
- category: `type`, `group`
- description: `notes`, `desc`
- low_threshold: `reorder point`, `reorder at`, `min qty`, `min`
- ideal_stock: `max qty`, `target`
- default_location: `location`, `bin`, `shelf`
- trade: `discipline`, `trade type`

A column the app does not recognise is left out and reported in the preview, never guessed into a field. If two columns mean the same field, the later one wins.

## Places

The import does **not** create shelves, bins or rooms. `default_location` (or `location`, `bin`, `shelf`) is stored as plain text on the part, as a note of where it lives, for example `Truck 2, left drawer`. Shelves and bins that carry QR labels and per-shelf counts are made in the app afterwards (Parts, By place). So a good import file keeps one consistent text label per place, which makes the later setup quick.

## What happens to each row

- **New part** (nothing on the phone matches): created with the given count (0 if blank).
- **Existing part**: a row matches an existing part by `id`, then by `manufacturer` plus `sku`, then by `name` (case does not matter). Matching rows update the part's other fields.
- **A different count for an existing part**: the app asks, and shows both numbers, unless you choose "the file wins" for a count you just took. The default is to ask. The file never silently overwrites a count.
- **Blank cells on an existing part**: a blank number keeps the stored number, but a blank text cell blanks that text. To leave a field alone on a re-import, delete the column instead of leaving it empty.
- **Duplicates inside one file** are not detected. Two rows for the same part in the same file make two parts. Merge them before importing.
- **A row with no name and no sku** is skipped and listed as an error.
- **A number that cannot be read** (for example `count` of `a few`) is a warning, and 0 is used.
- **Numbers are read by dropping every character except digits and a minus sign.** So `12 pcs` is 12, `1,250` is 1250, but `1.5` becomes 15 and `5-6` fails. Write whole numbers.
- **Extra cells** beyond the header are ignored. An unquoted comma inside a name causes this: quote any cell that contains a comma, as `"Belt, A42"`.
- Blank lines are skipped. Quoted cells may contain commas, line breaks and doubled quotes (`""`).
- Photos are not imported (a CSV cannot hold them). Add photos in the app.

## Minimal valid file

```csv
name,count
Damper actuator,4
```

## A fuller example (invented demo data)

```csv
name,sku,manufacturer,category,count,low_threshold,ideal_stock,unit_cost,supplier,default_location,description
"Damper actuator, 24 V spring return",LMB24-3-T,Belimo,Actuator,4,2,6,$212.50,Demo Supply Co,Shop crib shelf B3,"Fits 3/4 in shaft, 24 V"
Current switch sensor,H977,Veris,Sensor,2,2,4,$38.00,Demo Supply Co,Shop crib shelf B3,Solid core
Belt A42,A42,,Belts,12,4,12,$9.75,Demo Supply Co,Truck 2 left drawer,
Filter 20x25x2,,,Filters,30,10,40,$6.20,Demo Filters Inc,Basement storage rack 1,MERV 8
```

The header may be renamed to the aliases above, for example `Part #,Qty,Bin` instead of `sku,count,default_location`.
