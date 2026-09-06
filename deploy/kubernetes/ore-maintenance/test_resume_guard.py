import copy
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

spec=importlib.util.spec_from_file_location('render_resume_guard',Path(__file__).with_name('render_resume_guard.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def intent():
    return {'operation':'frigate-ore-fixture-v1','pod_name':'frigate-dev-abcdef-abcde',
            'pod_uid':'03ff3cec-b970-414d-b576-223571a7d6d2','deadline':1110,
            'writers':{'schema':'frigate.writer-pause/v1','processes':[{'pid':100},{'pid':200}]}}


spec2=importlib.util.spec_from_file_location('resume_guard',Path(__file__).with_name('resume_guard.py'))
g=importlib.util.module_from_spec(spec2);spec2.loader.exec_module(g)


class GuardTests(unittest.TestCase):
    image='registry.vallery.net/jvallery/agents-agent-dev-runtime@sha256:'+'a'*64
    def test_exact_recovery_rbac_and_independent_placement(self):
        d=m.render(intent(),self.image,1000);by={x['kind']:x for x in d['items']}
        rules=by['Role']['rules'];self.assertEqual([x['resourceNames'] for x in rules],[[intent()['pod_name']]]*2)
        self.assertEqual([x['verbs'] for x in rules],[['get'],['get','create']])
        job=by['Job']['spec'];self.assertEqual(job['activeDeadlineSeconds'],420)
        self.assertEqual(job['template']['spec']['nodeSelector'],{'kubernetes.io/hostname':'vm-k3s-node4'})
        self.assertTrue(by['ConfigMap']['immutable'])
    def test_expired_or_imminent_guard_never_emits_ready(self):
        for deadline in [999, 1039]:
            d=intent();d['deadline']=deadline
            with mock.patch.object(g,'original_state',return_value='present'), mock.patch.object(g.time,'time',return_value=1000), mock.patch('builtins.print') as out:
                with self.assertRaisesRegex(ValueError,'remaining lifetime'):g.run(d)
                out.assert_not_called()

    def test_terminating_original_never_executes_resume(self):
        d = intent()
        with mock.patch.object(g, 'original_state', side_effect=['present', 'terminating']), \
             mock.patch.object(g.time, 'time', side_effect=[1000, 1111]), \
             mock.patch.object(g.subprocess, 'run') as execute, mock.patch('builtins.print') as output:
            g.run(d)
            execute.assert_not_called()
            self.assertIn('original-pod-terminating', output.call_args[0][0])

    def test_original_state_distinguishes_deletion_from_absence(self):
        import json
        metadata = {'uid': intent()['pod_uid'], 'deletionTimestamp': '2026-09-06T00:00:00Z'}
        with mock.patch.object(g.subprocess, 'run', return_value=mock.Mock(stdout=json.dumps({'metadata': metadata}))):
            self.assertEqual(g.original_state(intent()), 'terminating')

    def test_missing_or_unbounded_deadline_rejected(self):
        for deadline in [999,1059,1121]:
            d=intent();d['deadline']=deadline
            with self.subTest(deadline=deadline),self.assertRaises(ValueError):m.render(d,self.image,1000)
    def test_foreign_pod_rejected(self):
        d=intent();d['pod_name']='production-frigate-1'
        with self.assertRaises(ValueError):m.render(d,self.image,1000)
    def test_mutable_runtime_image_rejected(self):
        with self.assertRaises(ValueError):m.render(intent(),'registry.vallery.net/jvallery/agents-agent-dev-runtime:latest',1000)
    def test_wrong_pod_identity_rejected(self):
        d=intent();d['pod_uid']='unknown'
        with self.assertRaises(ValueError):m.render(d,self.image,1000)


if __name__=='__main__':unittest.main()
