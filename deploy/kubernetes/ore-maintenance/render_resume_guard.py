"""Render a bounded off-Ore recovery Job; never dispatch or pause a workload."""
import argparse
import json
from pathlib import Path
import re
import time
import uuid


def render(envelope, image, now):
    if set(envelope) != {'operation', 'pod_name', 'pod_uid', 'deadline', 'writers'}:
        raise ValueError('guard envelope differs')
    if not re.fullmatch(r'frigate-ore-[a-z0-9-]{1,20}', envelope['operation']):
        raise ValueError('operation outside bounded scope')
    if not re.fullmatch(r'frigate-dev-[a-z0-9]+-[a-z0-9]+', envelope['pod_name']):
        raise ValueError('guard target differs')
    if str(uuid.UUID(envelope['pod_uid'])) != envelope['pod_uid'] or not now + 60 <= envelope['deadline'] <= now + 300:
        raise ValueError('guard identity or lifetime differs')
    if not re.fullmatch(r'registry.vallery.net/jvallery/agents-agent-dev-runtime@sha256:[0-9a-f]{64}', image):
        raise ValueError('qualified operator runtime digest required')
    writers = envelope['writers']
    if writers.get('schema') != 'frigate.writer-pause/v1' or not 2 <= len(writers['processes']) <= 128:
        raise ValueError('writer authority differs')
    name=envelope['operation']; ns='agent-fleet-repo-auth'; labels={'app.kubernetes.io/name':name}
    def obj(api, kind, namespace, spec):
        return {'apiVersion':api,'kind':kind,'metadata':{'name':name,'namespace':namespace},**spec}
    data={p:Path(__file__).with_name(p).read_text() for p in ['resume_guard.py','process_pause.py']};data['intent.json']=json.dumps(envelope,sort_keys=True)
    return {'apiVersion':'v1','kind':'List','items':[
        obj('v1','ServiceAccount',ns,{'automountServiceAccountToken':True}),
        obj('v1','ConfigMap',ns,{'immutable':True,'data':data}),
        obj('rbac.authorization.k8s.io/v1','Role','frigate-dev',{'rules':[
            {'apiGroups':[''],'resources':['pods'],'resourceNames':[envelope['pod_name']],'verbs':['get']},
            {'apiGroups':[''],'resources':['pods/exec'],'resourceNames':[envelope['pod_name']],'verbs':['get','create']}]}),
        obj('rbac.authorization.k8s.io/v1','RoleBinding','frigate-dev',{'roleRef':{'apiGroup':'rbac.authorization.k8s.io','kind':'Role','name':name},'subjects':[{'kind':'ServiceAccount','name':name,'namespace':ns}]}),
        obj('networking.k8s.io/v1','NetworkPolicy',ns,{'spec':{'podSelector':{'matchLabels':labels},'policyTypes':['Egress'],'egress':[
            {'to':[{'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':'kube-system'}}}],'ports':[{'protocol':'UDP','port':53},{'protocol':'TCP','port':53}]},
            {'to':[{'ipBlock':{'cidr':ip+'/32'}} for ip in ['10.43.0.1','192.168.30.31','192.168.30.32','192.168.30.33','192.168.30.240']],'ports':[{'protocol':'TCP','port':443},{'protocol':'TCP','port':6443}]}]}}),
        obj('batch/v1','Job',ns,{'spec':{'backoffLimit':2,'activeDeadlineSeconds':420,'template':{'metadata':{'labels':labels},'spec':{
            'serviceAccountName':name,'imagePullSecrets':[{'name':'zot-origin-cluster-pull'}],'restartPolicy':'Never','nodeSelector':{'kubernetes.io/hostname':'vm-k3s-node4'},
            'containers':[{'name':'guard','image':image,'command':['python3','/guard/resume_guard.py'],'resources':{'requests':{'cpu':'10m','memory':'64Mi'},'limits':{'cpu':'200m','memory':'128Mi'}},'volumeMounts':[{'name':'source','mountPath':'/guard','readOnly':True}]}],
            'volumes':[{'name':'source','configMap':{'name':name}}]}}}})
    ]}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--intent',type=Path,required=True);parser.add_argument('--runtime-image',required=True)
    args=parser.parse_args();print(json.dumps(render(json.loads(args.intent.read_text()),args.runtime_image,int(time.time())),indent=2))
