#!/usr/bin/env python3
"""Disposable Omarchy resource-observation proof, never alters existing jobs."""
import datetime
import hashlib
import json
import importlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import socket
import subprocess
import time
import uuid
from cloudworkbench.runtime import Runtime
from cloudworkbench.resource_samples import SampleWindow, sample_runtime

IMAGE = 'sha256:408eb6e2b5c4cb58747fbb1fbac31031545135005079abf878012c33a6632331'


def docker(*args):
    result = subprocess.run(['/usr/bin/docker', *args], capture_output=True, text=True, timeout=15, check=True, env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
    return result.stdout.strip()


def main():
    assert socket.gethostname() == 'omarchy'
    assert docker('image', 'inspect', IMAGE, '--format', '{{.Id}}') == IMAGE
    owner = 'resprobe-' + uuid.uuid4().hex[:12]
    name = 'cwb2-' + owner
    runtime_id = None
    receipt = {'host': socket.gethostname(), 'time': datetime.datetime.now(datetime.timezone.utc).isoformat(),
               'owner': owner, 'image': IMAGE, 'generation': 1, 'purpose': 'disposable resource sampler proof',
               'services_changed': False, 'source_deployed': False,
               'docker_server_version': docker('version', '--format', '{{.Server.Version}}'),
               'cgroup': docker('info', '--format', '{{.CgroupDriver}}/{{.CgroupVersion}}'),
               'docker_environment': {'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},
               'source_sha256': {name: hashlib.sha256(path.read_bytes()).hexdigest() for name,path in (
                   ('src/cloudworkbench/runtime.py', Path(importlib.import_module('cloudworkbench.runtime').__file__)),
                   ('src/cloudworkbench/resource_samples.py', Path(importlib.import_module('cloudworkbench.resource_samples').__file__)),
                   ('scripts/qualify-resource-samples.py', Path(__file__)))}}

    try:
        workload = 'import time; data=bytearray(8*1024*1024); end=time.monotonic()+10\nwhile time.monotonic()<end: sum(range(10000))\ntime.sleep(25)'
        runtime_id = docker('create', '--name', name, '--label', 'io.cloudworkbench.managed=true',
                            '--label', 'io.cloudworkbench.owner='+owner, '--label', 'io.cloudworkbench.attempt='+owner,
                            '--label', 'io.cloudworkbench.generation=1', '--label', 'io.cloudworkbench.role=job',
                            '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                            '--user', '1000:1000', '--cpus', '.25', '--memory', '128m', '--memory-swap', '128m',
                            '--pids-limit', '16', '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=16m',
                            '--entrypoint', 'python3', IMAGE, '-c', workload)
        docker('start', runtime_id)
        config = {'root': '/tmp', 'image': IMAGE, 'owner': owner, 'cpus': .25, 'memory_mib': 128, 'pids': 16}
        runtime = Runtime(config)
        settings = json.loads(docker('inspect', '--format', '{{json .HostConfig}}', runtime_id))
        assert settings['NanoCpus'] == 250000000 and settings['Memory'] == 134217728 and settings['PidsLimit'] == 16
        assert settings['NetworkMode'] == 'none' and settings['ReadonlyRootfs'] is True
        assert not json.loads(docker('inspect', '--format', '{{json .Mounts}}', runtime_id))
        window = SampleWindow(3)
        values = [sample_runtime(runtime, runtime_id, expected_generation=1) for _ in range(2)]
        def modest_read_load():
            for _ in range(8):
                docker('inspect', '--format', '{{.Id}}', runtime_id)
                time.sleep(.1)
        with ThreadPoolExecutor(max_workers=1) as executor:
            background = executor.submit(modest_read_load)
            values.append(sample_runtime(runtime, runtime_id, expected_generation=1))
            background.result()
        receipt['load_probe'] = {'scope':'eight targeted inspect calls on the same disposable container',
                                 'sample_index':2, 'status':values[2]['status'], 'elapsed_ms':values[2]['elapsed_ms']}

        assert all(value['status'] == 'observed' and value['elapsed_ms'] <= 3000 and value['memory_bytes_approx'] > 0 and
                   value['memory_limit_bytes_approx'] == 134217728 and value['pids'] >= 1 for value in values)
        assert any(value['cpu_percent'] > 0 for value in values)
        for value in values:
            window.add(value)
        wrong_generation = sample_runtime(runtime, runtime_id, expected_generation=2)
        assert wrong_generation['status'] == 'unknown' and wrong_generation['reason'] == 'ownership_unconfirmed'
        wrong_owner = sample_runtime(Runtime({**config, 'owner': owner+'-other'}), runtime_id, expected_generation=1)
        assert wrong_owner['status'] == 'unknown' and wrong_owner['reason'] == 'runtime_missing'
        receipt.update(runtime_id=runtime_id, limits={'cpus': .25, 'memory_mib': 128, 'pids': 16, 'tmpfs_mib': 16},
                       sample_window=window.summary(), wrong_generation=wrong_generation['reason'], wrong_owner=wrong_owner['reason'])
        docker('stop', '--time', '1', runtime_id)
        stopped = sample_runtime(runtime, runtime_id, expected_generation=1)
        assert stopped['status'] == 'unknown' and stopped['reason'] == 'runtime_not_running'
        receipt['stopped_reason'] = stopped['reason']
    finally:
        if runtime_id:
            labels = json.loads(docker('inspect', '--format', '{{json .Config.Labels}}', runtime_id))
            assert labels['io.cloudworkbench.owner'] == owner and labels['io.cloudworkbench.generation'] == '1'
            docker('rm', '-f', runtime_id)
        remaining = docker('container', 'ls', '--all', '--no-trunc', '--filter', 'label=io.cloudworkbench.owner='+owner, '--format', '{{.ID}}')
        assert not remaining
        receipt['cleanup_verified'] = True
    receipt['status'] = 'passed'
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()
