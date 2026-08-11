# Workspace lease

Exclusive writer coordination for registered project checkouts.

## Tools

- `workspace_acquire(project="<id>", mode="exclusive", lease_id?)`
- `workspace_status(project?, lease_id?)`
- `workspace_release(lease_id)`

Project ids come from the server registry — see [`projects-registry.md`](projects-registry.md).

## Rules

- One exclusive lease per project id (maps to a fixed `root` from `projects.yaml`).
- Opaque `lease_id` is the capability token; never logged at INFO in full.
- Second acquire without the holder token → structured `LEASE_BUSY` (no MCP exception).
- Pass holder `lease_id` to renew TTL (activity).
- Release is idempotent; does not kill processes/sessions.
- Expiry is lazy; does not destroy processes.
- Persist: `CODEAGENT_LEASE_STORE` (default `/var/lib/codeagent-mcp/leases.json`) with flock + atomic rewrite.
- TTL: `CODEAGENT_LEASE_TTL_S` (default 2700s).
- Real writes also require the project's write gate (`writable` / `writable_env`) plus OS permissions. Mutating tools still need a valid lease.

## Errors

Recoverable conflicts return `{ok:false, error:{code,message,retryable,next_action,...}}` with codes `LEASE_BUSY`, `LEASE_EXPIRED`, `INVALID_ARGUMENT`, `INTERNAL_ERROR`.

Mutating/exec tools such as `exec_run` require an active `lease_id` (see [`exec-run.md`](exec-run.md)).
