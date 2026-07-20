import os
import shutil
from functools import lru_cache
from urllib.parse import quote

from rich.console import Console
from rich.style import Style
from rich.table import Table
from rich.text import Text
from rich import box
from .git_utils import RepoStatus, BranchInfo

console = Console()


@lru_cache(maxsize=1)
def _vscode_is_installed() -> bool:
    """Check for VS Code without starting a process."""
    if shutil.which("code"):
        return True

    install_roots = (
        os.environ.get("LOCALAPPDATA"),
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMFILES(X86)"),
    )
    return any(
        root and os.path.isfile(os.path.join(root, "Microsoft VS Code", "Code.exe"))
        for root in install_roots
    )


def _repo_name(repo_path: str) -> str | Text:
    """Render a repo name as a VS Code link when terminal hyperlinks work."""
    name = os.path.basename(repo_path)
    if not console.is_terminal or not _vscode_is_installed():
        return name

    path = os.path.abspath(repo_path).replace("\\", "/")
    vscode_uri = "vscode://file/{}".format(quote(path, safe="/:"))
    text = Text()
    text.append(name, style=Style(color="cyan", link=vscode_uri, underline=True))
    # Rich extends a cell's final style across its padding. End with a plain
    # space so the hyperlink underline stops at the repository name.
    text.append(" ", style=Style(color="cyan"))
    return text


def _fmt_ab(ahead: int, behind: int, clean: bool = True) -> str:
    parts = []
    if ahead > 0:
        parts.append(f"↑{ahead}")
    if behind > 0:
        parts.append(f"↓{behind}")
    if parts:
        return " ".join(parts)
    return "✓" if clean else "~"


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


class LiveDisplay:
    def __init__(self, repos: list[str], branch_all: bool = False):
        self.repos = repos
        self.branch_all = branch_all
        self._statuses: dict[str, RepoStatus] = {}
        self._branches: dict[str, list[BranchInfo]] = {}
        self._fetching: set[str] = set()

    def set_status(self, repo_path: str, status: RepoStatus) -> None:
        self._statuses[repo_path] = status

    def set_branches(self, repo_path: str, branches: list[BranchInfo]) -> None:
        self._branches[repo_path] = branches

    def set_fetching(self, repo_path: str) -> None:
        self._fetching.add(repo_path)

    def clear_fetching(self, repo_path: str) -> None:
        self._fetching.discard(repo_path)

    def _render(self) -> Table:
        if self.branch_all:
            return self._render_all_branches()
        return self._render_status()

    def _render_status(self) -> Table:
        table = Table(title="mgitm status", box=box.ROUNDED)
        table.add_column("Repository", style="cyan")
        table.add_column("Branch", style="bold")
        table.add_column("Remote")
        table.add_column("↕", justify="center")
        table.add_column("Status")

        for repo_path in self.repos:
            repo_name = _repo_name(repo_path)
            s = self._statuses.get(repo_path)

            if repo_path in self._fetching:
                if s:
                    table.add_row(
                        repo_name,
                        s.branch,
                        _fmt_remote(s.remote_branch),
                        _fmt_ab(s.ahead, s.behind, s.is_clean),
                        "[dim]fetching…[/]",
                    )
                else:
                    table.add_row(repo_name, "…", "", "", "[dim]fetching…[/]")
            elif s:
                table.add_row(
                    repo_name,
                    s.branch,
                    _fmt_remote(s.remote_branch),
                    _fmt_ab(s.ahead, s.behind, s.is_clean),
                    _fmt_status(s),
                )
            else:
                table.add_row(repo_name, "…", "", "", "[dim]…[/]")

        return table

    def _render_all_branches(self) -> Table:
        table = Table(title="mgitm status — all branches", box=box.ROUNDED)
        table.add_column("Repository", style="cyan")
        table.add_column("Branch", style="bold")
        table.add_column("Remote")
        table.add_column("↕", justify="center")
        table.add_column("Status")

        for repo_path in self.repos:
            repo_name = _repo_name(repo_path)
            s = self._statuses.get(repo_path)
            branches = self._branches.get(repo_path, [])

            clean = s.is_clean if s else True

            if repo_path in self._fetching:
                if branches:
                    for i, info in enumerate(branches):
                        marker = "→" if info.is_current else " "
                        branch_display = f"{marker} {info.branch}"
                        status_str = "[dim]fetching…[/]" if info.is_current else "[dim]—[/]"
                        table.add_row(
                            repo_name if i == 0 else "",
                            branch_display,
                            _fmt_remote(info.remote_branch),
                            _fmt_ab(info.ahead, info.behind, clean),
                            status_str,
                        )
                elif s:
                    table.add_row(
                        repo_name,
                        f"→ {s.branch}",
                        _fmt_remote(s.remote_branch),
                        _fmt_ab(s.ahead, s.behind, clean),
                        "[dim]fetching…[/]",
                    )
                else:
                    table.add_row(repo_name, "…", "", "", "[dim]fetching…[/]")
                continue

            if branches:
                for i, info in enumerate(branches):
                    marker = "→" if info.is_current else " "
                    branch_display = f"{marker} {info.branch}"
                    if info.is_current and s:
                        status_str = _fmt_status(s)
                    else:
                        status_str = "[dim]—[/]"
                    table.add_row(
                        repo_name if i == 0 else "",
                        branch_display,
                        _fmt_remote(info.remote_branch),
                        _fmt_ab(info.ahead, info.behind, clean),
                        status_str,
                    )
            elif s:
                table.add_row(
                    repo_name,
                    f"→ {s.branch}",
                    _fmt_remote(s.remote_branch),
                    _fmt_ab(s.ahead, s.behind, clean),
                    _fmt_status(s),
                )
            else:
                table.add_row(repo_name, "…", "", "", "[dim]…[/]")

        return table
