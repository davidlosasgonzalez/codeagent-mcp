# Workspace lease

Exclusive writer coordination for registered project checkouts.

## Tools

- `workspace_acquire(project="<id>", mode="exclusive", lease_id?)`
- `workspace_status(project?, lease_id?)`
- `workspace_release(lease_id)`
- `workspace_diff_since_acquire(lease_id, path?, max_bytes?)`

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

## What changed since I got here

`git_status` answers *what is dirty*. On a checkout that was already dirty when you
connected — someone else's staged work, a half-finished edit, stray untracked files —
that is not the question you have. You need *what did I change*, and staging state does
not separate the two.

So a fresh acquire records a **baseline**: a git tree object for the worktree exactly as
it stood at that moment. `workspace_diff_since_acquire` writes a second tree from the
worktree now and diffs the two.

- Pre-existing staged, unstaged and untracked work is excluded by construction.
- Staging, unstaging and commits made *during* the lease do not change the answer;
  `head_moved` reports whether HEAD advanced.
- Renewing or reclaiming a lease keeps its original baseline. That is the point: the
  baseline marks when you arrived, not when you last spoke.

The snapshot writes blob and tree objects through a **temporary index file**. It never
touches HEAD, refs, the real index or the worktree; unreferenced objects are reclaimed by
the repository's own `gc`. It runs outside the store lock, so no other writer queues
behind a large checkout being hashed.

If the snapshot fails — not a git repo, unwritable object store — the acquire still
succeeds and returns `baseline: null` with `baseline_error`. Only this one tool is
unavailable on that lease, and it says so with `BASELINE_UNAVAILABLE` rather than
quietly showing you the whole dirty tree.

Operators can switch snapshots off with `CODEAGENT_LEASE_BASELINE=0` (very large
checkouts where hashing at acquire time is not worth it).

## Where to put temp files

`workspace_acquire` and `terminal_create` return `tmpdir`: the private temp root already
exported as `TMPDIR`/`TEMP`/`TMP` into every pane shell and every `exec_run` child. A
hardened unit gives the service no usable `/tmp`, so a tool that defaults there fails on
a path that looks perfectly ordinary. Write scratch files under `tmpdir`.

Clients cannot override it — `TMPDIR` is on the `exec_run` env denylist and is re-pinned
after project env is applied. For panes specifically, see
[`terminals.md`](terminals.md): a shell created before this shipped still lacks the
variable and has to be recreated.

## Errors

Recoverable conflicts return `{ok:false, error:{code,message,retryable,next_action,...}}` with codes `LEASE_BUSY`, `LEASE_EXPIRED`, `INVALID_ARGUMENT`, `BASELINE_UNAVAILABLE`, `INTERNAL_ERROR`.

Mutating/exec tools such as `exec_run` require an active `lease_id` (see [`exec-run.md`](exec-run.md)).

## Baseline in a long session

The baseline is taken when the lease is acquired. If a lease expires mid-session
and a new one is acquired, the new baseline is the tree **as it is now** —
including everything done under the old lease. `workspace_diff_since_acquire`
is then correct and useless for reconstructing the earlier work.

This is the contract working as designed, not a defect, but it has a
consequence worth stating: in a long implementation session, capture the diff
before the lease lapses. Renewing by activity is what keeps a lease alive, so a
long stretch of thinking with no tool calls is exactly when one expires.
