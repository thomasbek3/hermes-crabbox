import json
from pathlib import Path
import subprocess

import pytest
from cloudworkbench.runtime import Runtime, RuntimeError

DIGEST = 'sha256:' + 'a' * 64
RID = 'b' * 64

@pytest.fixture
def runtime(tmp_path):
    return Runtime({'root':tmp_path, 'image':DIGEST, 'test_path_workspace':True})

@pytest.mark.parametrize('image', ['ubuntu:latest', 'agent', 'sha256:abc', '--privileged'])
def test_unpinned_image_rejected(tmp_path, image):
    with pytest.raises(RuntimeError):
        Runtime({'root':tmp_path,'image':image})

def test_network_requires_allowlist(tmp_path):
    with pytest.raises(RuntimeError):
        Runtime({'root':tmp_path,'image':DIGEST,'network_enabled':True})

@pytest.mark.parametrize('field,value', [('uid',0),('gid',0),('pids',99999),('memory_mib',999999),('cpus',0),('workspace_mib',0)])
def test_resource_rejection(tmp_path, field, value):
    with pytest.raises(RuntimeError):
        Runtime({'root':tmp_path,'image':DIGEST,field:value})

def test_argv_injection_stays_literal(runtime, monkeypatch):
    calls=[]
    secret='super-secret-not-in-process-argv'
    def run(args, **kwargs):
        calls.append(args)
        if args[0]=='create':
            contents=Path(args[args.index('--env-file')+1]).read_text()
            assert secret in contents
            return RID
        return ''
    monkeypatch.setattr(runtime,'_run',run)
    prompt='$(touch /tmp/OWNED); `whoami` && echo nope'
    assert runtime.launch('attempt','session',['python3','-c',prompt],{'TOKEN':secret},generation=7)==RID
    create=calls[0]
    assert create[-3:]==[DIGEST,'-c',prompt]
    assert not any(secret in arg for arg in create)
    assert '--privileged' not in create
    assert create[create.index('--network')+1]=='none'
    assert '--read-only' in create and 'ALL' in create
    assert '--entrypoint' in create
    assert 'io.cloudworkbench.generation=7' in create
    assert 'cloudd=1' not in create
    assert create[create.index('--name')+1].startswith('cwb2-')
    assert not Path(create[create.index('--env-file')+1]).exists()
    assert runtime.native_state('session')==runtime.root/'session'/'native'
    assert not runtime.release_ready

def test_readiness_never_substitutes_path_for_quota(tmp_path, monkeypatch):
    r=Runtime({'root':tmp_path,'image':DIGEST})
    monkeypatch.setattr(subprocess,'run',lambda *a,**k: (_ for _ in ()).throw(subprocess.CalledProcessError(1, 'findmnt')))
    with pytest.raises(RuntimeError, match='hard quota'):
        r.make_workspace('session')

@pytest.mark.parametrize('source,target', [('/etc/passwd','/input'),('/var/run/docker.sock','/input')])
def test_arbitrary_host_mount_rejected(runtime, source, target):
    with pytest.raises(RuntimeError):
        runtime._mounts([{'source':source,'target':target}])

def test_approved_mounts_still_reject_symlinks_destinations_and_writes(tmp_path):
    approved=tmp_path/'approved';approved.mkdir()
    file=approved/'file';file.write_text('x')
    link=approved/'link';link.symlink_to(file)
    r=Runtime({'root':tmp_path,'image':DIGEST,'approved_mount_roots':[approved]})
    assert ',readonly' in r._mounts([{'source':file,'target':'/run/task/task.json'}])[-1]
    for mount in [{'source':link,'target':'/input'},{'source':file,'target':'/workspace'}, {'source':file,'target':'/input','readonly':False},{'source':file,'target':'/a/../etc/x'}]:
        with pytest.raises(RuntimeError): r._mounts([mount])

