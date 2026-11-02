"""Behavioural tests for the public workflow installer.

These tests treat ``scripts/install.py`` as the product surface: every case runs
the real CLI against disposable temporary roots, never against the host's
``~/.codex`` or ``~/.agents`` configuration.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / 'scripts' / 'install.py'
SOURCE_SKILLS = ROOT / '.agents' / 'skills'
SKILL_NAMES = [
    'codex-tiered-workflow',
    'project-corrections',
    'humanizer',
    'humanizer-zh',
]
EXCLUDED_PARTS = {
    '__pycache__', '.git', '.pytest_cache', '.mypy_cache', '.venv',
    'node_modules', '.DS_Store',
}
AGENTS_TEMPLATE_PROJECT = ROOT / 'templates' / 'project.AGENTS.md'
MARKERS = ('codex-tiered-workflow', 'project-corrections')


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run_cli(*args, cwd=None, env=None):
    environment = dict(os.environ)
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    # Project-mode tests must not be able to reach the host's Codex profile.
    environment.pop('CODEX_HOME', None)
    if env:
        environment.update(env)
    return subprocess.run(
        [sys.executable, str(INSTALLER), *[str(arg) for arg in args]],
        capture_output=True, text=True, cwd=str(cwd or ROOT), env=environment,
    )


def parse_json(proc):
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - failure path
        raise AssertionError(
            f'CLI did not print JSON (rc={proc.returncode}):\n'
            f'stdout={proc.stdout!r}\nstderr={proc.stderr!r}'
        ) from exc


def tree_snapshot(root) -> dict:
    """Map every path under *root* to a content fingerprint."""
    root = Path(root)
    snapshot = {}
    if not root.exists():
        return snapshot
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[rel] = ('link', os.readlink(path))
        elif path.is_dir():
            snapshot[rel] = ('dir',)
        elif path.is_file():
            snapshot[rel] = ('file', sha256(path.read_bytes()))
    return snapshot


def source_skill_files(name: str) -> list:
    base = SOURCE_SKILLS / name
    files = []
    for path in sorted(base.rglob('*')):
        rel = path.relative_to(base)
        if any(part in EXCLUDED_PARTS for part in rel.parts):
            continue
        if path.is_file():
            files.append(rel)
    return files


MARKDOWN_LINK = re.compile(r'\]\(\s*(<[^>]+>|[^)\s]+)\s*\)')
FENCED_SH = re.compile(r'```sh\n(.*?)```', re.S)


def markdown_links(text: str) -> list:
    destinations = []
    for match in MARKDOWN_LINK.finditer(text):
        dest = match.group(1)
        if dest.startswith('<') and dest.endswith('>'):
            dest = dest[1:-1]
        if dest.startswith(('http://', 'https://', '#')):
            continue
        destinations.append(dest)
    return destinations


def assert_links_resolve(case, document: Path):
    text = document.read_text(encoding='utf-8')
    for dest in markdown_links(text):
        target = Path(dest)
        if not target.is_absolute():
            target = document.parent / dest
        case.assertTrue(target.exists(), f'{document}: broken link {dest} -> {target}')


def assert_tree_links_resolve(case, root: Path):
    for document in sorted(Path(root).rglob('*.md')):
        if '.workflow-backups' in document.parts:
            continue
        assert_links_resolve(case, document)


def shell_commands(text: str) -> list:
    commands = []
    for block in FENCED_SH.findall(text):
        for line in block.splitlines():
            line = line.strip()
            if line.startswith('python3 '):
                commands.append(line)
    return commands


def quoted_paths(text: str) -> list:
    return re.findall(r'"([^"\n]*dispatch\.py)"', text)


class InstallerTestBase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='install-tests-')).resolve()
        self.addCleanup(self.cleanup)
        self.project = self.root / 'project'
        self.codex_home = self.root / 'codex-home'
        self.skills_home = self.root / 'skills-home'
        self.records_root = self.root / 'records-root'

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # -- helpers ---------------------------------------------------------
    def apply(self, *args, env=None):
        return run_cli(*args, '--apply', env=env)

    def install_project(self, project=None, env=None):
        project = Path(project or self.project)
        proc = self.apply('--project', project, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return parse_json(proc)

    def install_global(self, *extra, env=None):
        args = ['--global',
                '--codex-home', self.codex_home,
                '--skills-home', self.skills_home,
                '--records-root', self.records_root,
                *extra]
        proc = self.apply(*args, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return parse_json(proc)

    def backup_dirs(self, root):
        base = Path(root) / '.workflow-backups'
        if not base.exists():
            return []
        return sorted(path for path in base.iterdir() if path.is_dir())

    def installed_skill(self, root, name):
        return Path(root) / name

    def assert_skill_payload(self, skills_dir, expected_extra=()):
        for name in SKILL_NAMES:
            target = self.installed_skill(skills_dir, name)
            self.assertTrue(target.is_dir(), f'missing skill {name}')
            expected = {rel.as_posix() for rel in source_skill_files(name)}
            actual = {
                path.relative_to(target).as_posix()
                for path in target.rglob('*') if path.is_file()
            }
            self.assertEqual(expected, actual, f'payload mismatch for {name}')
            for rel in actual:
                self.assertFalse(
                    any(part in EXCLUDED_PARTS for part in Path(rel).parts),
                    f'excluded artifact installed: {name}/{rel}',
                )
        for rel in expected_extra:
            self.assertTrue((Path(skills_dir) / rel).is_file(), f'missing extra {rel}')

    def assert_dispatch_help(self, script: Path):
        proc = subprocess.run(
            [sys.executable, str(script), '--help'],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('dispatch.py', proc.stdout + proc.stderr)


class ProjectInstallTests(InstallerTestBase):
    def test_dry_run_writes_nothing(self):
        project = self.root / 'brand new project'
        before = tree_snapshot(self.root)
        proc = run_cli('--project', project)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        plan = parse_json(proc)
        self.assertFalse(plan['apply'])
        self.assertTrue(plan['changed'])
        self.assertFalse(project.exists(), 'preview created the target project')
        self.assertEqual(before, tree_snapshot(self.root), 'preview wrote files')
        self.assertEqual([], self.backup_dirs(self.root))

    def test_new_project_install(self):
        payload = self.install_project()
        skills_dir = self.project / '.agents' / 'skills'
        self.assert_skill_payload(skills_dir)
        self.assertEqual(payload['status'], 'applied')
        self.assertTrue(payload['changed'])

        # Upstream licences travel with the two third-party skills.
        for name in ('humanizer', 'humanizer-zh'):
            self.assertEqual(
                (skills_dir / name / 'LICENSE').read_bytes(),
                (SOURCE_SKILLS / name / 'LICENSE').read_bytes(),
                f'{name} LICENSE was not preserved',
            )

        # AGENTS.md is the project template, with relative links into the target.
        agents = self.project / 'AGENTS.md'
        self.assertEqual(
            agents.read_text(encoding='utf-8'),
            AGENTS_TEMPLATE_PROJECT.read_text(encoding='utf-8'),
        )
        assert_links_resolve(self, agents)
        for marker in MARKERS:
            text = agents.read_text(encoding='utf-8')
            self.assertEqual(text.count(f'<!-- {marker}:start -->'), 1)
            self.assertEqual(text.count(f'<!-- {marker}:end -->'), 1)

        # Records are initialised from the public template, no private cases.
        records_dir = self.project / 'docs' / 'corrections'
        for name in ('index.md', 'records.md'):
            self.assertTrue((records_dir / name).is_file())
            assert_links_resolve(self, records_dir / name)
        index_text = (records_dir / 'index.md').read_text(encoding='utf-8')
        records_text = (records_dir / 'records.md').read_text(encoding='utf-8')
        self.assertNotIn('../workflow-cases.md', index_text)
        self.assertIn('源仓库', index_text)
        self.assertNotIn('CORR-2026', index_text + records_text)
        self.assertIn(
            str(skills_dir / 'project-corrections' / 'SKILL.md'), records_text
        )
        assert_tree_links_resolve(self, self.project)

        # The tiered skill's commands and links point at the installed copy.
        tiered = skills_dir / 'codex-tiered-workflow' / 'SKILL.md'
        corrections = skills_dir / 'project-corrections' / 'SKILL.md'
        tiered_text = tiered.read_text(encoding='utf-8')
        corrections_text = corrections.read_text(encoding='utf-8')
        script = skills_dir / 'codex-tiered-workflow' / 'scripts' / 'dispatch.py'
        self.assertIn(str(script), tiered_text)
        self.assertNotIn(
            'python3 .agents/skills/codex-tiered-workflow/scripts/dispatch.py',
            tiered_text,
        )
        for command in shell_commands(tiered_text):
            parts = shlex.split(command)
            self.assertEqual(parts[0], 'python3')
            self.assertTrue(Path(parts[1]).is_file(), command)
        self.assertEqual(len(shell_commands(tiered_text)), 5)
        assert_links_resolve(self, tiered)
        self.assertIn(str(project_docs := (self.project / 'docs/corrections/index.md')), tiered_text)
        self.assertIn(str(self.project / 'docs/corrections/records.md'), tiered_text)
        self.assertIn(str(self.project / 'AGENTS.md'), corrections_text)
        self.assertIn(str(project_docs), corrections_text)
        assert_links_resolve(self, corrections)
        self.assert_dispatch_help(script)

        workflow = json.loads(
            (skills_dir / 'codex-tiered-workflow' / 'scripts' / 'workflow.json').read_text()
        )
        self.assertIsNone(workflow['dsh_model'])
        self.assertIsNone(workflow['dsh_model_name'])
        self.assertEqual(workflow['luna_model'], 'gpt-6-luna')

        expected_files = sum(len(source_skill_files(name)) for name in SKILL_NAMES)
        expected_files += 1  # project AGENTS.md
        expected_files += 2  # docs/corrections/index.md + records.md
        self.assertEqual(payload['file_count'], expected_files)
        self.assertEqual(payload['backup']['files'], [])
        self.assertFalse((self.project / 'config.toml').exists())
        leftovers = [path for path in self.project.rglob('*.tmp') if path.is_file()]
        self.assertEqual([], leftovers, 'atomic write left temporary files')

    def test_stale_skill_file_is_updated_and_backed_up(self):
        stale = self.project / '.agents' / 'skills' / 'humanizer' / 'SKILL.md'
        stale.parent.mkdir(parents=True)
        stale.write_text('旧的技能正文\n', encoding='utf-8')
        payload = self.install_project()
        self.assertEqual(
            stale.read_bytes(),
            (SOURCE_SKILLS / 'humanizer' / 'SKILL.md').read_bytes(),
        )
        self.assertEqual(len(self.backup_dirs(self.project)), 1)
        backup = self.backup_dirs(self.project)[0]
        backed_up = backup / '.agents' / 'skills' / 'humanizer' / 'SKILL.md'
        self.assertEqual(backed_up.read_text(encoding='utf-8'), '旧的技能正文\n')
        self.assertEqual(stat.S_IMODE(backed_up.stat().st_mode), 0o600)
        self.assertIn(str(stale), [item['path'] for item in payload['backup']['files']])

    def test_symlinked_records_file_is_rejected(self):
        records_dir = self.project / 'docs' / 'corrections'
        records_dir.mkdir(parents=True)
        outside = self.root / 'outside-index.md'
        outside.write_text('不要覆盖\n', encoding='utf-8')
        (records_dir / 'index.md').symlink_to(outside)
        proc = self.apply('--project', self.project)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('符号链接', proc.stderr)
        self.assertEqual(outside.read_text(encoding='utf-8'), '不要覆盖\n')
        self.assertFalse((self.project / '.agents').exists())
        self.assertFalse((self.project / 'AGENTS.md').exists())

    def test_install_is_idempotent(self):
        self.install_project()
        first = tree_snapshot(self.project)
        payload = self.install_project()
        self.assertEqual(tree_snapshot(self.project), first, 'rerun changed files')
        self.assertFalse(payload['changed'])
        self.assertEqual(payload['file_count'], 0)
        self.assertEqual(payload['status'], 'up-to-date')
        self.assertEqual(payload['backup']['files'], [])
        self.assertIsNone(payload['backup']['dir'])
        self.assertEqual([], self.backup_dirs(self.project))
        agents = (self.project / 'AGENTS.md').read_text(encoding='utf-8')
        for marker in MARKERS:
            self.assertEqual(agents.count(f'<!-- {marker}:start -->'), 1)
        tiered = (
            self.project / '.agents/skills/codex-tiered-workflow/SKILL.md'
        ).read_text(encoding='utf-8')
        self.assertEqual(tiered.count(str(self.project / '.agents/skills/codex-tiered-workflow')), 6)

    def test_existing_agents_keeps_other_content_when_merging(self):
        self.project.mkdir()
        agents = self.project / 'AGENTS.md'
        original = (
            '# 我的项目规则\n\n保留这一行。\n\n'
            '<!-- codex-tiered-workflow:start -->\n旧的分工说明\n'
            '<!-- codex-tiered-workflow:end -->\n\n'
            '尾部内容必须保留。\n'
        )
        agents.write_text(original, encoding='utf-8')
        payload = self.install_project()
        merged = agents.read_text(encoding='utf-8')
        self.assertTrue(payload['changed'])
        self.assertTrue(merged.startswith('# 我的项目规则\n\n保留这一行。\n\n'))
        self.assertTrue(merged.endswith('尾部内容必须保留。\n'))
        self.assertNotIn('旧的分工说明', merged)
        self.assertEqual(merged.count('<!-- codex-tiered-workflow:start -->'), 1)
        self.assertEqual(merged.count('<!-- project-corrections:start -->'), 1)
        self.assertIn('## 默认协作工作流', merged)
        self.assertIn('## 纠正记录与解决方法复用', merged)
        self.assertIn('.agents/skills/codex-tiered-workflow/SKILL.md', merged)
        self.assertTrue(
            (self.project / '.agents/skills/codex-tiered-workflow/SKILL.md').is_file()
        )
        assert_links_resolve(self, agents)

        # A changed existing file is backed up byte-for-byte.
        self.assertEqual(len(self.backup_dirs(self.project)), 1)
        backup = self.backup_dirs(self.project)[0]
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        backup_file = backup / 'AGENTS.md'
        self.assertEqual(backup_file.read_bytes(), original.encode('utf-8'))
        self.assertEqual(stat.S_IMODE(backup_file.stat().st_mode), 0o600)
        self.assertEqual(payload['backup']['files'][0]['path'], str(agents))

        # A second run must not append the inserted corrections block again.
        snapshot = tree_snapshot(self.project)
        rerun = self.install_project()
        self.assertFalse(rerun['changed'])
        self.assertEqual(tree_snapshot(self.project), snapshot)
        self.assertEqual(len(self.backup_dirs(self.project)), 1)
        self.assertEqual(
            agents.read_text(encoding='utf-8').count(
                '<!-- project-corrections:start -->'
            ),
            1,
        )

    def test_existing_agents_without_markers_appends_managed_blocks(self):
        self.project.mkdir()
        agents = self.project / 'AGENTS.md'
        agents.write_text('只属于部署者的规则，不要改。', encoding='utf-8')
        self.install_project()
        merged = agents.read_text(encoding='utf-8')
        self.assertTrue(merged.startswith('只属于部署者的规则，不要改。'))
        self.assertEqual(merged.count('<!-- codex-tiered-workflow:start -->'), 1)
        self.assertEqual(merged.count('<!-- project-corrections:start -->'), 1)
        self.assertLess(
            merged.index('<!-- codex-tiered-workflow:start -->'),
            merged.index('<!-- project-corrections:start -->'),
        )
        assert_links_resolve(self, agents)

    def test_existing_records_are_preserved(self):
        records_dir = self.project / 'docs' / 'corrections'
        records_dir.mkdir(parents=True)
        index = records_dir / 'index.md'
        records = records_dir / 'records.md'
        index_text = '# 本项目的索引\n\nCORR-20260101-001 私人案例\n'
        records_text = '# 本项目的记录\n\n不要覆盖。\n'
        index.write_text(index_text, encoding='utf-8')
        records.write_text(records_text, encoding='utf-8')
        payload = self.install_project()
        self.assertEqual(index.read_text(encoding='utf-8'), index_text)
        self.assertEqual(records.read_text(encoding='utf-8'), records_text)
        actions = {item['path']: item['action'] for item in payload['records']['files']}
        self.assertEqual(actions[str(index)], 'preserve')
        self.assertEqual(actions[str(records)], 'preserve')
        self.assertEqual([], self.backup_dirs(self.project))

    def test_extra_skill_resources_are_kept(self):
        extra = self.project / '.agents' / 'skills' / 'humanizer' / 'notes' / 'mine.md'
        extra.parent.mkdir(parents=True)
        extra.write_text('部署者自己的补充\n', encoding='utf-8')
        other = self.project / '.agents' / 'skills' / 'my-own-skill' / 'SKILL.md'
        other.parent.mkdir(parents=True)
        other.write_text('---\nname: my-own-skill\n---\n', encoding='utf-8')
        self.install_project()
        self.assertEqual(extra.read_text(encoding='utf-8'), '部署者自己的补充\n')
        self.assertEqual(other.read_text(encoding='utf-8'), '---\nname: my-own-skill\n---\n')

    def test_space_path_quotes_shell_examples_and_links(self):
        project = self.root / 'project with spaces'
        payload = self.install_project(project)
        self.assertTrue(payload['changed'])
        skills_dir = project / '.agents' / 'skills'
        script = skills_dir / 'codex-tiered-workflow' / 'scripts' / 'dispatch.py'
        tiered_text = (
            skills_dir / 'codex-tiered-workflow' / 'SKILL.md'
        ).read_text(encoding='utf-8')
        self.assertTrue(script.is_file())
        self.assertIn(f'python3 "{script}"', tiered_text)
        quoted = quoted_paths(tiered_text)
        self.assertGreaterEqual(len(quoted), 6)
        for raw in quoted:
            self.assertTrue(Path(raw).is_file(), raw)
        for command in shell_commands(tiered_text):
            parts = shlex.split(command)
            self.assertEqual(parts[0], 'python3')
            self.assertTrue(Path(parts[1]).is_file(), command)
        assert_links_resolve(self, project / 'AGENTS.md')
        assert_links_resolve(self, skills_dir / 'codex-tiered-workflow' / 'SKILL.md')
        assert_links_resolve(self, skills_dir / 'project-corrections' / 'SKILL.md')
        self.assert_dispatch_help(script)

    def test_shell_examples_use_literal_paths_with_special_characters(self):
        for name in ('project $UNSET_REVIEW_VARIABLE', 'project \\q', 'project\\1',
                     'project `false`', 'project;false', "project 'quote'"):
            with self.subTest(name=name):
                project = self.root / name
                self.install_project(project)
                text = (project / '.agents/skills/codex-tiered-workflow/SKILL.md').read_text(encoding='utf-8')
                match = re.search(r'(?<!`)(`+)(python3 .*? --help)\1(?!`)', text)
                self.assertIsNotNone(match)
                help_command = match.group(2)
                environment = dict(os.environ)
                environment.pop('UNSET_REVIEW_VARIABLE', None)
                proc = subprocess.run(help_command, shell=True, executable='/bin/sh',
                                      capture_output=True, text=True, env=environment)
                self.assertEqual(proc.returncode, 0, f'{help_command}\n{proc.stderr}')

    def test_symlinked_project_ancestor_is_rejected_without_writes(self):
        outside = self.root / 'outside'
        outside.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(outside, target_is_directory=True)
        before = tree_snapshot(self.root)
        for extra in ((), ('--apply',)):
            with self.subTest(extra=extra):
                proc = run_cli('--project', alias / 'child', *extra)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn('符号链接', proc.stderr)
                self.assertEqual(before, tree_snapshot(self.root))

    def test_invalid_markers_are_rejected_before_any_write(self):
        variants = {
            'duplicate start': (
                '<!-- codex-tiered-workflow:start -->\nA\n'
                '<!-- codex-tiered-workflow:start -->\nB\n'
                '<!-- codex-tiered-workflow:end -->\n'
            ),
            'missing end': (
                '<!-- codex-tiered-workflow:start -->\nA\n'
            ),
            'reversed': (
                '<!-- codex-tiered-workflow:end -->\nA\n'
                '<!-- codex-tiered-workflow:start -->\n'
            ),
            'missing start': (
                'A\n<!-- project-corrections:end -->\n'
            ),
        }
        for label, content in variants.items():
            with self.subTest(label=label):
                project = self.root / ('project-' + label.replace(' ', '-'))
                project.mkdir()
                agents = project / 'AGENTS.md'
                agents.write_text(content, encoding='utf-8')
                proc = self.apply('--project', project)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn('marker', proc.stderr.lower())
                self.assertEqual(agents.read_text(encoding='utf-8'), content)
                self.assertFalse((project / '.agents').exists())
                self.assertFalse((project / 'docs').exists())
                self.assertFalse((project / '.workflow-backups').exists())

    def test_symlinked_destination_is_rejected(self):
        self.project.mkdir()
        outside = self.root / 'outside.txt'
        outside.write_text('do not touch\n', encoding='utf-8')
        (self.project / 'AGENTS.md').symlink_to(outside)
        proc = self.apply('--project', self.project)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('符号链接', proc.stderr)
        self.assertEqual(outside.read_text(encoding='utf-8'), 'do not touch\n')
        self.assertFalse((self.project / '.agents').exists())
        self.assertFalse((self.project / '.workflow-backups').exists())

    def test_symlinked_directory_is_rejected(self):
        self.project.mkdir()
        outside = self.root / 'outside-skills'
        outside.mkdir()
        (self.project / '.agents').symlink_to(outside, target_is_directory=True)
        proc = self.apply('--project', self.project)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('符号链接', proc.stderr)
        self.assertEqual([], list(outside.iterdir()))
        self.assertFalse((self.project / 'AGENTS.md').exists())

    def test_project_mode_does_not_touch_codex_home(self):
        codex_home = self.root / 'host-codex'
        codex_home.mkdir()
        config = codex_home / 'config.toml'
        config.write_text('[profile]\nmodel = "host-value"\n', encoding='utf-8')
        before = tree_snapshot(codex_home)
        proc = self.apply('--project', self.project, env={'CODEX_HOME': str(codex_home)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(before, tree_snapshot(codex_home))
        payload = parse_json(proc)
        self.assertFalse(payload['config_toml']['checked'])


class GlobalInstallTests(InstallerTestBase):
    def setUp(self):
        super().setUp()
        self.codex_home = self.root / 'codex home'
        self.skills_home = self.root / 'skills home'
        self.records_root = self.root / 'records root'

    def test_global_install_uses_three_custom_roots_and_keeps_config(self):
        self.codex_home.mkdir()
        config = self.codex_home / 'config.toml'
        config.write_text('[profiles.default]\nmodel = "host-model"\n', encoding='utf-8')
        config_before = config.read_bytes()
        auth = self.codex_home / 'auth.json'
        auth.write_text('{"token": "host-auth"}\n', encoding='utf-8')
        auth_before = auth.read_bytes()

        preview = run_cli(
            '--global', '--codex-home', self.codex_home,
            '--skills-home', self.skills_home,
            '--records-root', self.records_root,
        )
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertFalse(self.skills_home.exists())
        self.assertFalse(self.records_root.exists())
        self.assertEqual(config.read_bytes(), config_before)

        payload = self.install_global()
        self.assertEqual(payload['status'], 'applied')
        self.assert_skill_payload(self.skills_home)

        global_agents = self.codex_home / 'AGENTS.md'
        self.assertTrue(global_agents.is_file())
        global_text = global_agents.read_text(encoding='utf-8')
        self.assertTrue(global_text.startswith('-总体指令'))
        for marker in MARKERS:
            self.assertEqual(global_text.count(f'<!-- {marker}:start -->'), 1)
        self.assertIn(str(self.skills_home / 'codex-tiered-workflow' / 'SKILL.md'), global_text)
        self.assertIn(str(self.records_root / 'docs/corrections/index.md'), global_text)
        assert_links_resolve(self, global_agents)

        records_agents = self.records_root / 'AGENTS.md'
        self.assertTrue(records_agents.is_file())
        records_text = records_agents.read_text(encoding='utf-8')
        self.assertTrue(records_text.startswith('# 项目指令'))
        for marker in MARKERS:
            self.assertEqual(records_text.count(f'<!-- {marker}:start -->'), 1)
        self.assertIn(str(self.skills_home / 'project-corrections' / 'SKILL.md'), records_text)
        assert_links_resolve(self, records_agents)

        for name in ('index.md', 'records.md'):
            document = self.records_root / 'docs/corrections' / name
            self.assertTrue(document.is_file())
            assert_links_resolve(self, document)
        global_index = (
            self.records_root / 'docs/corrections/index.md'
        ).read_text(encoding='utf-8')
        self.assertNotIn('../workflow-cases.md', global_index)
        self.assertIn(
            str(self.skills_home / 'project-corrections' / 'SKILL.md'),
            (self.records_root / 'docs/corrections/records.md').read_text(encoding='utf-8'),
        )
        assert_tree_links_resolve(self, self.root)

        corrections_text = (
            self.skills_home / 'project-corrections' / 'SKILL.md'
        ).read_text(encoding='utf-8')
        self.assertIn(str(self.records_root / 'docs/corrections/index.md'), corrections_text)
        self.assertIn(str(self.records_root / 'docs/corrections/records.md'), corrections_text)
        self.assertIn(str(records_agents), corrections_text)
        self.assertIn(str(self.records_root), corrections_text)
        self.assertNotIn('向上三级定位记录库根目录', corrections_text)
        assert_links_resolve(
            self, self.skills_home / 'project-corrections' / 'SKILL.md'
        )
        tiered_text = (
            self.skills_home / 'codex-tiered-workflow' / 'SKILL.md'
        ).read_text(encoding='utf-8')
        self.assertTrue(
            f'python3 "{self.skills_home / "codex-tiered-workflow" / "scripts" / "dispatch.py"}"'
            in tiered_text
        )
        assert_links_resolve(
            self, self.skills_home / 'codex-tiered-workflow' / 'SKILL.md'
        )
        self.assert_dispatch_help(
            self.skills_home / 'codex-tiered-workflow' / 'scripts' / 'dispatch.py'
        )

        self.assertIsNone(json.loads(
            (self.skills_home / 'codex-tiered-workflow/scripts/workflow.json').read_text()
        )['dsh_model'])

        # Codex config is byte-identical and reported as evidence.
        self.assertEqual(config.read_bytes(), config_before)
        evidence = payload['config_toml']
        self.assertTrue(evidence['checked'])
        self.assertTrue(evidence['exists'])
        self.assertTrue(evidence['unchanged'])
        self.assertEqual(evidence['sha256_before'], evidence['sha256_after'])
        self.assertEqual(evidence['sha256_after'], sha256(config_before))
        self.assertEqual(auth.read_bytes(), auth_before, 'auth.json was modified')

        expected_files = sum(len(source_skill_files(name)) for name in SKILL_NAMES)
        expected_files += 4  # global AGENTS, records AGENTS, index.md, records.md
        self.assertEqual(payload['file_count'], expected_files)
        self.assertEqual(payload['backup']['files'], [])

    def test_global_rerun_is_idempotent(self):
        self.install_global()
        first = tree_snapshot(self.root)
        payload = self.install_global()
        self.assertEqual(tree_snapshot(self.root), first)
        self.assertFalse(payload['changed'])
        self.assertEqual(payload['file_count'], 0)
        self.assertEqual([], self.backup_dirs(self.records_root))

    def test_global_defaults_come_from_codex_home_env(self):
        env_home = self.root / 'env-codex'
        env_home.mkdir()
        config = env_home / 'config.toml'
        config.write_text('model = "env-model"\n', encoding='utf-8')
        before = config.read_bytes()
        proc = self.apply(
            '--global',
            '--skills-home', self.skills_home,
            env={'CODEX_HOME': str(env_home)},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = parse_json(proc)
        records_root = env_home / 'agent-workflows'
        self.assertEqual(payload['roots']['codex_home'], str(env_home))
        self.assertEqual(payload['roots']['records_root'], str(records_root))
        self.assertTrue((env_home / 'AGENTS.md').is_file())
        self.assertTrue((records_root / 'AGENTS.md').is_file())
        self.assertTrue((records_root / 'docs/corrections/index.md').is_file())
        self.assert_skill_payload(self.skills_home)
        self.assertEqual(config.read_bytes(), before)
        self.assertTrue(payload['config_toml']['unchanged'])

    def test_global_existing_agents_merges_and_backs_up(self):
        self.codex_home.mkdir()
        config = self.codex_home / 'config.toml'
        config.write_text('model = "keep-me"\n', encoding='utf-8')
        config_before = config.read_bytes()
        agents = self.codex_home / 'AGENTS.md'
        original = (
            '我的全局规则\n\n'
            '<!-- codex-tiered-workflow:start -->\n旧的全局说明\n'
            '<!-- codex-tiered-workflow:end -->\n\n'
            '尾部规则\n'
        )
        agents.write_text(original, encoding='utf-8')
        payload = self.install_global()
        merged = agents.read_text(encoding='utf-8')
        self.assertTrue(merged.startswith('我的全局规则\n\n'))
        self.assertIn('尾部规则\n', merged)
        self.assertNotIn('旧的全局说明', merged)
        self.assertEqual(merged.count('<!-- codex-tiered-workflow:start -->'), 1)
        self.assertEqual(merged.count('<!-- project-corrections:start -->'), 1)
        self.assertIn(str(self.records_root / 'docs/corrections/index.md'), merged)
        assert_links_resolve(self, agents)
        self.assertEqual(config.read_bytes(), config_before)

        backups = self.backup_dirs(self.records_root)
        self.assertEqual(len(backups), 1)
        backup = backups[0]
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        backed_up = [path for path in backup.rglob('AGENTS.md') if 'codex home' in str(path)]
        self.assertEqual(len(backed_up), 1)
        self.assertEqual(backed_up[0].read_bytes(), original.encode('utf-8'))
        self.assertEqual(payload['backup']['files'][0]['path'], str(agents))
        self.assertEqual(stat.S_IMODE(backed_up[0].stat().st_mode), 0o600)

    def test_existing_dsh_model_choice_is_preserved_on_upgrade(self):
        self.install_global()
        workflow_path = self.skills_home / 'codex-tiered-workflow' / 'scripts' / 'workflow.json'
        data = json.loads(workflow_path.read_text())
        data['dsh_model'] = 'sel-existing'
        data['dsh_model_name'] = 'Existing Model'
        data['dsh_provider'] = 'existing-provider'
        data['deployment_note'] = '保留我'
        workflow_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        edited = workflow_path.read_bytes()

        payload = self.install_global()
        after = json.loads(workflow_path.read_text())
        self.assertEqual(after['dsh_model'], 'sel-existing')
        self.assertEqual(after['dsh_model_name'], 'Existing Model')
        self.assertEqual(after['dsh_provider'], 'existing-provider')
        self.assertEqual(after['deployment_note'], '保留我')
        self.assertEqual(workflow_path.read_bytes(), edited)
        self.assertFalse(payload['workflow_json']['dsh_model_changed'])
        self.assertEqual([], self.backup_dirs(self.records_root))

    def test_explicit_dsh_model_updates_selection_and_backs_up(self):
        self.install_global()
        workflow_path = self.skills_home / 'codex-tiered-workflow' / 'scripts' / 'workflow.json'
        data = json.loads(workflow_path.read_text())
        data.update({
            'dsh_model': 'sel-old', 'dsh_model_name': 'Old Model',
            'dsh_provider': 'old-provider', 'deployment_note': '保留我',
        })
        workflow_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        previous = workflow_path.read_bytes()

        payload = self.install_global('--dsh-model', 'sel-new')
        updated = json.loads(workflow_path.read_text())
        self.assertEqual(updated['dsh_model'], 'sel-new')
        self.assertIsNone(updated['dsh_model_name'])
        self.assertIsNone(updated['dsh_provider'])
        self.assertEqual(updated['deployment_note'], '保留我')
        self.assertEqual(updated['luna_model'], 'gpt-6-luna')
        self.assertTrue(payload['workflow_json']['dsh_model_changed'])
        self.assertEqual(payload['workflow_json']['dsh_model'], 'sel-new')
        backups = self.backup_dirs(self.records_root)
        self.assertEqual(len(backups), 1)
        matches = list(backups[0].rglob('workflow.json'))
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].read_bytes(), previous)
        self.assertEqual(stat.S_IMODE(matches[0].stat().st_mode), 0o600)

    def test_new_install_honours_explicit_dsh_model(self):
        self.codex_home.mkdir()
        payload = self.install_global('--dsh-model', 'sel-fresh')
        workflow = json.loads((
            self.skills_home / 'codex-tiered-workflow/scripts/workflow.json'
        ).read_text())
        self.assertEqual(workflow['dsh_model'], 'sel-fresh')
        self.assertIsNone(workflow['dsh_model_name'])
        self.assertTrue(payload['workflow_json']['dsh_model_changed'])
        self.assertEqual(payload['workflow_json']['dsh_model'], 'sel-fresh')

    def test_invalid_workflow_json_is_rejected_before_any_write(self):
        self.codex_home.mkdir()
        workflow_path = self.skills_home / 'codex-tiered-workflow' / 'scripts' / 'workflow.json'
        workflow_path.parent.mkdir(parents=True)
        workflow_path.write_text('{not json', encoding='utf-8')
        before = tree_snapshot(self.root)
        proc = self.apply(
            '--global', '--codex-home', self.codex_home,
            '--skills-home', self.skills_home, '--records-root', self.records_root,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('workflow.json', proc.stderr)
        self.assertEqual(before, tree_snapshot(self.root))
        self.assertFalse((self.codex_home / 'AGENTS.md').exists())
        self.assertFalse((self.records_root / 'AGENTS.md').exists())

    def test_global_invalid_marker_is_rejected_before_any_write(self):
        self.codex_home.mkdir()
        agents = self.codex_home / 'AGENTS.md'
        original = (
            '全局规则\n'
            '<!-- project-corrections:start -->\nA\n'
            '<!-- project-corrections:start -->\nB\n'
            '<!-- project-corrections:end -->\n'
        )
        agents.write_text(original, encoding='utf-8')
        proc = self.apply(
            '--global', '--codex-home', self.codex_home,
            '--skills-home', self.skills_home, '--records-root', self.records_root,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('marker', proc.stderr.lower())
        self.assertEqual(agents.read_text(encoding='utf-8'), original)
        self.assertFalse(self.skills_home.exists())
        self.assertFalse(self.records_root.exists())
        self.assertFalse((self.codex_home / '.workflow-backups').exists())

    def test_symlinked_global_root_is_rejected(self):
        self.codex_home.mkdir()
        outside = self.root / 'outside skills'
        outside.mkdir()
        link = self.root / 'linked skills'
        link.symlink_to(outside, target_is_directory=True)
        proc = self.apply(
            '--global', '--codex-home', self.codex_home,
            '--skills-home', link, '--records-root', self.records_root,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('符号链接', proc.stderr)
        self.assertEqual([], list(outside.iterdir()))
        self.assertFalse((self.codex_home / 'AGENTS.md').exists())
        self.assertFalse(self.records_root.exists())

    def test_symlinked_global_root_ancestors_are_rejected_without_writes(self):
        outside = self.root / 'outside'
        outside.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(outside, target_is_directory=True)
        for root_option in ('--codex-home', '--skills-home', '--records-root'):
            for extra in ((), ('--apply',)):
                with self.subTest(root_option=root_option, extra=extra):
                    roots = {'--codex-home': self.codex_home,
                             '--skills-home': self.skills_home,
                             '--records-root': self.records_root}
                    roots[root_option] = alias / 'child'
                    args = [part for pair in roots.items() for part in pair]
                    before = tree_snapshot(self.root)
                    proc = run_cli('--global', *args, *extra)
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertIn('符号链接', proc.stderr)
                    self.assertEqual(before, tree_snapshot(self.root))

    def test_global_existing_records_agents_is_merged(self):
        self.records_root.mkdir()
        agents = self.records_root / 'AGENTS.md'
        original = '记录库自定义规则\n'
        agents.write_text(original, encoding='utf-8')
        payload = self.install_global()
        merged = agents.read_text(encoding='utf-8')
        self.assertTrue(merged.startswith(original))
        for marker in MARKERS:
            self.assertEqual(merged.count(f'<!-- {marker}:start -->'), 1)
        assert_links_resolve(self, agents)
        self.assertEqual(len(payload['backup']['files']), 1)
        backups = self.backup_dirs(self.records_root)
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'AGENTS.md').read_text(encoding='utf-8'), original)

    def test_mutually_exclusive_modes_are_rejected(self):
        proc = run_cli('--project', self.project, '--global')
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(self.project.exists())
        proc = run_cli('--project', self.project, '--codex-home', self.codex_home)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(self.project.exists())


if __name__ == '__main__':
    unittest.main()
