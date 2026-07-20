import argparse
import os
from contextlib import nullcontext

from rich.live import Live

from .git_utils import (
    find_git_repos,
    fetch_repo,
    parse_status_porcelain,
    get_all_branches_status,
    run_git,
    requires_github_auth,
)
from .display import console, LiveDisplay


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mgitm",
        description="Run git commands across multiple repositories",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Skip fetching from remotes",
    )
    parser.add_argument(
        "--branch_all",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    subparsers = parser.add_subparsers(dest="command")

    status_parser = subparsers.add_parser(
        "status", help="Fetch and show status of all repos"
    )
    status_parser.add_argument(
        "--branch_all",
        action="store_true",
        help="Show status for every local branch",
    )
    status_parser.add_argument(
        "--no-fetch",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    args = parser.parse_args()

    if args.command not in (None, "status"):
        parser.print_help()
        return

    root_dir = os.getcwd()
    repos = find_git_repos(root_dir)

    if not repos:
        console.print(
            "[yellow]No git repositories found in[/] [bold]{}[/]".format(root_dir)
        )
        return

    branch_all = getattr(args, "branch_all", False)
    no_fetch = getattr(args, "no_fetch", False)

    display = LiveDisplay(repos, branch_all=branch_all)

    # Rich cannot redraw in redirected / non-interactive terminals.  Refreshing
    # a Live display there appends a new table on every update, so only use it
    # when the shared console can actually move the cursor.
    live_context = (
        Live(
            get_renderable=display._render,
            console=console,
            refresh_per_second=4,
        )
        if console.is_terminal
        else nullcontext(None)
    )

    warnings: list[str] = []

    with live_context as live:
        for repo_path in repos:
            status = parse_status_porcelain(repo_path)
            display.set_status(repo_path, status)

            if branch_all:
                branches = get_all_branches_status(repo_path, status)
                display.set_branches(repo_path, branches)

            if live:
                live.refresh()

        if not no_fetch:
            github_auth_repos = [
                repo_path for repo_path in repos if requires_github_auth(repo_path)
            ]
            github_auth_repo_paths = set(github_auth_repos)
            non_interactive_repos = [
                repo_path for repo_path in repos if repo_path not in github_auth_repo_paths
            ]

            # GitHub may invoke an interactive login through either gh or Git
            # Credential Manager, so fetch those repositories last.
            for repo_path in non_interactive_repos + github_auth_repos:
                repo_name = os.path.basename(repo_path)
                display.set_fetching(repo_path)
                if live:
                    live.refresh()

                fetch_ok = fetch_repo(repo_path)
                display.clear_fetching(repo_path)

                if not fetch_ok:
                    remotes, _, _ = run_git(repo_path, ["remote"])
                    if remotes.strip():
                        warnings.append("{}: fetch failed".format(repo_name))

                status = parse_status_porcelain(repo_path)
                display.set_status(repo_path, status)

                if branch_all:
                    branches = get_all_branches_status(repo_path, status)
                    display.set_branches(repo_path, branches)

                if live:
                    live.refresh()

    if not console.is_terminal:
        console.print(display._render())

    for warning in warnings:
        console.print("[yellow]Warning:[/] {}".format(warning))
