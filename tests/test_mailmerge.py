import os

import pytest

from mailmerge import (
    EXIT_FINDINGS,
    EXIT_OK,
    EXIT_USAGE,
    TemplateError,
    UsageError,
    format_money,
    load_csv,
    main,
    parse_template,
    placeholders,
    plan_outputs,
    render,
    safe_filename,
    select_rows,
)

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")
REMINDER = os.path.join(EXAMPLES, "reminder.txt")
GUESTS = os.path.join(EXAMPLES, "guests.csv")
BROKEN = os.path.join(EXAMPLES, "guests-broken.csv")
NAME = "{{last}}-{{first}}.txt"


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_placeholders_and_case_filters():
    nodes = parse_template("{{first|title}} {{last|upper}} {{first|lower}}")
    record = {"first": "ada LOVELACE", "last": "lovelace"}
    assert render(nodes, record) == "Ada Lovelace LOVELACE ada lovelace"


def test_trim_and_default_filters():
    nodes = parse_template('[{{note|trim}}] [{{time|default:"7:00 pm"}}]')
    assert render(nodes, {"note": "  hi  ", "time": ""}) == "[hi] [7:00 pm]"
    assert render(nodes, {"note": "", "time": "9:30"}) == "[] [9:30]"


def test_money_formatting_is_exact():
    assert format_money("1250.5", 1) == "1,250.50"
    assert format_money("-3", 1) == "-3.00"
    assert format_money("12345678", 1) == "12,345,678.00"
    assert format_money("0.005", 1) == "0.01"
    assert format_money("0.001", 1) == "0.00"
    assert format_money("", 1) == ""


def test_money_rejects_non_numbers_with_template_line():
    nodes = parse_template("line one\nline two\n{{amount|money}}")
    with pytest.raises(TemplateError) as info:
        render(nodes, {"amount": "abc"})
    assert info.value.line == 3
    assert "needs a number" in info.value.message


def test_if_and_unless_blocks():
    nodes = parse_template("{{#if seat}}Seat {{seat}}.{{/if}}{{#unless seat}}Standing.{{/unless}}")
    assert render(nodes, {"seat": "b4"}) == "Seat b4."
    assert render(nodes, {"seat": "  "}) == "Standing."


def test_unclosed_block_reports_its_line():
    with pytest.raises(TemplateError) as info:
        parse_template("first line\n{{#if a}}hello")
    assert info.value.line == 2
    assert "never closed" in info.value.message


def test_mismatched_close_tag_is_an_error():
    with pytest.raises(TemplateError) as info:
        parse_template("{{#if a}}x{{/unless}}")
    assert "closes '#if a'" in info.value.message


def test_unknown_filter_and_stray_braces_are_errors():
    with pytest.raises(TemplateError, match="unknown filter 'reverse'"):
        parse_template("{{name|reverse}}")
    with pytest.raises(TemplateError, match="without a matching"):
        parse_template("hello {{ name")


def test_render_unknown_field_fails_with_line():
    nodes = parse_template("ok\n{{nope}}")
    with pytest.raises(TemplateError) as info:
        render(nodes, {"first": "ada"})
    assert info.value.line == 2


def test_placeholders_lists_fields_and_block_lines():
    nodes = parse_template("{{a}}\n{{#if b}}{{c}}{{/if}}")
    assert placeholders(nodes) == [("a", 1), ("b", 2), ("c", 2)]


def test_load_csv_skips_blank_lines_and_reports_field_counts():
    text = "first,last\nada,lovelace\n\ngrace\nalan,turing\n"
    header, rows, findings = load_csv(text)
    assert header == ["first", "last"]
    assert [record["first"] for _, record in rows] == ["ada", "alan"]
    assert findings == [("error", 4, "expected 2 fields, found 1")]


def test_load_csv_reports_duplicate_and_empty_columns():
    _, _, findings = load_csv("a,a,\n1,2,3\n")
    messages = [message for _, _, message in findings]
    assert "duplicate column 'a'" in messages
    assert "empty column name in header" in messages


def test_select_rows_filters_and_rejects_unknown_columns():
    rows = [(2, {"city": "York"}), (3, {"city": "Leeds"}), (4, {"city": "York"})]
    assert [line for line, _ in select_rows(rows, ["city=York"], ["city"])] == [2, 4]
    with pytest.raises(UsageError):
        select_rows(rows, ["town=York"], ["city"])
    with pytest.raises(UsageError):
        select_rows(rows, ["city"], ["city"])


