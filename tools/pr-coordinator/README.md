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
Alternatively run `dashboard`, `collect`, `event-collect`, `supervise`, and `train run`
separately. `collect --mutate` explicitly enables yield/stale cancellation;
`collect --once` and `supervise --once` run one cycle. Monitor foreground errors
and supervise processes externally. No units are installed or enabled by default.

### Event watcher

`run` collects new main failures, queue drops, questions and idle-host alerts
even while the supervisor is away. `[events] interval` sets the collection
interval in seconds (default `90`). The dashboard shows recent events, the
pending count and when each consumer last received a batch.

```sh
pr-coord --project project events --wait
pr-coord --project project events --wait --consumer audit --json
```

`--wait` reads local data only. It blocks for the next unseen batch, prints it,
commits the cursor, and exits. Invoke it again when ready for the next batch;
events accumulate in the meantime. Use the default `supervisor` cursor on
handoff. A second waiter for the same name is refused; a different name reads
the same journal from its own cursor. A crash before stdout is flushed does
not consume the batch. Stdout and the on-disk cursor cannot commit together:
if the process dies after output but before saving the cursor, it can replay
that batch. Use `--json` and deduplicate by `id` if automating batch handling.

At the start of every supervisor turn, run `pr-coord --project PROJECT agent
supervisor heartbeat`. A blocked waiter (or reading a batch) is not a
heartbeat. After `[events] supervisor_idle_minutes` (default `30`) without a
heartbeat, the collector emits one `SUPERVISOR-IDLE` event per gap if it
observes a fresh, reachable idle host with idle alerts enabled and finds an
eligible issue. Until the first heartbeat, the clock starts on the first
collection with this feature. Detection runs on the collection interval.

The default queue check queries the first 1,000 open issues in `[github] repo`,
requesting only metadata. It excludes `check-later`, `research-done`,
`hands-off`, and labels starting with `[labels] machine_prefix` (`machine-`
by default). Set `[events] queue_search` to an
additional GitHub issue search to narrow the queue (for example,
`'label:efficiency'`). This is a dispatch signal, not proof the real queue is
empty or permission to take an issue; the supervisor still checks policy.
A failed GitHub query does not count as evidence of eligible work.

If an owner prompt such as “continue” or “status” ends a long gap, use the
same heartbeat with `--owner-resume`. The coordinator cannot read chat; the
flag records an `OWNER-RESUME` incident after the configured threshold, with
no chat content saved. Alerts and incident counts appear on the dashboard.

To upgrade an existing installation, stop any old `events` process, then
stop the running `run` with SIGTERM (or use your service manager) and relaunch
`pr-coord --project PROJECT run` with the same options as before. This one-time
restart enables the new collector. `events_seen.json` seeds the journal without
replaying old IDs, and `events_known.json` continues to suppress known checks;
neither file is rewritten. `events N` still waits for a batch and, if no service
collector is running, polls on its own every `N` seconds.

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

