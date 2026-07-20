import sys
import shutil

from rich.console import Console
from rich.table import Table
from rich import box
from .git_utils import RepoStatus, BranchInfo

console = Console()


class Progress:
    def __init__(self) -> None:
        self._is_tty = sys.stdout.isatty()
        self._width = shutil.get_terminal_size((80, 20)).columns

    def update(self, text: str) -> None:
        if not self._is_tty:
            self._last_text = text
            return
        text = text[: self._width - 1]
        sys.stdout.write(f"\r\033[K{text}")
        sys.stdout.flush()

    def warn(self, text: str) -> None:
        if self._is_tty:
            sys.stdout.write(f"\r\033[K{text}\n")
        else:
            sys.stdout.write(f"{text}\n")
        sys.stdout.flush()

    def clear(self) -> None:
        if not self._is_tty:
            return
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()


def _fmt_ab(ahead: int, behind: int) -> str:
    parts = []
    if ahead > 0:
        parts.append(f"↑{ahead}")
    if behind > 0:
        parts.append(f"↓{behind}")
    return " ".join(parts) if parts else "—"


def _fmt_status(status: RepoStatus) -> str:
    if status.fetch_failed:
        return "[red]fetch failed[/]"
    if status.is_clean:
        return "[green]✓ clean[/]"
    parts = []
    if status.has_staged:
        parts.append("[yellow]staged[/]")
    if status.has_unstaged:
        parts.append("[red]unstaged[/]")
    if status.has_untracked:
        parts.append("[blue]untracked[/]")
    return ", ".join(parts)


def _fmt_remote(remote: str | None) -> str:
    return f"[dim]{remote}[/]" if remote else "[dim]none[/]"


def show_status(repos_status: list[RepoStatus]) -> None:
    if not repos_status:
        console.print("[yellow]No git repositories found[/]")
        return

    table = Table(title="mgitm status", box=box.ROUNDED)
    table.add_column("Repository", style="cyan")
    table.add_column("Branch", style="bold")
    table.add_column("Remote")
    table.add_column("↕", justify="center")
    table.add_column("Status")

    for s in repos_status:
        table.add_row(
            s.repo_name,
            s.branch,
            _fmt_remote(s.remote_branch),
            _fmt_ab(s.ahead, s.behind),
            _fmt_status(s),
        )

    console.print(table)


def show_branch_all(repos_branches: list[list[BranchInfo]]) -> None:
    if not repos_branches:
        console.print("[yellow]No git repositories found[/]")
        return

    table = Table(title="mgitm status — all branches", box=box.ROUNDED)
    table.add_column("Repository", style="cyan")
    table.add_column("Branch", style="bold")
    table.add_column("Remote")
    table.add_column("↕", justify="center")
    table.add_column("Status")

    for repo_branches in repos_branches:
        for i, info in enumerate(repo_branches):
            marker = "→" if info.is_current else " "
            branch_display = f"{marker} {info.branch}"

            if info.is_current and not info.is_clean:
                parts = []
                if info.has_staged:
                    parts.append("[yellow]staged[/]")
                if info.has_unstaged:
                    parts.append("[red]unstaged[/]")
                if info.has_untracked:
                    parts.append("[blue]untracked[/]")
                status_str = ", ".join(parts) if parts else "[green]✓ clean[/]"
            elif info.is_current:
                status_str = "[green]✓ clean[/]"
            else:
                status_str = "[dim]—[/]"

            table.add_row(
                info.repo_name if i == 0 else "",
                branch_display,
                _fmt_remote(info.remote_branch),
                _fmt_ab(info.ahead, info.behind),
                status_str,
            )

    console.print(table)
