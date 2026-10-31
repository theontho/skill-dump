---
name: pr-babysitter
description: "Use when asked to babysit, shepherd, or watch a pull request until it merges: monitor CI and review comments on a schedule, fix failures, stack fixes for blockers from main, resolve threads, keep it rebased, and land via auto-merge only when everything is clean."
license: MIT
metadata:
  author: theontho
  version: "1.0.0"
---

# PR babysitter

Own the pull request until it merges or the person says stop. Use `gh` for GitHub operations.

## Loop

1. **Check on a schedule**, not only when prompted: about every 20–30 minutes (use the agent's scheduler; if it has none, say so rather than implying the PR is watched), look at CI (`gh pr checks`), new review comments and threads, and whether `main` or a base PR moved. Stop the schedule once the PR merges.
2. **CI:** follow every check to completion. Fix failures caused by the PR. Rerun flaky or infrastructure failures, or report them with evidence. Run the repository's local gate before every push.
3. **Blockers from `main`:** if something already broken on `main` blocks the PR (flaky test, unrelated bug, broken tooling), fix it in its own small PR and stack ours on it, so the fix can be reviewed and landed on its own and our PR passes. Link the two and say why in both descriptions. If upstream fixes part of it first, shrink the fix PR to what is left; once it merges, rebase ours onto `main`.
4. **Review comments:** read every comment, bot or human. Fix the valid ones and push. Reply on the thread with what changed, or explain why you disagree, then resolve the thread (GraphQL `resolveReviewThread`). A comment that needs the person's decision stays open; tell them.
5. **Stay mergeable:** when `main` or the base PR moves, rebase, re-verify, and push. Keep the description and test notes true for the latest commit.

## Landing

- Don't merge until all CI passes and every review thread is resolved.
- Enable auto-merge (`gh pr merge --auto`) when the repository supports it, but only once those conditions hold or branch protection enforces them. If a new comment or failure appears, fix it first or turn auto-merge off (`gh pr merge --disable-auto`).
- Never merge by hand, approve the PR, bypass branch protection, or skip required checks.

## Boundaries

Ask first before anything outward or destructive you weren't asked to do, such as force-pushing someone else's branch or closing PRs.
