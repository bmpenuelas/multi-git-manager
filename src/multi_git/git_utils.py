import os
import subprocess
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class RepoStatus:
    repo_name: str
    repo_path: str
    branch: str
    ahead: int
    behind: int
    has_staged: bool
    has_unstaged: bool
    has_untracked: bool
    is_clean: bool
    remote_branch: Optional[str] = None
    fetch_failed: bool = False


@dataclass
class BranchInfo:
    repo_name: str
    repo_path: str
    branch: str
    remote_branch: Optional[str]
    ahead: int
    behind: int
    is_current: bool
    has_staged: bool
    has_unstaged: bool
    has_untracked: bool
    is_clean: bool


def find_git_repos(root_dir: str) -> list[str]:
    repos: list[str] = []
    try:
        items = os.listdir(root_dir)
    except PermissionError:
        return repos

    for item in sorted(items):
        item_path = os.path.join(root_dir, item)
        if not os.path.isdir(item_path):
            continue
        git_path = os.path.join(item_path, ".git")
        if os.path.isdir(git_path) or os.path.isfile(git_path):
            repos.append(item_path)
    return repos


def run_git(repo_path: str, args: list[str]) -> tuple[str, str, int]:
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return "", "timed out", -1
    except FileNotFoundError:
        return "", "git not found", -1


def fetch_repo(repo_path: str) -> bool:
    _, _, rc = run_git(repo_path, ["fetch", "--all", "--prune", "--quiet"])
    return rc == 0


def parse_status_porcelain(repo_path: str) -> RepoStatus:
    output, stderr, rc = run_git(repo_path, ["status", "--branch", "--porcelain"])

    repo_name = os.path.basename(repo_path)

    if rc != 0:
        return RepoStatus(
            repo_name=repo_name,
            repo_path=repo_path,
            branch="ERROR",
            ahead=0,
            behind=0,
            has_staged=False,
            has_unstaged=False,
            has_untracked=False,
            is_clean=False,
            remote_branch=None,
            fetch_failed=True,
        )

    lines = output.split("\n")

    branch_line = lines[0] if lines else ""
    branch_info = branch_line.removeprefix("## ").strip()

    remote_branch: Optional[str] = None
    ahead = 0
    behind = 0
    branch = branch_info

    if "..." in branch_info:
        parts = branch_info.split("...", 1)
        branch = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        if " [" in rest:
            remote_part, status_part = rest.split(" [", 1)
            remote_branch = remote_part
            status_part = status_part.rstrip("]")
            m = re.search(r"ahead (\d+)", status_part)
            if m:
                ahead = int(m.group(1))
            m = re.search(r"behind (\d+)", status_part)
            if m:
                behind = int(m.group(1))
        else:
            remote_branch = rest

    if branch in ("HEAD", "HEAD (no branch)"):
        short_hash, _, _ = run_git(repo_path, ["rev-parse", "--short", "HEAD"])
        if short_hash:
            branch = f"detached at {short_hash}"

    has_staged = False
    has_unstaged = False
    has_untracked = False

    for line in lines[1:]:
        if not line.strip() or len(line) < 2:
            continue
        if line.startswith("?? "):
            has_untracked = True
            continue

        if line[0] not in (" ", "?"):
            has_staged = True
        if line[1] not in (" ", "?"):
            has_unstaged = True

    is_clean = not (has_staged or has_unstaged or has_untracked)

    return RepoStatus(
        repo_name=repo_name,
        repo_path=repo_path,
        branch=branch,
        ahead=ahead,
        behind=behind,
        has_staged=has_staged,
        has_unstaged=has_unstaged,
        has_untracked=has_untracked,
        is_clean=is_clean,
        remote_branch=remote_branch,
    )


def get_local_branches(repo_path: str) -> list[str]:
    output, _, rc = run_git(repo_path, ["branch", "--format", "%(refname:short)"])
    if rc != 0 or not output:
        return []
    return [b.strip() for b in output.split("\n") if b.strip()]


def get_branch_tracking(
    repo_path: str, branch: str
) -> tuple[Optional[str], int, int]:
    remote, _, rc = run_git(
        repo_path, ["rev-parse", "--abbrev-ref", f"{branch}@{{u}}"]
    )
    if rc != 0 or not remote:
        return None, 0, 0

    ahead_behind, _, rc = run_git(
        repo_path, ["rev-list", "--count", "--left-right", f"{branch}...{remote}"]
    )
    if rc != 0 or not ahead_behind:
        return remote, 0, 0

    parts = ahead_behind.split("\t")
    ahead = int(parts[0]) if len(parts) > 0 and parts[0] else 0
    behind = int(parts[1]) if len(parts) > 1 and parts[1] else 0

    return remote, ahead, behind


def get_all_branches_status(repo_path: str, status: RepoStatus) -> list[BranchInfo]:
    current_branch = status.branch
    branches = get_local_branches(repo_path)

    result: list[BranchInfo] = []
    for branch in branches:
        if branch == current_branch:
            result.append(
                BranchInfo(
                    repo_name=status.repo_name,
                    repo_path=repo_path,
                    branch=branch,
                    remote_branch=status.remote_branch,
                    ahead=status.ahead,
                    behind=status.behind,
                    is_current=True,
                    has_staged=status.has_staged,
                    has_unstaged=status.has_unstaged,
                    has_untracked=status.has_untracked,
                    is_clean=status.is_clean,
                )
            )
        else:
            remote, ahead, behind = get_branch_tracking(repo_path, branch)
            result.append(
                BranchInfo(
                    repo_name=status.repo_name,
                    repo_path=repo_path,
                    branch=branch,
                    remote_branch=remote,
                    ahead=ahead,
                    behind=behind,
                    is_current=False,
                    has_staged=False,
                    has_unstaged=False,
                    has_untracked=False,
                    is_clean=True,
                )
            )

    return result
