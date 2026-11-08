# CLI launchers

Shared versions of the small mise launchers previously installed by hand:
`claude`, `codex`, `crush`, `cursor-agent`, `gemini`, `gh`, `ghui`, `grok`,
`hunk`, `muse`, `omp`, `opencode`, `pi`, and `playwright`.

Each command selects its existing mise package with `mise use -g --quiet`,
then executes it with arguments unchanged. The launchers retain the original
`MISE_MINIMUM_RELEASE_AGE=0` behavior. Installation only creates links; it does
not download or launch those programs.

Copilot has no launcher here: install it with mise and run it directly.

Run `python3 -m unittest -q test_launcher` here for isolated fake-mise tests.
