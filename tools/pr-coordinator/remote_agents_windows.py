"""Launch, list, tail and stop configured implementers on Windows hosts.

There is no tmux. A static PowerShell wrapper (run.ps1) in a private per-agent
directory is started through WMI (Win32_Process.Create), so it outlives the SSH
call. The wrapper puts itself in a Job Object that is killed when the wrapper
ends, so stopping it ends the whole agent tree, orphans included.

Every remote call runs a small fixed bootstrap (`powershell -EncodedCommand`)
that reads one base64 JSON line from stdin: the script and its arguments as
data. No configured or user value is ever spliced into script text.
"""

import base64
import json
import re
import subprocess
import sys
from pathlib import Path

from remote import record
from runtime import P

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
RESERVED = re.compile(r"(?i)(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])\Z")
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
# Conservative ASCII paths only: a drive or home-relative root, nothing a shell
# or cmd.exe could interpret. Fails closed on anything else (e.g. Unicode quotes).
PATH = re.compile(r"(~[\\/]|[A-Za-z]:[\\/])[A-Za-z0-9 _.\\/-]*\Z")
DOTDOT = re.compile(r"(^|[\\/])\.\.([\\/]|$)")
# The agent and reporter argv are joined into a command line that cmd.exe may
# parse (the agent's redirections; .cmd shims), so they may not contain
# characters cmd.exe interprets even inside quotes.
CMD_UNSAFE = re.compile(r"[\"%!^&|<>\r\n]")
LAUNCH_FILES = {"prompt.md", "launch.json", "run.ps1"}
STATE_FILES = {
    "output.log",
    "output.1.log",
    "exit.code",
    "pid.json",
    "stopped.json",
    "failed.json",
    "run.lock",
    "start.lock",
}

