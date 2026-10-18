#!/usr/bin/env python3
"""Mailmerge: turn a CSV of records and a text template into one file per row.

Standard library only. Subcommands:
  check   lint the template and CSV, and test-render every row
  fields  list the placeholders a template uses and whether the CSV has them
  preview print one rendered record (or all of them) to stdout
  render  write one file per record into a directory (or --dry-run)
"""
import argparse
import csv
import io
import os
import re
import sys
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

EXIT_OK, EXIT_FINDINGS, EXIT_USAGE = 0, 1, 2
FILTERS = ("upper", "lower", "title", "trim", "money", "default")

TAG_RE = re.compile(r"\{\{(.*?)\}\}")
BLOCK_RE = re.compile(r"([#/])(if|unless)(?:[ \t]+([A-Za-z_][A-Za-z0-9_]*))?")
VAR_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)((?:[ \t]*\|[ \t]*[a-z]+(?::\"[^\"]*\")?)*)")
FILTER_RE = re.compile(r'\|[ \t]*([a-z]+)(?::"([^"]*)")?')
UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class TemplateError(Exception):
    """A problem in the template or in one record, pinned to a template line."""

    def __init__(self, line, message):
        super().__init__(f"line {line}: {message}")
        self.line = line
        self.message = message


class UsageError(Exception):
    """A bad command-line argument."""


# ---- template language ---------------------------------------------------

def _line_at(text, index):
    return text.count("\n", 0, index) + 1


def _parse_tag(body, line):
    text = body.strip()
    if text[:1] in ("#", "/"):
        match = BLOCK_RE.fullmatch(text)
        if match is None:
            raise TemplateError(line, f"malformed block tag '{{{{{text}}}}}'")
        sigil, kind, name = match.groups()
        if sigil == "#":
            if name is None:
                raise TemplateError(line, f"'#{kind}' needs a field name")
            return ("open", kind, name, line)
        if name is not None:
            raise TemplateError(line, f"'/{kind}' takes no field name")
        return ("close", kind, None, line)

    match = VAR_RE.fullmatch(text)
    if match is None:
        raise TemplateError(line, f"malformed placeholder '{{{{{text}}}}}'")
    name, rest = match.groups()
    filters = []
    for fname, arg in FILTER_RE.findall(rest):
        if fname not in FILTERS:
            raise TemplateError(line, f"unknown filter '{fname}'")
        if fname == "default":
            filters.append((fname, arg or ""))
        elif arg:
            raise TemplateError(line, f"filter '{fname}' takes no argument")
        else:
            filters.append((fname, None))
    return ("var", name, filters, line)


def _add_text(tokens, template, start, end):
    if start == end:
        return
    chunk = template[start:end]
    if "{{" in chunk:
        at = start + chunk.index("{{")
        raise TemplateError(_line_at(template, at), "'{{' without a matching '}}'")
    tokens.append(("text", chunk, _line_at(template, start)))


def _tokenize(template):
    tokens, pos = [], 0
    for match in TAG_RE.finditer(template):
        _add_text(tokens, template, pos, match.start())
        tokens.append(_parse_tag(match.group(1), _line_at(template, match.start())))
        pos = match.end()
    _add_text(tokens, template, pos, len(template))
    return tokens


def _nest(tokens, index=0, opener=None):
    """Group flat tokens into nodes; blocks become ("block", open_token, children)."""
    nodes = []
    while index < len(tokens):
        token = tokens[index]
        if token[0] == "open":
            children, end = _nest(tokens, index + 1, token)
            if end >= len(tokens):
                raise TemplateError(token[3], f"'#{token[1]} {token[2]}' is never closed")
            nodes.append(("block", token, children))
            index = end + 1
        elif token[0] == "close":
            if opener is None:
                raise TemplateError(token[3], f"'/{token[1]}' has no opening tag")
            if token[1] != opener[1]:
                raise TemplateError(
                    token[3], f"'/{token[1]}' closes '#{opener[1]} {opener[2]}'")
            return nodes, index
        else:
            nodes.append(token)
            index += 1
    return nodes, index


def parse_template(text):
    return _nest(_tokenize(text))[0]


def placeholders(nodes):
    """Return (field name, template line) for every field the template reads."""
    found = []
    for node in nodes:
        if node[0] == "var":
            found.append((node[1], node[3]))
        elif node[0] == "block":
            found.append((node[1][2], node[1][3]))
            found.extend(placeholders(node[2]))
    return found


def format_money(text, line):
    raw = text.strip()
    if raw == "":
        return ""
    try:
        amount = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        amount = None
    if amount is None or not amount.is_finite():
        raise TemplateError(line, f"money filter needs a number, got '{raw}'")
    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    whole, _, cents = f"{abs(amount):.2f}".partition(".")
    sign = "-" if amount < 0 else ""
    return f"{sign}{int(whole):,}.{cents}"


def _apply(fname, arg, value, line):
    if fname == "upper":
        return value.upper()
    if fname == "lower":
        return value.lower()
    if fname == "title":
        return value.title()
    if fname == "trim":
        return value.strip()
    if fname == "money":
        return format_money(value, line)
    return value if value.strip() else arg  # default


def _lookup(record, name, line):
    if name not in record:
        raise TemplateError(line, f"unknown field '{name}'")
    return record[name]


def _render_into(nodes, record, parts):
    for node in nodes:
        if node[0] == "text":
            parts.append(node[1])
        elif node[0] == "var":
            _, name, filters, line = node
            value = _lookup(record, name, line)
            for fname, arg in filters:
                value = _apply(fname, arg, value, line)
            parts.append(value)
        else:
            _, tag, children = node
            filled = _lookup(record, tag[2], tag[3]).strip() != ""
            if filled == (tag[1] == "if"):
                _render_into(children, record, parts)


def render(nodes, record):
    parts = []
    _render_into(nodes, record, parts)
    return "".join(parts)


# ---- data ----------------------------------------------------------------

def load_csv(text, delimiter=","):
    """Parse CSV text. Returns (header, rows, findings); rows are (line, record)."""
    header, rows, findings = None, [], []
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    for cells in reader:
        line = reader.line_num
        if not any(cell.strip() for cell in cells):
            continue
        if header is None:
            header = [cell.strip() for cell in cells]
            seen = set()
            for name in header:
                if not name:
                    findings.append(("error", line, "empty column name in header"))
                elif name in seen:
                    findings.append(("error", line, f"duplicate column '{name}'"))
                seen.add(name)
            continue
        if len(cells) != len(header):
            findings.append(
                ("error", line, f"expected {len(header)} fields, found {len(cells)}"))
            continue
        rows.append((line, {name: cell.strip() for name, cell in zip(header, cells)}))
    if header is None:
        findings.append(("error", 1, "no header row found"))
        header = []
    return header, rows, findings


def select_rows(rows, where, header):
    conditions = []
    for item in where:
        column, sep, value = item.partition("=")
        if not sep or not column.strip():
            raise UsageError(f"--where needs COLUMN=VALUE, got '{item}'")
        column = column.strip()
        if column not in header:
            raise UsageError(f"--where column '{column}' is not in the CSV header")
        conditions.append((column, value.strip()))
    return [(line, rec) for line, rec in rows
            if all(rec[column] == value for column, value in conditions)]


def safe_filename(text):
    return UNSAFE_FILENAME_RE.sub("_", text.strip()).strip("._")


def plan_outputs(rows, body, name_nodes, template_path, csv_path):
    """Render every row. Returns (outputs, findings); outputs are (name, text, line)."""
    outputs, findings, reported = [], [], set()
    for index, (line, record) in enumerate(rows, 1):
        try:
            text = render(body, record)
            if name_nodes is None:
                name = f"record-{index:03d}.txt"
            else:
                name = safe_filename(render(name_nodes, record))
        except TemplateError as err:
            key = (err.line, err.message)
            if key not in reported:
                reported.add(key)
                findings.append(
                    ("error", csv_path, line, f"{template_path}:{err.line}: {err.message}"))
            continue
        if not name:
            findings.append(("error", csv_path, line, "file name pattern gives an empty name"))
            continue
        outputs.append((name, text, line))

    first_line = {}
    for name, _, line in outputs:
        if name in first_line:
            findings.append(
                ("error", csv_path, line,
                 f"file name '{name}' is also produced by line {first_line[name]}"))
        else:
            first_line[name] = line
    return outputs, findings


# ---- CLI helpers ---------------------------------------------------------

def _read(path):
    with open(path, encoding="utf-8-sig") as fh:
        return fh.read()


def _parse_file(path):
    try:
        return parse_template(_read(path)), []
    except TemplateError as err:
        return None, [("error", path, err.line, err.message)]


def _name_nodes(pattern):
    if pattern is None:
        return None, []
    try:
        return parse_template(pattern), []
    except TemplateError as err:
        return None, [("error", "--name", err.line, err.message)]


def _load(template_path, csv_path, delimiter, where):
    body, findings = _parse_file(template_path)
    header, rows, csv_findings = load_csv(_read(csv_path), delimiter)
    findings.extend((sev, csv_path, line, msg) for sev, line, msg in csv_findings)
    rows = select_rows(rows, where, header)
    return body, header, rows, findings


def _print_findings(findings):
    for severity, where, line, message in findings:
        print(f"{where}:{line}: {severity}: {message}")


def _has_errors(findings):
    return any(severity == "error" for severity, _, _, _ in findings)


def _unknown_field_findings(sources, header):
    found = []
    for where, uses in sources:
        for name, line in uses:
            if name not in header:
                found.append(("error", where, line,
                              f"unknown field '{name}' (columns: {', '.join(header)})"))
    return found


# ---- commands ------------------------------------------------------------

def cmd_check(args):
    body, header, rows, findings = _load(args.template, args.csv, args.delimiter, args.where)
    name_nodes, name_findings = _name_nodes(args.name)
    findings.extend(name_findings)

    sources = []
    if body is not None:
        sources.append((args.template, placeholders(body)))
    if name_nodes is not None:
        sources.append(("--name", placeholders(name_nodes)))
    unknown = _unknown_field_findings(sources, header)
    findings.extend(unknown)

    used = {name for _, uses in sources for name, _ in uses}
    for column in header:
        if column not in used:
            findings.append(("warning", args.csv, 1,
                             f"column '{column}' is never used by the template"))
    for line, record in rows:
        for name in sorted(used & set(header)):
            if not record[name]:
                findings.append(("warning", args.csv, line, f"empty value for '{name}'"))

    can_render = body is not None and not unknown and (args.name is None or name_nodes is not None)
    if can_render:
        _, render_findings = plan_outputs(rows, body, name_nodes, args.template, args.csv)
        findings.extend(render_findings)

    _print_findings(findings)
    errors = sum(1 for severity, *_ in findings if severity == "error")
    warnings = len(findings) - errors
    print(f"{len(rows)} rows checked: {errors} error(s), {warnings} warning(s)")
    return EXIT_FINDINGS if errors else EXIT_OK


def cmd_fields(args):
    body, findings = _parse_file(args.template)
    if body is None:
        _print_findings(findings)
        return EXIT_FINDINGS
    header = None
    if args.csv:
        header, _, _ = load_csv(_read(args.csv), args.delimiter)

    lines_by_name = {}
    for name, line in placeholders(body):
        lines_by_name.setdefault(name, []).append(line)
    if not lines_by_name:
        print("no placeholders in template")
        return EXIT_OK

    missing = False
    for name in sorted(lines_by_name):
        where = ", ".join(f"line {n}" for n in lines_by_name[name])
        status = ""
        if header is not None:
            if name in header:
                status = "  [ok]"
            else:
                status = "  [MISSING]"
                missing = True
        print(f"{name}  ({where}){status}")
    return EXIT_FINDINGS if missing else EXIT_OK


def cmd_preview(args):
    body, header, rows, findings = _load(args.template, args.csv, args.delimiter, args.where)
    if body is None or _has_errors(findings):
        _print_findings(findings)
        return EXIT_FINDINGS
    if not rows:
        raise UsageError("no rows selected")
    if args.all:
        chosen = list(enumerate(rows, 1))
    else:
        if not 1 <= args.row <= len(rows):
            raise UsageError(f"--row must be between 1 and {len(rows)}")
        chosen = [(args.row, rows[args.row - 1])]

    try:
        texts = [(number, line, render(body, record)) for number, (line, record) in chosen]
    except TemplateError as err:
        print(f"{args.template}:{err.line}: error: {err.message}")
        return EXIT_FINDINGS

    for position, (number, line, text) in enumerate(texts):
        if args.all:
            if position:
                print()
            print(f"----- row {number} (csv line {line}) -----")
        print(text)
    return EXIT_OK


def cmd_render(args):
    body, header, rows, findings = _load(args.template, args.csv, args.delimiter, args.where)
    name_nodes, name_findings = _name_nodes(args.name)
    findings.extend(name_findings)
    outputs = []
    if body is not None and not _has_errors(findings) and (args.name is None or name_nodes is not None):
        outputs, plan_findings = plan_outputs(rows, body, name_nodes, args.template, args.csv)
        findings.extend(plan_findings)

    if not args.overwrite:
        for name, _, line in outputs:
            path = os.path.join(args.outdir, name)
            if os.path.exists(path):
                findings.append(("error", path, line, "file already exists (use --overwrite)"))

    if _has_errors(findings):
        _print_findings(findings)
        print("nothing written")
        return EXIT_FINDINGS

    if args.dry_run:
        for name, text, _ in outputs:
            print(f"would write {os.path.join(args.outdir, name)} ({len(text)} characters)")
        print(f"dry run: {len(outputs)} file(s), nothing written")
        return EXIT_OK

    os.makedirs(args.outdir, exist_ok=True)
    for name, text, _ in outputs:
        with open(os.path.join(args.outdir, name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    print(f"wrote {len(outputs)} file(s) to {args.outdir}")
    return EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(
        prog="mailmerge",
        description="Render one text document per CSV row from a {{field}} template.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_data_args(p):
        p.add_argument("template", help="template file")
        p.add_argument("csv", help="CSV file with a header row")
        p.add_argument("--delimiter", default=",", help="CSV field separator (default ',')")
        p.add_argument("--where", action="append", default=[], metavar="COLUMN=VALUE",
                       help="only use rows whose COLUMN equals VALUE (repeatable)")

    p = sub.add_parser("check", help="lint the template and CSV and test-render every row")
    add_data_args(p)
    p.add_argument("--name", help="file name pattern to test for collisions")
    p.set_defaults(handler=cmd_check)

    p = sub.add_parser("fields", help="list the placeholders a template uses")
    p.add_argument("template", help="template file")
    p.add_argument("--csv", help="check each field against this CSV's header")
    p.add_argument("--delimiter", default=",", help="CSV field separator (default ',')")
    p.set_defaults(handler=cmd_fields)

    p = sub.add_parser("preview", help="print one rendered record to stdout")
    add_data_args(p)
    p.add_argument("--row", type=int, default=1, help="1-based row among selected rows")
    p.add_argument("--all", action="store_true", help="print every selected row")
    p.set_defaults(handler=cmd_preview)

    p = sub.add_parser("render", help="write one file per record")
    add_data_args(p)
    p.add_argument("--outdir", required=True, help="directory to write files into")
    p.add_argument("--name", help="file name pattern, e.g. '{{last}}-{{first}}.txt'")
    p.add_argument("--dry-run", action="store_true", help="list the files without writing")
    p.add_argument("--overwrite", action="store_true", help="replace existing files")
    p.set_defaults(handler=cmd_render)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except UsageError as err:
        print(f"mailmerge: {err}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as err:
        print(f"mailmerge: {err}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
