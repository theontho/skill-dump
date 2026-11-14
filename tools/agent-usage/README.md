# agent-usage

Counts-only, per-machine/per-repository/per-day agent usage. Python **3.11+**,
standard library only. `gh` must already be installed and authenticated for
merged-PR counts; nothing installs dependencies or authenticates on your behalf.

## Install and opt in

After the PR lands, on **lin-mid only**:

```sh
skill-dump update
mkdir -p ~/.config/agent-usage
cp ~/.local/share/skill-dump/tools/agent-usage/config.example.toml \
  ~/.config/agent-usage/config.toml
# Edit paths, timezone and (only if needed) the stable machine name.
agent-usage collect --days 14
agent-usage timer enable
agent-usage timer status
```

For an explicitly approved preview while the PR remains draft, run
`./bin/skill-dump update --no-pull` from the retained feature worktree instead.
This uses the normal installer and saved install options without merging or
changing the canonical checkout's branch. Installed tool/skill links and units
point into that worktree, so **retain it while the preview is installed**.
After merge, run `~/.local/share/skill-dump/bin/skill-dump update` to restore
canonical-checkout links before removing the preview worktree.

The skill-dump installer links the executable and renders the systemd user
service/timer (Linux) or `~/Library/LaunchAgents/com.theontho.agent-usage.plist`
(macOS). **Installation does not activate this tool's schedule**. Activation is
explicit and per machine. It runs daily at 03:15 local time (systemd adds up to
15 minutes of jitter and catches missed runs). It only collects, never posts.
Use `agent-usage timer disable` to opt out.
The launchd plist ships disabled; enable/disable persists in launchd's override
database, so installation alone never opts a Mac in at its next login and
disabling it survives subsequent logins.

For **lin-big** later: `skill-dump update`, copy/edit the example config, run one
collection, then `agent-usage timer enable`. Do not reuse lin-mid's machine name.
For the **M1 Pro** later: the same commands install and bootstrap the launchd
job; ensure Python 3.11+ and `gh` are on the job's PATH (`~/.local/bin`,
`/opt/homebrew/bin`, `/usr/local/bin`, `/usr/bin`, `/bin`).
`launchctl print gui/$(id -u)/com.theontho.agent-usage` shows its last exit status.
Neither machine should be enabled as part of the lin-mid rollout.

`machine` is a configurable display label, defaulting to the short hostname.
This host's machine-label name is **lin-mid**, while its hostname is
**lin-omarchy**; its TOML setting is `machine = "lin-mid (lin-omarchy)"`.
Spaces and parentheses are supported, including in the storage directory name.
Labels cannot contain path separators, newlines or Markdown table delimiters.

The default config path is `~/.config/agent-usage/config.toml`;
`agent-usage --config PATH …` selects another. No environment-variable settings.
With no repositories configured collection fails without writing misleading
empty days. `paths` match directory boundaries (including worktrees beneath
them); add paths for worktrees outside the configured checkout. Copilot's
explicit `workspace.yaml` repository name takes precedence over path matching.

## Collection and reports

```sh
agent-usage collect --days 7 --end 2026-11-12
# Offline collection explicitly marks PR counts unavailable, never zero:
agent-usage collect --days 7 --no-github
agent-usage report --weekly --repo WGSExtract/wgsextract-cli
agent-usage report --weekly --repo WGSExtract/wgsextract-cli --post-issue 765 --dry-run
# Only an explicit non-dry-run invocation posts; supervisor approval required:
# agent-usage report --weekly --repo WGSExtract/wgsextract-cli --post-issue 765
```

Weekly windows are the last **seven complete days** vs the preceding seven in
the configured IANA timezone; the default is UTC. `--end` fixes the comparison
date. The report starts `(AI):`, names each machine and refuses any posting
target other than the owner's WGSExtract/wgsextract-cli issue **765 (agent token
waste)**. No schedules post. Ordinary reports and `--dry-run` never write to
GitHub. The owner check uses only author metadata.

Storage defaults to `~/.local/state/agent-usage/MACHINE/YYYY-MM-DD.json`.
Change `storage` in TOML. Daily files contain per-agent (hashed ID), source,
model and root/child role counts, repository totals, cached/uncached prompts,
cache writes, output/reasoning, API-rate estimates, and prompt-size histograms
for exact pooled mean and nearest-rank p90. Repeated collection replaces daily
snapshots atomically under a collection lock, rather than appending duplicates.
The daily job refreshes 14 complete days; older JSON remains available for
week-over-week comparisons. Retain the source usage logs for this refresh
window: deleting them and recollecting would replace those days' counts.
Changing timezone or machine name requires an intentional historical rebuild.
To compare machines locally, copy **only their counts JSON machine directories**
into one storage directory, with the same timezone. PR counts are the repository's
global denominator, not PRs attributed to a particular machine; don't sum that
denominator across machines. Missing days/PR queries produce `n/a`, not zero.

