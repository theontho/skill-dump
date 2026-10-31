# Babysitting pull requests

When asked to babysit a PR, own it until it merges or the person says stop:

- **Check on a schedule**, not only when prompted: about every 20–30 minutes, look at CI, new review comments, and whether `main` or a base PR moved.
- **CI:** follow every check to completion. Fix failures caused by the PR; rerun or report flaky or infrastructure failures with evidence.
- **Blockers from `main`:** if something already broken on `main` blocks the PR (flaky test, unrelated bug, broken tooling), fix it in its own small PR and stack ours on it, so the fix can be reviewed and landed on its own and our PR passes. Link the two and say why in both descriptions.
- **Review comments:** read every comment, bot or human. Fix the valid ones and push. Reply on the thread with what changed, or explain why you disagree, then resolve the thread.
- **Stay mergeable:** when `main` or the base PR moves, rebase, re-verify, and push. Keep the description and test notes true for the latest commit.
- **Landing:** don't merge until all CI passes and every review thread is resolved. Enable auto-merge (`gh pr merge --auto`) when the repository supports it, but only once those conditions hold or branch protection enforces them.
- **Ask first** before anything outward or destructive you weren't asked to do, such as force-pushing someone else's branch or closing PRs.