def test_generation_mismatch_blocks_stop(runtime, monkeypatch):
    calls=[]
    def run(args,**kwargs):
        calls.append(args)
        if args[0]=='container': return RID
        if '.Config.Labels' in args[2]: return json.dumps({'io.cloudworkbench.attempt':'attempt','io.cloudworkbench.generation':'2'})
        return json.dumps({'Running':True})
    monkeypatch.setattr(runtime,'_run',run)
    with pytest.raises(RuntimeError,match='generation'):
        runtime.stop(RID,expected_generation=1)
    assert not any(c[0]=='stop' for c in calls)
    runtime.stop(RID,expected_generation=2)
    assert calls[-1]==['stop','--time','10',RID]

def test_unowned_cleanup_is_noop(runtime, monkeypatch):
    calls=[]
    monkeypatch.setattr(runtime,'_run',lambda args,**kwargs: calls.append(args) or '')
    runtime.cleanup(RID,expected_generation=1)
    assert len(calls)==1

def test_network_downgrade_fails_closed(runtime, monkeypatch):
    calls=[]
    def run(args,**kwargs):
        calls.append(args)
        if args[:2]==['network','inspect']:
            return json.dumps({'Internal':True,'Options':{}})
        return 'network'
    monkeypatch.setattr(runtime,'_run',run)
    cleanup=[]
    monkeypatch.setattr(runtime,'cleanup_attempt',lambda *a,**kw:cleanup.append((a,kw)))
    with pytest.raises(RuntimeError,match='isolated'):
        runtime._network('attempt','session',1)
    assert len(calls)==2
    assert cleanup == [(('attempt',), {'expected_generation':1})]
    assert 'com.docker.network.bridge.gateway_mode_ipv4=isolated' in calls[0]

def test_gateway_internal_bind_and_nonforwarding(runtime, monkeypatch):
    calls=[]
    runtime.allowed_domains=['api.anthropic.com']
    def run(args,**kwargs):
        calls.append(args)
        if args[:2]==['network','inspect']:
            return json.dumps({'Internal':True,'Options':{'com.docker.network.bridge.gateway_mode_ipv4':'isolated'},'IPAM':{'Config':[{'Subnet':'172.30.0.0/24'}]}})
        if args[0]=='create':return RID
        return ''
    monkeypatch.setattr(runtime,'_run',run)
    name,proxy=runtime._network('attempt','session',1)
    create=next(c for c in calls if c[0]=='create')
    assert create[create.index('--listen')+1]=='172.30.0.2'
    assert 'net.ipv4.ip_forward=0' in create
    assert 'net.ipv6.conf.all.forwarding=0' in create
    assert '--publish' not in create and '--privileged' not in create
    assert proxy=='http://172.30.0.2:8080'
    assert name.startswith('cwb2-')

def test_cleanup_infrastructure_refuses_running_job(runtime,monkeypatch):
    monkeypatch.setattr(runtime,'_run',lambda *a,**k:RID)
    with pytest.raises(RuntimeError,match='running'):
        runtime.cleanup_infrastructure('attempt',expected_generation=1)

def test_logs_bounded_even_with_one_oversized_line(runtime,monkeypatch):
    import sys
    monkeypatch.setattr(runtime,'_owned',lambda *_: {'Running':False})
    original=subprocess.Popen
    def fake(*a,**kwargs):
        return original([sys.executable,'-c','import sys; sys.stdout.write("x"*2000000)'],**kwargs)
    monkeypatch.setattr(subprocess,'Popen',fake)
    assert runtime.logs(RID,1024)==b'x'*1024

def test_verifier_workspace_is_readonly(runtime, monkeypatch):
    calls=[]
    runtime.config['workspace_readonly']=True
    monkeypatch.setattr(runtime,'_run',lambda args,**kwargs:calls.append(args) or (RID if args[0]=='create' else ''))
    runtime.launch('verify','session',['python3','/run/task/check.py'],{})
    assert any(arg.endswith('dst=/workspace,readonly') for arg in calls[0])
    assert calls[0][calls[0].index('--network')+1]=='none'