- **Idle hosts:** `[events] idle_minutes` (default `45`) is the minimum time a
  host must be observed reachable without working or waiting dashboard agents,
  held `agent-coord` leases, heavy processes from a configured process probe,
  or running certifications or remote tests. Report agents with
  `pr-coord agent NAME STATUS --machine HOST` using the host key, display name,
  or SSH alias. The host collector supplies the observations; `event-collect` does
  not probe the hosts itself. Enable `agent_coord = true` on hosts whose leases
  should be collected; hosts without a workload probe get a reachability check.
  An offline host is not idle. Loss of observations resets the timer, but a
  reported host must be seen busy before another alert.
  Set `idle_alert = false` in a host entry to suppress its alerts without
  hiding the dashboard's “idle since” marker.
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
  Before provisioning, certification checks SSH reachability and live
  `agent-coord status --json` on the controller and on workers configured with
  `agent_coord`. It checks locks and lease locks named for the host key or SSH
  alias, and whole-machine exclusive leases on the selected machine. A worker
  can also define `cert_worker_lock` for a project-specific lock on that worker.
  Non-certification holders and holds expected to last more than
  `certification.long_hold_seconds` (3600 by default) make a host unavailable.
  Holds labeled `certification`, `local certification`, or `remote certification`
  with no long estimate are ordinary contention: wait for a slot without
  provisioning. If every configured host is reserved or offline, optional
  targets skip immediately (hosted CI covers them); `must_run` targets keep
  checking. Availability is re-evaluated without changing `certification.slots`.
  If `agent-coord` is unavailable, SSH reachability and controller busy retries
  still apply. `finish-draft` stops only for test failures, or for errors on
  `drafts.required_statuses`.
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
  `remote_agents = true` uses the same commands and keys. Each remote call
  runs a small fixed bootstrap (`powershell -EncodedCommand`, well under the
  cmd.exe line limit, so the OpenSSH default shell may be PowerShell or cmd)
  that reads the script and every value as base64 JSON on stdin; no
  configured or user value is spliced into script text. Windows PowerShell 5.1
  and .NET Framework (both part of Windows) compile a small native helper with
  `Add-Type` on each call.
  - **Folders.** Agents live in `AGENT_ROOT\NAME` (host key `agent_root`,
    default `CHECKOUT\out\agents`; a drive or `~\` path of ASCII letters,
    digits, space, `_`, `.`, `-` and separators; the checkout must match the
    same rule). The root, each agent folder and the environment folder are
    created in one call already owned by the user, readable only by the user,
    SYSTEM and Administrators, and labelled High integrity with no-write-up
    and no-read-up, so non-elevated processes cannot read or change them. An
    existing root or folder is used only if it is not a junction or link, is
    owned by the user, SYSTEM or Administrators, grants nobody else access
    and carries that label; otherwise the command refuses (`--list` shows it
    as `untrusted`), and so is a root with a junction or link anywhere up its
    parent chain. Point `agent_root` at a new folder under a path only this
    user can change (e.g. under the user profile): an existing folder that
    inherits broad access is refused, and the checks work on paths, so an
    ancestor that other accounts can rename is a weak spot.
  - **Files.** Each agent folder holds `prompt.md`, the runbook, `launch.json`,
    a static wrapper `run.ps1`, `pid.json` (pid, process start time, start
    date), `output.log` (the previous run's is kept as `output.1.log`),
    `exit.code`, and `failed.json` or `stopped.json` when relevant. A UTF-8
    BOM is removed from the prompt and runbook.
  - **Running.** The wrapper is started through `Win32_Process.Create`, so it
    outlives the SSH call (a `Start-Process` child is killed when the SSH
    session ends). It puts itself in a Job Object that is killed when the
    wrapper ends, then runs the agent in the host `checkout` through `cmd.exe`
    with stdin from `prompt.md` and stdout and stderr in `output.log`, as the
    agent writes them (Node-based runtimes write UTF-8; the wrapper's own
    `EXIT N` line is UTF-8). Processes the agent leaves behind end with it.
    The executable is looked up on PATH only (never in the checkout) and run
    by full path. The prompt never appears on a command line: `-p` is
    appended without an argument (Claude Code reads the prompt from stdin),
    or an argument containing `{prompt_file}` gets the prompt's path and no
    `-p` is added. A host's `agent_command` and `agent_report` override
    `[remote_agents]` when the executables differ there. Both argv lists are
    joined into command lines that cmd.exe may parse, so their arguments may
    not contain `"%!^&|<>`; run a `.ps1` shim through its `.cmd` twin or
    `powershell.exe -File`. The start report is not waited for; the end
    report gets 30 seconds. `shell_prefix` does not apply.
  - **One run per name.** A start holds `start.lock` in the agent folder and
    the wrapper holds `run.lock` for its lifetime; a second start of a
    running or starting agent is refused. A start that fails is listed as
    `failed`.
  - `remote-agent HOST --list` prints each agent with `running`,
    `exited RC`, `stopped`, `lost` (its process is gone without an exit
    code, e.g. after a reboot), `failed`, `starting` or `untrusted (...)`.
  - `remote-agent HOST NAME --log [--lines N]` prints the end of its log.
  - `remote-agent HOST NAME --stop` opens a handle to the recorded wrapper
    pid, checks through that handle that it still has the recorded start
    time, runs that agent's `run.ps1` and belongs to the user, and
    terminates it through the same handle (Windows does not reuse a pid
    while a handle is open). The wrapper's job then kills the rest of the
    agent's tree, including orphans whose parent already exited. A missing,
    zero or unverified pid is refused, and nothing is killed by name. On the
    controller every kill goes through one function, `kill_tree`. Checks
    that do not kill open processes with query rights only. A process the
    agent starts outside its job (through WMI, `schtasks`, a service, or
    `pr-coord remote-agent` itself) survives `--stop`.
  - Names are limited to letters, digits, `_` and `-` (64 characters, not
    a device name such as `CON`) and checked again on the host.
  - `agent-env HOST` on Windows starts a detached holder process for the
    host's `agent_env_session`. The controller reads the first line of its
    stdin and passes it, without decoding or logging it, through SSH stdin to
    the host, where it is handed to the holder over a named pipe. The client checks
    that the pipe server is the recorded holder (pid, start time and user,
    through one handle); the holder answers only clients of the same user at
    High integrity on the same machine, judged from the client's own token by
    impersonation (SMB clients are refused; the
    NETWORK group cannot simply be denied, because OpenSSH key logons are
    network logons). The holder keeps the
    credential only in its own process environment as `GH_TOKEN` and
    `COPILOT_GITHUB_TOKEN` and starts that host's agents, which inherit it.
    It is never written to a file, the registry, a log or a command line. A
    second `agent-env` reports that the holder is already running (it takes
    a credential only once);
    `agent-env HOST --stop` stops the holder (verified like an agent) and is
    how to replace the credential. Running agents keep their environment.
    With `agent_env_session` set, new agents need the holder.
  - **Limits.** Remote logons (SSH, WMI) of an administrator get the full
    administrator token, so agents run elevated; no interactive desktop
    session is needed or used. Agents and the holder do not survive a
    reboot or a sign-out of all sessions: they show as `lost`, and
    `agent-env` must be piped again. Any elevated process of the same user,
    and any administrator, can read the holder's and agents' memory and
    environment, and memory can be paged to disk. Non-elevated processes of
    the user cannot use the holder or change agent folders, but the High
    label protects only those: it does not protect the agent and reporter
    programs on PATH (e.g. a user npm folder or `~\.local\bin`) nor the
    checkout's git hooks and build scripts. A non-elevated process that
    changes those gets elevated execution, with the credential, at the next
    agent start. UAC is not a formal security boundary. Host policy can still record things the tool
    does not: PowerShell module logging or script block logging may capture
    script text and command parameters (the credential is only ever an
    argument of .NET method calls, which those do not record, but review such
    policies on hosts that receive credentials); the same account can open named pipes over SMB
    from another machine; the holder refuses such clients. A runtime that
    keeps its own sign-in (e.g. Claude Code's credentials file in the
    user's profile) needs no `agent-env`.
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
   use `pr-coord --project PROJECT events --wait` for the next batch. Use
   dashboard-only mode to inspect migrated data before enabling any mutations.

For browser verification on a populated copy, run `browser_smoke.py --base
http://127.0.0.1:8787 --out out/browser.json`. This optional test needs Chromium
and `websocket-client`; the regular tool and test suite do not.
