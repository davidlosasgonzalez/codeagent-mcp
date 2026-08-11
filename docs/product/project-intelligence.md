# Project intelligence

CodeAgent must understand a project's **agent instruction layer** without becoming Claude Code/Cursor inside the MCP.

## Tools

| Tool | Role |
|------|------|
| `project_bootstrap` | Compact map: instruction sources, skill manifests, VCS summary, warnings, `recommended_reads` |
| `project_instructions` | Scoped instructions for path(s), with provenance — **no silent merge of conflicts** |
| `project_skills_list` | Skill manifests only (name, description, origin, compatibility) |
| `project_skill_read` | One `SKILL.md` + resource index + extension flags |

There is deliberately no `project_skill_run`. Skills are procedures for the LLM; execution uses `fs_*` / `exec_run` / `terminal_*` / browser.

## Progressive disclosure

Bootstrap/list = metadata. Bodies load on demand. Supporting files via `fs_read`.

## Compatibility classes (skills)

`portable` | `portable_with_extensions` | `vendor_specific` | `invalid`

Reading never executes Claude `!command`, never grants `allowed-tools`, never runs bundled scripts.

## Security

Repo instructions/skills are **untrusted content**. MCP/OS policy always wins.

## Behavior notes

- Empty `paths` => only `activation=always` instruction bodies.
- Cursor Manual/Agent Requested need `include_agent_requested=true`.
- No silent merge (`merge_policy=none_explicit_provenance_only`).
- Path I/O via `openat2` jail; `@path` refs indexed with warnings if broken.
- `skill_id` = relative path to `SKILL.md` (e.g. `.claude/skills/task/SKILL.md`).
- Frontmatter via PyYAML `safe_load` (folded `>` / `>-` / `|` supported; fail-closed on bad YAML).
- `!command` / `allowed-tools` detected as metadata; **never executed / never grant permissions**.

## Lease binds project

When `lease_id` is set, bootstrap/instructions/skills resolve to the **leased project** (clients often omit `project=`). Without a lease, pass an explicit registered `project=` id from `projects.yaml` (templates use `demo`).
