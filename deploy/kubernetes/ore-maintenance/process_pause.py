"""Pause exact Frigate writers only after an independent resume guard is ready."""
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time

SCHEMA = "frigate.writer-pause/v1"


def process(pid, root=Path('/proc')):
    try:
        directory = root / str(pid)
        stat = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
        command = (directory / 'cmdline').read_bytes()
        return {'pid': int(pid), 'ppid': int(stat[1]), 'start_ticks': int(stat[19]),
                'state': stat[0], 'command_sha256': hashlib.sha256(command).hexdigest(),
                'args': command.split(b'\0')}
    except (FileNotFoundError, ProcessLookupError):
        return None


def inspect():
    rows = {int(p.name): process(int(p.name)) for p in Path('/proc').iterdir() if p.name.isdigit()}
    rows = {p: r for p, r in rows.items() if r}
    mains = [p for p, r in rows.items() if any(r['args'][i:i+2] == [b'-m', b'frigate'] for i in range(len(r['args'])-1))]
    go = [p for p, r in rows.items() if r['args'] and Path(os.fsdecode(r['args'][0])).name == 'go2rtc']
    if len(mains) != 1 or len(go) != 1:
        raise ValueError('exact Frigate and go2rtc writers required')
    selected = set(mains + go)
    while True:
        expanded = selected | {p for p, r in rows.items() if r['ppid'] in selected}
        if expanded == selected: break
        selected = expanded
    if len(selected) > 128 or 1 in selected or os.getpid() in selected:
        raise ValueError('writer set outside bounded scope')
    order = mains + go + sorted(selected-set(mains+go))
    return {'schema': SCHEMA, 'roots': mains+go,
            'processes': [{k: rows[p][k] for k in ['pid', 'ppid', 'start_ticks', 'command_sha256']} for p in order]}


def same(expected, actual):
    return actual is not None and all(actual[k] == expected[k] for k in ['pid', 'start_ticks', 'command_sha256'])


def resume(intent, read=process, send=os.kill):
    resumed = []
    if intent.get('schema') != SCHEMA or not 2 <= len(intent['processes']) <= 128:
        raise ValueError('pause intent differs')
    for row in intent['processes']:
        if row['pid'] <= 1:
            raise ValueError('protected process identity')
        if same(row, read(row['pid'])):
            send(row['pid'], signal.SIGCONT); resumed.append(row['pid'])
    return {'resumed_count': len(resumed)}


def pause(intent):
    # The launcher verifies the independently running resume Job before this call.
    if inspect() != intent:
        raise ValueError('writer identity changed before pause')
    stopped = []
    try:
        for row in intent['processes']:
            if not same(row, process(row['pid'])):
                raise ValueError('writer identity changed during pause')
            os.kill(row['pid'], signal.SIGSTOP); stopped.append(row)
        for _ in range(20):
            if all(same(r, process(r['pid'])) and process(r['pid'])['state'] in ['T', 't'] for r in stopped):
                if inspect() != intent:
                    raise ValueError('writer set changed during pause')
                return {'writer_quiesced': True, 'process_count': len(stopped)}
            time.sleep(0.05)
        raise ValueError('writer pause did not converge')
    except BaseException:
        resume({**intent, 'processes': stopped}) if len(stopped) >= 2 else [os.kill(r['pid'], signal.SIGCONT) for r in stopped if same(r, process(r['pid']))]
        raise


if __name__ == '__main__':
    mode = sys.argv[1]
    if mode == 'inspect': result = inspect()
    elif mode == 'pause': result = pause(json.load(sys.stdin))
    elif mode == 'resume': result = resume(json.load(sys.stdin))
    else: raise SystemExit('unknown mode')
    print(json.dumps(result, sort_keys=True))
