# Jarvis-hermes: instructions for coding agents

Read CONTEXT.md, docs/specs/jarvis-v1.md and the relevant ADR before implementation.
This is one assistant, MCP-first, using Hermes and the existing Telegram bot.
Do not invent SDK capabilities, model availability or successful host integration.
Do not start a second poller or replace an existing Hermes installation without inspecting it.
Never commit credentials, personal vault contents, runtime databases or screenshots.
Every future execution path (MCP, terminal, browser, GUI) must respect the same policy.

## Verification

- `uv sync --locked`
- `uv run python -m unittest discover -s tests -v`
- `uv run jarvis doctor --json`

The doctor reports its current execution host, not the user's Windows computer.
Run host-dependent acceptance tests on the real Windows/WSL host before closing their issues.

## Agent skills

### Issue tracker

GitHub Issues in nuno80/Jarvis-hermes. See docs/agents/issue-tracker.md.

### Domain docs

Single-context: CONTEXT.md and docs/adr/. See docs/agents/domain.md.

Use Matt Pocock skills installed in the coding agent. Skill source is not vendored here.
The tickets were prepared with to-tickets and publication authorized by the owner.

---

## Execution environment

- Pi, git and `uv` run **inside WSL**. Every command in this file runs there.
- Keep the repository on the WSL filesystem (e.g. `~/Jarvis-hermes`), not under `/mnt/c`: it is faster and avoids permission and line-ending problems.
- Runtime: `Pi coding agent standard`

- For `codemode` to be binding, set `codemode.mode = "only"` in Pi settings. With `on` the rules below are only a preference.

## Workflow

Use the installed skills: `greel me` → `to task` → `to issue` → `implement`.
Do not redefine the flow here. Check that the names match the installed skills; this repo's tickets were created with `to-tickets`.
The source of truth for issues is GitHub Issues. Do not keep a second copy of issue state in local files.

## codemode policy

To preserve context, do **not** chain raw calls to `bash`, `read` or search tools when the task is filtering or aggregating. Use `codemode`.

`tools.bash` returns `{ output, truncated, full_output_path?, exit_code, wall_time_seconds }`.

- stdout and stderr are merged in `output`. There is no `stdout`, `stderr` or `exitCode`.
- A non-zero exit code does **not** throw. Check `exit_code`.
- The first line of a script may be `// @options: {...}`. `timeout_ms` has no default: always set it.
- The sandbox has no Node, filesystem or network: go through `tools.*` only.

### A. Tests (unittest)

Never dump full output into the context. Return status, first failures and the log path.

```javascript
// @options: {"timeout_ms": 300000, "max_output_tokens": 1500}
const r = await tools.bash({ command: "uv run python -m unittest discover -s tests" });
const lines = r.output.split("\n").filter(Boolean);
return {
  ok: r.exit_code === 0,
  exit_code: r.exit_code,
  summary: lines[lines.length - 1] ?? "", // "OK" or "FAILED (failures=N)"
  issues: r.exit_code === 0 ? [] : lines.filter((l) => /^(FAIL|ERROR): /.test(l)).slice(0, 5),
  log: r.truncated ? r.full_output_path : null,
};
```

To see one failure in detail, rerun only that test: `uv run python -m unittest tests.module.Class.test_name -v`.

### B. Doctor

The JSON schema of `jarvis doctor --json` is not documented here. Adapt this script once the real fields are known.

```javascript
// @options: {"timeout_ms": 60000, "max_output_tokens": 1000}
const r = await tools.bash({ command: "uv run jarvis doctor --json" });
try {
  const j = JSON.parse(r.output);
  return { exit_code: r.exit_code, report: JSON.stringify(j).slice(0, 1500) };
} catch (e) {
  return { exit_code: r.exit_code, parse_error: true, tail: r.output.trim().split("\n").slice(-10) };
}
```

Remember: this reports the WSL host, not the Windows computer.

### C. Multi-file reading and search

If you need more than 2 files, or references across the codebase, filter inside the script. Use `Promise.allSettled` so one missing file does not cancel the rest.

```javascript
const paths = ["src/a.py", "src/b.py"];
const res = await Promise.allSettled(paths.map((p) => tools.read({ path: p })));
return res.map((r, i) =>
  r.status === "fulfilled"
    ? { path: paths[i], hits: r.value.split("\n").flatMap((l, n) => (/PATTERN/.test(l) ? [`${n + 1}: ${l}`] : [])).slice(0, 10) }
    : { path: paths[i], error: String(r.reason) }
);
```

For codebase search: `tools.bash({ command: "rg -n PATTERN src | head -n 30" })`.

### D. Fallback

If `codemode` is unavailable, use plain bash with limited output (`| tail -n 30`) and still report only a short outcome. If the user asks for the full output, provide it.

## Safety before `implement`

Run this preflight **before every `implement`**. Do not start without `status: "ok"`. If the result is `blocked`, stop and ask the user. Do not commit, stash or discard changes on your own initiative.

- The base branch is `main` (change `BASE` if different).
- The branch protects only tracked, committed files. Ignored files (`.env`, local databases, vault contents) are not covered: never touch them.
- Replace `ISSUE_ID` with the GitHub issue number.

```javascript
// @options: {"timeout_ms": 30000}
const ISSUE_ID = "REPLACE_ME"; // GitHub issue number
const BASE = "main";
if (!/^\d+$/.test(ISSUE_ID)) return { status: "blocked", reason: "invalid issue number" };

const git = async (cmd) => {
  const r = await tools.bash({ command: `git ${cmd}` });
  return { code: r.exit_code, out: r.output.trim() };
};

const st = await git("status --porcelain");
if (st.code !== 0) return { status: "blocked", reason: "not a git repository", detail: st.out };
if (st.out) return { status: "blocked", reason: "working tree not clean", files: st.out.split("\n").slice(0, 10) };

const branch = `issue/${ISSUE_ID}`;
const exists = (await git(`rev-parse --verify --quiet refs/heads/${branch}`)).code === 0;

if (exists) {
  const s = await git(`switch ${branch}`);
  if (s.code !== 0) return { status: "blocked", reason: "switch failed", detail: s.out };
  return { status: "ok", resumed: true, branch, head: (await git("rev-parse HEAD")).out };
}

const cur = (await git("rev-parse --abbrev-ref HEAD")).out;
if (cur !== BASE) return { status: "blocked", reason: `on '${cur}', expected '${BASE}'` };

const c = await git(`switch -c ${branch}`);
if (c.code !== 0) return { status: "blocked", reason: "branch creation failed", detail: c.out };
return { status: "ok", resumed: false, branch, start_sha: (await git("rev-parse HEAD")).out };
```

The script is idempotent: rerunning it after a crash is safe. If `resumed` is `false`, add `start_sha` as a comment on the GitHub issue.

## Persistence

- **Pi coding agent standard**: there is no automatic workflow checkpoint. At the end of a phase, post a short status comment on the GitHub issue. `store(key, value)` / `load(key)` in codemode are only for small values between calls (ids, cursors), not for specs.
- **Pi Durable (experimental, API may change)**: do not save manual checkpoints or summarize the conversation by hand. Use a deterministic `requestId` (`implement:<issue_number>`) to resume or retry `implement`. After a crash, tools not marked `replay: "safe"` are not rerun: check `git status` before continuing.

## Output

- Short answers, with the outcome extracted through `codemode`.
- Report only relevant tracebacks or targeted changes (diffs or specific blocks).
- Exception: if the user asks for the full output, provide it.