def test_partial_cleanup_retry_discovers_resources_after_job_removed(runtime, monkeypatch):
    job='a'*64;proxy='b'*64;network1='c'*64;network2='d'*64
    containers={job:{'Running':False,'Status':'exited','ExitCode':0,'role':'job'},proxy:{'Running':True,'Status':'running','ExitCode':None,'role':'egress'}}
    networks={network1,network2};calls=[];failed_once=False
    def run(args,**kwargs):
        nonlocal failed_once
        calls.append(args)
        if args[:2]==['container','ls']:
            assert 'label=io.cloudworkbench.managed=true' in args
            assert 'label=io.cloudworkbench.owner=primary' in args
            assert 'label=io.cloudworkbench.attempt=attempt' in args
            assert 'label=io.cloudworkbench.generation=3' in args
            role='egress' if 'label=io.cloudworkbench.role=egress' in args else 'job'
            return '\n'.join(key for key,value in containers.items() if value['role']==role and ('--all' in args or value['Running']))
        if args[0]=='inspect':
            return json.dumps({'io.cloudworkbench.role':containers[args[-1]]['role'],'io.cloudworkbench.attempt':'attempt','io.cloudworkbench.generation':'3'})
        if args[0]=='stop':
            containers[args[-1]].update(Running=False,Status='exited');return ''
        if args[0]=='rm':
            del containers[args[-1]];return ''
        if args[:2]==['network','ls']:
            assert '--no-trunc' in args and 'label=io.cloudworkbench.generation=3' in args
            return '\n'.join(sorted(networks))
        if args[:2]==['network','rm']:
            if args[-1]==network2 and not failed_once:
                failed_once=True;raise RuntimeError('synthetic rm failure')
            networks.remove(args[-1]);return ''
        raise AssertionError(args)
    monkeypatch.setattr(runtime,'_run',run)
    monkeypatch.setattr(runtime,'_owned',lambda rid,expected_generation=None:containers.get(rid))
    with pytest.raises(RuntimeError,match='synthetic'):
        runtime.cleanup(job,expected_generation=3)
    assert containers=={} and networks=={network2}
    runtime.cleanup_attempt('attempt',expected_generation=3)
    assert networks==set()
    runtime.cleanup_attempt('attempt',expected_generation=3)
    assert not any('prune' in arg or 'cloudd=1' in arg for cmd in calls for arg in cmd)


def test_attempt_cleanup_refuses_any_live_job_before_removing_any(runtime,monkeypatch):
    first='a'*64;second='b'*64;cleaned=[]
    monkeypatch.setattr(runtime,'_run',lambda *a,**k:first+'\n'+second)
    monkeypatch.setattr(runtime,'status',lambda rid,*a:{'state':'running' if rid==second else 'created'})
    monkeypatch.setattr(runtime,'cleanup',lambda *a,**k:cleaned.append(a))
    with pytest.raises(RuntimeError,match='running'):
        runtime.cleanup_attempt('attempt',expected_generation=1)
    assert cleaned==[]


def test_attempt_cleanup_removes_created_and_then_infrastructure(runtime,monkeypatch):
    calls=[]
    monkeypatch.setattr(runtime,'_run',lambda *a,**k:RID)
    monkeypatch.setattr(runtime,'status',lambda *a,**k:{'state':'created'})
    monkeypatch.setattr(runtime,'cleanup',lambda *a,**k:calls.append(('job',a,k)))
    monkeypatch.setattr(runtime,'cleanup_infrastructure',lambda *a,**k:calls.append(('infra',a,k)))
    runtime.cleanup_attempt('attempt',expected_generation=5)
    assert calls==[('job',(RID,),{'expected_generation':5}),('infra',('attempt',),{'expected_generation':5})]