def test_safe_filename_strips_unsafe_characters():
    assert safe_filename("hamilton, jr.-margaret") == "hamilton_jr.-margaret"
    assert safe_filename("../../etc/passwd") == "etc_passwd"
    assert safe_filename("...") == ""


def test_plan_outputs_detects_name_collisions_and_bad_rows():
    body = parse_template("hi {{first}}")
    name_nodes = parse_template("{{last}}.txt")
    rows = [
        (2, {"first": "ada", "last": "x"}),
        (3, {"first": "grace", "last": "x"}),
    ]
    outputs, findings = plan_outputs(rows, body, name_nodes, "t.txt", "g.csv")
    assert [name for name, _, _ in outputs] == ["x.txt", "x.txt"]
    assert findings == [
        ("error", "g.csv", 3, "file name 'x.txt' is also produced by line 2")]

    bad = parse_template("{{first|money}}")
    _, findings = plan_outputs(
        [(2, {"first": "a", "last": "b"}), (3, {"first": "c", "last": "d"})],
        bad, None, "t.txt", "g.csv")
    assert len(findings) == 2
    assert all("money filter" in message for _, _, _, message in findings)


def test_plan_outputs_uses_default_record_names():
    body = parse_template("{{first}}")
    outputs, findings = plan_outputs([(2, {"first": "ada"})], body, None, "t", "c")
    assert findings == []
    assert outputs == [("record-001.txt", "ada", 2)]


def test_cli_check_clean_example_passes(capsys):
    assert main(["check", REMINDER, GUESTS, "--name", NAME]) == EXIT_OK
    out = capsys.readouterr().out
    assert "4 rows checked: 0 error(s)" in out


def test_cli_check_broken_csv_reports_line_numbers(capsys):
    assert main(["check", REMINDER, BROKEN, "--name", NAME]) == EXIT_FINDINGS
    out = capsys.readouterr().out
    assert "guests-broken.csv:5: error: expected 8 fields, found 7" in out
    assert "guests-broken.csv:4: error: " in out
    assert "money filter needs a number, got 'abc'" in out


def test_cli_fields_flags_missing_columns(tmp_path, capsys):
    template = _write(tmp_path, "t.txt", "Hi {{first}} {{nickname}}")
    csv_path = _write(tmp_path, "c.csv", "first\nada\n")
    assert main(["fields", template, "--csv", csv_path]) == EXIT_FINDINGS
    out = capsys.readouterr().out
    assert "first  (line 1)  [ok]" in out
    assert "nickname  (line 1)  [MISSING]" in out


def test_cli_preview_row_out_of_range_is_usage_error(capsys):
    assert main(["preview", REMINDER, GUESTS, "--row", "99"]) == EXIT_USAGE


def test_cli_preview_all_prints_every_selected_row(capsys):
    assert main(["preview", REMINDER, GUESTS, "--all", "--where", "seat=b4"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "----- row 1 (csv line 2) -----" in out
    assert "Dear Ada Lovelace," in out
    assert "Grace" not in out


def test_cli_render_dry_run_writes_nothing(tmp_path, capsys):
    outdir = tmp_path / "letters"
    code = main(["render", REMINDER, GUESTS, "--outdir", str(outdir),
                 "--name", NAME, "--dry-run"])
    assert code == EXIT_OK
    assert not outdir.exists()
    assert "dry run: 4 file(s), nothing written" in capsys.readouterr().out


def test_cli_render_writes_files_and_refuses_overwrite(tmp_path, capsys):
    outdir = tmp_path / "letters"
    args = ["render", REMINDER, GUESTS, "--outdir", str(outdir), "--name", NAME]
    assert main(args) == EXIT_OK
    written = sorted(os.listdir(outdir))
    assert written == ["hamilton_jr.-margaret.txt", "hopper-grace.txt",
                       "lovelace-ada.txt", "turing-alan.txt"]
    ada = (outdir / "lovelace-ada.txt").read_text(encoding="utf-8")
    assert "Your Garden Supper is booked" in ada
    assert "Your seat is B4." in ada
    assert "A deposit of $1,250.50 has been received." in ada

    capsys.readouterr()
    assert main(args) == EXIT_FINDINGS
    assert "already exists (use --overwrite)" in capsys.readouterr().out
    assert main(args + ["--overwrite"]) == EXIT_OK


def test_cli_render_refuses_to_write_when_any_row_fails(tmp_path, capsys):
    outdir = tmp_path / "letters"
    code = main(["render", REMINDER, BROKEN, "--outdir", str(outdir), "--name", NAME])
    assert code == EXIT_FINDINGS
    assert not outdir.exists()
    assert "nothing written" in capsys.readouterr().out
