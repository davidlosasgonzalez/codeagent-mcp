# Security

CodeAgent MCP executes commands and writes files on the host that runs it. Treat any deployment as granting the connected AI assistant shell-level access to the configured project roots, bounded by the controls below.

## Security model

- The service runs as a dedicated system user with no sudo and no Docker access; the package tree is read-only to it.
- Agents can only reach project roots registered server-side in `projects.yaml`. Clients never supply filesystem paths.
- Writes are off by default and gated per project (`writable` / `writable_env`), plus systemd `ReadWritePaths=`, plus an exclusive workspace lease per mutating call.
- The HTTP transport binds to loopback only and is meant to sit behind a TLS reverse proxy. With auth enabled, startup fails closed unless the GitHub OAuth app, JWT signing key, and subject allowlist are all configured.

The full baseline, including rotation and recovery procedures, is in [`docs/architecture/hardening.md`](docs/architecture/hardening.md). Run `scripts/threat_tests.sh` against a deployed host to verify the perimeter.

## Reporting a vulnerability

Please do not open a public issue for security problems. Use [GitHub private vulnerability reporting](https://github.com/davidlosasgonzalez/codeagent-mcp/security/advisories/new) on this repository. You should get a first response within a week.

## Supported versions

Only the latest release on `main` receives security fixes.
