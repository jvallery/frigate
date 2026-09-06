"""Independent Kubernetes Job: resume only the original paused Pod and processes."""
import json
from pathlib import Path
import subprocess
import time


def original_state(envelope):
    result = subprocess.run(['kubectl', '-n', 'frigate-dev', 'get', 'pod', envelope['pod_name'], '-o', 'json', '--ignore-not-found'], capture_output=True, text=True, timeout=15, check=True)
    if not result.stdout.strip():
        return 'absent'
    metadata = json.loads(result.stdout)['metadata']
    if metadata['uid'] != envelope['pod_uid']:
        return 'different-uid'
    return 'terminating' if metadata.get('deletionTimestamp') else 'present'


def run(envelope):
    if original_state(envelope) != 'present':
        raise ValueError('original Pod absent or terminating before guard readiness')
    if time.time() + 40 > envelope['deadline']:
        raise ValueError('insufficient remaining lifetime for readiness')
    print(json.dumps({'schema': 'frigate.resume-guard/v1', 'phase': 'ready', 'operation': envelope['operation'], 'pod_uid': envelope['pod_uid'], 'deadline': envelope['deadline']}), flush=True)
    while time.time() < envelope['deadline']:
        time.sleep(max(0.01, min(2, envelope['deadline'] - time.time())))
    state = original_state(envelope)
    if state != 'present':
        print(json.dumps({'phase': 'original-pod-' + state, 'operation': envelope['operation']}), flush=True)
        return
    helper = Path('/guard/process_pause.py').read_text()
    marker = '/tmp/' + envelope['operation'] + '.json'
    script = 'import json;from pathlib import Path;assert json.loads(Path(' + repr(marker) + ').read_text()) == ' + repr({'pod_uid': envelope['pod_uid'], 'operation': envelope['operation']}) + '\n' + helper
    result = subprocess.run(['kubectl', '-n', 'frigate-dev', 'exec', '-i', envelope['pod_name'], '-c', 'frigate', '--', 'python3', '-c', script, 'resume'], input=json.dumps(envelope['writers']), capture_output=True, text=True, timeout=30, check=True)
    print(json.dumps({'phase': 'resumed-original', 'operation': envelope['operation'], 'result': json.loads(result.stdout)}), flush=True)


if __name__ == '__main__':
    run(json.loads(Path('/guard/intent.json').read_text()))