# Native helpers, compiled on the host with Add-Type (C# 5, .NET Framework).
NATIVE = r"""
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Principal;
using Microsoft.Win32.SafeHandles;
namespace PrCoord {
public sealed class Proc : IDisposable {
    [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
    [DllImport("kernel32.dll")] static extern bool GetProcessTimes(IntPtr h, out long created, out long exited, out long kernel, out long user);
    [DllImport("kernel32.dll")] static extern bool TerminateProcess(IntPtr h, uint code);
    [DllImport("kernel32.dll")] static extern uint WaitForSingleObject(IntPtr h, uint ms);
    [DllImport("kernel32.dll")] static extern int GetCurrentProcessId();
    [DllImport("ntdll.dll")] static extern int NtQueryInformationProcess(IntPtr h, int cls, IntPtr buf, int len, out int ret);
    [DllImport("advapi32.dll")] static extern bool OpenProcessToken(IntPtr h, uint access, out IntPtr token);
    [DllImport("advapi32.dll")] static extern bool GetTokenInformation(IntPtr token, int cls, IntPtr buf, int len, out int ret);
    [DllImport("advapi32.dll")] static extern IntPtr GetSidSubAuthority(IntPtr sid, uint n);
    [DllImport("advapi32.dll")] static extern IntPtr GetSidSubAuthorityCount(IntPtr sid);
    IntPtr handle;
    public readonly int Id;
    Proc(IntPtr h, int id) { handle = h; Id = id; }
    // QUERY_LIMITED_INFORMATION | TERMINATE | SYNCHRONIZE. While this handle is
    // open Windows does not reuse the pid, so checks and kill see one process.
    public static Proc Open(int pid) {
        if (pid <= 0) return null;
        IntPtr h = OpenProcess(0x1000 | 0x0001 | 0x00100000, false, pid);
        return h == IntPtr.Zero ? null : new Proc(h, pid);
    }
    public static Proc Self() { return Open(GetCurrentProcessId()); }
    public long Created() {
        long c, e, k, u;
        return GetProcessTimes(handle, out c, out e, out k, out u) ? c : 0;
    }
    public bool Exited() { return WaitForSingleObject(handle, 0) == 0; }
    public bool WaitExit(int ms) { return WaitForSingleObject(handle, (uint)ms) == 0; }
    public bool Terminate() { return TerminateProcess(handle, 1); }
    public string CommandLine() {
        int len = 1 << 16;
        IntPtr buf = Marshal.AllocHGlobal(len);
        try {
            int ret;
            if (NtQueryInformationProcess(handle, 60, buf, len, out ret) != 0) return null;
            int bytes = (ushort)Marshal.ReadInt16(buf);
            IntPtr text = Marshal.ReadIntPtr(buf, IntPtr.Size);
            return bytes == 0 ? "" : Marshal.PtrToStringUni(text, bytes / 2);
        } finally { Marshal.FreeHGlobal(buf); }
    }
    IntPtr TokenInfo(int cls) {
        IntPtr token;
        if (!OpenProcessToken(handle, 0x0008, out token)) return IntPtr.Zero;
        try {
            int need;
            GetTokenInformation(token, cls, IntPtr.Zero, 0, out need);
            if (need <= 0) return IntPtr.Zero;
            IntPtr buf = Marshal.AllocHGlobal(need);
            if (GetTokenInformation(token, cls, buf, need, out need)) return buf;
            Marshal.FreeHGlobal(buf);
            return IntPtr.Zero;
        } finally { CloseHandle(token); }
    }
    public string User() {
        IntPtr buf = TokenInfo(1);
        if (buf == IntPtr.Zero) return null;
        try { return new SecurityIdentifier(Marshal.ReadIntPtr(buf)).Value; }
        finally { Marshal.FreeHGlobal(buf); }
    }
    public int Integrity() {
        IntPtr buf = TokenInfo(25);
        if (buf == IntPtr.Zero) return 0;
        try {
            IntPtr sid = Marshal.ReadIntPtr(buf);
            int n = Marshal.ReadByte(GetSidSubAuthorityCount(sid));
            return Marshal.ReadInt32(GetSidSubAuthority(sid, (uint)(n - 1)));
        } finally { Marshal.FreeHGlobal(buf); }
    }
    public void Dispose() {
        if (handle != IntPtr.Zero) { CloseHandle(handle); handle = IntPtr.Zero; }
    }
}
public static class Job {
    [StructLayout(LayoutKind.Sequential)] struct Basic {
        public long a; public long b; public uint LimitFlags; public UIntPtr c; public UIntPtr d;
        public uint e; public UIntPtr f; public uint g; public uint h;
    }
    [StructLayout(LayoutKind.Sequential)] struct Io { public ulong a, b, c, d, e, f; }
    [StructLayout(LayoutKind.Sequential)] struct Extended { public Basic Basic; public Io Io; public UIntPtr a, b, c, d; }
    [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr CreateJobObjectW(IntPtr sa, IntPtr name);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool SetInformationJobObject(IntPtr job, int cls, ref Extended info, int size);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
    static IntPtr held;
    // Puts this process, and everything it starts, in an unnamed job that is
    // killed when its only handle (held here, not inheritable) closes: when
    // this process exits or is terminated.
    public static void ContainSelf() {
        IntPtr job = CreateJobObjectW(IntPtr.Zero, IntPtr.Zero);
        if (job == IntPtr.Zero) throw new Win32Exception();
        Extended info = new Extended();
        info.Basic.LimitFlags = 0x2000;
        if (!SetInformationJobObject(job, 9, ref info, Marshal.SizeOf(typeof(Extended)))) throw new Win32Exception();
        if (!AssignProcessToJobObject(job, GetCurrentProcess())) throw new Win32Exception();
        held = job;
    }
}
public static class Pipe {
    [DllImport("kernel32.dll")] static extern bool GetNamedPipeServerProcessId(SafePipeHandle pipe, out uint pid);
    [DllImport("kernel32.dll")] static extern bool GetNamedPipeClientProcessId(SafePipeHandle pipe, out uint pid);
    public static int Server(SafePipeHandle p) { uint id; return GetNamedPipeServerProcessId(p, out id) ? (int)id : 0; }
    public static int Client(SafePipeHandle p) { uint id; return GetNamedPipeClientProcessId(p, out id) ? (int)id : 0; }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern bool GetNamedPipeClientComputerNameW(SafePipeHandle pipe, System.Text.StringBuilder name, uint len);
    // A local client has no computer name (ERROR_PIPE_LOCAL); an SMB client does.
    public static bool Remote(SafePipeHandle p) { return GetNamedPipeClientComputerNameW(p, new System.Text.StringBuilder(256), 512); }
}
public static class Dir {
    [StructLayout(LayoutKind.Sequential)] struct SA { public int Length; public IntPtr Descriptor; public bool Inherit; }
    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern bool ConvertStringSecurityDescriptorToSecurityDescriptorW(string sddl, int rev, out IntPtr sd, IntPtr size);
    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern bool ConvertSecurityDescriptorToStringSecurityDescriptorW(IntPtr sd, int rev, int info, out IntPtr text, IntPtr size);
    [DllImport("advapi32.dll", CharSet = CharSet.Unicode)]
    static extern int GetNamedSecurityInfoW(string name, int type, int info, IntPtr o, IntPtr g, IntPtr d, IntPtr s, out IntPtr sd);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)] static extern bool CreateDirectoryW(string path, ref SA sa);
    [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr p);
    // Owner this user; only this user, SYSTEM and Administrators; no inherited
    // entries; High mandatory label with no-write-up and no-read-up.
    public static string PrivateSddl(string user) {
        return "O:" + user + "D:P(A;OICI;FA;;;" + user + ")(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)S:(ML;OICI;NWNR;;;HI)";
    }
    // Creates the directory with that descriptor in one call; false if it exists.
    public static bool Create(string path, string user) {
        IntPtr sd;
        if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(PrivateSddl(user), 1, out sd, IntPtr.Zero)) throw new Win32Exception();
        try {
            SA sa = new SA();
            sa.Length = Marshal.SizeOf(typeof(SA));
            sa.Descriptor = sd;
            if (CreateDirectoryW(path, ref sa)) return true;
            int err = Marshal.GetLastWin32Error();
            if (err == 183) return false;
            throw new Win32Exception(err);
        } finally { LocalFree(sd); }
    }
    // Owner, DACL and mandatory label as SDDL.
    public static string Sddl(string path) {
        IntPtr sd;
        int info = 0x1 | 0x4 | 0x10;
        int err = GetNamedSecurityInfoW(path, 1, info, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, out sd);
        if (err != 0) throw new Win32Exception(err);
        try {
            IntPtr text;
            if (!ConvertSecurityDescriptorToStringSecurityDescriptorW(sd, 1, info, out text, IntPtr.Zero)) throw new Win32Exception();
            try { return Marshal.PtrToStringUni(text); } finally { LocalFree(text); }
        } finally { LocalFree(sd); }
    }
}
}
"""

