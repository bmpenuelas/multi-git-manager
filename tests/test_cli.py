import io
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from multi_git_manager import cli
from multi_git_manager.display import LiveDisplay, _action, _fmt_status
from multi_git_manager.git_utils import (
    find_git_repos, get_all_branches_status, parse_status_porcelain,
)


class HelpTests(unittest.TestCase):
    def test_top_level_help_includes_every_command_parameter(self):
        help_text = cli._build_parser().format_help()
        for parameter in ('--branch-all', '--no-fetch', '--repo REPO',
                          '--repos REPOS', 'destination', '{push,pull,sync}',
                          '--submodules', '--submodules-none'):
            self.assertIn(parameter, help_text)
        self.assertNotIn('--branch_all', help_text)
        for command in ('status', 'action', 'copy', 'example'):
            self.assertIn(f'{command} command:', help_text)

    def test_status_flags_work_before_after_and_without_command(self):
        for arguments in (
            ['--branch-all', '--no-fetch'],
            ['--branch-all', '--no-fetch', 'status'],
            ['status', '--branch-all', '--no-fetch'],
            ['--branch-all', 'status', '--no-fetch'],
        ):
            with self.subTest(arguments=arguments):
                args = cli._build_parser().parse_args(arguments)
                self.assertTrue(args.branch_all)
                self.assertTrue(args.no_fetch)

    def test_command_help_lists_status_flags(self):
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            with self.assertRaises(SystemExit) as error:
                cli._build_parser().parse_args(['status', '--help'])
        self.assertEqual(error.exception.code, 0)
        self.assertIn('--no-fetch', output.getvalue())
        self.assertIn('--branch-all', output.getvalue())
        self.assertIn('--submodules-none', output.getvalue())

    def test_submodule_options_preserve_context_default_and_explicit_choice(self):
        parser = cli._build_parser()
        for arguments in ([], ['status']):
            self.assertIsNone(parser.parse_args(arguments).include_submodules)
        for flag, expected in (('--submodules', True), ('--submodules-none', False)):
            for arguments in ([flag], [flag, 'status'], ['status', flag]):
                with self.subTest(arguments=arguments):
                    self.assertIs(parser.parse_args(arguments).include_submodules, expected)

    def test_submodule_options_are_mutually_exclusive(self):
        for arguments in (['--submodules', '--submodules-none'],
                          ['status', '--submodules', '--submodules-none']):
            with patch('sys.stderr', new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as error:
                    cli._build_parser().parse_args(arguments)
            self.assertEqual(error.exception.code, 2)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.cleanup_temp)
        self.root = Path(self.temp.name).resolve()
        environment = patch.dict(os.environ, {'GIT_CEILING_DIRECTORIES': str(self.root.parent)})
        environment.start()
        self.addCleanup(environment.stop)

    def cleanup_temp(self):
        # Git for Windows helpers can briefly retain directory handles.
        for attempt in range(30):
            try:
                self.temp.cleanup()
                return
            except PermissionError:
                if attempt == 29:
                    raise
                time.sleep(0.1)

    def git(self, path, *args):
        result = subprocess.run(
            ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
             '-c', 'protocol.file.allow=always', '-c', 'gc.auto=0',
             '-c', 'maintenance.auto=false', *args],
            cwd=path, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def repo(self, name):
        path = self.root / name
        path.mkdir()
        self.git(path, 'init', '-b', 'main')
        (path / 'file.txt').write_text('initial\n')
        self.git(path, 'add', '.')
        self.git(path, 'commit', '-m', 'initial')
        return path

    def test_workspace_finds_sorted_children(self):
        z = self.repo('z')
        a = self.repo('a')
        (self.root / 'plain').mkdir()
        self.assertEqual(find_git_repos(str(self.root)), [str(a), str(z)])

    def test_current_repository_is_single_row(self):
        repo = self.repo('project')
        self.assertEqual(find_git_repos(str(repo)), [str(repo)])
        self.assertTrue(parse_status_porcelain(str(repo)).is_clean)

    def test_nested_directory_does_not_use_containing_repository(self):
        repo = self.repo('project')
        nested = repo / 'src' / 'nested'
        nested.mkdir(parents=True)
        self.assertEqual(find_git_repos(str(nested)), [])

    def test_non_repository_folder_inside_repository_scans_children(self):
        repo = self.repo('project')
        workspace = repo / 'workspace'
        workspace.mkdir()
        child = workspace / 'child'
        child.mkdir()
        self.git(child, 'init', '-b', 'main')
        self.assertEqual(find_git_repos(str(workspace)), [str(child)])

    def test_worktree_git_file(self):
        repo = self.repo('project')
        worktree = self.root / 'worktree'
        self.git(repo, 'worktree', 'add', '-b', 'other', str(worktree))
        self.assertEqual(find_git_repos(str(worktree)), [str(worktree)])

    def test_recursive_submodules_and_dirty_status(self):
        leaf = self.repo('leaf')
        middle = self.repo('middle')
        self.git(middle, 'submodule', 'add', str(leaf), 'nested leaf')
        self.git(middle, 'commit', '-am', 'nested submodule')
        repo = self.repo('project')
        self.git(repo, 'submodule', 'add', str(middle), 'libs/module with spaces')
        self.git(repo, 'commit', '-am', 'submodule')
        self.git(repo, 'submodule', 'update', '--init', '--recursive')
        module = repo / 'libs' / 'module with spaces'
        nested = module / 'nested leaf'
        expected = [str(repo), str(module), str(nested)]
        self.assertEqual(find_git_repos(str(repo)), expected)
        self.assertEqual(find_git_repos(str(repo), include_submodules=False), [str(repo)])
        # Scanning the workspace lists only direct-child repositories unless
        # submodules are explicitly enabled; repository mode defaults to on.
        children = [str(leaf), str(middle), str(repo)]
        self.assertEqual(find_git_repos(str(self.root)), children)
        expanded = [str(leaf), str(middle), str(middle / 'nested leaf'), *expected]
        self.assertEqual(find_git_repos(str(self.root), include_submodules=True), expanded)
        self.assertEqual(find_git_repos(str(self.root), include_submodules=False), children)
        self.assertTrue(parse_status_porcelain(str(nested)).is_clean)
        (nested / 'file.txt').write_text('changed\n')
        self.assertTrue(parse_status_porcelain(str(nested)).has_unstaged)
        self.assertFalse(parse_status_porcelain(str(module)).is_clean)

    def test_uninitialized_submodule_does_not_inherit_parent_status(self):
        source = self.repo('source')
        repo = self.repo('project')
        self.git(repo, 'submodule', 'add', str(source), 'module')
        self.git(repo, 'commit', '-am', 'submodule')
        self.git(repo, 'submodule', 'deinit', '-f', '--', 'module')
        module = str(repo / 'module')
        self.assertEqual(find_git_repos(str(repo)), [str(repo), module])
        status = parse_status_porcelain(module)
        self.assertFalse(status.is_initialized)
        self.assertIn('not initialized', _fmt_status(status))
        self.assertEqual(_action(module, status), '')
        self.assertEqual(get_all_branches_status(module, status), [])
        display = LiveDisplay([module])
        with patch.object(cli, 'requires_github_auth', return_value=False), \
                patch.object(cli, 'fetch_repo') as fetch:
            cli._FetchController([module], display, False, []).fetch()
        fetch.assert_not_called()

    def test_status_command_renders_single_repository(self):
        repo = self.repo('project')
        output = io.StringIO()
        console = Console(file=output, width=120, force_terminal=False)
        with patch.object(cli.os, 'getcwd', return_value=str(repo)), \
                patch.object(cli, 'console', console), \
                patch('sys.argv', ['mgitm', 'status', '--no-fetch']), \
                patch.object(cli, 'fetch_repo') as fetch:
            cli.main()
        self.assertIn('project', output.getvalue())
        self.assertIn('clean', output.getvalue())
        fetch.assert_not_called()

    def test_status_command_passes_submodule_choice_to_discovery(self):
        for flags, expected in (([], None), (['--submodules'], True),
                                (['--submodules-none'], False)):
            with self.subTest(flags=flags), \
                    patch('sys.argv', ['mgitm', 'status', '--no-fetch', *flags]), \
                    patch.object(cli, 'find_git_repos', return_value=[]) as discover, \
                    patch.object(cli, 'console'):
                cli.main()
            discover.assert_called_once_with(os.getcwd(), include_submodules=expected)

    def test_empty_folder(self):
        self.assertEqual(find_git_repos(str(self.root)), [])


if __name__ == '__main__':
    unittest.main()
