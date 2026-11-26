# PR coordinator

`pr-coord` coordinates a GitHub PR backlog across machines. The controller runs on
Linux/macOS with Python 3.11+, Git and `gh`; remote features use SSH, `rt` also
uses rsync, and resource probes and leases can use `agent-coord`. Windows can be
a remote certification/JUnit worker and run remote implementers. Installing the command does not start
anything. No Python packages are needed for the tool.

## Set up a project

Copy [`config.example.toml`](config.example.toml) to
`~/.config/pr-coordinator/<project>.toml` and set the repository, owner, local
checkout, landing policy and hosts. Config and data are private, outside all
checkouts by default. `XDG_CONFIG_HOME` and `XDG_STATE_HOME` are honored.
`--project PROJECT` selects the project (`PR_COORD_PROJECT` is an optional
default). `--config FILE` and `--data-dir DIR` override the selection. Place
these switches **before** the command.

```sh
pr-coord --project project status
pr-coord --project project dashboard --bind 127.0.0.1 --port 8787
pr-coord --project project run                  # dashboard + collectors
pr-coord --project project run --landing        # also supervise, train, yield, and cancel
```

Use one foreground `run` per project in a managed terminal or service. It stops
the other children if any exits, and stops its children on Ctrl-C or SIGTERM.
Alternatively run `dashboard`, `collect`, `supervise`, and `train run`
separately. `collect --mutate` explicitly enables yield/stale cancellation;
`collect --once` and `supervise --once` run one cycle. `events` blocks until a
new main failure, queue drop, or question, prints it and exits; restart it on
notification. Monitor foreground errors and supervise processes externally.
No units are installed or enabled by default.

The dashboard reads only local snapshots and does not contact GitHub. `/` has PR
tables, conflicts, dependencies, leases, certifications and questions;
`/lifecycle` shows phases, rework and job traces; `/tests` shows CI cost, per-test
results and candidates for less frequent execution. The HTTP server has no
authentication. Keep it on loopback and use an SSH tunnel, or put it behind an
authenticated proxy on a trusted network. Turn on `dashboard.allow_answers` to
enable owner answers from the UI; otherwise use `pr-coord answer ID TEXT`.
Agent questions are separate from owner questions. Read or answer them with
`pr-coord agent-questions [ID ANSWER]`; `dashboard.show_agent_questions` exposes
that queue to dashboard readers but does not put it in the owner answer form.

## Configuration and command contracts

- **GitHub:** `github.repo`, `owner`, `tracked_authors`, `base_branch`, `remote`.
  Tracked bots appear in the dashboards but are not automatically refreshed or
  enqueued as owner PRs. Mutating loops check the authenticated owner. A
  `hands-off` label forbids all mutation. A `priority` PR can yield capacity;
  an existing reviewed queue entry can be moved to the front. `land-asap`
  authorizes cancelled-only retries and front enqueueing, without editing the
  collaborator's branch. Coordinate ownership and label rules with the repo.
- **Landing:** `lane.generated` is a list of full-path regexes, with ordered
  PR classes `top` and `priority`. `landing.pr_checks_ready` receives `gh pr
  checks --json bucket,name,workflow,link` on stdin; use it for draft-to-ready
  run selection and superseded attempts. Without it, every returned job must
  pass or skip and all `required_checks` must pass. `aggregate_checks` lists
  failing rollups that do not block a cancellation-only retry. `merge_flags`
  must use `--auto`, never `--admin`. `provision`, `regenerate`, `quick_gate`,
  `quick_tests`, `quick_host`, `quick_local_prefix`, and `ci_capacity` are
  opt-in commands. Quick tests absent in the refreshed worktree are omitted.
  `quick_host` uses `rt --admit-now` and falls back to `quick_local_prefix`
  when the remote host cannot admit the job immediately. Remote test failures
  still fail the gate; they are not retried locally.
- **Hosts:** entries have a key, display text, SSH alias (omitted for local),
  platform, core count, roles, checkout, `windows` and `agent_coord`. Use
  `cert_priority` for a host's preferred layer and `slots` for its concurrency.
  `shell_prefix` is an optional trusted remote shell prefix (e.g. tool PATH).
  `process_probe` and `process_probe_ssh` can supply a POSIX `ps`/load fallback.
  Hosts that don't answer SSH within the connect timeout are skipped when
  picking a certification slot. If none of a target's hosts is online and the
  target isn't in `must_run`, its certification is recorded as skipped at once
  (e.g. a laptop that left; hosted CI covers that platform). `finish-draft`
  stops only for test failures, or for errors on `drafts.required_statuses`.
