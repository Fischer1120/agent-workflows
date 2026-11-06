"""Tests for speed-priority dispatch and DSH independent review markers."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".agents/skills/codex-tiered-workflow/scripts"))

import argparse
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import dispatch
import dsh_bridge


CATALOG = {
    'permissions': [
        {'enabled': True, 'selectionId': 'perm-read-only',
         'metadata': {'preset': 'read-only'}},
        {'enabled': True, 'selectionId': 'perm-workspace-write',
         'metadata': {'preset': 'workspace-write'}},
    ],
}


class DispatchSubmitTests(unittest.TestCase):
    def setUp(self):
        # Dedicated per-test root; cleanup never removes anything recursively.
        self.root = Path(tempfile.mkdtemp(prefix='dispatch-tests-')).resolve()
        self.addCleanup(self.cleanup)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.runs = self.root / 'runs'
        self.brief = self.root / 'brief.txt'
        self.brief.write_text('完成任务。')
        self.calls = []

    def cleanup(self):
        root = self.root
        for path in sorted(root.rglob('*'), key=lambda item: len(item.parts), reverse=True):
            resolved = path.resolve()
            if root not in resolved.parents:
                raise AssertionError(f'temporary path escaped test root: {resolved}')
            if resolved.is_dir():
                resolved.rmdir()
            else:
                resolved.unlink()
        root.rmdir()

    def args(self, **overrides):
        values = {
            'backend': 'dsh', 'speed_priority': False, 'phase': 'execute',
            'cwd': str(self.project), 'project_title': '测试项目',
            'task_title': '测试子任务', 'brief_file': str(self.brief),
            'runs_dir': str(self.runs), 'sandbox': 'read-only', 'timeout': 2700,
            'dsh_model': None, 'dsh_preset': None,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def fake_discover(self):
        self.calls.append(('discover',))
        return dict(CATALOG)

    def fake_submit(self, cwd, content, session_id, client_message_id, **kwargs):
        self.calls.append(('submit', session_id, content, kwargs))
        return {'accepted': True, 'sessionId': session_id,
                'clientMessageId': client_message_id}

    def run_submit(self, args):
        with mock.patch.object(dsh_bridge, 'discover', side_effect=self.fake_discover), \
                mock.patch.object(dsh_bridge, 'submit', side_effect=self.fake_submit):
            return dispatch.submit(args)

    def read_job(self, result):
        return json.loads(Path(result['job_file']).read_text())

    def read_prompt(self, result):
        return (Path(result['job_file']).parent / 'brief.txt').read_text()

    def test_speed_priority_defaults_to_dsh_and_marks_dsh_reviewer(self):
        result = self.run_submit(self.args(backend=None, speed_priority=True))

        job = self.read_job(result)
        self.assertEqual(job['backend'], 'dsh')
        self.assertIs(job['speed_priority'], True)
        self.assertEqual(job['phase'], 'execute')
        self.assertEqual(job['final_reviewer'], 'dsh')
        for key in ('speed_priority', 'phase', 'final_reviewer'):
            self.assertEqual(result[key], job[key])
        prompt = self.read_prompt(result)
        self.assertIn('另一 DSH 会话独立验收', prompt)
        self.assertIn('未参与实现', prompt)
        self.assertNotIn('由未参与实现的旗舰档子代理独立验收', prompt)
        submits = [call for call in self.calls if call[0] == 'submit']
        self.assertEqual(len(submits), 1)
        self.assertEqual(submits[0][1], job['session_id'])

    def test_ordinary_dsh_keeps_existing_prompt_and_sol_review(self):
        result = self.run_submit(self.args())

        job = self.read_job(result)
        self.assertEqual(job['backend'], 'dsh')
        self.assertIs(job['speed_priority'], False)
        self.assertEqual(job['final_reviewer'], 'gpt-6.1-sol')
        expected = (f'Codex 项目：测试项目\n子任务：测试子任务\n'
                    f'共享项目目录：{self.project}\n'
                    '你接受旗舰档主模型的任务分派，只执行这份简报，不启动其他模型或重新分配任务。'
                    '保留用户与其他执行者的修改。只操作简报允许的文件；需要新权限或关键判断时停止并回报。'
                    '完成后给出结果、实际文件路径、验证证据和未解决事项，由未参与实现的旗舰档子代理独立验收。\n\n'
                    '完成任务。')
        self.assertEqual(self.read_prompt(result), expected)
        self.assertEqual(result['final_reviewer'], 'gpt-6.1-sol')

    def test_ordinary_luna_behavior_is_preserved(self):
        with mock.patch.object(dispatch.subprocess, 'Popen') as popen:
            result = dispatch.submit(self.args(backend='luna'))

        job = self.read_job(result)
        self.assertEqual(job['backend'], 'luna')
        self.assertIs(job['speed_priority'], False)
        self.assertEqual(job['final_reviewer'], 'gpt-6.1-sol')
        self.assertIn('由未参与实现的旗舰档子代理独立验收', self.read_prompt(result))
        self.assertEqual([call for call in self.calls if call[0] == 'submit'], [])
        self.assertEqual(popen.call_count, 1)
        self.assertIn('_luna-worker', popen.call_args.args[0])

    def test_luna_multimodal_is_rejected_before_worker_starts(self):
        with mock.patch.object(dispatch.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(ValueError, '交回旗舰档'):
                self.run_submit(self.args(backend='luna', multimodal=True))
        popen.assert_not_called()
        self.assertEqual(self.calls, [])
        self.assertFalse(self.runs.exists())

    def test_speed_priority_luna_is_rejected_before_any_backend_call(self):
        with self.assertRaises(ValueError):
            self.run_submit(self.args(backend='luna', speed_priority=True))
        self.assertEqual(self.calls, [])
        self.assertFalse(self.runs.exists())

    def test_ordinary_dsh_multimodal_is_rejected_before_read_or_backend(self):
        with mock.patch.object(dispatch.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(ValueError, '交回旗舰档'):
                self.run_submit(self.args(multimodal=True, brief_file=str(self.root / 'missing')))
        self.assertEqual(self.calls, [])
        popen.assert_not_called()
        self.assertFalse(self.runs.exists())

    def test_speed_priority_multimodal_rejects_execute_and_review(self):
        with mock.patch.object(dispatch.subprocess, 'Popen') as popen:
            for phase in ('execute', 'review'):
                with self.subTest(phase=phase):
                    with self.assertRaisesRegex(ValueError, '交回旗舰档'):
                        self.run_submit(self.args(backend=None, speed_priority=True,
                                                  phase=phase, multimodal=True,
                                                  brief_file=str(self.root / 'missing')))
                    self.assertEqual(self.calls, [])
                    self.assertFalse(self.runs.exists())
        popen.assert_not_called()

    def test_cli_multimodal_flag_rejects_before_creating_job(self):
        result = subprocess.run(
            [sys.executable, str(Path(dispatch.__file__).resolve()), 'submit',
             '--backend', 'dsh', '--multimodal', '--cwd', str(self.project),
             '--task-title', '测试', '--brief-file', str(self.root / 'missing'),
             '--runs-dir', str(self.runs)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('交回旗舰档', json.loads(result.stderr)['error'])
        self.assertFalse(self.runs.exists())

    def test_review_without_speed_priority_is_rejected(self):
        with self.assertRaises(ValueError):
            self.run_submit(self.args(backend='dsh', speed_priority=False, phase='review'))
        self.assertEqual(self.calls, [])
        self.assertFalse(self.runs.exists())

    def test_review_rejects_writable_sandbox(self):
        with self.assertRaises(ValueError):
            self.run_submit(self.args(backend=None, speed_priority=True, phase='review',
                                      sandbox='workspace-write'))
        self.assertEqual(self.calls, [])
        self.assertFalse(self.runs.exists())

    def test_review_prompt_is_independent_and_omits_sol_review(self):
        result = self.run_submit(self.args(backend=None, speed_priority=True,
                                           phase='review', sandbox='read-only'))

        job = self.read_job(result)
        self.assertEqual(job['backend'], 'dsh')
        self.assertIs(job['speed_priority'], True)
        self.assertEqual(job['phase'], 'review')
        self.assertEqual(job['final_reviewer'], 'dsh')
        self.assertEqual(job['sandbox'], 'read-only')
        prompt = self.read_prompt(result)
        self.assertIn('通过/不通过', prompt)
        self.assertIn('不修改文件', prompt)
        self.assertIn('不追加旗舰档或低端档复核', prompt)
        self.assertNotIn('Sol 独立验收', prompt)
        self.assertNotIn('由未参与实现的旗舰档子代理独立验收', prompt)

    def test_two_submits_use_independent_sessions_without_history(self):
        first = self.run_submit(self.args(backend=None, speed_priority=True))
        second = self.run_submit(self.args(backend=None, speed_priority=True,
                                           phase='review', sandbox='read-only'))

        first_job = self.read_job(first)
        second_job = self.read_job(second)
        self.assertNotEqual(first_job['session_id'], second_job['session_id'])
        self.assertNotEqual(first['job_id'], second['job_id'])
        submits = [call for call in self.calls if call[0] == 'submit']
        self.assertEqual(len(submits), 2)
        self.assertNotEqual(submits[0][1], submits[1][1])
        second_prompt = self.read_prompt(second)
        self.assertNotIn(first['job_id'], second_prompt)
        self.assertNotIn('旗舰档子代理独立验收', second_prompt)

    def test_legacy_cli_backend_form_still_works(self):
        argv = ['dispatch.py', 'submit', '--backend', 'dsh', '--cwd', str(self.project),
                '--project-title', '测试项目', '--task-title', '测试子任务',
                '--brief-file', str(self.brief), '--runs-dir', str(self.runs)]
        stdout = io.StringIO()
        with mock.patch.object(sys, 'argv', argv), \
                mock.patch.object(dsh_bridge, 'discover', side_effect=self.fake_discover), \
                mock.patch.object(dsh_bridge, 'submit', side_effect=self.fake_submit), \
                contextlib.redirect_stdout(stdout):
            dispatch.main()
        result = json.loads(stdout.getvalue())
        self.assertEqual(result['backend'], 'dsh')
        self.assertIs(result['speed_priority'], False)
        self.assertEqual(result['phase'], 'execute')
        self.assertEqual(result['final_reviewer'], 'gpt-6.1-sol')

    def test_ordinary_cli_without_backend_still_fails(self):
        argv = ['dispatch.py', 'submit', '--cwd', str(self.project),
                '--task-title', '测试子任务', '--brief-file', str(self.brief)]
        stderr = io.StringIO()
        with mock.patch.object(sys, 'argv', argv), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                dispatch.main()
        self.assertEqual(raised.exception.code, 1)
        error = json.loads(stderr.getvalue())
        self.assertEqual(error['status'], 'error')
        self.assertIn('--backend', error['error'])
        self.assertFalse(self.runs.exists())


if __name__ == '__main__':
    unittest.main()
