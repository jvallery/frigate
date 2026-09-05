import importlib.util
from pathlib import Path
import signal
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('process_pause', Path(__file__).with_name('process_pause.py'))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


def intent():
    return {'schema': m.SCHEMA, 'roots': [100, 200], 'processes': [
        {'pid': p, 'ppid': 1, 'start_ticks': p*10, 'command_sha256': str(p)} for p in [100, 200]]}


class PauseTests(unittest.TestCase):
    def test_resume_targets_exact_original_processes(self):
        d = intent(); sent = []
        m.resume(d, read=lambda p: next(r for r in d['processes'] if r['pid'] == p), send=lambda p,s: sent.append((p,s)))
        self.assertEqual(sent, [(100, signal.SIGCONT), (200, signal.SIGCONT)])

    def test_reused_pid_and_changed_command_never_signalled(self):
        d = intent(); sent = []
        for field in ['start_ticks', 'command_sha256']:
            m.resume(d, read=lambda p: {**next(r for r in d['processes'] if r['pid'] == p), field: 'changed'}, send=lambda p,s: sent.append(p))
        self.assertEqual(sent, [])

    def test_absent_original_process_is_not_replaced(self):
        self.assertEqual(m.resume(intent(), read=lambda p: None, send=lambda p,s: self.fail('signal')), {'resumed_count': 0})

    def test_identity_change_prevents_any_pause(self):
        with mock.patch.object(m, 'inspect', return_value={'changed':True}), mock.patch.object(m.os, 'kill') as kill:
            with self.assertRaisesRegex(ValueError, 'before pause'):m.pause(intent())
            kill.assert_not_called()

    def test_partial_pause_failure_resumes_already_paused_original(self):
        d = intent(); original=d['processes'][0]
        with mock.patch.object(m, 'inspect', return_value=d), mock.patch.object(m, 'process', side_effect=lambda p: original if p==100 else None), mock.patch.object(m.os, 'kill') as kill:
            with self.assertRaisesRegex(ValueError, 'during pause'):m.pause(d)
            self.assertEqual(kill.call_args_list, [mock.call(100, signal.SIGSTOP), mock.call(100, signal.SIGCONT)])

    def test_init_process_is_protected(self):
        d=intent();d['processes'][0]['pid']=1
        with self.assertRaisesRegex(ValueError, 'protected'):m.resume(d)


if __name__=='__main__':unittest.main()
