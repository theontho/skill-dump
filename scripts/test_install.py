"""Exercise install, symlink entry points, git hooks and updates in an isolated HOME."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def test_isolated_install_and_both_update_paths(self):
        with tempfile.TemporaryDirectory(prefix=".install-test-", dir=ROOT) as directory:
            temporary = Path(directory)
            source = temporary / "source"
            source.mkdir()
            for name in ("bin", "githooks", "tools", "skills", "agents"):
                shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns(
                    "__pycache__", "*.pyc",
                ))
            shutil.copy2(ROOT / "install.sh", source / "install.sh")
            home = temporary / "home"
            for name in (".copilot", ".claude", ".codex", ".gemini", ".config/opencode"):
                (home / name).mkdir(parents=True)
            env = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
                       XDG_DATA_HOME=str(home / ".local/share"),
                       XDG_STATE_HOME=str(home / ".local/state"),
                       SKILL_DUMP_HOME=str(home / ".local/share/skill-dump"),
                       SKILL_DUMP_BIN_DIR=str(home / ".local/bin"),
                       SKILL_DUMP_REPO=str(temporary / "origin.git"),
                       AGENT_SESSION_STATE_HOME=str(home / ".local/state/copilot-agent-sessions"),
                       GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                       GIT_AUTHOR_NAME="Installer Test", GIT_AUTHOR_EMAIL="test@example.invalid",
                       GIT_COMMITTER_NAME="Installer Test", GIT_COMMITTER_EMAIL="test@example.invalid")
            for name in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
                env.pop(name, None)

            def run(*command, cwd=source):
                result = subprocess.run(command, cwd=cwd, env=env, text=True,
                                        capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout

            run("git", "init", "-q", "--initial-branch=main")
            run("git", "add", ".")
            run("git", "commit", "-qm", "Initial installer fixture")
            run("git", "clone", "--bare", "-q", str(source), env["SKILL_DUMP_REPO"])
            run("/bin/bash", str(source / "install.sh"), "--skip", "agent-coord,copilot-persistent")
            installed = Path(env["SKILL_DUMP_HOME"])
            command = home / ".local/bin/skill-dump"
            self.assertTrue(command.is_symlink())
            self.assertIn("hooks:    reinstall on git pull/checkout", run(str(command), "status"))
            self.assertIn("op-session start", run(str(home / ".local/bin/op-session"), "--help"))
            self.assertIn("sudo-session start", run(str(home / ".local/bin/sudo-session"), "--help"))

            for version, method in (("pull", "pull"), ("update", "update")):
                marker = source / f"skills/installer-{version}/SKILL.md"
                marker.parent.mkdir()
                marker.write_text(f"---\nname: installer-{version}\ndescription: Test fixture\n---\n")
                run("git", "add", ".")
                run("git", "commit", "-qm", f"Add {version} fixture")
                run("git", "push", "-q", env["SKILL_DUMP_REPO"], "main")
                if method == "pull":
                    run("git", "pull", "--ff-only", "-q", cwd=installed)
                else:
                    self.assertIn("updated", run(str(command), "update"))
                for root in (home / ".agents/skills", home / ".claude/skills"):
                    self.assertEqual((root / marker.parent.name).resolve(),
                                     installed / marker.parent.relative_to(source))
            self.assertIn("up to date", run(str(command), "update"))
            for file in (home / ".copilot/copilot-instructions.md", home / ".claude/CLAUDE.md",
                         home / ".codex/AGENTS.md", home / ".gemini/GEMINI.md",
                         home / ".config/opencode/AGENTS.md"):
                self.assertEqual(file.read_text().count("<!-- BEGIN skill-dump:"), 1)
            self.assertEqual(run("git", "status", "--porcelain", cwd=installed), "")


if __name__ == "__main__":
    unittest.main()