# PowerShell helpers shared by the remote scripts and the holder. They use
# $me (this user's SID string) and $utf8, and Fail, defined by the caller.
HELPERS = r"""
$ProgressPreference = 'SilentlyContinue'
$utf8 = New-Object Text.UTF8Encoding $false
if (-not ('PrCoord.Proc' -as [type])) { Add-Type -TypeDefinition $native }
$me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$trusted = @($me, 'S-1-5-18', 'S-1-5-32-544')
function Test-Name([string]$n) {
    if ($n -cnotmatch '\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\z' -or
        $n -match '\A(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])\z') { Fail 'invalid agent name' }
}
function Resolve-Root([string]$r) {
    if ($r -cnotmatch '\A(~[\\/]|[A-Za-z]:[\\/])[A-Za-z0-9 _.\\/-]*\z' -or
        $r -match '(\A|[\\/])\.\.([\\/]|\z)') { Fail 'invalid agent root' }
    if ($r -match '\A~[\\/]') { $r = Join-Path $HOME $r.Substring(2) }
    $r.TrimEnd('\', '/')
}
# Why a directory may not be trusted, or $null: a junction or link, an owner
# or an allowed ACL entry outside this user, SYSTEM and Administrators, or no
# High integrity label (so a non-elevated process could write into it).
function Test-Private([string]$path) {
    $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    if (-not $item -or -not $item.PSIsContainer) { return 'is not a directory' }
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { return 'is a junction or link' }
    $sddl = [PrCoord.Dir]::Sddl($path)
    $sd = New-Object Security.AccessControl.RawSecurityDescriptor $sddl
    if ($trusted -notcontains $sd.Owner.Value) { return 'is owned by another account' }
    if ($null -eq $sd.DiscretionaryAcl) { return 'has no access list' }
    foreach ($ace in $sd.DiscretionaryAcl) {
        if ($ace -isnot [Security.AccessControl.CommonAce]) { return 'has an unexpected access entry' }
        if ($ace.AceQualifier -ne 'AccessDenied' -and $trusted -notcontains $ace.SecurityIdentifier.Value) {
            return 'grants access to another account'
        }
    }
    if ($sddl -cnotmatch '\(ML;[A-Z]*;(?=[A-Z]*NW)(?=[A-Z]*NR)[A-Z]+;;;(HI|SI)\)') { return 'lacks the High integrity label' }
    $null
}
function Assert-Private([string]$path, [string]$what) {
    $why = Test-Private $path
    if ($why) { Fail ($what + ' ' + $path + ' ' + $why + '; refusing to use it') }
}
# Creates a private directory atomically if missing, then checks it.
function Use-Private([string]$path, [string]$what) {
    if (-not (Test-Path -LiteralPath $path)) { [void][PrCoord.Dir]::Create($path, $me) }
    Assert-Private $path $what
}
function Open-Root([string]$value, [bool]$create) {
    $root = Resolve-Root $value
    if (-not (Test-Path -LiteralPath $root)) {
        if (-not $create) { return $null }
        $parent = Split-Path -Parent $root
        if ($parent) { [void][IO.Directory]::CreateDirectory($parent) }
    }
    Use-Private $root 'agent root'
    $root
}
function Read-Json([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try { [IO.File]::ReadAllText($path, $utf8) | ConvertFrom-Json } catch { $null }
}
function Write-Text([string]$path, [string]$text) {
    $tmp = $path + '.tmp'
    [IO.File]::WriteAllText($tmp, $text, $utf8)
    Move-Item -LiteralPath $tmp -Destination $path -Force
}
function Test-Script($proc, [string]$script) {
    $line = $proc.CommandLine()
    [bool]($line -and $line.ToLowerInvariant().Contains(('"' + $script + '"').ToLowerInvariant()))
}
# Facts about a recorded pid, all read through one process handle: its start
# time, whether it runs the expected script, and whether it is this user's.
function Get-Facts($rec, [string]$script) {
    $id = 0; $created = [int64]0
    if ($rec) { try { $id = [int]$rec.pid; $created = [int64]$rec.created } catch {} }
    $facts = [ordered]@{ pid = $id; created = $created; live_created = [int64]0; cmdline_match = $false; owner_match = $false }
    $proc = [PrCoord.Proc]::Open($id)
    if ($proc) {
        try {
            if (-not $proc.Exited()) {
                $facts.live_created = $proc.Created()
                $facts.cmdline_match = Test-Script $proc $script
                $facts.owner_match = ($proc.User() -eq $me)
            }
        } finally { $proc.Dispose() }
    }
    $facts
}
function Test-Verified($f) {
    $f.pid -gt 0 -and $f.created -ne 0 -and $f.live_created -eq $f.created -and $f.cmdline_match -and $f.owner_match
}
function Get-Holder([string]$root, [string]$session) {
    $dir = Join-Path $root ('.env-' + $session)
    if (Test-Private $dir) { return $null }
    $f = Get-Facts (Read-Json (Join-Path $dir 'holder-pid.json')) (Join-Path $dir 'holder.ps1')
    if (Test-Verified $f) { $f } else { $null }
}
function Start-Detached([string]$script, [string]$cwd) {
    $line = '"' + (Join-Path $PSHOME 'powershell.exe') + '" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $script + '"'
    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $line; CurrentDirectory = $cwd }
    if ($r.ReturnValue -ne 0) { Fail ('process creation failed: ' + $r.ReturnValue) }
}
# Connect to the verified holder's pipe; the pipe server must be that very
# process (pid, start time and user, read through one handle).
function Connect-Holder([string]$session, $holder) {
    $client = New-Object IO.Pipes.NamedPipeClientStream('.', ('pr-coord-env-' + $session), 'InOut')
    $deadline = (Get-Date).AddSeconds(10)
    while ($true) {
        try { $client.Connect(1000); break } catch {
            if ((Get-Date) -gt $deadline) { $client.Dispose(); Fail 'environment session is not answering' }
            Start-Sleep -Milliseconds 300
        }
    }
    $server = [PrCoord.Proc]::Open([PrCoord.Pipe]::Server($client.SafePipeHandle))
    if (-not $server) { $client.Dispose(); Fail 'environment pipe server is gone' }
    try {
        if ($server.Id -ne $holder.pid -or $server.Created() -ne $holder.created -or $server.User() -ne $me) {
            $client.Dispose(); Fail 'environment pipe is not served by the recorded holder'
        }
    } finally { $server.Dispose() }
    $client
}
function Send-Holder([string]$session, $holder, [string]$line) {
    $client = Connect-Holder $session $holder
    try {
        $w = New-Object IO.StreamWriter($client, $utf8); $w.AutoFlush = $true
        $r = New-Object IO.StreamReader($client, $utf8)
        $w.WriteLine($line)
        $r.ReadLine()
    } finally { $client.Dispose() }
}
"""