@pytest.mark.parametrize('generation',[0,-1,True,'1',None])
def test_cleanup_rejects_invalid_generation_without_docker(runtime,monkeypatch,generation):
    monkeypatch.setattr(runtime,'_run',lambda *a,**k:pytest.fail('Docker must not be called'))
    with pytest.raises(RuntimeError,match='generation'):
        runtime.cleanup_infrastructure('attempt',expected_generation=generation)


def test_partial_egress_creation_failure_preserves_cleanup_uncertainty(runtime,monkeypatch):
    def fail_create(*a,**k):raise RuntimeError('synthetic setup failure')
    def fail_cleanup(*a,**k):raise RuntimeError('synthetic cleanup failure')
    monkeypatch.setattr(runtime,'_create_network',fail_create)
    monkeypatch.setattr(runtime,'cleanup_attempt',fail_cleanup)
    with pytest.raises(RuntimeError,match='cleanup incomplete'):
        runtime._network('attempt','session',1)


def test_proxy_stop_not_effective_preserves_networks(runtime,monkeypatch):
    calls=[]
    def run(args,**kwargs):
        calls.append(args)
        return RID if 'label=io.cloudworkbench.role=egress' in args else ''
    monkeypatch.setattr(runtime,'_run',run)
    monkeypatch.setattr(runtime,'stop',lambda *a,**k:None)
    monkeypatch.setattr(runtime,'status',lambda *a,**k:{'state':'running'})
    with pytest.raises(RuntimeError,match='did not stop'):
        runtime.cleanup_infrastructure('attempt',expected_generation=1)
    assert not any(c[0]=='rm' or c[:2]==['network','rm'] for c in calls)

@pytest.mark.parametrize('args,stderr,code,operation',[
    (['create'],'Error response from daemon: No such image: sha256:abc','docker_image_missing','create'),
    (['create'],'Error response from daemon: invalid mount config for type bind: bind source path does not exist','docker_mount_invalid','create'),
    (['create'],'Conflict: The container name is already in use by container abc','docker_name_conflict','create'),
    (['network','create'],'network with name test already exists','docker_name_conflict','network_create'),
    (['container','ls'],'Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?','docker_daemon_unavailable','container_list'),
    (['start'],'OCI runtime create failed: process startup rejected','docker_start_failed','start'),
    (['network','rm'],'network has active endpoints','docker_command_failed','network_rm'),
])
def test_docker_errors_expose_only_safe_structured_categories(runtime,monkeypatch,args,stderr,code,operation):
    secret='SENSITIVE-DO-NOT-EMIT'
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:subprocess.CompletedProcess(a,125,stdout='',stderr=stderr+' token='+secret))
    with pytest.raises(RuntimeError) as caught:runtime._run(args)
    assert caught.value.code==code and caught.value.operation==operation
    assert secret not in str(caught.value) and secret not in repr(caught.value.__dict__)
    assert stderr not in str(caught.value)


def test_timeout_has_safe_code_without_command_or_stderr(runtime,monkeypatch):
    def expired(*a,**k):raise subprocess.TimeoutExpired(['docker','secret-command'],30,stderr='private token')
    monkeypatch.setattr(subprocess,'run',expired)
    with pytest.raises(RuntimeError) as caught:runtime._run(['start',RID])
    assert caught.value.code=='docker_timeout' and caught.value.operation=='start'
    assert 'private token' not in str(caught.value) and 'secret-command' not in str(caught.value)
    assert caught.value.__suppress_context__


def test_missing_docker_executable_has_diagnostic_code(runtime,monkeypatch):
    def missing(*a,**k):raise FileNotFoundError('sensitive path')
    monkeypatch.setattr(subprocess,'run',missing)
    with pytest.raises(RuntimeError) as caught:runtime._run(['inspect',RID])
    assert caught.value.code=='docker_unavailable' and caught.value.operation=='inspect'
    assert 'sensitive path' not in str(caught.value)


def test_error_codes_and_operations_cannot_contain_arbitrary_values():
    error=RuntimeError('safe generic message',code='secret-value',operation='secret-value')
    assert error.code=='runtime_error' and error.operation is None
