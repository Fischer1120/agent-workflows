"""Run a bounded low-end fast task through the installed Codex app-server protocol."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class AppServer:
    def __init__(self, cwd: str):
        binary = shutil.which('codex')
        if not binary:
            raise RuntimeError('找不到已安装的 codex CLI')
        args = [binary, 'app-server', '--stdio', '-c', 'service_tier="fast"',
                '-c', 'model="gpt-6-luna"', '-c', 'model_reasoning_effort="max"',
                '-c', 'features.fast_mode=true', '-c', 'features.multi_agent=false']
        self.proc = subprocess.Popen(args, cwd=cwd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1)
        self.inbox = queue.Queue()
        self.pending = []
        self.counter = 0
        def read():
            try:
                for line in self.proc.stdout:
                    try:
                        self.inbox.put(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            finally:
                self.inbox.put(None)
        threading.Thread(target=read, daemon=True).start()

    def send(self, obj):
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + '\n')
        self.proc.stdin.flush()

    def receive(self, deadline):
        try:
            item = self.inbox.get(timeout=max(0.001, deadline - time.monotonic()))
        except queue.Empty:
            raise TimeoutError('Codex 请求超时')
        if item is None:
            raise RuntimeError('Codex app-server 已退出')
        return item

    def request(self, method, params, timeout=30):
        self.counter += 1
        request_id = self.counter
        self.send({'id': request_id, 'method': method, 'params': params})
        deadline = time.monotonic() + timeout
        while True:
            msg = self.receive(deadline)
            if msg.get('id') == request_id and ('result' in msg or 'error' in msg):
                if 'error' in msg:
                    raise RuntimeError(f'{method}: {msg["error"].get("message", "RPC error")}')
                return msg['result']
            self.pending.append(msg)

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)


def run(job_dir: Path):
    job = json.loads((job_dir / 'job.json').read_text())
    state = {'status': 'running', 'backend': 'luna', 'model': 'gpt-6-luna',
             'requested_effort': 'max', 'requested_service_tier': 'fast',
             'started_at': time.time()}
    write_json(job_dir / 'status.json', state)
    server = None
    try:
        server = AppServer(job['cwd'])
        server.request('initialize', {'clientInfo': {'name': 'codex_workflow_luna',
                                                     'version': '1.0.0'}})
        server.send({'method': 'initialized', 'params': {}})
        response = server.request('thread/start', {
            'model': 'gpt-6-luna', 'serviceTier': 'fast', 'cwd': job['cwd'],
            'sandbox': job['sandbox'], 'approvalPolicy': 'on-request',
            'ephemeral': True,
            'developerInstructions': '你是低端快速档执行者，接受旗舰档主模型的分派。只完成给定简报，不重新分流、不启动子代理或 dsh。需要新权限或业务判断时停止并回报。',
            'config': {'model_reasoning_effort': 'max', 'features.multi_agent': False,
                       'features.fast_mode': True}})
        actual = {key: response.get(key) for key in
                  ['model', 'reasoningEffort', 'serviceTier', 'cwd', 'sandbox',
                   'approvalPolicy', 'approvalsReviewer']}
        state['effective'] = actual
        state['thread_id'] = response['thread']['id']
        write_json(job_dir / 'status.json', state)
        if actual['model'] != 'gpt-6-luna':
            raise RuntimeError('运行时未接受指定的 Luna 模型')
        if actual['serviceTier'] not in ('fast', 'priority'):
            raise RuntimeError('运行时未接受 Fast，任务未执行')
        if actual['reasoningEffort'] != 'max':
            raise RuntimeError('运行时未接受 max，任务未执行')
        expected_sandbox = {'read-only': 'readOnly', 'workspace-write': 'workspaceWrite'}[job['sandbox']]
        if (not isinstance(actual['sandbox'], dict) or
                actual['sandbox'].get('type') != expected_sandbox):
            raise RuntimeError('运行时未接受指定沙箱，任务未执行')
        if Path(actual['cwd']).resolve() != Path(job['cwd']).resolve():
            raise RuntimeError('运行时项目目录不匹配，任务未执行')
        if actual['approvalPolicy'] != 'on-request' or actual['approvalsReviewer'] != 'user':
            raise RuntimeError('运行时审批策略不匹配，任务未执行')
        prompt = (job_dir / 'brief.txt').read_text()
        turn = server.request('turn/start', {
            'threadId': state['thread_id'], 'model': 'gpt-6-luna', 'effort': 'max',
            'serviceTier': 'fast', 'input': [{'type': 'text', 'text': prompt}]})
        state['turn_id'] = turn['turn']['id']
        write_json(job_dir / 'status.json', state)
        deadline = time.monotonic() + job.get('timeout', 2700)
        messages = []
        while True:
            msg = server.pending.pop(0) if server.pending else server.receive(deadline)
            if 'id' in msg and 'method' in msg:
                # No approval or user-input channel exists in this detached worker.
                # End the worker and leave the decision with the dispatching parent.
                state['status'] = 'needs_attention'
                state['request_method'] = msg['method']
                state['error'] = '子任务需要权限或用户输入，请由派发的旗舰档接回处理'
                break
            method, params = msg.get('method'), msg.get('params', {})
            if params.get('threadId') not in (None, state['thread_id']):
                continue
            if method == 'item/completed':
                item = params.get('item', {})
                if item.get('type') == 'agentMessage' and item.get('phase') != 'commentary':
                    messages.append(item.get('text', ''))
            if method == 'turn/completed' and params.get('turn', {}).get('id') == state['turn_id']:
                final_turn = params['turn']
                state['turn_status'] = final_turn['status']
                state['status'] = 'completed' if final_turn['status'] == 'completed' else 'failed'
                if final_turn.get('error'):
                    state['error'] = final_turn['error'].get('message', 'Codex turn failed')
                break
        (job_dir / 'result.txt').write_text('\n\n'.join(messages) + '\n')
        os.chmod(job_dir / 'result.txt', 0o600)
        if state['status'] == 'completed' and not any(t.strip() for t in messages):
            state.update(status='failed', error='Codex 完成但未返回可验收的结果')
    except Exception as exc:
        state.update(status='failed', error=str(exc))
    finally:
        if server:
            server.close()
        state['finished_at'] = time.time()
        write_json(job_dir / 'status.json', state)