def _native_block():
    return "$native = @'\n" + NATIVE.strip("\n") + "\n'@\n"


# Fixed bootstrap: reads base64 JSON {script, args} from the first stdin line
# and runs the script with args as data. Results come back as one marked line,
# because the SSH shell turns exit codes into 0/1 and re-encodes non-ASCII.
BOOT = r"""
$ErrorActionPreference = 'Stop'
$enc = New-Object Text.UTF8Encoding $false
function Out-Ok($o) {
    $j = ConvertTo-Json -InputObject $o -Compress -Depth 6
    [Console]::Out.WriteLine('PRCOORD-OK ' + [Convert]::ToBase64String($enc.GetBytes($j)))
}
function Fail([string]$m) {
    [Console]::Out.WriteLine('PRCOORD-ERROR ' + [Convert]::ToBase64String($enc.GetBytes($m)))
    [Environment]::Exit(1)
}
try {
    $h = $enc.GetString([Convert]::FromBase64String([Console]::In.ReadLine())) | ConvertFrom-Json
    & ([ScriptBlock]::Create($h.script)) $h.args
} catch { Fail $_.Exception.Message }
"""
BOOT_ENCODED = base64.b64encode(BOOT.encode("utf-16-le")).decode()

# Static wrapper written into each agent directory.
RUN = "$ErrorActionPreference = 'Stop'\n" + _native_block() + r"""
$utf8 = New-Object Text.UTF8Encoding $false
$dir = $PSScriptRoot
# One wrapper per agent directory: hold run.lock for this process's lifetime.
try { $runLock = [IO.File]::Open((Join-Path $dir 'run.lock'), 'OpenOrCreate', 'ReadWrite', 'None') } catch { exit 1 }
function Write-Text([string]$name, [string]$text) {
    $tmp = Join-Path $dir ('.' + $name + '.tmp')
    [IO.File]::WriteAllText($tmp, $text, $utf8)
    Move-Item -LiteralPath $tmp -Destination (Join-Path $dir $name) -Force
}
function Stamp { (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }
try {
    Add-Type -TypeDefinition $native
    [PrCoord.Job]::ContainSelf()
} catch {
    Write-Text 'failed.json' (ConvertTo-Json -Compress -InputObject ([ordered]@{
        at = (Stamp); reason = ('wrapper setup failed: ' + $_.Exception.Message) }))
    exit 1
}
$self = [PrCoord.Proc]::Self()
Write-Text 'pid.json' (ConvertTo-Json -Compress -InputObject ([ordered]@{
    pid = $self.Id; created = $self.Created(); started = (Stamp) }))
$log = Join-Path $dir 'output.log'
$prompt = Join-Path $dir 'prompt.md'
$spec = [IO.File]::ReadAllText((Join-Path $dir 'launch.json'), $utf8) | ConvertFrom-Json
function Quote-Arg([string]$s) {
    if ($s -match '["%!^&|<>\r\n]') { throw 'argument contains a cmd.exe metacharacter' }
    if ($s -ne '' -and $s -notmatch '\s') { return $s }
    '"' + ($s -replace '(\\+)\z', '$1$1') + '"'
}
# PATH lookup only, never the working directory (the checkout).
function Resolve-Exe([string]$name) {
    if ([IO.Path]::IsPathRooted($name)) { return $name }
    $c = Get-Command -CommandType Application -Name $name -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $c) { throw ('not found on PATH: ' + $name) }
    $c.Source
}
# Reporter runs inside the job; it never delays the agent start, and the end
# report gets $wait ms before the job (and with it the reporter) closes.
function Start-Report($argv, [string[]]$extra, [int]$wait) {
    $a = @(@($argv) + $extra)
    if (-not $argv -or $a.Count -eq 0) { return }
    try {
        $psi = New-Object Diagnostics.ProcessStartInfo
        $psi.FileName = Resolve-Exe $a[0]
        if ($a.Count -gt 1) { $psi.Arguments = (@($a[1..($a.Count - 1)]) | ForEach-Object { Quote-Arg $_ }) -join ' ' }
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.WorkingDirectory = $dir
        $p = [Diagnostics.Process]::Start($psi)
        if ($wait -gt 0) { [void]$p.WaitForExit($wait) }
    } catch {}
}
Start-Report $spec.working @() 0
$rc = 1
try {
    $argv = @($spec.command | ForEach-Object { $_.Replace('{prompt_file}', $prompt) })
    $argv[0] = Resolve-Exe $argv[0]
    $line = ($argv | ForEach-Object { Quote-Arg $_ }) -join ' '
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
Start-Report $spec.done @(('remote agent exited ' + $rc + '; log ' + $log + ' on ' + $env:COMPUTERNAME)) 30000
exit $rc
"""

