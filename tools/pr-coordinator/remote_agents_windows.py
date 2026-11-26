"""Launch, list, tail and stop configured implementers on Windows hosts.

There is no tmux: a static PowerShell wrapper (run.ps1) in a per-agent
directory is started through WMI (Win32_Process.Create), so it outlives the SSH
call that started it. Prompt, runbook and launch settings travel as files; the
only values put on a command line are validated names and paths.
"""

import base64
import json
import re
import subprocess
import sys
from pathlib import Path

from remote import record, remote
from runtime import P

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
RESERVED = re.compile(r"(?i)(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])\Z")
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
# A root is a drive or home-relative path; nothing a cmd.exe line could interpret.
ROOT = re.compile(r"(~[\\/]|[A-Za-z]:[\\/])[^\"%!^&|<>*?\r\n]*\Z")
# The agent argv is joined into one cmd.exe line (for the redirections), so it
# must not contain characters cmd.exe would interpret even inside quotes.
CMD_UNSAFE = re.compile(r"[\"%!^&|<>\r\n]")
LAUNCH_FILES = {"prompt.md", "launch.json", "run.ps1"}
STATE_FILES = ("output.log", "exit.code", "pid.json", "stopped.json")

# Shared helpers prepended to every remote script. Values arrive as $args
# (single-quoted by remote()) or base64 JSON on stdin, never spliced into code.
COMMON = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$utf8 = New-Object Text.UTF8Encoding $false
function Fail([string]$m) { [Console]::Error.WriteLine($m); [Environment]::Exit(1) }
function Out-Json($o) {
    $j = ConvertTo-Json -InputObject $o -Compress -Depth 6
    [Console]::Out.WriteLine([Convert]::ToBase64String($utf8.GetBytes($j)))
}
function Test-Name([string]$n) {
    if ($n -cnotmatch '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$' -or
        $n -match '^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])$') { Fail 'invalid agent name' }
}
function Resolve-Root([string]$r) {
    if ($r -notmatch '^(~[\\/]|[A-Za-z]:[\\/])[^"%!^&|<>*?\r\n]*$' -or
        $r -match '(^|[\\/])\.\.([\\/]|$)') { Fail 'invalid agent root' }
    if ($r -match '^~[\\/]') { $r = Join-Path $HOME $r.Substring(2) }
    $r.TrimEnd('\', '/')
}
function Read-Json([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try { [IO.File]::ReadAllText($path, $utf8) | ConvertFrom-Json } catch { $null }
}
function Get-Created([int]$id) {
    if ($id -le 0) { return [int64]0 }
    try { [Diagnostics.Process]::GetProcessById($id).StartTime.ToUniversalTime().Ticks } catch { [int64]0 }
}
# Facts about a recorded pid: its current start time and whether its command
# line runs the expected script. The caller decides from both.
function Get-Facts($rec, [string]$script) {
    $id = 0; $created = [int64]0
    if ($rec) { try { $id = [int]$rec.pid; $created = [int64]$rec.created } catch {} }
    $live = Get-Created $id
    $match = $false
    if ($id -gt 0 -and $live -ne 0) {
        $p = Get-CimInstance Win32_Process -Filter ("ProcessId = " + $id)
        $needle = ('"' + $script + '"').ToLowerInvariant()
        $match = [bool]($p -and $p.CommandLine -and $p.CommandLine.ToLowerInvariant().Contains($needle))
    }
    [ordered]@{ pid = $id; created = $created; live_created = $live; cmdline_match = $match }
}
function Test-Verified($f) {
    $f.pid -gt 0 -and $f.created -ne 0 -and $f.live_created -eq $f.created -and $f.cmdline_match
}
function Get-Sid { [Security.Principal.WindowsIdentity]::GetCurrent().User }
function Protect-Dir([string]$dir) {
    # Like umask 077: only this user and SYSTEM, no inherited entries.
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in @((Get-Sid), (New-Object Security.Principal.SecurityIdentifier 'S-1-5-18'))) {
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
            $sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    }
    Set-Acl -LiteralPath $dir -AclObject $acl
}
function Write-Text([string]$path, [string]$text) { [IO.File]::WriteAllText($path, $text, $utf8) }
function Start-Detached([string]$script, [string]$cwd) {
    $line = 'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $script + '"'
    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = $line; CurrentDirectory = $cwd }
    if ($r.ReturnValue -ne 0) { Fail ('process creation failed: ' + $r.ReturnValue) }
}
$pipeApi = $null
function Get-PipeServerPid($stream) {
    if (-not $script:pipeApi) {
        $script:pipeApi = Add-Type -PassThru -Name PipePid -Namespace PrCoord -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError = true)]
