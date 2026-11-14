"""Repository-attribution regressions, observed through collected counts."""

import unittest
from pathlib import Path

from usage_fixture import REPO, UsageFixture


class RepositoryTests(UsageFixture):
    def test_copilot_fork_alias_counts_outside_configured_paths(self):
        self.config.write_text(
            self.config.read_text() + 'aliases = ["theontho/wgsextract-cli"]\n'
        )
        self.events(
            "fork", [self.usage()], repo="theontho/wgsextract-cli", cwd="/outside"
        )
        self.run_cli("collect", "--days", "1", "--end", "2026-11-12", "--no-github")
        self.assertEqual(self.daily()["repositories"][REPO]["calls"], 1)
        self.assertEqual(self.daily()["agents"][0]["repository"], REPO)

    def test_copilot_unmatched_fork_name_falls_back_to_cwd(self):
        self.events("fork", [self.usage()], repo="unlisted/fork")
        self.run_cli("collect", "--days", "1", "--end", "2026-11-12", "--no-github")
        self.assertEqual(self.daily()["repositories"][REPO]["calls"], 1)

    def test_copilot_unmatched_name_and_cwd_fall_back_to_git_root(self):
        self.events(
            "fork",
            [self.usage()],
            repo="unlisted/fork",
            cwd="/outside",
            git_root="/work/repo",
        )
        self.run_cli("collect", "--days", "1", "--end", "2026-11-12", "--no-github")
        self.assertEqual(self.daily()["repositories"][REPO]["calls"], 1)

    def test_copilot_worktree_glob_counts_descendants_not_neighbor_repositories(self):
        self.config.write_text(
            self.config.read_text().replace(
                'paths = ["/work/repo"]',
                'paths = ["/work/repo", "~/.herdr/worktrees/wgsextract-cli/*", "/external/scratch/wt-*"]',
            )
        )
        for identifier, directory in (
            (
                "herdr",
                str(Path.home() / ".herdr/worktrees/wgsextract-cli/topic/subdir"),
            ),
            ("scratch", "/external/scratch/wt-task/nested"),
            ("neighbor", str(Path.home() / ".herdr/worktrees/wgsextract-climax/topic")),
        ):
            self.events(
                identifier,
                [self.usage(identifier)],
                repo="unlisted/fork",
                cwd=directory,
            )
        self.run_cli("collect", "--days", "1", "--end", "2026-11-12", "--no-github")
        total = self.daily()["repositories"][REPO]
        self.assertEqual(total["calls"], 2)
        self.assertEqual(total["prompt_uncached"] + total["prompt_cached"], 200)

    def test_known_repository_name_still_precedes_another_repositories_path(self):
        self.config.write_text(
            self.config.read_text()
            + (
                '\n[[repositories]]\nname = "another/project"\npaths = ["/work/other"]\n'
            )
        )
        self.events("other", [self.usage()], repo="another/project")
        self.run_cli("collect", "--days", "1", "--end", "2026-11-12", "--no-github")
        totals = self.daily()["repositories"]
        self.assertEqual(totals[REPO]["calls"], 0)
        self.assertEqual(totals["another/project"]["calls"], 1)


if __name__ == "__main__":
    unittest.main()