# Environment holder: keeps a credential only in its own process environment
# and starts agent wrappers, which inherit it, on request over a named pipe.
# Clients must be local, this user and at High integrity.
HOLDER = (
    "$ErrorActionPreference = 'Stop'\n"
    + _native_block()
    + r"""
$dir = $PSScriptRoot
try { $holderLock = [IO.File]::Open((Join-Path $dir 'holder.lock'), 'OpenOrCreate', 'ReadWrite', 'None') } catch { exit 1 }
function Fail([string]$m) {
    [IO.File]::WriteAllText((Join-Path $dir 'holder-error.txt'), $m)
    exit 1
}
"""
    + HELPERS
    + r"""
$cfg = [IO.File]::ReadAllText((Join-Path $dir 'holder.json'), $utf8) | ConvertFrom-Json
$root = $cfg.root
$sec = New-Object IO.Pipes.PipeSecurity
$sec.AddAccessRule((New-Object IO.Pipes.PipeAccessRule(
    (New-Object Security.Principal.SecurityIdentifier $me), 'FullControl', 'Allow')))
$pipeName = 'pr-coord-env-' + $cfg.session
function New-Server {
    try { New-Object IO.Pipes.NamedPipeServerStream($pipeName, 'InOut', 1, 'Byte', 'None', 4096, 4096, $sec) }
    catch { Fail ('pipe name unavailable: ' + $_.Exception.Message) }
}
# Only local clients of this user at High integrity (SSH logons are network
# logons, so the NETWORK group cannot be denied; SMB clients are refused here).
function Test-Client($server) {
    if ([PrCoord.Pipe]::Remote($server.SafePipeHandle)) { return $false }
    $client = [PrCoord.Proc]::Open([PrCoord.Pipe]::Client($server.SafePipeHandle))
    if (-not $client) { return $false }
    try { $client.User() -eq $me -and $client.Integrity() -ge 0x3000 } finally { $client.Dispose() }
}
$server = New-Server
$self = [PrCoord.Proc]::Self()
Write-Text (Join-Path $dir 'holder-pid.json') (ConvertTo-Json -Compress -InputObject ([ordered]@{
    pid = $self.Id; created = $self.Created() }))
$held = $false
while ($true) {
    if (-not $server) { $server = New-Server }
    try {
        $server.WaitForConnection()
        $w = New-Object IO.StreamWriter($server, $utf8); $w.AutoFlush = $true
        if (-not (Test-Client $server)) { $w.WriteLine('denied'); continue }
        $r = New-Object IO.StreamReader($server, $utf8)
        $line = $r.ReadLine()
        if ($line -cmatch '\Acredential ([A-Za-z0-9_-]+)\z') {
            if ($held) { $w.WriteLine('already-held') } else {
                [Environment]::SetEnvironmentVariable('GH_TOKEN', $Matches[1], 'Process')
                [Environment]::SetEnvironmentVariable('COPILOT_GITHUB_TOKEN', $Matches[1], 'Process')
                $held = $true
                $w.WriteLine('ok')
            }
            $Matches.Clear(); $line = $null
        } elseif ($line -cmatch '\Alaunch ([A-Za-z0-9][A-Za-z0-9_-]{0,63})\z') {
            $agent = Join-Path $root $Matches[1]
            $script = Join-Path $agent 'run.ps1'
            if (-not $held) { $w.WriteLine('no-credential') }
            elseif ((Test-Private $root) -or (Test-Private $agent) -or -not (Test-Path -LiteralPath $script)) { $w.WriteLine('untrusted') }
            else {
                $psi = New-Object Diagnostics.ProcessStartInfo
                $psi.FileName = Join-Path $PSHOME 'powershell.exe'
                $psi.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $script + '"'
                $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true; $psi.WorkingDirectory = $agent
                $w.WriteLine('started ' + [Diagnostics.Process]::Start($psi).Id)
            }
        } elseif ($line -eq 'ping') { $w.WriteLine('pong') } else { $w.WriteLine('invalid') }
    } catch {
    } finally { $server.Dispose(); $server = $null }
}
"""
)


def _script(body):
    return "$A = $args[0]\n" + _native_block() + HELPERS + body


