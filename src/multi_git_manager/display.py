import os
import shutil
import threading
from functools import lru_cache
from urllib.parse import quote

from rich.console import Console
from rich.cells import cell_len
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

    return _vscode_link(repo_path, name)


def _vscode_link(repo_path: str, label: str) -> Text:
    """Create a VS Code folder hyperlink whose underline stops at its label."""

    path = os.path.abspath(repo_path).replace("\\", "/")
    vscode_uri = "vscode://file/{}".format(quote(path, safe="/:"))
    text = Text()
    text.append(label, style=Style(color="cyan", link=vscode_uri, underline=True))
    # Rich extends a cell's final style across its padding. End with a plain
    # space so the hyperlink underline stops at the repository name.
    text.append(" ", style=Style(color="cyan"))
    return text


def _open_action(repo_path: str) -> str | Text:
    """Offer VS Code for a dirty repository, without suggesting Git changes."""
    if not _vscode_is_installed():
        return ""
    return "Open (o)"


def _action(repo_path: str, status: RepoStatus) -> str | Text:
    """Return the safe sync action suggested by the current tracking state."""
    if not status.is_initialized:
        return ""
    if status.sync_conflict or not status.is_clean:
        return _open_action(repo_path)
    if status.fetch_failed or not status.remote_branch:
        return ""
    return "Sync (s)" if status.ahead or status.behind else ""


def _fmt_ab(ahead: int, behind: int, clean: bool = True) -> str:
    parts = []
    if ahead > 0:
        parts.append(f"↑{ahead}")
    if behind > 0:
        parts.append(f"↓{behind}")
    if parts:
        return "[yellow]{}[/]".format(" ".join(parts))
    return "✓" if clean else "[yellow]~[/]"


