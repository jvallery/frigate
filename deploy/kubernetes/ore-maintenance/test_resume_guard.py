import copy
import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('render_resume_guard',Path(__file__).with_name('render_resume_guard.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def intent():
    return {'operation':'frigate-ore-fixture-v1','pod_name':'frigate-dev-abcdef-abcde',
            'pod_uid':'03ff3cec-b970-414d-b576-223571a7d6d2','deadline':1180,
            'writers':{'schema':'frigate.writer-pause/v1','processes':[{'pid':100},{'pid':200}]}}


class GuardTests(unittest.TestCase):
    image='registry.vallery.net/jvallery/agents-agent-dev-runtime@sha256:'+'a'*64
    def test_exact_recovery_rbac_and_independent_placement(self):
        d=m.render(intent(),self.image,1000);by={x['kind']:x for x in d['items']}
        rules=by['Role']['rules'];self.assertEqual([x['resourceNames'] for x in rules],[[intent()['pod_name']]]*2)
        self.assertEqual([x['verbs'] for x in rules],[['get'],['get','create']])
        job=by['Job']['spec'];self.assertEqual(job['activeDeadlineSeconds'],420)
        self.assertEqual(job['template']['spec']['nodeSelector'],{'kubernetes.io/hostname':'vm-k3s-node4'})
        self.assertTrue(by['ConfigMap']['immutable'])
    def test_missing_or_unbounded_deadline_rejected(self):
        for deadline in [999,1059,1301]:
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
