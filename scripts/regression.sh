#!/usr/bin/env bash
# Portable regression checks (no ChatGPT UI). Host-only harness scripts are optional.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
echo "== pytest =="
.venv/bin/python -m pytest -q --tb=line
echo "== threat tests (public HTTPS) =="
bash scripts/threat_tests.sh
echo "== tool catalog check =="
# The snapshot is the default surface, so pin the example registry: a host whose
# own projects.yaml declares a control_socket exposes the service tools as well.
env CODEAGENT_PROJECTS_FILE=deploy/projects.example.yaml .venv/bin/python - <<'PY'
import asyncio, json
from pathlib import Path
from codeagent_mcp.server import create_server

async def main() -> None:
    frozen = json.loads(Path("docs/product/tool-catalog.json").read_text())
    names = [r["name"] for r in frozen]
    server = create_server(transport="stdio")
    live = sorted(t.name for t in await server.list_tools())
    assert live == names, (set(live) ^ set(names), live, names)
    http = create_server(transport="http")
    live_http = sorted(t.name for t in await http.list_tools())
    assert live_http == names
    print(f"PASS: catalog snapshot matches stdio+http ({len(names)} tools)")

asyncio.run(main())
PY
echo "regression: portable PASS"
