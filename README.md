# Mailmerge

**Offline mail merge from a plain CSV and a plain-text template.** One file per record, with exact money formatting, conditional sections, line-numbered errors and a dry run that writes nothing until every row renders.

No servers, no accounts, no network calls, no dependencies beyond the Python 3 standard library (pytest only for the tests).

## Features

- **Plain CSV in** — any header-row CSV, with a custom `--delimiter` (e.g. `;`). Quoted fields and BOM-prefixed files from spreadsheets work.
- **`{{field}}` placeholders** — every column becomes a field, with surrounding whitespace trimmed.
- **Text filters** — `upper`, `lower`, `title` and `trim`, chainable with `|`.
- **Fallback values** — `{{time|default:"7:00 pm"}}` fills in blank cells.
- **Exact money** — `{{deposit|money}}` turns `1250.5` into `1,250.50` using decimal arithmetic and half-up rounding, never floats.
- **Conditional sections** — `{{#if field}}…{{/if}}` and `{{#unless field}}…{{/unless}}`, where "blank" means empty or whitespace.
- **`check` linter** — reports template and CSV problems with `file:line` positions, flags columns the template never uses and blank values, and test-renders every row before anything is written.
- **`fields` report** — lists each placeholder with the lines it appears on, and marks missing CSV columns with `[MISSING]` (exit code 1).
- **`preview`** — prints one chosen record (`--row N`) or every record (`--all`) to the terminal.
- **`--where COLUMN=VALUE`** — restricts any command to a subset of rows, such as one event or one seating section.
- **Custom file names** — `--name "{{last}}-{{first}}.txt"`, with unsafe characters replaced by `_`. Without `--name`, files are named `record-001.txt`, `record-002.txt`, and so on.
- **Collision detection** — two rows that would produce the same file name are reported with both line numbers.
- **All-or-nothing rendering** — `render` writes nothing if any row fails, so you never get half a mailing.
- **Dry run** — `render --dry-run` lists every file that would be written.
- **Safe overwrites** — existing files are refused unless you pass `--overwrite`.
- **CI-friendly exit codes** — `0` clean, `1` findings (errors, missing fields, refused writes), `2` usage or file-access errors.

## Quick start

```sh
python3 mailmerge.py check examples/reminder.txt examples/guests.csv --name "{{last}}-{{first}}.txt"
python3 mailmerge.py preview examples/reminder.txt examples/guests.csv --where seat=b4
python3 mailmerge.py render examples/reminder.txt examples/guests.csv --outdir letters --name "{{last}}-{{first}}.txt" --dry-run
python3 mailmerge.py render examples/reminder.txt examples/guests.csv --outdir letters --name "{{last}}-{{first}}.txt"
```

The template in `examples/reminder.txt`:

```text
Dear {{first|title}} {{last|title}},

Your {{event|trim}} is booked for {{date}} at {{time|default:"7:00 pm"}}.
{{#if seat}}Your seat is {{seat|upper}}.{{/if}}
{{#unless seat}}We will seat you when you arrive.{{/unless}}
{{#if deposit}}A deposit of ${{deposit|money}} has been received. Thank you.{{/if}}

Please reply by {{rsvp_by}} if you cannot attend.
```

The first row of `examples/guests.csv` is `ada,lovelace,  Garden Supper ,2026-11-07,,b4,1250.5,2026-10-30`. Its rendered letter, written to `letters/lovelace-ada.txt`, reads:

```text
Dear Ada Lovelace,

Your Garden Supper is booked for 2026-11-07 at 7:00 pm.
Your seat is B4.

A deposit of $1,250.50 has been received. Thank you.

Please reply by 2026-10-30 if you cannot attend.
```

Try the error reporting on the deliberately broken file:

```sh
python3 mailmerge.py check examples/reminder.txt examples/guests-broken.csv
```

It reports the short row at `guests-broken.csv:5` (expected 8 fields, found 7) and the bad deposit at `guests-broken.csv:4`, with the template line where the money filter failed. It exits with code `1`.

## Commands

| Command | What it does |
| --- | --- |
| `check TEMPLATE CSV [--name PATTERN]` | Lint and test-render everything; exit `1` on any error |
| `fields TEMPLATE [--csv CSV]` | List placeholders and their lines; check them against the CSV header |
| `preview TEMPLATE CSV [--row N \| --all]` | Print rendered records to stdout |
| `render TEMPLATE CSV --outdir DIR [--name PATTERN] [--dry-run] [--overwrite]` | Write one file per record |

Every data command also accepts `--delimiter` and `--where COLUMN=VALUE` (repeatable).

## Template reference

| Syntax | Meaning |
| --- | --- |
| `{{name}}` | Value of column `name` |
| `{{name\|upper}}`, `lower`, `title`, `trim` | Text filters |
| `{{name\|default:"text"}}` | Use `text` when the value is blank |
| `{{name\|money}}` | Exact `1,234.50` format; blank stays blank; non-numbers are an error |
| `{{#if name}}…{{/if}}` | Include the section when `name` is not blank |
| `{{#unless name}}…{{/unless}}` | Include the section when `name` is blank |

Filters can be chained, as in `{{name|trim|upper}}`.

## Requirements

- Python 3.8 or newer
- pytest, only to run the test suite

## How to run

Run the tool from the project directory with `python3 mailmerge.py <command> …`, or import `mailmerge` from another script. The file has no install step.

## How to test

```sh
python3 -m pytest -q
```

The tests cover the template parser, filters, money formatting, CSV loading and line numbers, row selection, file-name safety, collision detection, and the CLI commands including dry runs, overwrite protection and exit codes. They run against the files in `examples/`, and write only to pytest's temporary directories.
