#!/usr/bin/env python3
"""Dispatch main-model assignments to DSH (DeepSeek) or to a low-end fast model."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from luna_worker import write_json


def project_title(cwd: Path) -> str:
    """Use the matching Codex project name; never infer from the focused UI."""
    path = Path.home() / '.codex/.codex-global-state.json'
    matches = []
    try:
        projects = json.loads(path.read_text()).get('local-projects', {})
        for project in projects.values():
            for root in project.get('rootPaths', []):
                resolved = Path(root).resolve()
                if cwd == resolved or resolved in cwd.parents:
                    matches.append((len(resolved.parts), project.get('name') or resolved.name))
    except (OSError, ValueError, TypeError):
        pass
    return max(matches, default=(0, cwd.name))[1]


def resolve_backend(args) -> str:
    """Speed-priority tasks default to DSH; ordinary tasks still require an explicit backend."""
    speed_priority = bool(getattr(args, 'speed_priority', False))
    backend = getattr(args, 'backend', None)
    if backend is None:
        if speed_priority:
            return 'dsh'
        raise ValueError('普通任务必须显式指定 --backend（dsh 或 luna）')
    if speed_priority and backend == 'luna':
        raise ValueError('速度优先任务只能派发给 DSH；低端快速档不支持速度优先')
    return backend


def validate_phase(args, backend: str) -> None:
    """Reject unsupported combinations before a job directory or backend call exists."""
    speed_priority = bool(getattr(args, 'speed_priority', False))
    phase = getattr(args, 'phase', 'execute')
    if phase != 'review':
        return
    if not speed_priority:
        raise ValueError('review 阶段只允许速度优先任务')
    if backend != 'dsh':
        raise ValueError('review 阶段只允许 DSH 后端')
    if args.sandbox != 'read-only':
        raise ValueError('review 阶段只允许 read-only 沙箱')


def build_prompt(title: str, task_title: str, cwd: Path, brief: str,
                 speed_priority: bool, phase: str) -> str:
    base = (f'Codex 项目：{title}\n子任务：{task_title}\n'
            f'共享项目目录：{cwd}\n')
    if phase == 'review':
        return (base +
                '你接受旗舰档主模型的任务分派，只执行这份独立验收简报，不启动其他模型或重新分配任务，'
                '也不自动派发后续任务。只依据成品和任务要求进行独立验收，不修改文件；'
                '给出通过/不通过结论和必要证据。不追加旗舰档或低端档复核。\n\n' + brief)
    header = base + '你接受旗舰档主模型的任务分派，只执行这份简报，不启动其他模型或重新分配任务。'
    if speed_priority:
        return (header +
                '保留用户与其他执行者的修改。只操作简报允许的文件；需要新权限或关键判断时停止并回报。'
                '完成后给出结果、实际文件路径、验证证据和未解决事项，交给未参与实现的另一 DSH 会话独立验收，'
                '不再要求旗舰档复核。\n\n' + brief)
    return (header +
            '保留用户与其他执行者的修改。只操作简报允许的文件；需要新权限或关键判断时停止并回报。'
            '完成后给出结果、实际文件路径、验证证据和未解决事项，由未参与实现的旗舰档子代理独立验收。\n\n' + brief)


def summary(job_dir: Path) -> dict:
    job = json.loads((job_dir / 'job.json').read_text())
    state_path = job_dir / 'status.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'status': 'starting'}
    speed_priority = bool(job.get('speed_priority', False))
    result = {k: job.get(k) for k in ['job_id', 'backend', 'cwd', 'project_title', 'task_title']}
    result['speed_priority'] = speed_priority
    result['phase'] = job.get('phase', 'execute')
    result['final_reviewer'] = job.get('final_reviewer',
                                       'dsh' if speed_priority else 'gpt-6.1-sol')
    result.update(state)
    result['job_file'] = str(job_dir / 'job.json')
    if (job_dir / 'result.txt').exists():
        result['result_file'] = str(job_dir / 'result.txt')
        result['result_preview'] = (job_dir / 'result.txt').read_text()[-6000:]
    return result


def refresh_dsh(job_dir: Path) -> dict:
    from dsh_bridge import status
    job = json.loads((job_dir / 'job.json').read_text())
    current = status(job['session_id'])
    # The adapter only reports completion after observing this session's turn.end.
    previous = json.loads((job_dir / 'status.json').read_text())
    if current['terminal']:
        status_name = 'completed' if current['outcome'] == 'done' else 'failed'
    else:
        status_name = 'needs_attention' if current.get('needsAttention') else 'running'
    state = {**current, 'dsh_status': current['status'], 'status': status_name}
    if previous.get('accepted'):
        state['accepted'] = previous['accepted']
    result = state.pop('answer', '')
    if current['terminal']:
        (job_dir / 'result.txt').write_text(result)
        os.chmod(job_dir / 'result.txt', 0o600)
    write_json(job_dir / 'status.json', state)
    return state


def submit(args):
    backend = resolve_backend(args)
    if getattr(args, 'multimodal', False):
        raise ValueError(
            '多模态任务不能派发给 DSH 或低端档；请交回旗舰档选择支持所需模态的原生执行者'
        )
    validate_phase(args, backend)
    speed_priority = bool(getattr(args, 'speed_priority', False))
    phase = getattr(args, 'phase', 'execute')
    cwd = Path(args.cwd).expanduser().resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError('cwd 必须是项目目录')
    brief_path = Path(args.brief_file).expanduser().resolve(strict=True)
    brief = brief_path.read_text().strip()
    if not brief:
        raise ValueError('任务简报为空')
    if len(brief.encode()) > 1024 * 1024:
        raise ValueError('简报超过 1 MiB，请改用必要资料的文件路径')
    title = args.project_title or project_title(cwd)
    job_id = 'cw-' + uuid.uuid4().hex
    base = Path(args.runs_dir).expanduser().resolve() if args.runs_dir else cwd / '.workflow-runs'
    base.mkdir(parents=True, exist_ok=True)
    job_dir = base / job_id
    job_dir.mkdir(mode=0o700)
    job = {'job_id': job_id, 'backend': backend, 'cwd': str(cwd),
           'project_title': title, 'task_title': args.task_title,
           'speed_priority': speed_priority, 'phase': phase,
           'final_reviewer': 'dsh' if speed_priority else 'gpt-6.1-sol',
           'parent_thread_id': os.environ.get('CODEX_THREAD_ID'),
           'sandbox': args.sandbox, 'timeout': args.timeout,
           'session_id': job_id, 'client_message_id': job_id + '-initial',
           'created_at': time.time()}
    write_json(job_dir / 'job.json', job)
    prompt = build_prompt(title, args.task_title, cwd, brief, speed_priority, phase)
    (job_dir / 'brief.txt').write_text(prompt)
    os.chmod(job_dir / 'brief.txt', 0o600)
    if backend == 'luna':
        write_json(job_dir / 'status.json', {'status': 'starting'})
        with (job_dir / 'worker.log').open('a') as log:
            proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                     '_luna-worker', str(job_dir)],
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    start_new_session=True)
    else:
        from dsh_bridge import discover, submit as dsh_submit
        settings_path = Path(__file__).with_name('workflow.json')
        settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
        catalog = discover()
        permission = next((item['selectionId'] for item in catalog['permissions']
                           if item.get('enabled') and
                           item.get('metadata', {}).get('preset') == args.sandbox), None)
        if permission is None:
            raise ValueError('DSH 不支持请求的沙箱权限；未派发任务')
        model = args.dsh_model or settings.get('dsh_model')
        # Persist identifiers before RPC: uncertain delivery is never retried with a new id.
        write_json(job_dir / 'status.json', {'status': 'submitting'})
        try:
            response = dsh_submit(str(cwd), prompt, job_id, job['client_message_id'],
                                  model=model, permission=permission,
                                  preset=args.dsh_preset)
            write_json(job_dir / 'status.json', {'status': 'running', 'accepted': response})
        except Exception as exc:
            write_json(job_dir / 'status.json', {'status': 'submission_unknown', 'error': str(exc)})
    return summary(job_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('submit')
    p.add_argument('--backend', choices=['dsh', 'luna'],
                   help='速度优先可省略（默认 dsh）；普通任务必须显式指定。'
                        '任务已在 DSH 内发起时可省去本脚本，直接用原生子代理')
    p.add_argument('--speed-priority', action='store_true',
                   help='速度优先：默认派发 DSH（DeepSeek），由未参与实现的另一 DSH 会话独立验收')
    p.add_argument('--multimodal', action='store_true',
                   help='任务需要实际图片、音频、视频或视觉判断；不能派发给 DSH 或低端档')
    p.add_argument('--phase', choices=['execute', 'review'], default='execute',
                   help='review 仅允许速度优先 + DSH + read-only')
    p.add_argument('--cwd', default=str(Path.cwd()))
    p.add_argument('--project-title')
    p.add_argument('--task-title', required=True)
    p.add_argument('--brief-file', required=True)
    p.add_argument('--runs-dir')
    p.add_argument('--sandbox', choices=['read-only', 'workspace-write'], default='read-only',
                   help='两条派发路径都使用此权限，DSH 按实际 catalog 匹配')
    p.add_argument('--timeout', type=int, default=2700)
    p.add_argument('--dsh-model')
    p.add_argument('--dsh-preset')
    p = sub.add_parser('status')
    p.add_argument('--job-file', required=True)
    p = sub.add_parser('discover')
    sub.add_parser('_luna-worker').add_argument('job_dir')
    args = parser.parse_args()
    try:
        if args.command == '_luna-worker':
            from luna_worker import run
            run(Path(args.job_dir))
            return
        if args.command == 'submit':
            result = submit(args)
        elif args.command == 'discover':
            from dsh_bridge import discover
            result = discover()
        else:
            job_file = Path(args.job_file).expanduser().resolve(strict=True)
            job = json.loads(job_file.read_text())
            if job['backend'] == 'dsh':
                refresh_dsh(job_file.parent)
            result = summary(job_file.parent)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