- **Certification:** `certification.slots` maps targets to ordered host keys.
  `local_command` runs in the selected worktree. `remote_command` also runs
  on the controller, and should invoke the project's remote controller with
  its `remote_config`. Optional `environments` outputs a whitespace-separated
  environment list, supplied as one argument to preparation hooks. Local and
  remote preparation/cleanup and `notify` are explicit hooks. A host may
  override `cert_prepare` and `cert_cleanup`; on Windows the
  `["powershell-script", "SCRIPT", "ARG", ...]` form runs the configured code
  with `$args`. Project hooks own lockfile validation, worker input caches,
  limits and teardown. `cert-pr` uses an existing clean worktree on the PR
  branch or prepares a dedicated one, then waits for the configured targets;
  run it in a managed background shell if needed.
- **Draft handoff:** `watch-draft PR` waits for a submitted bot review and
  terminal successful certification statuses on the exact head, with no
  certification still running. Configure `drafts.required_statuses` to require
  particular status contexts and `drafts.reviewer_pattern` to override the
  default Copilot bot login regex. `finish-draft PR` additionally checks review
  threads and ownership, runs `landing.ci_capacity` (plus optional
  `drafts.capacity_args`), retries exit 3, marks the PR ready and submits it to
  the train. If `drafts.ready_workflow` is set, the next adoption waits for the
  previous admission's run with `drafts.ready_run_suffix` (default `/ ready`).
  `ATTENTION` means an operator must investigate; already-ready PRs can be
  submitted with `handoff PR`. It reuses the clean PR branch worktree or creates
  one and fast-forwards only; it does not discard local changes.
- **Remote implementers:** opt a host in with `remote_agents = true` and
  configure `[remote_agents]` with `command` (agent argv before `-p PROMPT`),
  `report` (reporter argv, run on the host), and an optional `runbook` file.
  `remote-agent HOST NAME PROMPT_FILE` sends the prompt and optional runbook
  over SSH to that trusted host and starts the agent. On a POSIX host it runs in
  tmux. Set a host's `agent_env_session` to require an existing session for its
  windows. For a keychain inaccessible over SSH, an operator can explicitly
  pipe an authorized credential to `agent-env HOST`; it initializes that tmux
  session in memory without storing the credential in settings or logs. Only
  do this for a host authorized to receive the credential. Closing the session
  drops its tmux environment; already-running programs retain their own
  environment.
