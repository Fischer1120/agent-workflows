#!/usr/bin/env python3
"""Install the public Codex/DSH workflow bundle into a project or a global setup.

The default is a read-only preview printed as JSON. ``--apply`` performs the
same plan atomically and returns the written file count, backups and evidence
that the host's Codex ``config.toml`` was not touched.

Layout produced by the installer:

* project mode: ``PATH/.agents/skills``, ``PATH/AGENTS.md``,
  ``PATH/docs/corrections``;
* global mode: ``SKILLS_HOME``, ``CODEX_HOME/AGENTS.md``,
  ``RECORDS_ROOT/AGENTS.md`` and ``RECORDS_ROOT/docs/corrections``.

Only the four exported skills are deployed. Destination symlinks are rejected,
existing document records are never rewritten and existing ``AGENTS.md`` files
are merged marker-block by marker-block.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from datetime import datetime

SOURCE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_SKILLS = SOURCE_ROOT / '.agents' / 'skills'
SOURCE_TEMPLATES = SOURCE_ROOT / 'templates'
SOURCE_CORRECTIONS = SOURCE_ROOT / 'docs' / 'corrections'

SKILL_NAMES = (
    'codex-tiered-workflow',
    'project-corrections',
    'humanizer',
    'humanizer-zh',
)
RENDERED_DOCUMENTS = {
    'codex-tiered-workflow': {Path('SKILL.md')},
    'project-corrections': {Path('SKILL.md')},
}
MARKERS = ('codex-tiered-workflow', 'project-corrections')
EXCLUDED_DIR_NAMES = {
    '__pycache__', '.git', '.pytest_cache', '.mypy_cache', '.venv',
    'node_modules',
}
EXCLUDED_FILE_NAMES = {'.DS_Store'}
EXCLUDED_SUFFIXES = {'.pyc', '.pyo'}
BACKUP_ROOT_NAME = '.workflow-backups'
WORKFLOW_SKILL = 'codex-tiered-workflow'
WORKFLOW_RELATIVE = Path('scripts') / 'workflow.json'
REQUIRED_TEMPLATES = ('project.AGENTS.md', 'global.AGENTS.md')
DEFAULT_CONFIG_REASON = '预览不写入 config.toml'


class InstallError(RuntimeError):
    """A preflight condition that must stop the run before any write."""


# ---------------------------------------------------------------------------
# Small helpers


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path):
    try:
        return sha256(Path(path).read_bytes())
    except OSError:
        return None


def shell_arg(path) -> str:
    """Render one literal path argument for a POSIX shell example."""
    text = str(path)
    if re.search(r'[^\w@%+=:,./-]', text):
        escaped = re.sub(r'([\\"$`])', r'\\\1', text)
        return '"' + escaped + '"'
    return text


def markdown_destination(path) -> str:
    """Markdown link destination that survives spaces and parentheses."""
    text = str(path)
    if re.search(r'[\s()<>]', text):
        return f'<{text}>'
    return text


def read_text(path: Path, label: str) -> str:
    try:
        return Path(path).read_text(encoding='utf-8')
    except OSError as exc:
        raise InstallError(f'无法读取{label} {path}: {exc}') from exc
    except UnicodeDecodeError as exc:
        raise InstallError(f'{label}不是 UTF-8 文本: {path}') from exc


def resolve_root(value, label: str) -> Path:
    """Resolve a root only after rejecting symlinks in its components.

    A symlinked root would silently redirect every managed write, so it is
    refused; symlinks below a real root are checked separately per file.
    """
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    for component in reversed((raw, *raw.parents)):
        if component.is_symlink():
            raise InstallError(f'{label}不能经过符号链接: {component}')
    path = raw.resolve()
    if path.exists() and not path.is_dir():
        raise InstallError(f'{label}必须是目录，当前是文件: {path}')
    return path


def check_destination(root: Path, target: Path, expect_dir: bool = False) -> None:
    """Reject symlinks or type conflicts on the way to a managed destination."""
    root = Path(root).resolve()
    try:
        rel = Path(target).relative_to(root)
    except ValueError as exc:
        raise InstallError(f'写入目标超出允许的根目录: {target}') from exc
    if not rel.parts:
        return
    parts = rel.parts if expect_dir else rel.parts[:-1]
    current = root
    for part in parts:
        current = current / part
        if os.path.lexists(current):
            if current.is_symlink():
                raise InstallError(f'拒绝通过符号链接写入: {current}')
            if not current.is_dir():
                raise InstallError(f'目标路径中的组件不是目录: {current}')
    if not expect_dir and os.path.lexists(target):
        if target.is_symlink():
            raise InstallError(f'拒绝写入符号链接目标: {target}')
        if target.is_dir():
            raise InstallError(f'目标位置是目录，拒绝覆盖: {target}')
    if expect_dir and os.path.lexists(target):
        if target.is_symlink():
            raise InstallError(f'拒绝写入符号链接目录: {target}')
        if not target.is_dir():
            raise InstallError(f'目标位置是文件，无法创建目录: {target}')


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            if os.path.lexists(tmp_path):
                tmp_path.unlink()
        finally:
            raise
    try:
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass


def makedirs_private(path: Path) -> None:
    """Create a directory tree readable only by the current user."""
    path = Path(path)
    missing = []
    current = path
    while not os.path.lexists(current):
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for directory in reversed(missing):
        os.mkdir(directory, mode=0o700)
        os.chmod(directory, 0o700)


# ---------------------------------------------------------------------------
# Source enumeration


def source_skill_files(skill: str):
    base = SOURCE_SKILLS / skill
    if not base.is_dir():
        raise InstallError(f'缺少源技能目录: {base}')
    files = []
    for path in sorted(base.rglob('*')):
        rel = path.relative_to(base)
        if any(part in EXCLUDED_DIR_NAMES for part in rel.parts):
            continue
        if not path.is_file():
            continue
        if path.name in EXCLUDED_FILE_NAMES or path.suffix in EXCLUDED_SUFFIXES:
            continue
        files.append((rel, path))
    return files


# ---------------------------------------------------------------------------
# Rendering


def build_context(mode: str, project, codex_home, skills_home, records_root) -> dict:
    if mode == 'project':
        records_agents = project / 'AGENTS.md'
    else:
        records_agents = records_root / 'AGENTS.md'
    return {
        'mode': mode,
        'tiered_skill_dir': skills_home / 'codex-tiered-workflow',
        'tiered_skill': skills_home / 'codex-tiered-workflow' / 'SKILL.md',
        'tiered_script': skills_home / 'codex-tiered-workflow' / 'scripts' / 'dispatch.py',
        'corrections_skill': skills_home / 'project-corrections' / 'SKILL.md',
        'records_root': records_root,
        'records_dir': records_root / 'docs' / 'corrections',
        'records_index': records_root / 'docs' / 'corrections' / 'index.md',
        'records_records': records_root / 'docs' / 'corrections' / 'records.md',
        'records_agents': records_agents,
    }


def render_skill(skill: str, text: str, mode: str, ctx: dict) -> str:
    if skill == 'codex-tiered-workflow':
        script_command = f'python3 {shell_arg(ctx["tiered_script"])}'
        # Inline Markdown code needs a longer fence when the path has backticks.
        ticks = [len(match.group()) for match in re.finditer(r'`+', script_command)]
        fence = '`' * (max(ticks, default=0) + 1)
        text = re.sub(
            r'`python3\s+\.agents/skills/codex-tiered-workflow/scripts/dispatch\.py --help`',
            lambda _: f'{fence}{script_command} --help{fence}',
            text,
        )
        text = re.sub(
            r'python3\s+\.agents/skills/codex-tiered-workflow/scripts/dispatch\.py',
            lambda _: script_command,
            text,
        )
    if skill == 'project-corrections' and mode == 'global':
        text = text.replace(
            '按本技能实际所在的 `.agents/skills/project-corrections/` 目录'
            '向上三级定位记录库根目录，不依赖当前工作目录',
            f'按安装器写入的记录库路径 `{ctx["records_root"]}` 定位记录库根目录，'
            '不依赖当前工作目录',
        )
    text = text.replace(
        '(../../../docs/corrections/index.md)',
        f'({markdown_destination(ctx["records_index"])})',
    )
    text = text.replace(
        '(../../../docs/corrections/records.md)',
        f'({markdown_destination(ctx["records_records"])})',
    )
    text = text.replace(
        '(../../../AGENTS.md)',
        f'({markdown_destination(ctx["records_agents"])})',
    )
    leftovers = [
        token for token in
        ('(../../../docs/corrections/', '(../../../AGENTS.md)')
        if token in text
    ]
    if re.search(
        r'python3\s+\.agents/skills/codex-tiered-workflow/scripts/dispatch\.py',
        text,
    ):
        leftovers.insert(0, '.agents/skills/codex-tiered-workflow/scripts/dispatch.py')
    if leftovers:
        raise InstallError(
            f'技能文档中的路径未全部渲染（{skill}）：{leftovers[0]}；未写入任何文件'
        )
    if skill == 'project-corrections' and mode == 'global' and '向上三级定位记录库根目录' in text:
        raise InstallError(
            'project-corrections 技能中的记录库定位说明无法渲染；未写入任何文件'
        )
    return text


def render_agents_template(text: str, mode: str, ctx: dict) -> str:
    """Render an AGENTS template.

    Project layout keeps the template's project-relative links, which already
    resolve to the installed copy. A global installation cannot rely on those
    relative paths, so the links are rendered to real installed paths.
    """
    if mode == 'project':
        return text
    replacements = (
        (
            '[project-corrections 技能](.agents/skills/project-corrections/SKILL.md)',
            f'[project-corrections 技能]({markdown_destination(ctx["corrections_skill"])})',
        ),
        (
            '[index.md](docs/corrections/index.md)',
            f'[index.md]({markdown_destination(ctx["records_index"])})',
        ),
        (
            '[records.md](docs/corrections/records.md)',
            f'[records.md]({markdown_destination(ctx["records_records"])})',
        ),
        (
            '这些路径均相对本文件所在的项目根目录，',
            '这些路径指向本机安装位置，',
        ),
        (
            '`.agents/skills/codex-tiered-workflow/SKILL.md`',
            f'`{ctx["tiered_skill"]}`',
        ),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def render_records_document(text: str, ctx: dict) -> str:
    """Render links inside a newly initialised records document.

    Existing records are never passed through this function. New records point
    at the installed project-corrections skill and, because the installer does
    not import private history, mention the source case file without a link.
    """
    text = re.sub(
        r'\[([^\]]*)\]\(\.\./\.\./\.agents/skills/project-corrections/SKILL\.md\)',
        lambda match: (
            f'[{match.group(1)}]'
            f'({markdown_destination(ctx["corrections_skill"])})'
        ),
        text,
    )
    text = re.sub(
        r'\[([^\]]*)\]\(\.\./workflow-cases\.md\)',
        '源仓库的 `docs/workflow-cases.md`（安装器不导入源案例）',
        text,
    )
    leftovers = [
        token for token in ('../../.agents/skills/', '../workflow-cases.md')
        if token in text
    ]
    if leftovers:
        raise InstallError(
            f'记录模板中的链接未全部渲染：{leftovers[0]}；未写入任何文件'
        )
    return text


# ---------------------------------------------------------------------------
# AGENTS.md marker handling


def _marker_tokens(marker: str):
    return f'<!-- {marker}:start -->', f'<!-- {marker}:end -->'


def marker_ranges(text: str):
    """Validate every managed marker pair and return the present ranges."""
    ranges = {}
    for marker in MARKERS:
        start_token, end_token = _marker_tokens(marker)
        starts = text.count(start_token)
        ends = text.count(end_token)
        if starts != ends:
            raise InstallError(
                f'marker 区块不完整（{marker}）：start={starts}, end={ends}；未写入任何文件'
            )
        if starts > 1:
            raise InstallError(
                f'marker 区块重复（{marker}）：发现 {starts} 个 start；未写入任何文件'
            )
        if starts == 0:
            continue
        start = text.index(start_token)
        end = text.index(end_token)
        if start > end:
            raise InstallError(
                f'marker 区块顺序错误（{marker}）：end 出现在 start 之前；未写入任何文件'
            )
        ranges[marker] = (start, end + len(end_token))
    ordered = sorted(ranges.values())
    for (start_a, end_a), (start_b, end_b) in zip(ordered, ordered[1:]):
        if start_b < end_a:
            raise InstallError('marker 区块交叉，无法安全合并；未写入任何文件')
    return ranges


def extract_block(text: str, marker: str) -> str:
    start_token, end_token = _marker_tokens(marker)
    if text.count(start_token) != 1 or text.count(end_token) != 1:
        raise InstallError(f'模板中的 marker 区块异常: {marker}')
    start = text.index(start_token)
    end = text.index(end_token, start) + len(end_token)
    return text[start:end]


def append_block(text: str, block: str) -> str:
    if not text:
        return block + '\n'
    if text.endswith('\n'):
        return text + '\n' + block + '\n'
    return text + '\n\n' + block + '\n'


def merge_agents(existing: bytes, blocks: dict) -> tuple:
    """Merge managed blocks into an existing AGENTS.md.

    Returns ``(data, marker_actions)``. Bytes outside marker blocks are kept
    untouched. A missing block is inserted next to its sibling so custom text
    below the managed region stays below it; if no block exists yet both are
    appended at the end.
    """
    try:
        text = existing.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise InstallError('AGENTS.md 不是 UTF-8 文本，拒绝合并') from exc
    ranges = marker_ranges(text)
    actions = {}
    result = text
    present = [marker for marker in MARKERS if marker in ranges]
    if not present:
        for marker in MARKERS:
            result = append_block(result, blocks[marker])
            actions[marker] = 'append'
    else:
        for marker in present:
            start, end = marker_ranges(result)[marker]
            result = result[:start] + blocks[marker] + result[end:]
            actions[marker] = 'replace'
        for marker in MARKERS:
            if marker in present:
                continue
            start, end = marker_ranges(result)[present[0]]
            if MARKERS.index(marker) < MARKERS.index(present[0]):
                result = result[:start] + blocks[marker] + '\n\n' + result[start:]
            else:
                result = result[:end] + '\n\n' + blocks[marker] + result[end:]
            actions[marker] = 'append'
    if not result.endswith('\n'):
        result += '\n'
    return result.encode('utf-8'), actions


# ---------------------------------------------------------------------------
# Planning


def read_existing(path: Path, root: Path):
    check_destination(root, path)
    if not os.path.lexists(path):
        return None, None
    try:
        data = path.read_bytes()
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise InstallError(f'无法读取已有文件 {path}: {exc}') from exc
    return data, mode


def plan_workflow_json(target: Path, existing, selection_id):
    source_text = read_text(
        SOURCE_SKILLS / WORKFLOW_SKILL / 'scripts' / 'workflow.json', '源 workflow.json'
    )
    try:
        defaults = json.loads(source_text)
    except json.JSONDecodeError as exc:
        raise InstallError(f'源 workflow.json 不是有效 JSON: {exc}') from exc
    if not isinstance(defaults, dict):
        raise InstallError('源 workflow.json 必须是 JSON 对象')
    current = None
    if existing is not None:
        try:
            current = json.loads(existing.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InstallError(f'已有 workflow.json 不是有效 JSON，拒绝覆盖: {exc}') from exc
        if not isinstance(current, dict):
            raise InstallError('已有 workflow.json 必须是 JSON 对象，拒绝覆盖')
    data = dict(defaults)
    if current:
        data.update(current)
    previous_model = current.get('dsh_model') if current else None
    if selection_id is not None:
        data['dsh_model'] = selection_id
        if previous_model != selection_id:
            data['dsh_model_name'] = None
            data['dsh_provider'] = None
    model_changed = selection_id is not None and previous_model != selection_id
    serialized = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    summary = {
        'path': str(target),
        'action': 'unchanged' if existing == serialized else (
            'update' if existing is not None else 'create'
        ),
        'dsh_model': data.get('dsh_model'),
        'dsh_model_name': data.get('dsh_model_name'),
        'dsh_provider': data.get('dsh_provider'),
        'dsh_model_changed': model_changed,
        'preserved': current is not None and selection_id is None,
        'selection_source': 'cli' if selection_id is not None else (
            'existing' if current and current.get('dsh_model') else 'source-default'
        ),
    }
    return serialized, summary


def build_plan(mode: str, project, codex_home, skills_home, records_root,
               selection_id) -> dict:
    if mode == 'project':
        skills_root = project / '.agents' / 'skills'
        agents_path = project / 'AGENTS.md'
        agents_root = project
        records_base = project
        check_destination(project, project / '.agents', expect_dir=True)
        check_destination(project, skills_root, expect_dir=True)
    else:
        skills_root = skills_home
        agents_path = codex_home / 'AGENTS.md'
        agents_root = codex_home
        records_base = records_root

    ctx = build_context(mode, project, codex_home, skills_home, records_base)
    ops = []
    skill_summaries = []
    warnings = []

    for template_name in REQUIRED_TEMPLATES:
        if not (SOURCE_TEMPLATES / template_name).is_file():
            raise InstallError(f'缺少模板: {SOURCE_TEMPLATES / template_name}')

    # Skills ---------------------------------------------------------------
    for skill in SKILL_NAMES:
        target_dir = skills_root / skill
        check_destination(skills_root, target_dir, expect_dir=True)
        files = source_skill_files(skill)
        counts = {'create': 0, 'update': 0, 'unchanged': 0}
        for rel, source in files:
            target = target_dir / rel
            check_destination(skills_root, target)
            if skill == WORKFLOW_SKILL and rel == WORKFLOW_RELATIVE:
                continue
            existing, existing_mode = read_existing(target, skills_root)
            source_data = source.read_bytes()
            data = source_data
            if rel in RENDERED_DOCUMENTS.get(skill, set()):
                text = render_skill(
                    skill, read_text(source, '技能文档'), mode, ctx
                )
                data = text.encode('utf-8')
            mode_bits = existing_mode if existing is not None else (
                stat.S_IMODE(source.stat().st_mode)
            )
            if existing is not None and existing == data:
                counts['unchanged'] += 1
                continue
            action = 'update' if existing is not None else 'create'
            counts[action] += 1
            ops.append({
                'kind': 'skill',
                'label': f'{skill}/{rel.as_posix()}',
                'path': target,
                'root': skills_root,
                'action': action,
                'data': data,
                'mode': mode_bits,
                'observed': existing,
                'source': source,
            })
        # workflow.json is deployed from the source template with merge rules.
        if skill == WORKFLOW_SKILL:
            target = skills_root / WORKFLOW_SKILL / WORKFLOW_RELATIVE
            check_destination(skills_root, target)
            existing, existing_mode = read_existing(target, skills_root)
            data, workflow_summary = plan_workflow_json(target, existing, selection_id)
            if existing != data:
                action = 'update' if existing is not None else 'create'
                counts[action] += 1
                ops.append({
                    'kind': 'workflow',
                    'label': WORKFLOW_RELATIVE.as_posix(),
                    'path': target,
                    'root': skills_root,
                    'action': action,
                    'data': data,
                    'mode': existing_mode if existing is not None else 0o644,
                    'observed': existing,
                    'source': None,
                })
                workflow_summary['action'] = action
            else:
                counts['unchanged'] += 1
        if counts['update'] or (counts['create'] and counts['unchanged']):
            skill_action = 'update'
        elif counts['create']:
            skill_action = 'create'
        else:
            skill_action = 'unchanged'
        skill_summaries.append({
            'name': skill,
            'source': str(SOURCE_SKILLS / skill),
            'target': str(target_dir),
            'files': len(files),
            'action': skill_action,
        })

    # Records --------------------------------------------------------------
    records_dir = records_base / 'docs' / 'corrections'
    check_destination(records_base, records_dir, expect_dir=True)
    records_files = []
    for name in ('index.md', 'records.md'):
        source = SOURCE_CORRECTIONS / name
        if not source.is_file():
            raise InstallError(f'缺少记录模板: {source}')
        target = records_dir / name
        check_destination(records_base, target)
        existing, existing_mode = read_existing(target, records_base)
        if existing is not None:
            records_files.append({'path': str(target), 'action': 'preserve'})
            continue
        source_data = render_records_document(
            read_text(source, '记录模板'), ctx
        ).encode('utf-8')
        records_files.append({'path': str(target), 'action': 'create'})
        ops.append({
            'kind': 'records',
            'label': f'docs/corrections/{name}',
            'path': target,
            'root': records_base,
            'action': 'create',
            'data': source_data,
            'mode': stat.S_IMODE(source.stat().st_mode),
            'observed': None,
            'source': source,
        })

    # AGENTS.md ------------------------------------------------------------
    project_template = read_text(SOURCE_TEMPLATES / 'project.AGENTS.md', '项目模板')
    global_template = read_text(SOURCE_TEMPLATES / 'global.AGENTS.md', '全局模板')
    marker_ranges(project_template)
    marker_ranges(global_template)
    rendered_project = render_agents_template(project_template, mode, ctx)
    rendered_global = render_agents_template(global_template, mode, ctx)
    tiered_source = rendered_global if mode == 'global' else rendered_project
    blocks = {
        'codex-tiered-workflow': extract_block(tiered_source, 'codex-tiered-workflow'),
        'project-corrections': extract_block(rendered_project, 'project-corrections'),
    }
    # A global AGENTS.md uses the global template's list-style block; the
    # records-root instruction file reuses the project template verbatim.
    records_blocks = {
        'codex-tiered-workflow': extract_block(
            rendered_project, 'codex-tiered-workflow'
        ),
        'project-corrections': blocks['project-corrections'],
    }

    def plan_agents_file(path: Path, root: Path, new_template: str,
                         merge_blocks: dict):
        check_destination(root, path)
        existing, existing_mode = read_existing(path, root)
        if existing is None:
            new_text = new_template
            if not new_text.endswith('\n'):
                new_text += '\n'
            data = new_text.encode('utf-8')
            marker_actions = {marker: 'create' for marker in MARKERS}
            action = 'create'
        else:
            data, marker_actions = merge_agents(existing, merge_blocks)
            action = 'unchanged' if data == existing else 'merge'
        if action != 'unchanged':
            ops.append({
                'kind': 'agents',
                'label': path.name,
                'path': path,
                'root': root,
                'action': 'create' if action == 'create' else 'update',
                'data': data,
                'mode': existing_mode if existing is not None else 0o644,
                'observed': existing,
                'source': None,
            })
        return {
            'path': str(path),
            'action': action,
            'markers': marker_actions,
        }, existing, data

    if mode == 'project':
        new_agents = rendered_project
        new_records_agents = None
    else:
        new_agents = (
            rendered_global.rstrip('\n') + '\n\n'
            + blocks['project-corrections'] + '\n'
        )
        # The records root also gets a project-style instruction file so the
        # records library keeps its maintenance rules next to the documents.
        new_records_agents = rendered_project
    agents_summary, _, _ = plan_agents_file(
        agents_path, agents_root, new_agents, blocks
    )
    records_agents_summary = None
    if mode == 'global':
        records_agents_path = records_base / 'AGENTS.md'
        records_agents_summary, _, _ = plan_agents_file(
            records_agents_path, records_base, new_records_agents, records_blocks
        )

    backup_root = records_base / BACKUP_ROOT_NAME
    backup_files = [
        {'path': str(op['path']), 'sha256': sha256(op['observed'])}
        for op in ops if op['observed'] is not None
    ]
    config_toml = config_evidence(codex_home if mode == 'global' else None)
    changes = [
        {'path': str(op['path']), 'action': op['action'], 'kind': op['kind']}
        for op in ops
    ]
    plan = {
        'mode': mode,
        'apply': False,
        'status': 'planned',
        'source_root': str(SOURCE_ROOT),
        'roots': {
            'project': str(project) if mode == 'project' else None,
            'codex_home': str(codex_home) if mode == 'global' else None,
            'skills_home': str(skills_root),
            'records_root': str(records_base),
        },
        'skills': skill_summaries,
        'agents': agents_summary,
        'records': {
            'root': str(records_base),
            'files': records_files,
            'agents': records_agents_summary,
        },
        'workflow_json': workflow_summary,
        'config_toml': config_toml,
        'backup': {
            'root': str(backup_root),
            'dir': None,
            'files': backup_files,
        },
        'changes': changes,
        'file_count': len(ops),
        'changed': bool(ops),
        'warnings': warnings,
    }
    plan['_ops'] = ops
    plan['_records_base'] = records_base
    return plan


def config_evidence(codex_home):
    if codex_home is None:
        return {
            'checked': False,
            'path': None,
            'exists': False,
            'unchanged': True,
            'sha256_before': None,
            'sha256_after': None,
            'note': '项目模式不读取或修改全局 Codex 配置',
        }
    path = Path(codex_home) / 'config.toml'
    exists = path.exists()
    digest = file_sha256(path) if exists else None
    return {
        'checked': True,
        'path': str(path),
        'exists': exists,
        'unchanged': True,
        'sha256_before': digest,
        'sha256_after': None,
        'note': DEFAULT_CONFIG_REASON,
    }


# ---------------------------------------------------------------------------
# Applying


def _check_unchanged_since_plan(op) -> None:
    path = op['path']
    observed = op['observed']
    if observed is None:
        if os.path.lexists(path):
            raise InstallError(f'计划生成后目标被创建，已停止: {path}')
        return
    try:
        current = path.read_bytes()
    except OSError as exc:
        raise InstallError(f'计划生成后无法读取目标: {path}: {exc}') from exc
    if current != observed:
        raise InstallError(f'计划生成后目标内容已变化，已停止: {path}')


def _backup_layout(backup_root: Path, target: Path) -> Path:
    try:
        rel = Path(target).relative_to(backup_root)
        if rel.parts and rel.parts[0] == BACKUP_ROOT_NAME:
            raise ValueError
        return rel
    except ValueError:
        return Path('_external') / Path(str(target)).as_posix().lstrip('/')


def write_backups(backup_base: Path, ops) -> dict:
    pending = [op for op in ops if op['observed'] is not None]
    if not pending:
        return {'root': str(backup_base / BACKUP_ROOT_NAME), 'dir': None, 'files': []}
    backup_root = backup_base / BACKUP_ROOT_NAME
    check_destination(backup_base, backup_root, expect_dir=True)
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup_dir = backup_root / timestamp
    suffix = 1
    while os.path.lexists(backup_dir):
        suffix += 1
        backup_dir = backup_root / f'{timestamp}-{suffix}'
    makedirs_private(backup_dir)
    manifest = []
    written = []
    for op in pending:
        rel = _backup_layout(backup_base, op['path'])
        destination = backup_dir / rel
        makedirs_private(destination.parent)
        atomic_write(destination, op['observed'], 0o600)
        manifest.append({
            'source': str(op['path']),
            'backup': rel.as_posix(),
            'sha256': sha256(op['observed']),
        })
        written.append({'path': str(op['path']), 'backup': str(destination)})
    manifest_path = backup_dir / 'manifest.json'
    atomic_write(
        manifest_path,
        (json.dumps({'created': timestamp, 'files': manifest},
                    ensure_ascii=False, indent=2) + '\n').encode('utf-8'),
        0o600,
    )
    return {'root': str(backup_root), 'dir': str(backup_dir), 'files': written}


def apply_plan(plan) -> dict:
    ops = plan['_ops']
    # Re-check every planned target before creating a backup or writing a file.
    for op in ops:
        _check_unchanged_since_plan(op)
        check_destination(op['root'], op['path'], expect_dir=False)
    backup = write_backups(plan['_records_base'], ops)
    for op in ops:
        atomic_write(op['path'], op['data'], op['mode'])
    plan['apply'] = True
    plan['backup'] = backup
    plan['changed'] = bool(ops)
    plan['file_count'] = len(ops)
    plan['status'] = 'applied' if ops else 'up-to-date'
    evidence = plan['config_toml']
    if evidence['checked']:
        path = Path(evidence['path'])
        after = file_sha256(path) if path.exists() else None
        evidence['sha256_after'] = after
        evidence['unchanged'] = evidence['sha256_before'] == after
        evidence['note'] = (
            '安装完成后 config.toml 字节未变'
            if evidence['unchanged'] else '检测到 config.toml 在安装期间发生变化'
        )
        if not evidence['unchanged']:
            plan['warnings'].append('config.toml 在安装期间发生变化；安装器未写入该文件')
    return plan


def public_payload(plan) -> dict:
    return {key: value for key, value in plan.items() if not key.startswith('_')}


# ---------------------------------------------------------------------------
# CLI


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='预览或安装公开的 Codex/DSH 工作流技能、AGENTS 指令和记录库。'
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--project', metavar='PATH',
                       help='把技能安装到 PATH/.agents/skills')
    group.add_argument('--global', dest='global_mode', action='store_true',
                       help='安装到全局 skills home、Codex home 和记录根目录')
    parser.add_argument('--codex-home', metavar='PATH',
                        help='全局模式：Codex home，默认 $CODEX_HOME 或 ~/.codex')
    parser.add_argument('--skills-home', metavar='PATH',
                        help='全局模式：技能目录，默认 ~/.agents/skills')
    parser.add_argument('--records-root', metavar='PATH',
                        help='全局模式：记录库根目录，默认 <codex-home>/agent-workflows')
    parser.add_argument('--dsh-model', metavar='SELECTION_ID',
                        help='把 DSH 默认模型写入 workflow.json，仅显式传入时赋值')
    parser.add_argument('--apply', action='store_true',
                        help='实际写入；省略时只输出预览 JSON，不创建任何目录')
    args = parser.parse_args(argv)
    if not args.global_mode:
        if args.codex_home or args.skills_home or args.records_root:
            parser.error('--codex-home/--skills-home/--records-root 只能与 --global 一起使用')
    if args.dsh_model is not None and not args.dsh_model.strip():
        parser.error('--dsh-model 不能为空')
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        selection = args.dsh_model.strip() if args.dsh_model is not None else None
        if args.global_mode:
            codex_value = args.codex_home or os.environ.get('CODEX_HOME') or '~/.codex'
            codex_home = resolve_root(codex_value, 'codex home')
            skills_home = resolve_root(
                args.skills_home or '~/.agents/skills', 'skills home'
            )
            records_root = resolve_root(
                args.records_root or (codex_home / 'agent-workflows'),
                'records root',
            )
            plan = build_plan('global', None, codex_home, skills_home,
                              records_root, selection)
        else:
            project = resolve_root(args.project, '项目路径')
            plan = build_plan('project', project, None,
                              project / '.agents' / 'skills', project, selection)
        if args.apply:
            plan = apply_plan(plan)
        print(json.dumps(public_payload(plan), ensure_ascii=False, indent=2))
        return 0
    except InstallError as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
