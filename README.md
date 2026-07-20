# mgitm (multi-git-manager)

Run git commands across multiple repositories at once.

Scans the current directory for git repos, fetches from remotes, and displays
a clean summary table — no more `cd`-ing into each folder to check what's going on.

## Installation

```bash
pip install multi-git-manager
```

Requires Python 3.10+ and `git` available on `PATH`.

## Example

A directory with four repos:

```text
$ mgitm status
                    mgitm status
╭──────────┬──────────────┬─────────────────┬──────┬────────────────╮
│ Repo     │ Branch       │ Remote          │ ↕    │ Status         │
├──────────┼──────────────┼─────────────────┼──────┼────────────────┤
│ frontend │ main         │ origin/main     │ ↑2   │ ✓ clean        │
│ backend  │ develop      │ origin/develop  │ ↑1 ↓3│ staged         │
│ docs     │ master       │ origin/master   │ —    │ ✓ clean        │
│ infra    │ main         │ none            │ —    │ untracked      │
╰──────────┴──────────────┴─────────────────┴──────┴────────────────╯
```

- **frontend** — 2 commits ahead of origin, clean working tree.
- **backend** — 1 ahead, 3 behind, has staged changes.
- **docs** — up to date, clean.
- **infra** — no remote configured, has untracked files.

With `--branch_all`:

```text
$ mgitm status --branch_all
               mgitm status — all branches
╭──────────┬──────────────┬─────────────────┬──────┬─────────────╮
│ Repo     │ Branch       │ Remote          │ ↕    │ Status      │
├──────────┼──────────────┼─────────────────┼──────┼─────────────┤
│ frontend │ → main       │ origin/main     │ ↑2   │ ✓ clean     │
│          │   develop    │ origin/develop  │ —    │ —           │
│          │   experiment │ origin/experime…│ ↓5   │ —           │
│ backend  │ → develop    │ origin/develop  │ ↑1 ↓3│ staged      │
│          │   main       │ origin/main     │ —    │ —           │
│ docs     │ → master     │ origin/master   │ —    │ ✓ clean     │
│ infra    │ → main       │ none            │ —    │ untracked   │
│          │   feature-x  │ none            │ —    │ —           │
╰──────────┴──────────────┴─────────────────┴──────┴─────────────╯
```

The `→` marks the currently checked-out branch in each repo. Status is only
meaningful for the active branch.

## Usage

```
mgitm status
```

Fetches all repos, then prints a table of branch, remote tracking, ahead/behind
counts, and working-tree cleanliness.

```
mgitm status --branch_all
```

Same table, but lists every local branch as its own row instead of just the
checked-out one.

## Commands

| Command               | Description                                  |
|-----------------------|----------------------------------------------|
| `mgitm status`        | Fetch remotes and show per-repo summary      |
| `mgitm status -h`     | Show help for the status command             |
| `mgitm --help`        | Show top-level help                          |

## PRs and new feature requests are welcome!

Open a GitHub issue and/or submit a PR for any useful improvement you come up with.