public static extern bool GetNamedPipeServerProcessId(Microsoft.Win32.SafeHandles.SafePipeHandle pipe, out uint pid);
'@
    }
    [uint32]$server = 0
    if (-not $script:pipeApi::GetNamedPipeServerProcessId($stream.SafePipeHandle, [ref]$server)) { return 0 }
    [int]$server
}
# Send one line to a verified environment holder and return its reply.
function Send-Holder([string]$session, $holder, [string]$line, [int]$wait) {
    $client = New-Object IO.Pipes.NamedPipeClientStream('.', ('pr-coord-env-' + $session), 'InOut')
    $deadline = (Get-Date).AddSeconds($wait)
    while ($true) {
        try { $client.Connect(1000); break } catch {
            if ((Get-Date) -gt $deadline) { $client.Dispose(); Fail 'environment session is not answering' }
            Start-Sleep -Milliseconds 300
        }
    }
    try {
        if ((Get-PipeServerPid $client) -ne $holder.pid) { Fail 'environment pipe is not served by the recorded holder' }
        $w = New-Object IO.StreamWriter($client, $utf8); $w.AutoFlush = $true
        $r = New-Object IO.StreamReader($client, $utf8)
        $w.WriteLine($line)
        $r.ReadLine()
    } finally { $client.Dispose() }
}
function Get-Holder([string]$root, [string]$session) {
    $dir = Join-Path $root ('.env-' + $session)
    $rec = Read-Json (Join-Path $dir 'holder-pid.json')
    $f = Get-Facts $rec (Join-Path $dir 'holder.ps1')
    if (Test-Verified $f) { $f } else { $null }
}
"""

# Static wrapper, written into each agent directory. It reads launch.json and
# runs the agent through cmd.exe so stdin comes from prompt.md and stdout and
# stderr go, byte for byte, to output.log.
RUN = r"""
$ErrorActionPreference = 'Stop'
$utf8 = New-Object Text.UTF8Encoding $false
$dir = $PSScriptRoot
function Write-Text([string]$name, [string]$text) {
    $tmp = Join-Path $dir ('.' + $name + '.tmp')
    [IO.File]::WriteAllText($tmp, $text, $utf8)
    Move-Item -LiteralPath $tmp -Destination (Join-Path $dir $name) -Force
}
$me = [Diagnostics.Process]::GetCurrentProcess()
Write-Text 'pid.json' (ConvertTo-Json -Compress -InputObject ([ordered]@{
    pid = $me.Id; created = $me.StartTime.ToUniversalTime().Ticks
    started = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }))