- **Remote implementers on Windows:** a host with `windows = true` and
  `remote_agents = true` uses the same commands and keys; the SSH shell may be
  PowerShell or cmd. There is no tmux: each agent gets a directory
  `AGENT_ROOT\NAME` (host key `agent_root`, default `CHECKOUT\out\agents`;
  a drive or `~\` path) holding `prompt.md`, the runbook, `launch.json`, a
  static wrapper `run.ps1`, `pid.json` (pid, process start time, start
  date), `output.log` and `exit.code`. The directory is created readable only
  by the user and SYSTEM. The wrapper is started through
  `Win32_Process.Create`, so it outlives the SSH call (a `Start-Process` child
  is killed when the SSH session ends), and runs the agent in the host
  `checkout` with stdin from `prompt.md` and stdout and stderr in
  `output.log`, as the agent writes them (Node-based runtimes write UTF-8; the
  wrapper's own `EXIT N` line is UTF-8). The prompt never appears on a command
  line: `-p` is appended without an argument (Claude Code reads the prompt from
  stdin), or an argument containing `{prompt_file}` gets the prompt's path and
  no `-p` is added. A host's `agent_command` and `agent_report` override
  `[remote_agents]` when the executables differ there. The agent argv is
  joined into one `cmd.exe` line for the redirections, so its arguments may not
  contain `"%!^&|<>`; run a `.ps1` shim through its `.cmd` twin or
  `powershell.exe -File`. Reporter arguments may not be empty or contain `"`.
  `shell_prefix` does not apply.
  - `remote-agent HOST --list` prints each agent with `running`,
    `exited RC`, `stopped`, `lost` (its process is gone without an exit
    code, e.g. after a reboot) or `starting`.
  - `remote-agent HOST NAME --log [--lines N]` prints the end of its log.
  - `remote-agent HOST NAME --stop` kills the process tree of the recorded
    wrapper pid, only after checking on the host that the pid still has the
    recorded start time and runs that agent's `run.ps1`; a missing, zero or
    unverified pid is refused. Children are followed by parent pid only when
    they started after their parent, so a reused pid is not followed. Nothing
    is killed by name.
  - Names are limited to letters, digits, `_` and `-` (64 characters, not
    a device name such as `CON`) and checked again on the host. Names, the
    root and the session are passed to the remote scripts as quoted arguments,
    and file contents as base64 JSON on stdin; no value is spliced into script
    text.
  - `agent-env HOST` on Windows starts a detached holder process for the
    host's `agent_env_session`. The credential is piped through SSH stdin to
    the host (the controller does not read it) and handed to the holder over a
    named pipe that only this user may open (the client checks the pipe's
    server pid is the recorded holder). The holder keeps it only in its own
    process environment as `GH_TOKEN` and `COPILOT_GITHUB_TOKEN`, and starts
    that host's agents, which inherit it. It is never written to a file, the
    registry, a log or a command line. Limits: any process of the same user
    (and any administrator) can read the holder's and agents' memory and
    environment, and memory can be paged to disk. `agent-env HOST --stop`
    stops the holder (verified like an agent); running agents keep their
    environment. With `agent_env_session` set, new agents need the holder.
  - Remote logons (SSH, WMI) of an administrator get the full administrator
    token, so agents run elevated; no interactive desktop session is needed
    or used. Agents and the holder do not survive a reboot or a sign-out of
    all sessions: they show as `lost`, and `agent-env` must be piped again.
    A runtime that keeps its own sign-in (e.g. Claude Code's credentials file
    in the user's profile) needs no `agent-env`.
- **Remote tests:** `rt HOST WORKTREE -- COMMAND ...` sends the current
  worktree, including uncommitted changes, to the configured SSH worker. Only
  point this at trusted machines. `remote_tests.prepare` must provision the
  destination before `rsync --delete` and excludes must preserve `.git`.
  `remote_tests.provision` can prepare environments after synchronization.
  `rt HOST WORKTREE --envs "base dev" -- COMMAND` overrides the environment
  list passed to hooks that use `{envs}`.
  Use the host's `rt_prefix` to hold resource leases and `rt_root` to keep
  disposable worker checkouts apart from primary checkouts.
  `rt HOST WORKTREE --admit-now -- COMMAND` declines with exit 75 if the
  host's per-controller slot is busy, the host is unreachable, or its
  configured `rt_admission` command is missing or returns nonzero. Set that
  command to match the resources in `rt_prefix` (e.g. `agent-coord check`).
  Probes run before copying and before launching; the prefix still owns the
  actual lease. Explicit `rt` calls without `--admit-now` can still wait.
- **Test evidence:** `tests --refresh` explicitly reads hosted job attempts
  and JUnit ZIPs, plus retained local/SSH test output; it doesn't rerun tests.
  `tests --jobs-only`, `tests --resume-jobs`, `tests --annotations`, and
  `reports --local-only` select individual collection paths. `tests` without
  flags rebuilds from cached inputs and the local checkout. Configure
  `tests.hosted_workflows` (workflow → event → stage), `workflow_events` (an
  optional event allowlist per workflow), `event_stages`, `nightly_workflows`,
  `release_workflows`, `stage_workflows`, `artifact_pattern`,
  `artifact_xml_names`, and per-host `junit` glob paths. A `tiers_command`
  takes a JSON array of test-file names on stdin and returns a JSON object
  keyed by name with `tier` and `portable` fields. `stage_descriptions`,
  `stage_details`, `stage_highlights`, `workflow_purpose`, `job_purpose`, and
  `areas` can explain project-specific selection policy.

Hooks are argv arrays. Known values such as `{repo}`, `{checkout}`, `{data}`,
`{worktree}`, `{pr}`, `{host}`, `{sha}`, `{base}`, `{platform}`, `{layer}`,
`{envs}`, `{remote_worktree}`, and `{remote_config}` are substituted as single
arguments; availability depends on the command. Do not copy untrusted hooks.

## Migrate and cut over

1. Stop **only** the old dashboard, supervisor, collector, train and event
   watcher through their existing launcher. Let current refresh and
   certification work finish first; stop agent submissions while migrating.
2. Before installing, run the new entry point in the approved dev checkout:
   `./tools/pr-coordinator/pr-coord --project PROJECT migrate --from OLD_DIR`.
   It copies recognized JSON/JSONL snapshots, caches and lane/priority markers
   without overwriting existing files or importing scripts or process locks.
   The target defaults to `~/.local/state/pr-coordinator/PROJECT/`. Keep the old
   directory intact for rollback. Use a separate `--data-dir` to preview a copy.
3. After the owner approves distribution and the commit lands in skill-dump,
   run `skill-dump update`; do not edit the installed clone.
4. Verify `pr-coord --project PROJECT status`; start
   `pr-coord --project PROJECT run --landing` under your process manager and
   re-arm `pr-coord --project PROJECT events`. Use dashboard-only mode to
   inspect migrated data before enabling any mutations.

For browser verification on a populated copy, run `browser_smoke.py --base
http://127.0.0.1:8787 --out out/browser.json`. This optional test needs Chromium
and `websocket-client`; the regular tool and test suite do not.