START = _script(r"""
Test-Name $A.name
$root = Open-Root $A.root $true
$holder = $null
if ($A.session) {
    Test-Name $A.session
    $holder = Get-Holder $root $A.session
    if (-not $holder) { Fail 'required environment session is not running' }
}
$dir = Join-Path $root $A.name
Use-Private $dir 'agent folder'
try { $startLock = [IO.File]::Open((Join-Path $dir 'start.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
catch { Fail 'another start of this agent is in progress' }
try { ([IO.File]::Open((Join-Path $dir 'run.lock'), 'OpenOrCreate', 'ReadWrite', 'None')).Dispose() }
catch { Fail 'agent is already running' }
if (Test-Verified (Get-Facts (Read-Json (Join-Path $dir 'pid.json')) (Join-Path $dir 'run.ps1'))) { Fail 'agent is already running' }
$log = Join-Path $dir 'output.log'
if (Test-Path -LiteralPath $log) { Move-Item -LiteralPath $log -Destination (Join-Path $dir 'output.1.log') -Force }
foreach ($old in @('exit.code', 'pid.json', 'stopped.json', 'failed.json')) {
    Remove-Item -LiteralPath (Join-Path $dir $old) -Force -ErrorAction SilentlyContinue
}
foreach ($p in $A.files.PSObject.Properties) {
    if ($p.Name -cnotmatch '\A[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\z') { Fail 'invalid launch file name' }
    Write-Text (Join-Path $dir $p.Name) ([string]$p.Value)
}
function Fail-Start([string]$m) {
    if (-not (Test-Path -LiteralPath (Join-Path $dir 'failed.json'))) {
        Write-Text (Join-Path $dir 'failed.json') (ConvertTo-Json -Compress -InputObject ([ordered]@{
            at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'); reason = $m }))
    }
    Fail $m
}
$script = Join-Path $dir 'run.ps1'
try {
    if ($holder) {
        $reply = Send-Holder $A.session $holder ('launch ' + $A.name)
        if ($reply -notlike 'started *') { Fail-Start ('environment session refused the launch: ' + $reply) }
    } else {
        Start-Detached $script $dir
    }
} catch { Fail-Start $_.Exception.Message }
$deadline = (Get-Date).AddSeconds(30)
while (-not (Test-Path -LiteralPath (Join-Path $dir 'pid.json'))) {
    $failed = Read-Json (Join-Path $dir 'failed.json')
    if ($failed) { Fail ('agent wrapper failed: ' + $failed.reason) }
    if ((Get-Date) -gt $deadline) { Fail-Start 'agent wrapper did not start' }
    Start-Sleep -Milliseconds 200
}
Out-Ok (Read-Json (Join-Path $dir 'pid.json'))
""")

LIST = _script(r"""
$root = Open-Root $A.root $false
$agents = @()
if ($root) {
    foreach ($d in Get-ChildItem -LiteralPath $root -Directory -Force) {
        if ($d.Name -cnotmatch '\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\z') { continue }
        $why = Test-Private $d.FullName
        if ($why) { $agents += [pscustomobject]@{ name = $d.Name; untrusted = $why }; continue }
        if (-not (Test-Path -LiteralPath (Join-Path $d.FullName 'launch.json'))) { continue }
        $rec = Read-Json (Join-Path $d.FullName 'pid.json')
        $f = Get-Facts $rec (Join-Path $d.FullName 'run.ps1')
        $rcFile = Join-Path $d.FullName 'exit.code'
        $failed = Read-Json (Join-Path $d.FullName 'failed.json')
        $f['name'] = $d.Name
        $f['started'] = if ($rec) { [string]$rec.started } else { '' }
        $f['rc'] = if (Test-Path -LiteralPath $rcFile) { ([IO.File]::ReadAllText($rcFile)).Trim() } else { $null }
        $f['stopped'] = Test-Path -LiteralPath (Join-Path $d.FullName 'stopped.json')
        $f['failed'] = if ($failed) { [string]$failed.reason } else { $null }
        $agents += [pscustomobject]$f
    }
}
$envState = ''
if ($A.session) {
    Test-Name $A.session
    $envState = if ($root -and (Get-Holder $root $A.session)) { 'running' } else { 'not running' }
}
Out-Ok ([ordered]@{ agents = @($agents); env = $envState })
""")

TAIL = _script(r"""
Test-Name $A.name
$root = Open-Root $A.root $false
if (-not $root) { Fail 'no agents on this host' }
$dir = Join-Path $root $A.name
Assert-Private $dir 'agent folder'
$log = Join-Path $dir 'output.log'
if (-not (Test-Path -LiteralPath $log)) { Fail 'no log for this agent' }
$text = (Get-Content -LiteralPath $log -Tail ([int]$A.lines) -Encoding UTF8) -join "`n"
Out-Ok ([ordered]@{ text = $text })
""")

# Stops a recorded process only after verifying, through the very handle used
# to terminate it, that it is still that agent's wrapper (or that session's
# holder): same pid, start time, command line and user. Terminating the
# wrapper closes its Job Object, which kills the rest of the agent's tree.
KILL = _script(r"""
Test-Name $A.name
$expectPid = [int]$A.pid; $expectCreated = [int64]$A.created
if ($expectPid -le 0 -or $expectCreated -eq 0) { Fail 'refusing to stop: no verified process recorded' }
$root = Open-Root $A.root $false
if (-not $root) { Fail 'no agents on this host' }
if ($A.kind -eq 'agent') {
    $dir = Join-Path $root $A.name; $recFile = Join-Path $dir 'pid.json'; $script = Join-Path $dir 'run.ps1'
} elseif ($A.kind -eq 'holder') {
    $dir = Join-Path $root ('.env-' + $A.name); $recFile = Join-Path $dir 'holder-pid.json'; $script = Join-Path $dir 'holder.ps1'
} else { Fail 'invalid kill kind' }
Assert-Private $dir 'folder'
$rec = Read-Json $recFile
if (-not $rec -or [int]$rec.pid -ne $expectPid -or [int64]$rec.created -ne $expectCreated) {
    Fail 'refusing to stop: the record changed'
}
$proc = [PrCoord.Proc]::Open($expectPid)
if (-not $proc) { Fail 'process already exited; nothing stopped' }
try {
    if ($proc.Exited()) { Fail 'process already exited; nothing stopped' }
    if ($proc.Created() -ne $expectCreated -or -not (Test-Script $proc $script) -or $proc.User() -ne $me) {
        Fail 'refusing to stop: the recorded process is not verified as this agent'
    }
    if (-not $proc.Terminate()) { Fail 'could not terminate the process' }
    [void]$proc.WaitExit(10000)
} finally { $proc.Dispose() }
if ($A.kind -eq 'agent') {
    Write-Text (Join-Path $dir 'stopped.json') (ConvertTo-Json -Compress -InputObject ([ordered]@{
        at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'); pid = $expectPid }))
}
Out-Ok ([ordered]@{ killed = $expectPid })
""")

