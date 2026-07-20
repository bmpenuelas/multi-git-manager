import argparse
import os

from .git_utils import (
    find_git_repos,
    fetch_repo,
    parse_status_porcelain,
    get_all_branches_status,
    run_git,
)
from .display import show_status, show_branch_all, console, Progress


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mgitm",
        description="Run git commands across multiple repositories",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Fetch and show status of all repos")
    status.add_argument(
        "--branch_all",
        action="store_true",
        help="Show status for every local branch",
    )

    args = parser.parse_args()

    progress = Progress()
    progress.update("Scanning directories...")

    root_dir = os.getcwd()
    repos = find_git_repos(root_dir)

    if not repos:
        progress.clear()
        console.print("[yellow]No git repositories found in[/] [bold]{}[/]".format(root_dir))
        return

    if args.command == "status":
        repos_status = []
        repos_branches: list[list] = []

        for repo_path in repos:
            repo_name = os.path.basename(repo_path)
            progress.update(f"Fetching {repo_name}...")

            fetch_ok = fetch_repo(repo_path)
            if not fetch_ok:
                remotes, _, _ = run_git(repo_path, ["remote"])
                if remotes.strip():
                    progress.warn(f"Warning: {repo_name}: fetch failed")

            progress.update(f"Status {repo_name}...")
            status = parse_status_porcelain(repo_path)
            repos_status.append(status)

            if args.branch_all:
                branches = get_all_branches_status(repo_path, status)
                repos_branches.append(branches)

        progress.clear()

        if args.branch_all:
            show_branch_all(repos_branches)
        else:
            show_status(repos_status)