$log = Join-Path $dir 'output.log'
$prompt = Join-Path $dir 'prompt.md'
$spec = [IO.File]::ReadAllText((Join-Path $dir 'launch.json'), $utf8) | ConvertFrom-Json
function Invoke-Report($argv, [string[]]$extra) {
    $a = @($argv)
    if ($a.Count -eq 0) { return }
    $rest = @(); if ($a.Count -gt 1) { $rest = $a[1..($a.Count - 1)] }
    try { & $a[0] @rest @extra *> $null } catch {}
}
function Quote-Arg([string]$s) {
    if ($s -match '["%!^&|<>\r\n]') { throw 'agent argument contains a cmd.exe metacharacter' }
    if ($s -ne '' -and $s -notmatch '\s') { return $s }
    '"' + ($s -replace '(\\+)$', '$1$1') + '"'
}
Invoke-Report $spec.working @()
$rc = 1
try {
    $line = (@($spec.command) | ForEach-Object { Quote-Arg ($_.Replace('{prompt_file}', $prompt)) }) -join ' '
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = Join-Path $env:SystemRoot 'System32\cmd.exe'
    $psi.Arguments = '/d /s /c "' + $line + ' < "' + $prompt + '" > "' + $log + '" 2>&1"'
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.WorkingDirectory = $spec.cwd
    $p = [Diagnostics.Process]::Start($psi)
    $p.WaitForExit()
    $rc = $p.ExitCode
} catch {
    [IO.File]::AppendAllText($log, ('wrapper error: ' + $_.Exception.Message + "`r`n"), $utf8)
}
[IO.File]::AppendAllText($log, ('EXIT ' + $rc + "`r`n"), $utf8)
Write-Text 'exit.code' ([string]$rc)
Invoke-Report $spec.done @(('remote agent exited ' + $rc + '; log ' + $log + ' on ' + $env:COMPUTERNAME))
exit $rc
"""

START = COMMON + r"""
$root = Resolve-Root $args[0]; $name = $args[1]; $session = $args[2]
Test-Name $name
$payload = $utf8.GetString([Convert]::FromBase64String([Console]::In.ReadToEnd().Trim())) | ConvertFrom-Json
$holder = $null
if ($session) {
    Test-Name $session
    $holder = Get-Holder $root $session
    if (-not $holder) { Fail 'required environment session is not running' }
}
$dir = Join-Path $root $name
if (Test-Path -LiteralPath $dir) {
    $f = Get-Facts (Read-Json (Join-Path $dir 'pid.json')) (Join-Path $dir 'run.ps1')
    if (Test-Verified $f) { Fail 'agent is already running' }
    foreach ($old in @('output.log', 'exit.code', 'pid.json', 'stopped.json')) {
        Remove-Item -LiteralPath (Join-Path $dir $old) -Force -ErrorAction SilentlyContinue
    }
} else {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    Protect-Dir $dir
}
foreach ($p in $payload.files.PSObject.Properties) {
    if ($p.Name -cnotmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$') { Fail 'invalid launch file name' }
    Write-Text (Join-Path $dir $p.Name) ([string]$p.Value)
}
$script = Join-Path $dir 'run.ps1'
if ($holder) {
    $reply = Send-Holder $session $holder ('launch ' + $name) 10
    if ($reply -notlike 'started *') { Fail ('environment session refused the launch: ' + $reply) }
} else {
    Start-Detached $script $dir
}
$deadline = (Get-Date).AddSeconds(20)
while (-not (Test-Path -LiteralPath (Join-Path $dir 'pid.json'))) {
    if ((Get-Date) -gt $deadline) { Fail 'agent wrapper did not start' }
    Start-Sleep -Milliseconds 200
}
Out-Json (Read-Json (Join-Path $dir 'pid.json'))
"""

LIST = COMMON + r"""
$root = Resolve-Root $args[0]; $session = $args[1]
$agents = @()
if (Test-Path -LiteralPath $root) {
    foreach ($d in Get-ChildItem -LiteralPath $root -Directory) {
        if ($d.Name -cnotmatch '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$') { continue }
        if (-not (Test-Path -LiteralPath (Join-Path $d.FullName 'launch.json'))) { continue }
        $rec = Read-Json (Join-Path $d.FullName 'pid.json')
        $f = Get-Facts $rec (Join-Path $d.FullName 'run.ps1')
        $rc = $null
        $rcFile = Join-Path $d.FullName 'exit.code'
        if (Test-Path -LiteralPath $rcFile) { $rc = ([IO.File]::ReadAllText($rcFile)).Trim() }
        $f['name'] = $d.Name
        $f['started'] = if ($rec) { [string]$rec.started } else { '' }
        $f['rc'] = $rc
        $f['stopped'] = Test-Path -LiteralPath (Join-Path $d.FullName 'stopped.json')
        $agents += [pscustomobject]$f
    }
}
$envState = ''
if ($session) { Test-Name $session; $envState = if (Get-Holder $root $session) { 'running' } else { 'not running' } }
Out-Json ([ordered]@{ agents = @($agents); env = $envState })
"""

TAIL = COMMON + r"""
$root = Resolve-Root $args[0]; $name = $args[1]; $lines = [int]$args[2]
Test-Name $name
$log = Join-Path (Join-Path $root $name) 'output.log'
if (-not (Test-Path -LiteralPath $log)) { Fail 'no log for this agent' }
$text = (Get-Content -LiteralPath $log -Tail $lines -Encoding UTF8) -join "`n"
Out-Json ([ordered]@{ text = $text })
"""

# Kills the process tree under a pid only after re-checking, on the host, that
# the pid is still the one recorded for this agent (start time and command line).
KILL = COMMON + r"""
$root = Resolve-Root $args[0]; $kind = $args[1]; $name = $args[2]
$expectPid = [int]$args[3]; $expectCreated = [int64]$args[4]
Test-Name $name
if ($kind -eq 'agent') {
    $dir = Join-Path $root $name; $recFile = Join-Path $dir 'pid.json'; $script = Join-Path $dir 'run.ps1'; $tree = $true
} elseif ($kind -eq 'holder') {
    $dir = Join-Path $root ('.env-' + $name); $recFile = Join-Path $dir 'holder-pid.json'; $script = Join-Path $dir 'holder.ps1'; $tree = $false
} else { Fail 'invalid kill kind' }
$f = Get-Facts (Read-Json $recFile) $script
if ($expectPid -le 0 -or $f.pid -ne $expectPid -or $f.created -ne $expectCreated -or -not (Test-Verified $f)) {
    Fail 'refusing to stop: the recorded process is not verified as this agent'
}
$all = @(Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId, CreationDate)
$byId = @{}; foreach ($p in $all) { $byId[[int]$p.ProcessId] = $p }
$rootProc = $byId[$expectPid]
if (-not $rootProc) { Fail 'process exited before it could be stopped' }
$victims = New-Object Collections.ArrayList
$queue = New-Object Collections.Queue
$queue.Enqueue($rootProc)
while ($queue.Count -gt 0) {
    $p = $queue.Dequeue(); [void]$victims.Add($p)
    if (-not $tree) { continue }
    foreach ($c in $all) {
        # A child must have started after its parent, or its parent pid was reused.
        if ([int]$c.ParentProcessId -eq [int]$p.ProcessId -and [int]$c.ProcessId -ne [int]$p.ProcessId -and
            $c.CreationDate -ge $p.CreationDate -and -not $victims.Contains($c)) { $queue.Enqueue($c) }
    }
}
$killed = @()
foreach ($v in $victims) {
    $id = [int]$v.ProcessId
    $now = Get-CimInstance Win32_Process -Filter ("ProcessId = " + $id)
    if (-not $now -or $now.CreationDate -ne $v.CreationDate) { continue }
    try { Stop-Process -Id $id -Force; $killed += $id } catch {}
}
if ($kind -eq 'agent') {
    Write-Text (Join-Path $dir 'stopped.json') (ConvertTo-Json -Compress -InputObject ([ordered]@{
        at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'); pids = @($killed) }))
}
Out-Json ([ordered]@{ killed = @($killed) })
"""

# Environment holder: a detached process that keeps a credential only in its
# own memory and environment, and starts agent wrappers (which inherit it) on
# request over a named pipe restricted to this user.
HOLDER = r"""
$ErrorActionPreference = 'Stop'
$utf8 = New-Object Text.UTF8Encoding $false
$dir = $PSScriptRoot
$cfg = [IO.File]::ReadAllText((Join-Path $dir 'holder.json'), $utf8) | ConvertFrom-Json
$me = [Diagnostics.Process]::GetCurrentProcess()
$sec = New-Object IO.Pipes.PipeSecurity
$sec.AddAccessRule((New-Object IO.Pipes.PipeAccessRule(
    [Security.Principal.WindowsIdentity]::GetCurrent().User, 'FullControl', 'Allow')))
$pipeName = 'pr-coord-env-' + $cfg.session
function New-Server {
    New-Object IO.Pipes.NamedPipeServerStream($pipeName, 'InOut', 1, 'Byte', 'None', 4096, 4096, $sec)
}
$server = New-Server
$tmp = Join-Path $dir '.holder-pid.json.tmp'
[IO.File]::WriteAllText($tmp, (ConvertTo-Json -Compress -InputObject ([ordered]@{
    pid = $me.Id; created = $me.StartTime.ToUniversalTime().Ticks })), $utf8)
Move-Item -LiteralPath $tmp -Destination (Join-Path $dir 'holder-pid.json') -Force
$server.WaitForConnection()
$r = New-Object IO.StreamReader($server, $utf8); $w = New-Object IO.StreamWriter($server, $utf8); $w.AutoFlush = $true
$token = $r.ReadLine()
if ($token -cnotmatch '^[A-Za-z0-9_-]+$') { $w.WriteLine('invalid'); exit 1 }
[Environment]::SetEnvironmentVariable('GH_TOKEN', $token, 'Process')
[Environment]::SetEnvironmentVariable('COPILOT_GITHUB_TOKEN', $token, 'Process')
$token = $null
$w.WriteLine('ok')
$server.Dispose()
while ($true) {
    $server = New-Server
    try {
        $server.WaitForConnection()
        $r = New-Object IO.StreamReader($server, $utf8); $w = New-Object IO.StreamWriter($server, $utf8); $w.AutoFlush = $true
        $line = $r.ReadLine()
        if ($line -cmatch '^launch ([A-Za-z0-9][A-Za-z0-9_-]{0,63})$') {
            $agent = Join-Path $cfg.root $Matches[1]
            $script = Join-Path $agent 'run.ps1'
            if (Test-Path -LiteralPath $script) {
                $psi = New-Object Diagnostics.ProcessStartInfo
                $psi.FileName = 'powershell.exe'
                $psi.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $script + '"'
                $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true; $psi.WorkingDirectory = $agent
                $w.WriteLine('started ' + [Diagnostics.Process]::Start($psi).Id)
            } else { $w.WriteLine('missing') }
        } elseif ($line -eq 'ping') { $w.WriteLine('pong') } else { $w.WriteLine('invalid') }
    } catch { try { $w.WriteLine('error') } catch {} } finally { $server.Dispose() }
}
"""

# Writes the holder's files (no secrets) before ENV starts it.
ENV_PREPARE = COMMON + r"""
$root = Resolve-Root $args[0]; $session = $args[1]
Test-Name $session
if (Get-Holder $root $session) { Out-Json ([ordered]@{ running = $true }); return }
$dir = Join-Path $root ('.env-' + $session)
if (-not (Test-Path -LiteralPath $dir)) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    Protect-Dir $dir
}
Remove-Item -LiteralPath (Join-Path $dir 'holder-pid.json') -Force -ErrorAction SilentlyContinue
Write-Text (Join-Path $dir 'holder.json') (ConvertTo-Json -Compress -InputObject ([ordered]@{ root = $root; session = $session }))
Write-Text (Join-Path $dir 'holder.ps1') $utf8.GetString([Convert]::FromBase64String([Console]::In.ReadToEnd().Trim()))
Out-Json ([ordered]@{ running = $false })
"""

# Reads the credential from stdin (piped through from the operator) and hands
# it to a freshly started holder over its pipe.
ENV = COMMON + r"""
$root = Resolve-Root $args[0]; $session = $args[1]
Test-Name $session
if (Get-Holder $root $session) { 'environment session already running'; return }
$token = [Console]::In.ReadLine()
if (-not $token -or $token -cnotmatch '^[A-Za-z0-9_-]+$') { Fail 'invalid credential input' }
$dir = Join-Path $root ('.env-' + $session)
Start-Detached (Join-Path $dir 'holder.ps1') $dir
$deadline = (Get-Date).AddSeconds(20)
$holder = $null
while (-not $holder) {
    if ((Get-Date) -gt $deadline) { Fail 'environment holder did not start' }
    Start-Sleep -Milliseconds 200
    $holder = Get-Holder $root $session
}
$reply = Send-Holder $session $holder $token 10
$token = $null
if ($reply -ne 'ok') { Fail 'environment holder rejected the credential' }
'environment session started'
"""

HOLDER_FACTS = COMMON + r"""
$root = Resolve-Root $args[0]; $session = $args[1]; Test-Name $session
$dir = Join-Path $root ('.env-' + $session)
Out-Json (Get-Facts (Read-Json (Join-Path $dir 'holder-pid.json')) (Join-Path $dir 'holder.ps1'))
"""


class WindowsAgentError(RuntimeError):
    pass


def agent_root(host):
    spec = P.hosts[host]
    root = spec.get("agent_root") or (
        spec.get("checkout", "").rstrip("\\/") + "\\out\\agents"
        if spec.get("checkout")
        else ""
    )
    if (
        not isinstance(root, str)
        or not ROOT.fullmatch(root)
        or re.search(r"(^|[\\/])\.\.([\\/]|$)", root)
    ):
        raise WindowsAgentError(
            "set hosts.<host>.agent_root (or checkout) to an absolute drive or ~\\ path"
            " without cmd.exe metacharacters"
        )
    return root


def valid_name(name):
    return bool(NAME.fullmatch(name or "")) and not RESERVED.fullmatch(name)


def check_command(argv, label):
    if not argv:
        raise WindowsAgentError(f"{label} is empty")
    for arg in argv:
        if CMD_UNSAFE.search(arg):
            raise WindowsAgentError(
                f"{label} contains a character cmd.exe would interpret: {arg!r}"
            )
    return argv


def check_reporter(argv, label):
    for arg in argv:
        if not arg or '"' in arg or "\r" in arg or "\n" in arg:
            raise WindowsAgentError(f"{label} has an empty or quoted argument")
    return argv


def agent_argv(host, name):
    spec = P.hosts[host]
    if "agent_command" in spec:
        argv = P.expand(
            spec["agent_command"],
            label=f"hosts.{host}.agent_command",
            host=host,
            name=name,
        )
    else:
        argv = P.command("remote_agents", "command", host=host, name=name)
    argv = check_command(argv, "agent command")
    # The prompt always arrives on stdin; a runtime that wants a path gets {prompt_file}.
    return argv if any("{prompt_file}" in a for a in argv) else [*argv, "-p"]


def reporter_argv(host, name):
    spec = P.hosts[host]
    if "agent_report" in spec:
        return P.expand(
            spec["agent_report"],
            label=f"hosts.{host}.agent_report",
            host=host,
            name=name,
        )
    return P.command("remote_agents", "report", host=host, name=name)


def call(host, script, *args, data="", timeout=60):
    """Run one remote script; return its output. data=None passes this
    process's stdin through (an operator-piped credential never enters Python)."""
    stdin = {"input": data} if data is not None else {}
    result = remote(
        host,
        ["powershell-script", script, *map(str, args)],
        **stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=timeout,
    )
    errors = stderr_lines(result.stderr or "")
    if result.returncode:
        raise WindowsAgentError(
            "; ".join(errors[-3:]) or f"remote command failed on {host}"
        )
    return (result.stdout or "").strip()


def stderr_lines(text):
    """Readable error lines: PowerShell wraps stderr in CLIXML when its input
    is redirected; drop SSH banner lines."""
    if "#< CLIXML" in text:
        text = "".join(re.findall(r'<S S="Error">(.*?)</S>', text, re.S))
        text = text.replace("_x000D__x000A_", "\n").replace("_x000A_", "\n")
        text = (
            text.replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&amp;", "&")
        )
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.startswith("** ")
    ]


def decode(output):
    line = output.splitlines()[-1] if output else ""
    try:
        return json.loads(base64.b64decode(line, validate=True).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise WindowsAgentError("unexpected reply from the host") from exc


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def verified(rec):
    """True only for a positive recorded pid whose live process has the
    recorded start time and runs this agent's wrapper."""
    try:
        pid, created, live = (
            int(rec.get("pid") or 0),
            int(rec.get("created") or 0),
            int(rec.get("live_created") or 0),
        )
    except (TypeError, ValueError):
        return False
    return (
        pid > 0
        and created != 0
        and live == created
        and rec.get("cmdline_match") is True
    )


def state(rec):
    if rec.get("stopped"):
        return "stopped"
    if rec.get("rc") not in (None, ""):
        return f"exited {rec['rc']}"
    if verified(rec):
        return "running"
    if int(rec.get("pid") or 0) > 0:
        return "lost"  # e.g. killed outside pr-coord or by a reboot
    return "starting"


def kill_tree(host, root, kind, name, pid, created):
    """The only path to a remote kill; it re-verifies the pid on the host."""
    return decode(call(host, KILL, root, kind, name, pid, created, timeout=90))


def list_agents(host, root, session=""):
    return decode(call(host, LIST, root, session, timeout=60))


def start(host, name, prompt, session):
    spec = P.hosts[host]
    root = agent_root(host)
    argv = agent_argv(host, name)
    reporter = reporter_argv(host, name)
    if not reporter or not spec.get("checkout"):
        raise WindowsAgentError(
            "configure host checkout, remote_agents.command and remote_agents.report"
        )
    check_reporter(reporter, "reporter")
    files = {"prompt.md": prompt.read_text(encoding="utf-8")}
    if runbook := P.get("remote_agents", "runbook"):
        base = Path(runbook).name
        if base in LAUNCH_FILES or base in STATE_FILES or not FILE_NAME.fullmatch(base):
            raise WindowsAgentError("runbook filename conflicts with launch files")
        files[base] = Path(runbook).expanduser().read_text(encoding="utf-8")
    files["launch.json"] = json.dumps(
        {
            "cwd": spec["checkout"],
            "command": argv,
            "working": [
                *reporter,
                "agent",
                name,
                "working",
                "--machine",
                host,
                "--task",
                "started",
            ],
            "done": [*reporter, "agent", name, "done", "--task"],
        }
    )
    files["run.ps1"] = RUN
    payload = base64.b64encode(json.dumps({"files": files}).encode()).decode()
    record("agent", name, "starting", "--machine", host, "--task", "launching")
    try:
        rec = decode(
            call(host, START, root, name, session, data=payload + "\n", timeout=90)
        )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        record("agent", name, "error", "--task", "remote launch failed")
        raise
    print(f"started agent {name} on {host} (pid {rec.get('pid')})")
    return 0


def stop(host, name, session=""):
    root = agent_root(host)
    rec = next(
        (
            a
            for a in as_list(list_agents(host, root, session).get("agents"))
            if a.get("name") == name
        ),
        None,
    )
    if rec is None:
        raise WindowsAgentError(f"no agent {name} on {host}")
    current = state(rec)
    if current != "running" or not verified(rec):
        raise WindowsAgentError(
            f"agent {name} is not running ({current}); nothing stopped"
        )
    killed = kill_tree(host, root, "agent", name, int(rec["pid"]), int(rec["created"]))
    pids = as_list(killed.get("killed"))
    print(f"stopped agent {name} on {host} ({len(pids)} processes)")
    record("agent", name, "done", "--task", "stopped by operator")
    return 0


def main(command, args, parser):
    host = P.hosts[args.host]
    session = host.get("agent_env_session", "")
    if session and not valid_name(session):
        parser.error("invalid environment session name")
    try:
        if command == "agent-env":
            root = agent_root(args.host)
            if args.stop:
                if not session:
                    parser.error("this host has no agent_env_session")
                holder = decode(call(args.host, HOLDER_FACTS, root, session))
                if not verified(holder):
                    print("environment session is not running")
                    return 0
                kill_tree(
                    args.host,
                    root,
                    "holder",
                    session,
                    int(holder["pid"]),
                    int(holder["created"]),
                )
                print(
                    "environment session stopped; running agents keep their environment"
                )
                return 0
            if not session or sys.stdin.isatty():
                parser.error(
                    "configure agent_env_session and provide credential input on stdin"
                )
            holder = base64.b64encode(HOLDER.encode()).decode()
            if decode(
                call(args.host, ENV_PREPARE, root, session, data=holder + "\n")
            ).get("running"):
                print("environment session already running")
                return 0
            print(call(args.host, ENV, root, session, data=None, timeout=90))
            return 0
        if args.list:
            data = list_agents(args.host, agent_root(args.host), session)
            for rec in as_list(data.get("agents")):
                print(f"{rec.get('name')}\t{state(rec)}\t{rec.get('started') or '-'}")
            if session:
                print(f"environment session {session}: {data.get('env')}")
            return 0
        if not args.name or not valid_name(args.name):
            parser.error("invalid agent name")
        if args.log:
            if not 1 <= args.lines <= 10000:
                parser.error("--lines must be between 1 and 10000")
            data = decode(
                call(args.host, TAIL, agent_root(args.host), args.name, args.lines)
            )
            print(data.get("text", ""))
            return 0
        if args.stop:
            return stop(args.host, args.name, session)
        if not args.prompt:
            parser.error("provide PROMPT_FILE, or --list, --log or --stop")
        return start(args.host, args.name, args.prompt, session)
    except WindowsAgentError as exc:
        parser.error(str(exc))