## Privacy and interpretation

* Copilot: only `session.usage_record` metadata is projected from JSONL;
  workspace reads whitelist `cwd`, `git_root`, `repository`. `inputTokens`
  already includes cache reads/writes. A write is part of uncached input,
  **not extra prompt tokens**. `accounting.usageId` prevents replay duplication;
  `sourceSessionId` groups child calls within the supervisor's events. On the
  inspected CLI, children occur in the same log with `parentToolCallId`; that
  marks implementers. Root calls are labelled supervisor **as a proxy**, not a
  claim about their actual job. No session IDs are saved in cleartext.
* OpenCode: a private filesystem snapshot of the DB and WAL is made under
  storage (never the system temp directory), queried only for allowlisted
  `session_v2`/`session_message` usage metadata, then removed even on error. No account,
  credential, title, prompt, or response columns are queried. The raw short-lived
  copy is not an output or archive. Assistant messages' `data.tokens` counts
  are extracted as individual numeric fields inside SQLite, never by returning
  their full `data` JSON. Usage is bucketed on `data.time.completed` (falling
  back to message `time_created`), so long-running sessions don't inflate
  their creation day. These messages contribute API calls and prompt statistics;
  message model metadata takes precedence over the current session model.
  Session lifetime totals are **not added** when that session has message usage.
  A session without message usage falls back to `session_v2` lifetime totals
  on its creation day, excluded from calls/prompt statistics and counted as a
  `session_records` fallback. The report explicitly flags this day-inflation
  caveat whenever fallback records occur in either week.
  On the inspected DB `session_part` had no token counts; `session_message`
  provided timestamped assistant usage. A parent session ID supplies the same
  root/child role proxy.
* Claude Code: optional recursive project JSONL, whitelist message usage/model,
  ID and timestamp/cwd only. Message/request IDs deduplicate repeated chunks,
  retaining the maximum cumulative usage rather than an early partial output;
  `isSidechain`/a subagents directory marks implementers. No content is decoded.
* The JSON projector scans past unwanted values as opaque lexical tokens; it
  never deserializes prompt/response strings or tool arguments. No message
  text, tool results, credentials, full workspace YAML, or raw events are stored.
  **Polling share is omitted**: deriving bash sleeps/status commands would
  require inspecting tool argument/message text.
* Optional absent sources are skipped. Unsupported OpenCode schemas and GitHub
  errors fail collection before replacing daily files. A concurrently written
  incomplete JSONL record is skipped and picked up next run.

## Public API-rate estimates

[`rates.json`](rates.json) is a small, dated, reviewable USD-per-million table,
verified 2026-11-13 against the linked official OpenAI and Anthropic pricing
pages. Rate order: **uncached input, cache read, cache write, output**.
TOML `[rates]` may override/add a model with the same four-number array.
Unknown models leave total cost null and increment `unpriced_records`, while
retaining the known-priced subtotal. They are never silently free.

Estimates use standard, non-batch public rates, not provider-reported `cost`
(Copilot's field is credits, not USD). Copilot cache TTL selects 5m vs 1h Claude
write pricing, and 30m vs 12h GPT write pricing. Missing TTL uses the short
cache rate. OpenAI prompts above 272k use the documented long-context multiplier
(2× input/cache, 1.5× output); Haiku 5.5 prompts above 100k use its 5× tier.
OpenCode session aggregates use short-context rates because individual call
sizes cannot be recovered; applying context tiers to lifetime totals is invalid.
Timestamped OpenCode message usage uses the same per-call context pricing as
the other sources.
Other tier, region, service-speed, volume and
historical price changes are not inferred. Reasoning is retained separately but
not charged again: provider output usage already includes reasoning.

## Tests

```sh
npm run tools:test:usage
npm run tools:test
npm run tools:lint
npm run tools:test:install
```

Synthetic JSONL, tiny SQLite (including a live WAL), fake network boundary and
CLI tests cover cached token accounting, replay/chunk duplication, role splits,
local date boundaries, unknown prices, repository isolation, missing sources,
pagination, missing-day ratios, posting restrictions and dry-run privacy.
