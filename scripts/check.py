#!/usr/bin/env python3
"""Run offline checks for the public workflow bundle without calling models."""
from pathlib import Path
import ast
import json
import os
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
IGNORED = {'.git', '__pycache__', '.work', '.verification', '.workflow-runs',
           '.workflow-backups', 'node_modules'}


def public_files():
    for path in ROOT.rglob('*'):
        rel = path.relative_to(ROOT)
        if not path.is_file() or any(part in IGNORED for part in rel.parts):
            continue
        if rel.as_posix() == 'tools/dsh-codex-import/manifest.json':
            continue
        yield path


def inspect():
    errors = []
    count = 0
    personal_path = re.compile(re.escape('/' + 'Users' + '/') + r'[^/\s]+/')
    secret = re.compile(r'(?:gh' + r'[opusr]_[A-Za-z0-9]{30,}|sk' + r'-[A-Za-z0-9_-]{32,}|-----BEGIN ' + r'(?:RSA |EC |OPENSSH )?PRIVATE KEY-----)')
    for path in public_files():
        count += 1
        if path.suffix in {'.png', '.jpg', '.jpeg', '.gif', '.webp'}:
            continue
        text = path.read_text(encoding='utf-8')
        if personal_path.search(text):
            errors.append(f'Personal absolute path: {path.relative_to(ROOT)}')
        if secret.search(text):
            errors.append(f'Possible credential: {path.relative_to(ROOT)}')
        if path.suffix == '.py':
            ast.parse(text, filename=str(path))
        if path.suffix == '.json':
            json.loads(text)
    skills = sorted((ROOT / '.agents/skills').glob('*/SKILL.md'))
    if len(skills) != 4:
        errors.append('Expected four deployable workflow skills')
    for skill in skills:
        text = skill.read_text()
        frontmatter = re.match(r'^---\n([\s\S]*?)\n---(?:\n|$)', text)
        if (not frontmatter or
                not re.search(r'^name:\s*\S', frontmatter[1], re.M) or
                not re.search(r'^description:\s*\S', frontmatter[1], re.M)):
            errors.append(f'Missing skill frontmatter: {skill.relative_to(ROOT)}')
    for name in ('humanizer', 'humanizer-zh'):
        if not (ROOT / '.agents/skills' / name / 'LICENSE').is_file():
            errors.append(f'Missing upstream license: {name}')
    optional = sorted((ROOT / 'optional/superpowers/skills').glob('*/SKILL.md'))
    if len(optional) != 15 or not (ROOT / 'optional/superpowers/LICENSE').is_file():
        errors.append('Expected complete licensed Superpowers skill snapshot')
    records = (ROOT / 'docs/corrections/records.md').read_text()
    template = records.split('## 条目模板\n', 1)[-1].split('\n## 正式记录\n', 1)[0]
    if (template.count('```') != 2 or '## 正式记录' not in records or
            '<a id="corr-YYYYMMDD-NNN"></a>' not in template):
        errors.append('Incomplete correction record template')
    if errors:
        raise RuntimeError('\n'.join(errors))
    print(json.dumps({'public_files_checked': count, 'skills': len(skills),
                      'optional_skills': len(optional),
                      'personal_paths': 0, 'credential_patterns': 0}), flush=True)


def main():
    inspect()
    subprocess.run([sys.executable,
                    '.agents/skills/humanizer/scripts/validate-package.py'],
                   cwd=ROOT, check=True)
    for folder in ('tests/workflow', 'tests'):
        subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', folder,
                        '-p', 'test_*.py', '-v'], cwd=ROOT, check=True,
                       env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    for script in ('test_install.py', 'test_read_catalog.py'):
        subprocess.run([sys.executable, str(Path('tools/dsh-codex-import') / script), '-v'],
                       cwd=ROOT, check=True,
                       env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    print('Offline workflow checks passed.', flush=True)


if __name__ == '__main__':
    main()
