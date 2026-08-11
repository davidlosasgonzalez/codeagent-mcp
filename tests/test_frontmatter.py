"""Frontmatter YAML parsing (folded scalars, SafeLoader, fail-closed)."""

from __future__ import annotations

from codeagent_mcp.project.frontmatter import parse_frontmatter


def test_no_frontmatter() -> None:
    meta, body = parse_frontmatter("# Hello\n")
    assert meta == {}
    assert body == "# Hello\n"


def test_simple_description() -> None:
    text = "---\nname: demo\ndescription: A simple skill\n---\nBody\n"
    meta, body = parse_frontmatter(text)
    assert meta["name"] == "demo"
    assert meta["description"] == "A simple skill"
    assert body == "Body\n"


def test_folded_description_gt() -> None:
    text = (
        "---\n"
        "name: fold\n"
        "description: >\n"
        "  First line of the skill.\n"
        "  Second line continues.\n"
        "---\n"
        "# Body\n"
    )
    meta, body = parse_frontmatter(text)
    assert meta["name"] == "fold"
    assert meta["description"] == "First line of the skill. Second line continues.\n"
    assert body.startswith("# Body")


def test_folded_description_gt_strip() -> None:
    text = (
        "---\n"
        "name: strip\n"
        "description: >-\n"
        "  First line of the skill.\n"
        "  Second line continues.\n"
        "---\n"
        "# Body\n"
    )
    meta, body = parse_frontmatter(text)
    assert meta["name"] == "strip"
    # >- folds newlines to spaces and strips the final newline
    assert meta["description"] == "First line of the skill. Second line continues."
    assert meta["description"] != ">-"
    assert body.startswith("# Body")


def test_multiline_literal_block() -> None:
    text = "---\nname: lit\ndescription: |\n  Line one\n  Line two\n---\nBody\n"
    meta, _body = parse_frontmatter(text)
    assert meta["description"] == "Line one\nLine two\n"


def test_bool_and_list_values() -> None:
    text = "---\nalwaysApply: true\nglobs: [a/**, b/**]\n---\nx\n"
    meta, _body = parse_frontmatter(text)
    assert meta["alwaysApply"] is True
    assert meta["globs"] == ["a/**", "b/**"]


def test_invalid_yaml_fail_closed() -> None:
    text = "---\n: bad: [unclosed\n---\nBody\n"
    meta, body = parse_frontmatter(text)
    assert meta == {}
    assert body == "Body\n"


def test_non_mapping_fail_closed() -> None:
    text = "---\n- just\n- a\n- list\n---\nBody\n"
    meta, body = parse_frontmatter(text)
    assert meta == {}
    assert body == "Body\n"


def test_oversized_frontmatter_fail_closed() -> None:
    huge = "x: " + ("a" * 70_000)
    text = f"---\n{huge}\n---\nBody\n"
    meta, body = parse_frontmatter(text)
    assert meta == {}
    assert body == "Body\n"


def test_deep_nesting_fail_closed() -> None:
    # Extreme nesting can raise RecursionError under SafeLoader; must not escape.
    nested = "[" * 5_000 + "]" * 5_000
    text = f"---\ndata: {nested}\n---\nBody\n"
    meta, body = parse_frontmatter(text)
    assert meta == {}
    assert body == "Body\n"