def _fmt_status(status: RepoStatus) -> str:
    if not status.is_initialized:
        return "[yellow]not initialized[/]"
    if status.sync_conflict:
        return "[red]rebase conflict[/]"
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
    def __init__(
        self, repos: list[str], branch_all: bool = False, title: str = "mgitm status"
    ):
        self.repos = repos
        self.branch_all = branch_all
        self.title = title
        self._statuses: dict[str, RepoStatus] = {}
        self._branches: dict[str, list[BranchInfo]] = {}
        self._fetching: set[str] = set()
        self._selected: str | None = None
        self._selected_branch: str | None = None
        self._warnings: list[str] = []
        self._prompt: str | None = None
        self._column_widths = [10, 6, 6, 1, 6, 7]
        self._lock = threading.RLock()

    def set_status(self, repo_path: str, status: RepoStatus) -> None:
        with self._lock:
            self._statuses[repo_path] = status

    def set_branches(self, repo_path: str, branches: list[BranchInfo]) -> None:
        with self._lock:
            self._branches[repo_path] = branches

    def set_fetching(self, repo_path: str) -> None:
        with self._lock:
            self._fetching.add(repo_path)

    def clear_fetching(self, repo_path: str) -> None:
        with self._lock:
            self._fetching.discard(repo_path)

    def set_selected(self, repo_path: str | None, branch: str | None = None) -> None:
        with self._lock:
            self._selected = repo_path
            self._selected_branch = branch

    def is_fetching(self, repo_path: str) -> bool:
        with self._lock:
            return repo_path in self._fetching

    def add_warning(self, message: str) -> None:
        with self._lock:
            self._warnings.append(message)

    def dismiss_warnings(self) -> None:
        with self._lock:
            self._warnings.clear()

    def set_prompt(self, prompt: str | None) -> None:
        with self._lock:
            self._prompt = prompt

    def _caption(self) -> Text:
        checkout = " · (c) checkout · (D) delete branch" if self.branch_all else ""
        caption = Text(
            "↑/↓ select · (s) sync · (S) sync all · (r) local refresh · "
            "(R) fetch refresh{} · (o) open · (d) dismiss warnings · (q) quit".format(
                checkout
            ),
            style="dim",
        )
        for warning in self._warnings:
            style = "red" if "failed" in warning else "yellow"
            caption.append("\n⚠ {}".format(warning), style=style)
        if self._prompt:
            caption.append("\n{}".format(self._prompt), style="bold yellow")
        return caption

    def _selected_repo_name(self, repo_path: str) -> str | Text:
        name = _repo_name(repo_path)
        if repo_path != self._selected or self._selected_branch is not None:
            return name
        if isinstance(name, Text):
            selected = Text("▶ ", style="bold yellow")
            selected.append_text(name)
            return selected
        return f"▶ {name}"

    def _branch_display(
        self, repo_path: str, branch: str, is_current: bool
    ) -> str | Text:
        if repo_path == self._selected and branch == self._selected_branch:
            selected = Text("▶ ", style="bold yellow")
            selected.append(branch)
            return selected
        marker = "→" if is_current else " "
        return f"{marker} {branch}"

    def _render(self) -> Table:
        with self._lock:
            self._measure_columns()
            if self.branch_all:
                return self._render_all_branches()
            return self._render_status()

    def _measure_columns(self) -> None:
        """Remember the widest content so transient states never shrink columns."""
        for repo_path in self.repos:
            repo_name = os.path.basename(repo_path)
            if repo_path == self._selected:
                repo_name = f"▶ {repo_name}"
            status = self._statuses.get(repo_path)
            if not status:
                self._remember_widths(repo_name, "…", "", "", "…", "")
                continue

            status_text = "fetching…" if repo_path in self._fetching else _fmt_status(status)
            action = "" if repo_path in self._fetching else _action(repo_path, status)
            self._remember_widths(
                repo_name,
                status.branch,
                _fmt_remote(status.remote_branch),
                _fmt_ab(status.ahead, status.behind, status.is_clean),
                status_text,
                action,
            )
            if self.branch_all:
                for info in self._branches.get(repo_path, []):
                    self._remember_widths(
                        "",
                        f"→ {info.branch}",
                        _fmt_remote(info.remote_branch),
                        _fmt_ab(info.ahead, info.behind, status.is_clean),
                        status_text if info.is_current else "—",
                        action if info.is_current else "",
                    )

    def _remember_widths(self, *values: str | Text) -> None:
        for index, value in enumerate(values):
            plain = value.plain if isinstance(value, Text) else Text.from_markup(value).plain
            self._column_widths[index] = max(self._column_widths[index], cell_len(plain))

    def _render_status(self) -> Table:
        table = Table(title=self.title, box=box.ROUNDED, show_lines=True)
        table.caption = self._caption()
        table.add_column("Repository", style="cyan", min_width=self._column_widths[0])
        table.add_column("Branch", style="bold", min_width=self._column_widths[1])
        table.add_column("Remote", min_width=self._column_widths[2])
        table.add_column("↕", justify="center", min_width=self._column_widths[3])
        table.add_column("Status", min_width=self._column_widths[4])
        table.add_column("Actions", min_width=self._column_widths[5])

        for repo_path in self.repos:
            repo_name = self._selected_repo_name(repo_path)
            s = self._statuses.get(repo_path)

            if repo_path in self._fetching:
                if s:
                    table.add_row(
                        repo_name,
                        s.branch,
                        _fmt_remote(s.remote_branch),
                        _fmt_ab(s.ahead, s.behind, s.is_clean),
                        "[dim]fetching…[/]",
                        "",
                    )
                else:
                    table.add_row(repo_name, "…", "", "", "[dim]fetching…[/]", "")
            elif s:
                table.add_row(
                    repo_name,
                    s.branch,
                    _fmt_remote(s.remote_branch),
                    _fmt_ab(s.ahead, s.behind, s.is_clean),
                    _fmt_status(s),
                    _action(repo_path, s),
                )
            else:
                table.add_row(repo_name, "…", "", "", "[dim]…[/]", "")

        return table

    def _render_all_branches(self) -> Table:
        table = Table(
            title="{} — all branches".format(self.title),
            box=box.ROUNDED,
            show_lines=True,
        )
        table.caption = self._caption()
        table.add_column("Repository", style="cyan", min_width=self._column_widths[0])
        table.add_column("Branch", style="bold", min_width=self._column_widths[1])
        table.add_column("Remote", min_width=self._column_widths[2])
        table.add_column("↕", justify="center", min_width=self._column_widths[3])
        table.add_column("Status", min_width=self._column_widths[4])
        table.add_column("Actions", min_width=self._column_widths[5])

        for repo_path in self.repos:
            repo_name = self._selected_repo_name(repo_path)
            s = self._statuses.get(repo_path)
            branches = self._branches.get(repo_path, [])

            clean = s.is_clean if s else True

            if repo_path in self._fetching:
                if branches:
                    for i, info in enumerate(branches):
                        branch_display = self._branch_display(
                            repo_path, info.branch, info.is_current
                        )
                        status_str = "[dim]fetching…[/]" if info.is_current else "[dim]—[/]"
                        table.add_row(
                            repo_name if i == 0 else "",
                            branch_display,
                            _fmt_remote(info.remote_branch),
                            _fmt_ab(info.ahead, info.behind, clean),
                            status_str,
                            _action(repo_path, s) if info.is_current and s else "",
                        )
                elif s:
                    table.add_row(
                        repo_name,
                        self._branch_display(repo_path, s.branch, True),
                        _fmt_remote(s.remote_branch),
                        _fmt_ab(s.ahead, s.behind, clean),
                        "[dim]fetching…[/]",
                        "",
                    )
                else:
                    table.add_row(repo_name, "…", "", "", "[dim]fetching…[/]", "")
                continue

            if branches:
                for i, info in enumerate(branches):
                    branch_display = self._branch_display(
                        repo_path, info.branch, info.is_current
                    )
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
                        _action(repo_path, s) if info.is_current and s else "",
                    )
            elif s:
                table.add_row(
                    repo_name,
                    self._branch_display(repo_path, s.branch, True),
                    _fmt_remote(s.remote_branch),
                    _fmt_ab(s.ahead, s.behind, clean),
                    _fmt_status(s),
                    _action(repo_path, s),
                )
            else:
                table.add_row(repo_name, "…", "", "", "[dim]…[/]", "")

        return table