ENV_PREPARE = _script(r"""
Test-Name $A.session
$root = Open-Root $A.root $true
if (Get-Holder $root $A.session) { Out-Ok ([ordered]@{ running = $true }); return }
$dir = Join-Path $root ('.env-' + $A.session)
Use-Private $dir 'environment folder'
foreach ($old in @('holder-pid.json', 'holder-error.txt')) {
    Remove-Item -LiteralPath (Join-Path $dir $old) -Force -ErrorAction SilentlyContinue
}
Write-Text (Join-Path $dir 'holder.json') (ConvertTo-Json -Compress -InputObject ([ordered]@{ root = $root; session = $A.session }))
Write-Text (Join-Path $dir 'holder.ps1') ([string]$A.holder)
Out-Ok ([ordered]@{ running = $false })
""")

# The credential follows the header on stdin; it is read here and written
# only to the verified holder's pipe.
ENV = _script(r"""
Test-Name $A.session
$root = Open-Root $A.root $false
if (-not $root) { Fail 'agent root missing' }
if (Get-Holder $root $A.session) { Fail 'environment holder is already running; stop it with agent-env --stop to replace its credential' }
$token = [Console]::In.ReadLine()
if (-not $token -or $token -cnotmatch '\A[A-Za-z0-9_-]+\z') { Fail 'invalid credential input' }
$dir = Join-Path $root ('.env-' + $A.session)
Assert-Private $dir 'environment folder'
Start-Detached (Join-Path $dir 'holder.ps1') $dir
$deadline = (Get-Date).AddSeconds(30)
$holder = $null
while (-not $holder) {
    $err = Join-Path $dir 'holder-error.txt'
    if (Test-Path -LiteralPath $err) { Fail ('environment holder failed: ' + [IO.File]::ReadAllText($err)) }
    if ((Get-Date) -gt $deadline) { Fail 'environment holder did not start' }
    Start-Sleep -Milliseconds 200
    $holder = Get-Holder $root $A.session
}
# The credential only passes through .NET method calls, never a cmdlet or
# function parameter that module logging could record.
$client = Connect-Holder $A.session $holder
try {
    $w = New-Object IO.StreamWriter($client, $utf8); $w.AutoFlush = $true
    $r = New-Object IO.StreamReader($client, $utf8)
    $w.Write('credential '); $w.WriteLine($token)
    $token = $null
    $reply = $r.ReadLine()
} finally { $client.Dispose() }
if ($reply -ne 'ok') { Fail ('environment holder did not accept the credential: ' + $reply) }
Out-Ok 'environment session started'
""")

HOLDER_FACTS = _script(r"""
Test-Name $A.session
$root = Open-Root $A.root $false
if (-not $root) { Out-Ok ([ordered]@{ pid = 0 }); return }
$dir = Join-Path $root ('.env-' + $A.session)
if (Test-Private $dir) { Out-Ok ([ordered]@{ pid = 0 }); return }
Out-Ok (Get-Facts (Read-Json (Join-Path $dir 'holder-pid.json')) (Join-Path $dir 'holder.ps1'))
""")


class WindowsAgentError(RuntimeError):
    pass


def safe_path(value):
    return (
        isinstance(value, str)
        and bool(PATH.fullmatch(value))
        and not DOTDOT.search(value)
    )


def agent_root(host):
    spec = P.hosts[host]
    checkout = spec.get("checkout", "")
    if checkout and not safe_path(checkout):
        raise WindowsAgentError(
            f"hosts.{host}.checkout must be a drive or ~\\ path of ASCII letters,"
            " digits, space, _ . - and separators"
        )
    root = spec.get("agent_root") or (
        checkout.rstrip("\\/") + "\\out\\agents" if checkout else ""
    )
    if not safe_path(root):
        raise WindowsAgentError(
            f"set hosts.{host}.agent_root (or checkout) to a drive or ~\\ path of"
            " ASCII letters, digits, space, _ . - and separators"
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
        argv = P.expand(
            spec["agent_report"],
            label=f"hosts.{host}.agent_report",
            host=host,
            name=name,
        )
    else:
        argv = P.command("remote_agents", "report", host=host, name=name)
    return check_command(argv, "reporter") if argv else argv


def header(script, **args):
    return base64.b64encode(
        json.dumps({"script": script, "args": args}).encode("utf-8")
    ).decode()


def transport(host, line, *, passthrough=False, timeout=60):
    """The only SSH call. Sends the header line (plus, with passthrough, the
    first line of this process's stdin, never decoded or shown)."""
    argv = [
        *P.ssh(host),
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        BOOT_ENCODED,
    ]
    data = line.encode() + b"\n"
    if passthrough:
        data += sys.stdin.buffer.readline(4096)
    return subprocess.run(
        argv, input=data, capture_output=True, timeout=timeout, check=False
    )


def stderr_lines(text):
    """Readable error lines: PowerShell wraps stderr in CLIXML when its input
    is redirected; drop SSH banner lines."""
    if "#< CLIXML" in text:
        text = "".join(re.findall(r'<S S="Error">(.*?)</S>', text, re.S))
        text = text.replace("_x000D__x000A_", "\n").replace("_x000A_", "\n")
        for entity, char in (
            ("&lt;", "<"),
            ("&gt;", ">"),
            ("&quot;", '"'),
            ("&amp;", "&"),
        ):
            text = text.replace(entity, char)
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.startswith("** ")
    ]


