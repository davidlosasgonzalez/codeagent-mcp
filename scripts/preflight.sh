#!/usr/bin/env bash
# Run what CI runs, in the order CI runs it, before pushing.
#
# Written after CI went red twice on a green local run. Both times the gap was
# a check CI had and the local habit did not: once basedpyright, once
# `uv sync --locked` — a version bump in pyproject.toml leaves uv.lock stale,
# and nothing else notices.
#
# The rule this encodes: a local gate set that is a subset of CI is not a gate
# set, it is a guess.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

step() { printf '\n== %s ==\n' "$1"; }

step "lockfile matches pyproject"
uv lock --check

step "lint"
uv run ruff check .

step "format"
uv run ruff format --check .

step "types"
uv run basedpyright

step "tests"
uv run pytest -q

printf '\nlisto: las mismas puertas que CI, en verde\n'
