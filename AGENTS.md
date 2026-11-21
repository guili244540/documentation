# Git policy

Read-only git commands (status, diff, log, show, etc.) are allowed and encouraged
for inspecting the repository.

## Local coding sessions
Do not commit, push, create branches, stash, or otherwise modify git state.
In local sessions, make file edits only and leave the repository in a
dirty working state; the owner handles all state-changing git operations.
Never assume whether changes are committed; check `git status` first.

## Remote coding sessions (Vibe Code Web sandboxes)
In remote coding sessions (Vibe Code Web sandboxes), creating a working
branch, committing (commits are allowed without the user confirmation) to it is allowed.
Opening a pull request is allowed as the final step of the task. Never push directly to main.
The owner handles promoting integration branches to `main`.

Before creating a branch, the agent must ask the owner a branch name.

Before committing (and especially before pushing), verify the branch is
still usable:

1. Check the current branch (`git branch --show-current`) and the state
   of its pull request (`gh pr view <branch> --json state`).
2. If the PR was already merged (or the branch was deleted), do not
   push to it: create a fresh branch cut from the up-to-date base
   branch (`origin/main`) and move the commits there (rebase), then open a new PR.

This avoids a common failure mode: the owner merges a PR while the agent
is still working, so a later commit lands on a dead branch and never
reaches `main`.