def call(host, script, *, passthrough=False, timeout=60, **args):
    """Run one remote script with args as data; return its decoded result."""
    result = transport(
        host, header(script, **args), passthrough=passthrough, timeout=timeout
    )
    out = (
        result.stdout.decode("utf-8", "replace")
        if isinstance(result.stdout, bytes)
        else result.stdout or ""
    )
    err = (
        result.stderr.decode("utf-8", "replace")
        if isinstance(result.stderr, bytes)
        else result.stderr or ""
    )
    for line in reversed(out.splitlines()):
        kind, _, payload = line.strip().partition(" ")
        if kind not in ("PRCOORD-OK", "PRCOORD-ERROR"):
            continue
        try:
            text = base64.b64decode(payload, validate=True).decode("utf-8")
            value = json.loads(text) if kind == "PRCOORD-OK" else text
        except (ValueError, UnicodeDecodeError) as exc:
            raise WindowsAgentError("unexpected reply from the host") from exc
        if kind == "PRCOORD-ERROR":
            raise WindowsAgentError(value)
        return value
    raise WindowsAgentError(
        "; ".join(stderr_lines(err)[-3:]) or f"no reply from {host}"
    )


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def verified(rec):
    """True only for a positive recorded pid whose live process has the
    recorded start time, runs this agent's wrapper and belongs to this user."""
    try:
        pid = int(rec.get("pid") or 0)
        created = int(rec.get("created") or 0)
        live = int(rec.get("live_created") or 0)
    except (TypeError, ValueError):
        return False
    return (
        pid > 0
        and created != 0
        and live == created
        and rec.get("cmdline_match") is True
        and rec.get("owner_match") is True
    )


def state(rec):
    if rec.get("untrusted"):
        return f"untrusted ({rec['untrusted']})"
    if rec.get("stopped"):
        return "stopped"
    if rec.get("rc") not in (None, ""):
        return f"exited {rec['rc']}"
    if verified(rec):
        return "running"
    if int(rec.get("pid") or 0) > 0:
        return "lost"  # e.g. killed outside pr-coord or by a reboot
    if rec.get("failed"):
        return "failed"
    return "starting"


def kill_tree(host, root, kind, name, pid, created):
    """The only path to a remote kill; the host re-verifies the pid through
    the handle it terminates, and the wrapper's job takes its tree with it."""
    return call(
        host,
        KILL,
        root=root,
        kind=kind,
        name=name,
        pid=pid,
        created=created,
        timeout=90,
    )


def list_agents(host, root, session=""):
    return call(host, LIST, root=root, session=session)


def start(host, name, prompt, session):
    spec = P.hosts[host]
    root = agent_root(host)
    argv = agent_argv(host, name)
    reporter = reporter_argv(host, name)
    if not reporter or not spec.get("checkout"):
        raise WindowsAgentError(
            "configure host checkout, remote_agents.command and remote_agents.report"
        )
    files = {"prompt.md": prompt.read_text(encoding="utf-8-sig")}
    if runbook := P.get("remote_agents", "runbook"):
        base = Path(runbook).name
        if base in LAUNCH_FILES or base in STATE_FILES or not FILE_NAME.fullmatch(base):
            raise WindowsAgentError("runbook filename conflicts with launch files")
        files[base] = Path(runbook).expanduser().read_text(encoding="utf-8-sig")
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
    record("agent", name, "starting", "--machine", host, "--task", "launching")
    try:
        rec = call(
            host, START, root=root, name=name, session=session, files=files, timeout=120
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
    kill_tree(host, root, "agent", name, int(rec["pid"]), int(rec["created"]))
    print(f"stopped agent {name} on {host}")
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
            if not session:
                parser.error("configure agent_env_session for this host")
            if args.stop:
                holder = call(args.host, HOLDER_FACTS, root=root, session=session)
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
            if sys.stdin.isatty():
                parser.error("provide credential input on stdin")
            prepared = call(
                args.host, ENV_PREPARE, root=root, session=session, holder=HOLDER
            )
            if prepared.get("running"):
                print(
                    "environment holder is already running (a credential is given only"
                    " once); run agent-env HOST --stop first to replace it"
                )
                return 0
            print(
                call(
                    args.host,
                    ENV,
                    root=root,
                    session=session,
                    passthrough=True,
                    timeout=90,
                )
            )
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
            lines = 40 if args.lines is None else args.lines
            if not 1 <= lines <= 10000:
                parser.error("--lines must be between 1 and 10000")
            data = call(
                args.host, TAIL, root=agent_root(args.host), name=args.name, lines=lines
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
