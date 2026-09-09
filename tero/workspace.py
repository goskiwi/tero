"""Workspace identity and bounded, read-only Git observations."""
import os
import subprocess
import tempfile
from pathlib import Path


def git(root, *args, budget=None):
    if budget:
        budget.check()
    timeout = budget.remaining(5) if budget else 5
    environment = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    environment['LC_ALL'] = 'C'
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        result = subprocess.run(
            ['git', '--no-optional-locks', '-c', 'core.fsmonitor=false', *args],
            cwd=root, env=environment, stdout=stdout, stderr=stderr, timeout=timeout,
        )
        for stream, name in ((stdout, 'stdout'), (stderr, 'stderr')):
            stream.seek(0)
            payload = stream.read(4 * 1024 * 1024 + 1)
            if len(payload) > 4 * 1024 * 1024:
                raise OSError('Git observation output exceeded limit; state unknown')
            setattr(result, name, payload)
    if budget:
        budget.check()
    return result


class Workspace:
    def __init__(self, directory, *, root=None):
        self.startup = Path(directory).resolve()
        if not self.startup.is_dir():
            raise ValueError('Workspace must be an existing directory')
        probe = Path(root).resolve() if root is not None else self.startup
        if not probe.is_dir():
            raise ValueError('Workspace root must be an existing directory')
        self.repository = None
        self.detection = 'unavailable'
        try:
            result = git(probe, 'rev-parse', '--show-toplevel')
            if result.returncode == 0:
                self.repository = Path(os.fsdecode(result.stdout).strip()).resolve()
                self.detection = 'git'
            elif b'not a git repository' in result.stderr.lower():
                self.detection = 'filesystem'
        except (OSError, subprocess.SubprocessError):
            pass
        self.root = probe if root is not None else self.repository or self.startup
        if not self.startup.is_relative_to(self.root):
            raise ValueError('Startup directory must be inside the workspace root')

    def observe(self, budget=None):
        value = {'startup_directory': str(self.startup), 'workspace_root': str(self.root),
                 'git_root': str(self.repository) if self.repository else None,
                 'git': self.detection,
                 'path_base': 'All relative tool paths use workspace_root, including dot.'}
        if self.detection != 'git':
            return value
        try:
            result = git(self.root, 'status', '--porcelain=v1', '--branch', '--untracked-files=normal',
                         '--', '.', ':(exclude).tero', ':(exclude).tero/**', budget=budget)
            if result.returncode:
                raise OSError('Git status unavailable; do not assume clean')
            lines = result.stdout.decode(errors='replace').splitlines()
            value['head'] = lines[0] if lines and lines[0].startswith('##') else 'unknown'
            changes = [s for s in lines if not s.startswith('##')]
            value['status'] = 'dirty' if changes else 'clean'
            visible, size = [], 0
            for line in changes:
                if size + len(line) + 1 > 1500:
                    break
                visible.append(line)
                size += len(line) + 1
            value.update(changes=visible, changes_truncated=len(visible) != len(changes),
                         conflicts=sum(s[:2] in {'DD','AU','UD','UA','DU','AA','UU'} for s in changes))
        except (OSError, subprocess.SubprocessError):
            value['status'] = 'unavailable; do not assume clean'
        return value


def git_state(root, budget):
    """Compare HEAD, symbolic branch and index without treating Git as a sandbox."""
    result = git(root, 'rev-parse', '--is-inside-work-tree', budget=budget)
    if result.returncode:
        if b'not a git repository' in result.stderr.lower():
            return {}
        raise OSError('Git state unavailable')
    result = git(root, 'rev-parse', '--verify', 'HEAD', budget=budget)
    head = result.stdout.decode().strip() if result.returncode == 0 else 'unborn'
    ref = git(root, 'symbolic-ref', '--quiet', 'HEAD', budget=budget)
    if ref.returncode not in (0, 1):
        raise OSError('Git branch observation failed')
    index = git(root, 'ls-files', '--stage', '-z', '--', '.', ':(exclude).tero',
                ':(exclude).tero/**', budget=budget)
    if index.returncode:
        raise OSError('Git index observation failed')
    return {'.git/observed-head': head, '.git/observed-ref': os.fsdecode(ref.stdout).strip(),
            '.git/observed-index': os.fsdecode(index.stdout)}
