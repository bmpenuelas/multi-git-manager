import argparse
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from contextlib import contextmanager, nullcontext

from rich.live import Live

from .git_utils import (
    RepoStatus,
    find_git_repos,
    fetch_repo,
    parse_status_porcelain,
    get_all_branches_status,
    run_git,
    requires_github_auth,
)
from .display import console, LiveDisplay


# These directories contain installed dependencies or disposable tool caches.
# Keep source-controlled configuration (lock files, requirements files, etc.)
# so each copied project can recreate them with its usual install command.
_COPY_IGNORED_DIRECTORIES = frozenset(
    {
        "node_modules",
        "bower_components",
        ".venv",
        "venv",
        "env",
        ".tox",
        ".nox",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".cache",
        ".gradle",
        ".parcel-cache",
        ".turbo",
    }
)


class _HelpParser(argparse.ArgumentParser):
    def format_help(self) -> str:
        help_text = super().format_help()
        for action in self._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, command_parser in action.choices.items():
                    help_text += f"\n{name} command:\n{command_parser.format_help()}"
        return help_text


def _add_submodule_options(parser: argparse.ArgumentParser, default: object) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--submodules", dest="include_submodules", action="store_true",
        default=default,
        help="Include submodules (default when the current directory is a repository)",
    )
    group.add_argument(
        "--submodules-none", dest="include_submodules", action="store_false",
        default=default,
        help="Exclude submodules (default when scanning child repositories)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = _HelpParser(
        prog="mgitm",
        description="Run git commands across multiple repositories",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Skip fetching from remotes",
    )
    parser.add_argument(
        "--branch-all",
        action="store_true",
        help="Show status for every local branch",
    )
    _add_submodule_options(parser, default=None)
    subparsers = parser.add_subparsers(dest="command")

    status_parser = subparsers.add_parser(
        "status", help="Fetch and show status of all repos"
    )
    _add_submodule_options(status_parser, default=argparse.SUPPRESS)
    status_parser.add_argument(
        "--branch-all",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Show status for every local branch",
    )
    status_parser.add_argument(
        "--no-fetch",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Skip fetching from remotes",
    )
    action_parser = subparsers.add_parser(
        "action", help="Run an action selected from the status table"
    )
    action_parser.add_argument(
        "operation", choices=("push", "pull", "sync"), help="Git action to confirm and run"
    )
    action_parser.add_argument("--repo", required=True, help="Path to the Git repository")
    copy_parser = subparsers.add_parser(
        "copy", help="Copy the current repository or child repositories to a destination"
    )
    copy_parser.add_argument("destination", help="Directory that will contain the copies")
    copy_parser.add_argument(
        "--repos",
        nargs="+",
        help="Comma-separated repository directory names to copy (default: all)",
    )
    subparsers.add_parser("example", help="Show an example status table")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "action":
        _run_action(args.repo, args.operation)
        return

    if args.command == "example":
        _show_example()
        return

    if args.command == "copy":
        _copy_repositories(os.getcwd(), args.destination, args.repos)
        return

    if args.command not in (None, "status"):
        parser.print_help()
        return

    root_dir = os.getcwd()
    repos = find_git_repos(root_dir, include_submodules=args.include_submodules)

    if not repos:
        console.print(
            "[yellow]No git repositories found in[/] [bold]{}[/]".format(root_dir)
        )
        return

    branch_all = getattr(args, "branch_all", False)
    no_fetch = getattr(args, "no_fetch", False)

    display = LiveDisplay(repos, branch_all=branch_all)

    # Rich cannot redraw in redirected / non-interactive terminals. Refreshing
    # a Live display there appends a new table on every update, so only use it
    # when the shared console can actually move the cursor.
    live_context = (
        Live(
            get_renderable=display._render,
            console=console,
            auto_refresh=False,
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

        fetcher = _FetchController(
            repos, display, branch_all, warnings, refresh=live.refresh if live else None
        )
        if live:
            if not no_fetch:
                fetcher.start()
            _handle_keyboard(live, display, repos, branch_all, warnings, fetcher)
        elif not no_fetch:
            fetcher.fetch()

    if not console.is_terminal:
        console.print(display._render())

    if not console.is_terminal:
        for warning in warnings:
            console.print("[yellow]Warning:[/] {}".format(warning))


def _handle_keyboard(
    live: Live,
    display: LiveDisplay,
    repos: list[str],
    branch_all: bool,
    warnings: list[str],
    fetcher: "_FetchController",
) -> None:
    """Keep the live table open and handle its compact keyboard controls."""
    selected_index: int | None = None
    pending_delete: tuple[str, str] | None = None

    try:
        with _key_reader() as read_key:
            while True:
                key = read_key()
                if key in ("q", "\x03"):
                    return
                rows = _selection_rows(repos, display, branch_all)
                if not rows:
                    continue
                if pending_delete:
                    repo_path, branch = pending_delete
                    if key.lower() == "y":
                        _delete_branch_and_refresh(repo_path, branch, display, warnings)
                    display.set_prompt(None)
                    pending_delete = None
                    rows = _selection_rows(repos, display, branch_all)
                    if selected_index is not None and rows:
                        selected_index = min(selected_index, len(rows) - 1)
                    live.refresh()
                    continue
                if key == "up":
                    selected_index = (
                        len(rows) - 1
                        if selected_index is None
                        else (selected_index - 1) % len(rows)
                    )
                elif key == "down":
                    selected_index = (
                        0
                        if selected_index is None
                        else (selected_index + 1) % len(rows)
                    )
                elif key == "s" and selected_index is not None:
                    repo_path, branch = rows[selected_index]
                    status = display._statuses.get(repo_path)
                    if not branch_all or (status and branch == status.branch):
                        _sync_and_refresh(repo_path, display, branch_all, warnings)
                elif key == "S":
                    for repo_path in repos:
                        _sync_and_refresh(repo_path, display, branch_all, warnings)
                elif key == "r":
                    _refresh_local(repos, display, branch_all)
                elif key == "R":
                    fetcher.start()
                elif key == "o" and selected_index is not None:
                    _open_in_vscode(rows[selected_index][0], display, warnings)
                elif key == "c" and branch_all and selected_index is not None:
                    repo_path, branch = rows[selected_index]
                    _checkout_and_refresh(repo_path, branch, display, warnings)
                elif key == "D" and branch_all and selected_index is not None:
                    repo_path, branch = rows[selected_index]
                    if _can_delete_branch(repo_path, branch, display, warnings):
                        pending_delete = (repo_path, branch)
                        display.set_prompt(
                            "Delete branch '{}' from {}? (y/N)".format(
                                branch, os.path.basename(repo_path)
                            )
                        )
                elif key == "d":
                    warnings.clear()
                    display.dismiss_warnings()

                rows = _selection_rows(repos, display, branch_all)
                if selected_index is not None and rows:
                    selected_index = min(selected_index, len(rows) - 1)
                    repo_path, branch = rows[selected_index]
                    display.set_selected(repo_path, branch if branch_all else None)
                else:
                    display.set_selected(None)
                live.refresh()
    except KeyboardInterrupt:
        return


def _selection_rows(
    repos: list[str], display: LiveDisplay, branch_all: bool
) -> list[tuple[str, str]]:
    if not branch_all:
        return [(repo_path, "") for repo_path in repos]

    rows: list[tuple[str, str]] = []
    with display._lock:
        for repo_path in repos:
            branches = display._branches.get(repo_path, [])
            if branches:
                rows.extend((repo_path, info.branch) for info in branches)
            elif status := display._statuses.get(repo_path):
                rows.append((repo_path, status.branch))
    return rows


def _copy_repositories(
    source_root: str, destination: str, requested_repos: list[str] | None = None
) -> None:
    """Copy repositories once, including any submodule working trees."""
    source_root = os.path.abspath(source_root)
    destination = os.path.abspath(os.path.expanduser(destination))
    repos = find_git_repos(source_root, include_submodules=False)

    requested_names = _parse_copy_repos(requested_repos)
    if requested_names:
        repos, missing = _filter_copy_repos(repos, requested_names)
        if missing:
            console.print(
                "[yellow]Not a discovered Git repository:[/] {}".format(
                    ", ".join(missing)
                )
            )

    if not repos:
        console.print("[yellow]No selected git repositories found in[/] [bold]{}[/]".format(source_root))
        return
    if os.path.exists(destination) and not os.path.isdir(destination):
        console.print("[red]Destination is not a directory:[/] {}".format(destination))
        return
    if os.path.normcase(destination) == os.path.normcase(source_root):
        console.print("[red]Destination must be different from the current directory.[/]")
        return
    if any(_is_within(destination, repo_path) for repo_path in repos):
        console.print(
            "[red]Destination cannot be inside one of the repositories being copied.[/]"
        )
        return

    os.makedirs(destination, exist_ok=True)
    for repo_path in repos:
        target_path = os.path.join(destination, os.path.basename(repo_path))
        if os.path.lexists(target_path):
            operation = _choose_copy_operation(target_path)
            if operation == "skip":
                console.print("[dim]Skipped[/] {}".format(os.path.basename(repo_path)))
                continue
            if operation == "replace":
                try:
                    _remove_copy_target(target_path)
                except OSError as error:
                    console.print("[red]Could not remove {}:[/] {}".format(target_path, error))
                    continue

        try:
            shutil.copytree(
                repo_path,
                target_path,
                dirs_exist_ok=True,
                ignore=_ignore_copy_directories,
            )
        except OSError as error:
            console.print(
                "[red]Could not copy {}:[/] {}".format(os.path.basename(repo_path), error)
            )
            continue
        console.print("[green]Copied[/] {}".format(os.path.basename(repo_path)))


def _parse_copy_repos(arguments: list[str] | None) -> list[str]:
    """Parse comma-separated names, tolerating whitespace and shell quotes."""
    if not arguments:
        return []

    names: list[str] = []
    for part in ",".join(arguments).split(","):
        name = part.strip()
        if len(name) >= 2 and name[0] == name[-1] and name[0] in ("'", '"'):
            name = name[1:-1].strip()
        if name:
            names.append(name)
    return names


def _filter_copy_repos(
    repos: list[str], requested_names: list[str]
) -> tuple[list[str], list[str]]:
    """Keep requested direct-child repositories in the normal sorted order."""
    requested = {os.path.normcase(name) for name in requested_names}
    available = {os.path.normcase(os.path.basename(repo_path)) for repo_path in repos}
    selected = [
        repo_path
        for repo_path in repos
        if os.path.normcase(os.path.basename(repo_path)) in requested
    ]
    missing = [
        name for name in requested_names if os.path.normcase(name) not in available
    ]
    return selected, missing


def _is_within(path: str, parent: str) -> bool:
    """Return whether *path* is within *parent*, including the same path."""
    try:
        return os.path.commonpath((os.path.normcase(path), os.path.normcase(parent))) == os.path.normcase(parent)
    except ValueError:
        return False


def _choose_copy_operation(target_path: str) -> str:
    """Ask how an existing target should be handled, defaulting safely to skip."""
    # Treat links as files so merge never writes through a destination link.
    if not os.path.isdir(target_path) or os.path.islink(target_path):
        prompt = "{} exists as a file. [d]elete and replace or [s]kip? ".format(target_path)
        valid = {"d": "replace", "delete": "replace", "s": "skip", "skip": "skip", "": "skip"}
    else:
        prompt = (
            "{} already exists. [d]elete and replace, [m]erge (source overwrites), "
            "or [s]kip? "
        ).format(target_path)
        valid = {
            "d": "replace",
            "delete": "replace",
            "m": "merge",
            "merge": "merge",
            "s": "skip",
            "skip": "skip",
            "": "skip",
        }

    while True:
        try:
            choice = input(prompt).strip().lower()
        except EOFError:
            return "skip"
        if choice in valid:
            return valid[choice]
        console.print("[yellow]Enter d, m, or s.[/]")


def _remove_copy_target(target_path: str) -> None:
    if os.path.isdir(target_path) and not os.path.islink(target_path):
        shutil.rmtree(target_path)
    else:
        os.unlink(target_path)


def _ignore_copy_directories(directory: str, names: list[str]) -> set[str]:
    """``copytree`` callback that omits installed dependencies and caches."""
    return {
        name
        for name in names
        if (
            (name in _COPY_IGNORED_DIRECTORIES or name.startswith(".venv-"))
            and os.path.isdir(os.path.join(directory, name))
        )
    }


class _FetchController:
    """Run serial fetches in the background while the live UI stays usable."""

    def __init__(
        self,
        repos: list[str],
        display: LiveDisplay,
        branch_all: bool,
        warnings: list[str],
        refresh: Callable[[], None] | None = None,
    ) -> None:
        self.repos = repos
        self.display = display
        self.branch_all = branch_all
        self.warnings = warnings
        self.refresh = refresh
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._thread = threading.Thread(target=self.fetch, daemon=True)
            self._thread.start()
            return True

    def fetch(self) -> None:
        github_auth_repos = [
            repo_path for repo_path in self.repos if requires_github_auth(repo_path)
        ]
        github_auth_repo_paths = set(github_auth_repos)
        non_interactive_repos = [
            repo_path for repo_path in self.repos
            if repo_path not in github_auth_repo_paths
        ]

        # GitHub may invoke an interactive login through either gh or Git
        # Credential Manager, so fetch those repositories last.
        for repo_path in non_interactive_repos + github_auth_repos:
            status = parse_status_porcelain(repo_path)
            if not status.is_initialized:
                self.display.set_status(repo_path, status)
                self._refresh()
                continue
            repo_name = os.path.basename(repo_path)
            self.display.set_fetching(repo_path)
            self._refresh()
            fetch_ok = fetch_repo(repo_path)
            self.display.clear_fetching(repo_path)

            if not fetch_ok:
                remotes, _, _ = run_git(repo_path, ["remote"])
                if remotes.strip():
                    warning = "{}: fetch failed".format(repo_name)
                    self.warnings.append(warning)
                    self.display.add_warning(warning)

            status = parse_status_porcelain(repo_path)
            self.display.set_status(repo_path, status)
            if self.branch_all:
                self.display.set_branches(
                    repo_path, get_all_branches_status(repo_path, status)
                )
            self._refresh()

    def _refresh(self) -> None:
        if self.refresh:
            self.refresh()


def _refresh_local(
    repos: list[str], display: LiveDisplay, branch_all: bool
) -> None:
    for repo_path in repos:
        status = parse_status_porcelain(repo_path)
        display.set_status(repo_path, status)
        if branch_all:
            display.set_branches(repo_path, get_all_branches_status(repo_path, status))


def _add_warning(display: LiveDisplay, warnings: list[str], message: str) -> None:
    warnings.append(message)
    display.add_warning(message)


def _sync_and_refresh(
    repo_path: str,
    display: LiveDisplay,
    branch_all: bool,
    warnings: list[str],
) -> None:
    status = display._statuses.get(repo_path)
    if (
        not status
        or display.is_fetching(repo_path)
        or not status.is_clean
        or not status.remote_branch
    ):
        return
    if not (status.ahead or status.behind):
        return

    display.set_fetching(repo_path)
    try:
        if status.ahead and status.behind:
            _, stderr, rc = run_git(
                repo_path, ["fetch", "--all", "--prune"], timeout=120
            )
            if rc != 0:
                _add_warning(display, warnings, "{}: sync failed: {}".format(status.repo_name, stderr))
                return
            status = parse_status_porcelain(repo_path)
            if not status.remote_branch:
                _add_warning(display, warnings, "{}: sync failed: no remote branch".format(status.repo_name))
                return
            _, stderr, rc = run_git(
                repo_path, ["rebase", status.remote_branch], timeout=120
            )
            if rc != 0:
                conflicted_status = parse_status_porcelain(repo_path)
                conflicts, _, _ = run_git(
                    repo_path, ["diff", "--name-only", "--diff-filter=U"]
                )
                if conflicts:
                    conflicted_status.sync_conflict = True
                    display.set_status(repo_path, conflicted_status)
                    if branch_all:
                        display.set_branches(
                            repo_path,
                            get_all_branches_status(repo_path, conflicted_status),
                        )
                else:
                    _add_warning(
                        display, warnings, "{}: sync failed: {}".format(status.repo_name, stderr)
                    )
                return
        elif status.behind:
            _, stderr, rc = run_git(repo_path, ["pull", "--rebase"], timeout=120)
            if rc != 0:
                _add_warning(display, warnings, "{}: sync failed: {}".format(status.repo_name, stderr))
                return

        status = parse_status_porcelain(repo_path)
        if status.ahead:
            _, stderr, rc = run_git(repo_path, ["push"], timeout=120)
            if rc != 0:
                _add_warning(display, warnings, "{}: sync failed: {}".format(status.repo_name, stderr))
                return
    finally:
        display.clear_fetching(repo_path)

    status = parse_status_porcelain(repo_path)
    display.set_status(repo_path, status)
    if branch_all:
        display.set_branches(repo_path, get_all_branches_status(repo_path, status))


def _checkout_and_refresh(
    repo_path: str, branch: str, display: LiveDisplay, warnings: list[str]
) -> None:
    status = display._statuses.get(repo_path)
    if not status or status.branch == branch:
        return
    if display.is_fetching(repo_path):
        _add_warning(
            display,
            warnings,
            "{}: checkout is unavailable while fetching".format(status.repo_name),
        )
        return

    display.set_fetching(repo_path)
    try:
        _, stderr, rc = run_git(repo_path, ["checkout", branch], timeout=120)
        if rc != 0:
            _add_warning(
                display,
                warnings,
                "{}: checkout failed: {}".format(status.repo_name, stderr),
            )
            return
    finally:
        display.clear_fetching(repo_path)

    status = parse_status_porcelain(repo_path)
    display.set_status(repo_path, status)
    display.set_branches(repo_path, get_all_branches_status(repo_path, status))


def _can_delete_branch(
    repo_path: str, branch: str, display: LiveDisplay, warnings: list[str]
) -> bool:
    status = display._statuses.get(repo_path)
    if not status:
        return False
    if branch == status.branch:
        _add_warning(
            display,
            warnings,
            "{}: cannot delete the current branch '{}'".format(status.repo_name, branch),
        )
        return False
    if display.is_fetching(repo_path):
        _add_warning(
            display,
            warnings,
            "{}: branch deletion is unavailable while fetching".format(status.repo_name),
        )
        return False

    _, _, rc = run_git(repo_path, ["merge-base", "--is-ancestor", branch, "HEAD"])
    if rc != 0:
        _add_warning(
            display,
            warnings,
            "{}: branch '{}' is not fully merged; refusing to delete it".format(
                status.repo_name, branch
            ),
        )
        return False
    return True


def _delete_branch_and_refresh(
    repo_path: str, branch: str, display: LiveDisplay, warnings: list[str]
) -> None:
    # The user may spend time at the confirmation prompt while a background
    # fetch finishes, so validate the branch again immediately before changing
    # the repository.
    if not _can_delete_branch(repo_path, branch, display, warnings):
        return
    status = display._statuses.get(repo_path)
    if not status:
        return
    _, stderr, rc = run_git(repo_path, ["branch", "-d", branch])
    if rc != 0:
        _add_warning(
            display,
            warnings,
            "{}: branch deletion failed: {}".format(status.repo_name, stderr),
        )
        return

    refreshed_status = parse_status_porcelain(repo_path)
    refreshed_branches = get_all_branches_status(repo_path, refreshed_status)
    if any(info.branch == branch for info in refreshed_branches):
        _add_warning(
            display,
            warnings,
            "{}: branch '{}' still exists after deletion".format(status.repo_name, branch),
        )
        return
    display.set_status(repo_path, refreshed_status)
    display.set_branches(repo_path, refreshed_branches)


def _open_in_vscode(
    repo_path: str, display: LiveDisplay, warnings: list[str]
) -> None:
    status = display._statuses.get(repo_path)
    if not status:
        return

    code_command = shutil.which("code")
    if not code_command:
        install_roots = (
            os.environ.get("LOCALAPPDATA"),
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
        )
        for root in install_roots:
            candidate = (
                os.path.join(root, "Microsoft VS Code", "Code.exe") if root else None
            )
            if candidate and os.path.isfile(candidate):
                code_command = candidate
                break
    if not code_command:
        _add_warning(display, warnings, "{}: VS Code is not installed".format(status.repo_name))
        return

    subprocess.Popen([code_command, "--new-window", repo_path])


@contextmanager
def _key_reader():
    """Yield a blocking key reader while restoring terminal settings on exit."""
    if os.name == "nt":
        import msvcrt

        def read_windows_key() -> str:
            key = msvcrt.getwch()
            if key in ("\x00", "\xe0"):
                key = msvcrt.getwch()
                return {"H": "up", "P": "down"}.get(key, "")
            return key

        yield read_windows_key
        return

    import termios
    import tty

    fd = sys.stdin.fileno()
    original_settings = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    try:
        def read_posix_key() -> str:
            key = os.read(fd, 1).decode(errors="ignore")
            if key != "\x1b":
                return key
            return {"[A": "up", "[B": "down"}.get(
                os.read(fd, 2).decode(errors="ignore"), ""
            )

        yield read_posix_key
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original_settings)


def _show_example() -> None:
    """Print a deterministic status table without reading local repositories."""
    example_root = os.path.join(os.getcwd(), "example-repos")
    examples = [
        RepoStatus(
            "frontend",
            os.path.join(example_root, "frontend"),
            "main",
            0,
            0,
            False,
            False,
            False,
            True,
            "origin/main",
        ),
        RepoStatus(
            "api",
            os.path.join(example_root, "api"),
            "main",
            2,
            0,
            False,
            False,
            False,
            True,
            "origin/main",
        ),
        RepoStatus(
            "docs",
            os.path.join(example_root, "docs"),
            "develop",
            0,
            3,
            False,
            False,
            False,
            True,
            "origin/develop",
        ),
        RepoStatus(
            "worker",
            os.path.join(example_root, "worker"),
            "feature/jobs",
            0,
            0,
            True,
            True,
            False,
            False,
            "origin/feature/jobs",
        ),
    ]
    display = LiveDisplay(
        [status.repo_path for status in examples], title="mgitm example"
    )
    for status in examples:
        display.set_status(status.repo_path, status)
    console.print(display._render())


def _run_action(repo_path: str, operation: str) -> None:
    """Confirm and run an action selected from an Actions link."""
    repo_path = os.path.abspath(repo_path)
    git_path = os.path.join(repo_path, ".git")
    if not (os.path.isdir(git_path) or os.path.isfile(git_path)):
        console.print("[red]Not a git repository:[/] {}".format(repo_path))
        return

    git_args = {
        "push": ["push"],
        "pull": ["pull", "--ff-only"],
        "sync": ["pull", "--rebase"],
    }[operation]
    command = "git " + " ".join(git_args)
    try:
        confirmed = input("Run '{}' in {}? [y/N] ".format(command, repo_path))
    except EOFError:
        return
    if confirmed.lower() not in ("y", "yes"):
        console.print("[dim]Cancelled.[/]")
        return

    _execute_action(repo_path, operation)


def _execute_action(repo_path: str, operation: str) -> None:
    git_args = {
        "push": ["push"],
        "pull": ["pull", "--ff-only"],
        "sync": ["pull", "--rebase"],
    }[operation]
    try:
        result = subprocess.run(["git", *git_args], cwd=repo_path)
    except FileNotFoundError:
        console.print("[red]git not found[/]")
        return
    if result.returncode != 0:
        console.print("[red]Action failed.[/]")
