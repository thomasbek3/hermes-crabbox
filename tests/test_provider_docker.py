from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import time

import pytest

from cloudworkbench.provider_docker import DockerConfig, ProviderDocker, DockerError, BoundedDocker, NoDockerRPC
from cloudworkbench.provider_dispatch import ProviderDispatch, DispatchError, Resources
from cloudworkbench.provider_leases import ProviderLeases, LeaseError
from cloudworkbench.store import Store


class FakeDocker:
    def __init__(self):self.image_labels={'org.opencontainers.image.version':'test'};self.commands=[];self.objects={};self.hook=None;self.response=b'{"status":"ok"}\n'
    def __call__(self,args,*,deadline,limit):
        self.commands.append(args)
        if self.hook:self.hook(args)
        def option(key,default=None):return args[args.index(key)+1] if key in args else default
        def options(key):return [args[i+1] for i,value in enumerate(args) if value==key]
        def labels():return dict(value.split('=',1) for value in options('--label'))
        if args[0]=='version':return json.dumps({'Client':{'Version':'29.7.2'},'Server':{'Version':'29.7.2'}}).encode()
        if args[:2]==['image','inspect']:return json.dumps([{'Id':'sha256:'+'a'*64,'Config':{'Labels':self.image_labels}}]).encode()
        if args[:2]==['network','create']:
            name=args[-1];identity=hashlib.sha256(name.encode()).hexdigest()
            self.objects[identity]={'Id':identity,'Name':name,'Labels':labels(),'Driver':'bridge','Internal':'--internal' in args,
                'Options':dict(v.split('=',1) for v in options('--opt')),'Containers':{},'IPAM':{'Config':[{'Subnet':'172.25.0.0/24'}]}}
            return identity.encode()
        if args[0]=='create':
            name=option('--name');identity=hashlib.sha256(name.encode()).hexdigest()
            memory=int(option('--memory')[:-1])*1024*1024
            mounts=[]
            for value in options('--mount'):
                values=dict(part.split('=',1) for part in value.split(',') if '=' in part)
                mounts.append({'Type':'bind','Source':values['src'],'Destination':values['dst'],'RW':False})
            self.objects[identity]={'Id':identity,'Name':'/'+name,'Image':'sha256:'+'a'*64,'Config':{'Labels':{**self.image_labels,**labels()},'User':option('--user'),'Entrypoint':[option('--entrypoint')],'Cmd':args[args.index('sha256:'+'a'*64)+1:]},
                'State':{'Status':'created','ExitCode':0},'NetworkSettings':{'Networks':{option('--network'):{'NetworkID':option('--network')}}},'Mounts':mounts,'HostConfig':{'ReadonlyRootfs':True,'Privileged':False,'CapAdd':None,
                'CapDrop':options('--cap-drop'),'SecurityOpt':options('--security-opt'),'NetworkMode':option('--network'),'PortBindings':{},'IpcMode':'private',
                'Tmpfs':dict(v.split(':',1) for v in options('--tmpfs')),'LogConfig':{'Type':option('--log-driver'),'Config':dict(v.split('=',1) for v in options('--log-opt'))},'Dns':options('--dns'),'RestartPolicy':{'Name':'no','MaximumRetryCount':0},'Sysctls':dict(v.split('=',1) for v in options('--sysctl')),'Memory':memory,'MemorySwap':memory,'PidsLimit':int(option('--pids-limit')),'NanoCpus':round(float(option('--cpus'))*1e9)}}
            return identity.encode()
        if args[:2]==['network','connect']:
            self.objects[args[-1]]['NetworkSettings']['Networks'][args[-2]]={'NetworkID':args[-2]};return b''
        if args[0]=='inspect' or args[:2]==['network','inspect']:
            if args[-1] not in self.objects:raise DockerError('docker_command_failed')
            return json.dumps([self.objects[args[-1]]]).encode()
        if args[0]=='start':
            obj=self.objects[args[-1]];obj['State']['Status']='exited' if obj['Name'].endswith('-provider') else 'running';return args[-1].encode()
        if args[0]=='exec':return b''
        if args[0]=='logs':
            if len(self.response)>limit:raise DockerError('docker_output_limit')
            return self.response
        if args[0]=='stop':self.objects[args[-1]]['State']['Status']='exited';return args[-1].encode()
        if args[0]=='rm' or args[:2]==['network','rm']:del self.objects[args[-1]];return args[-1].encode()
        raise AssertionError(args)


@pytest.fixture
def setup(tmp_path,monkeypatch):
    profile=tmp_path/'profile.json';profile.write_bytes(b'{}');credential=tmp_path/'synthetic-token';credential.write_bytes(b'SYNTHETIC-NOT-A-CREDENTIAL')
    root=tmp_path/'staging';root.mkdir(mode=0o700)
    config=DockerConfig('sha256:'+'a'*64,profile,hashlib.sha256(b'{}').hexdigest(),'b'*64,credential,root,('example.com',))
    monkeypatch.setattr(ProviderDocker,'_check_files',lambda _:None)
    monkeypatch.setattr(os,'fchown',lambda *args:None)
    fake=FakeDocker();runtime=ProviderDocker(config,cancel_check=lambda _:False,command=fake)
    store=Store(tmp_path/'state.db');principal=store.add_client('test','t'*40,['submit','cancel'],['project'])
    attempt=store.create_session(principal,{'project_id':'project','agent':'hermes','goal':'synthetic'},'task');store.claim_next()
    leases=ProviderLeases(store,cleanup_verifier=runtime.cleanup,inspector_id=config.inspector_id)
    leases.register_account('account',legacy_agent='claude',persistent_owner_id='owner');owner=leases.reserve('account',persistent_owner_id='owner')
    grant=leases.issue_grant(owner,attempt_id=attempt['attempt_id'],generation=1)
    d=ProviderDispatch(leases,runtime);payload=b'{"synthetic":true}'
    spec=d.admit(owner,grant,request_nonce=secrets.token_hex(32),payload=payload,profile_digest=config.profile_digest)
    return runtime,fake,leases,owner,d,spec,payload,store,principal,attempt


def test_create_is_stopped_four_ids_and_hardened_argv(setup):
    runtime,fake,leases,owner,d,spec,payload,*_=setup
    resources=runtime.create(spec,payload,deadline=time.monotonic()+10)
    assert len(set(vars(resources).values()))==4
    assert not any(args[0]=='start' for args in fake.commands)
    commands=[args for args in fake.commands if args[0]=='create']
    provider=next(args for args in commands if spec.provider_name in args)
    gateway=next(args for args in commands if spec.gateway_name in args)
    assert provider[provider.index('--user')+1]=='958:959'
    assert '--privileged' not in provider and '--publish' not in provider
    assert payload.decode() not in ' '.join(provider)
    assert '/run/secrets/claude-token' in ' '.join(provider)
    assert 'claude-token' not in ' '.join(gateway)
    assert '--ip' in gateway and '--proxy' in provider
    assert runtime._load(spec)['create_complete']


def test_dispatch_lifecycle_cleanup_then_empty_owner_release(setup):
    runtime,fake,leases,owner,d,spec,payload,*_=setup
    d.launch(spec.lease.request_id,payload);d.collect(spec.lease.request_id)
    with pytest.raises(DispatchError,match='response_not_cleaned'):d.deliver(spec.lease.request_id)
    d.cleanup(spec.lease.request_id)
    assert not fake.objects and not (runtime._folder(spec)/'request.json').exists()
    assert d.deliver(spec.lease.request_id)==fake.response
    leases.cleanup_owner(owner);assert leases.current('account') is None


def test_admitted_never_created_cleanup_is_safe_ledger_scope(setup):
    runtime,fake,leases,owner,d,spec,payload,*_=setup
    d.cleanup(spec.lease.request_id);leases.cleanup_owner(owner)
    assert not fake.commands and leases.current('account') is None


def test_held_request_cannot_bypass_cleanup_as_empty_owner(setup):
    runtime,fake,leases,owner,d,spec,payload,*_=setup
    fake.hook=lambda args:(_ for _ in ()).throw(TimeoutError()) if args[:2]==['network','create'] else None
    with pytest.raises(DispatchError,match='create_outcome_unknown'):d.launch(spec.lease.request_id,payload)
    with pytest.raises(LeaseError,match='dispatch_outcome_unknown'):leases.cleanup_owner(owner)
    assert leases.current('account') is not None


def test_unknown_rpc_retains_pending_even_if_objects_disappear(setup):
    runtime,fake,leases,owner,d,spec,payload,*_=setup
    fake.hook=lambda args:(_ for _ in ()).throw(TimeoutError()) if args[0]=='start' else None
    with pytest.raises(DispatchError,match='start_outcome_unknown'):d.launch(spec.lease.request_id,payload)
    assert runtime._load(spec)['pending'] is not None
    fake.objects.clear()
    with pytest.raises(DispatchError,match='operation_still_unknown'):d.reconcile(spec.lease.request_id)
    assert leases.active_request(owner) is not None


def test_acknowledged_start_timeout_can_reconcile_without_replay(setup):
    runtime,fake,leases,owner,d,spec,payload,*_=setup
    original=runtime.start
    def after_ack(*args,**kwargs):original(*args,**kwargs);raise TimeoutError()
    runtime.start=after_ack
    with pytest.raises(DispatchError,match='start_outcome_unknown'):d.launch(spec.lease.request_id,payload)
    starts=sum(args[0]=='start' for args in fake.commands)
    d.reconcile(spec.lease.request_id);d.cleanup(spec.lease.request_id)
    assert sum(args[0]=='start' for args in fake.commands)==starts
    assert not fake.objects


@pytest.mark.parametrize('field,value',[('Image','sha256:'+'f'*64),('Name','/unowned')])
def test_inspect_refuses_changed_container_identity(setup,field,value):
    runtime,fake,leases,owner,d,spec,payload,*_=setup
    resources=runtime.create(spec,payload,deadline=time.monotonic()+10)
    fake.objects[resources.provider_id][field]=value
    with pytest.raises(DockerError,match='container_policy_mismatch'):runtime.inspect(spec,resources,deadline=time.monotonic()+5)


def test_network_other_peer_refused(setup):
    runtime,fake,leases,owner,d,spec,payload,*_=setup
    resources=runtime.create(spec,payload,deadline=time.monotonic()+10)
    fake.objects[resources.internal_network_id]['Containers']['e'*64]={}
    with pytest.raises(DockerError,match='network_policy_mismatch'):runtime.inspect(spec,resources,deadline=time.monotonic()+5)


def test_profile_payload_mismatch_never_creates(setup):
    runtime,fake,leases,owner,d,spec,payload,*_=setup
    with pytest.raises(DockerError,match='profile_binding_mismatch'):runtime.create(replace(spec,profile_digest='c'*64),payload,deadline=time.monotonic()+5)
    with pytest.raises(DockerError,match='invalid_payload_binding'):runtime.create(spec,b'wrong',deadline=time.monotonic()+5)
    assert not any(args[0]=='create' or args[:2]==['network','create'] for args in fake.commands)


def test_bounded_command_argv_env_and_no_stderr_leak(tmp_path):
    binary=tmp_path/'fake-docker';binary.write_text('#!'+sys.executable+'\nimport os,sys,json\nsys.stderr.write("SYNTHETIC-PRIVATE-STDERR")\nprint(json.dumps({"argv":sys.argv[1:],"env":dict(os.environ)}))\n');binary.chmod(0o700)
    result=json.loads(BoundedDocker(str(binary),tmp_path/'docker-config')(['literal;$(touch nope)'],deadline=time.monotonic()+3))
    assert result['argv']==['--host','unix:///var/run/docker.sock','--config',str(tmp_path/'docker-config'),'literal;$(touch nope)']
    assert 'HOME' not in result['env'] and 'DOCKER_HOST' not in result['env']
    assert 'SYNTHETIC-PRIVATE' not in json.dumps(result)


@pytest.mark.parametrize('script,code',[('import time;time.sleep(20)','docker_timeout'),('print("x"*10000)','docker_output_limit')])
def test_bounded_command_timeout_and_output_cap(tmp_path,script,code):
    binary=tmp_path/'fake-docker';binary.write_text('#!'+sys.executable+'\n'+script+'\n');binary.chmod(0o700)
    with pytest.raises(DockerError,match=code):BoundedDocker(str(binary),tmp_path/'docker-config')([],deadline=time.monotonic()+1,limit=100)


@pytest.mark.parametrize('field,value',[('image','latest'),('uid',0),('gid',0),('allowed_domains',('127.0.0.1',)),('allowed_domains',('example.com','example.com'))])
def test_invalid_immutable_config(setup,field,value):
    runtime,*_=setup
    with pytest.raises(DockerError):replace(runtime.config,**{field:value})


def test_extra_provider_network_and_forwarding_are_refused(setup):
    runtime,fake,leases,owner,d,spec,payload,*_=setup
    resources=runtime.create(spec,payload,deadline=time.monotonic()+10)
    obj=fake.objects[resources.provider_id]
    obj['NetworkSettings']['Networks']['external-escape']={'NetworkID':'c'*64}
    with pytest.raises(DockerError,match='container_network_mismatch'):runtime.inspect(spec,resources,deadline=time.monotonic()+5)
    del obj['NetworkSettings']['Networks']['external-escape']
    obj['HostConfig']['Sysctls']['net.ipv4.ip_forward']='1'
    with pytest.raises(DockerError,match='container_forwarding_mismatch'):runtime.inspect(spec,resources,deadline=time.monotonic()+5)


def test_partial_acknowledged_create_can_be_physically_cleaned_on_reconcile(setup):
    runtime,fake,leases,owner,d,spec,payload,*_=setup
    original=runtime._mutation
    def interrupted(*args,**kwargs):
        result=original(*args,**kwargs)
        if kwargs.get('key')=='internal_network_id':raise RuntimeError('synthetic worker death after ack')
        return result
    runtime._mutation=interrupted
    with pytest.raises(DispatchError,match='create_outcome_unknown'):d.launch(spec.lease.request_id,payload)
    runtime._mutation=original
    d.reconcile(spec.lease.request_id);d.cleanup(spec.lease.request_id)
    assert not fake.objects and leases.active_request(owner) is None


def test_cancel_after_gateway_start_prevents_provider_start_and_reconciles(setup):
    runtime,fake,leases,owner,d,spec,payload,*_=setup
    runtime.cancel_check=lambda _:any(args[0]=='start' for args in fake.commands)
    with pytest.raises(DispatchError,match='start_outcome_unknown'):d.launch(spec.lease.request_id,payload)
    starts=[args for args in fake.commands if args[0]=='start']
    assert len(starts)==1
    d.reconcile(spec.lease.request_id);d.cleanup(spec.lease.request_id)
    assert not fake.objects and leases.active_request(owner) is None


def test_profile_credential_validation_is_metadata_only_for_secret(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import stat
    from cloudworkbench.inference_transport import PinnedCLI
    from cloudworkbench.profile_binding import canonical_pinned_profile_digest
    profile=tmp_path/'profile.json';value={'path':'/usr/bin/claude','sha256':'a'*64,'version':'2.1.274','native_model':'claude-fable-5-1','effort':'high'}
    profile.write_text(json.dumps(value,indent=2));token=tmp_path/'synthetic';token.write_bytes(b'synthetic');root=tmp_path/'private';root.mkdir(mode=0o700)
    digest=canonical_pinned_profile_digest(PinnedCLI(**{**value,'path':Path(value['path'])}))
    config=DockerConfig('sha256:'+'a'*64,profile,hashlib.sha256(profile.read_bytes()).hexdigest(),digest,token,root,('example.com',))
    original=Path.lstat;read=Path.read_bytes
    def metadata(path):
        if path in (profile,token):return SimpleNamespace(st_mode=stat.S_IFREG|(0o444 if path==profile else 0o640),st_uid=0,st_gid=959,st_nlink=1,st_size=path.stat().st_size)
        return original(path)
    def guarded_read(path):
        assert path!=token,'controller must not open credential bytes'
        return read(path)
    monkeypatch.setattr(Path,'lstat',metadata);monkeypatch.setattr(Path,'read_bytes',guarded_read)
    ProviderDocker(config,cancel_check=lambda _:False,command=FakeDocker())
    with pytest.raises(DockerError,match='profile_binding_mismatch'):
        ProviderDocker(replace(config,profile_digest='f'*64),cancel_check=lambda _:False,command=FakeDocker())


def test_image_cannot_inherit_provider_ownership_namespace(setup):
    runtime,fake,leases,owner,d,spec,payload,*_=setup
    fake.image_labels['io.cloudworkbench.provider-request']='other'
    with pytest.raises(DockerError,match='unsafe_image_labels'):runtime.create(spec,payload,deadline=time.monotonic()+5)
    assert not any(args[0]=='create' or args[:2]==['network','create'] for args in fake.commands)


def test_image_preflight_rejection_reconciles_without_unknown_mutation(setup):
    runtime,fake,leases,owner,d,spec,payload,*_=setup
    fake.image_labels['cloudd']='1'
    with pytest.raises(DispatchError,match='create_outcome_unknown'):d.launch(spec.lease.request_id,payload)
    assert runtime._load(spec)['pending'] is None
    d.reconcile(spec.lease.request_id);d.cleanup(spec.lease.request_id);leases.cleanup_owner(owner)
    assert not fake.objects and leases.current('account') is None


def test_stale_safe_journal_temporary_recovers_after_restart(setup):
    runtime,fake,leases,owner,d,spec,payload,*_=setup
    resources=runtime.create(spec,payload,deadline=time.monotonic()+10)
    temporary=runtime._folder(spec)/'journal.next';temporary.write_bytes(b'partial');temporary.chmod(0o600)
    restarted=ProviderDocker(runtime.config,cancel_check=lambda _:False,command=fake)
    restarted.start(spec,resources,deadline=time.monotonic()+10)
    assert not temporary.exists() and restarted._load(spec)['start_complete']


@pytest.mark.parametrize('kind',['symlink','hardlink','public'])
def test_unsafe_stale_journal_temporary_is_not_removed(setup,kind,tmp_path):
    runtime,fake,leases,owner,d,spec,payload,*_=setup
    runtime.create(spec,payload,deadline=time.monotonic()+10)
    temporary=runtime._folder(spec)/'journal.next';external=tmp_path/'external';external.write_bytes(b'preserve');external.chmod(0o600)
    if kind=='symlink':temporary.symlink_to(external)
    elif kind=='hardlink':os.link(external,temporary)
    else:temporary.write_bytes(b'public');temporary.chmod(0o644)
    with pytest.raises(DockerError,match='unsafe_journal_temporary'):runtime._save(spec,runtime._load(spec))
    assert temporary.exists() and external.read_bytes()==b'preserve'


def test_no_rpc_failure_clears_pending_but_arbitrary_failure_does_not(setup):
    runtime,fake,leases,owner,d,spec,payload,*_=setup
    fake.hook=lambda args:(_ for _ in ()).throw(NoDockerRPC('docker_unavailable')) if args[:2]==['network','create'] else None
    with pytest.raises(DispatchError,match='create_outcome_unknown'):d.launch(spec.lease.request_id,payload)
    assert runtime._load(spec)['pending'] is None
    d.reconcile(spec.lease.request_id);d.cleanup(spec.lease.request_id)
    assert not fake.objects


@pytest.mark.parametrize('mode',['expired','notfound'])
def test_bounded_runner_proves_not_spawned(tmp_path,mode):
    command=BoundedDocker(str(tmp_path/'missing'),tmp_path/'private')
    with pytest.raises(NoDockerRPC):command([],deadline=time.monotonic()+(-1 if mode=='expired' else 3))


@pytest.mark.parametrize('inspect_timeout',[False,True])
def test_execution_deadline_stops_provider_before_return(setup,inspect_timeout):
    runtime,fake,leases,owner,d,spec,payload,*_=setup
    resources=runtime.create(spec,payload,deadline=time.monotonic()+10)
    original=fake.__call__;deadline=time.monotonic()+.2
    def command(args,**kwargs):
        value=original(args,**kwargs)
        if args==['start',resources.provider_id]:fake.objects[resources.provider_id]['State']['Status']='running'
        if inspect_timeout and args==['inspect',resources.provider_id] and runtime._load(spec)['start_complete']:
            time.sleep(max(0,deadline-time.monotonic())+.01);raise DockerError('docker_timeout')
        return value
    runtime.command=command
    with pytest.raises(DockerError,match='provider_execution_timeout'):runtime.start(spec,resources,deadline=deadline)
    assert fake.objects[resources.provider_id]['State']['Status']=='exited'
    assert runtime._load(spec)['pending'] is None


@pytest.mark.parametrize('version',['27.5.1','28.0.0-rc.1','unknown',None])
def test_unsupported_engine_preflight_can_be_reconciled(setup,version):
    runtime,fake,leases,owner,d,spec,payload,*_=setup
    original=fake.__call__
    runtime.command=lambda args,**kwargs:json.dumps({'Client':{'Version':'29.7.2'},'Server':{'Version':version}}).encode() if args[0]=='version' else original(args,**kwargs)
    with pytest.raises(DispatchError,match='create_outcome_unknown'):d.launch(spec.lease.request_id,payload)
    d.reconcile(spec.lease.request_id);d.cleanup(spec.lease.request_id)
    assert not fake.objects


@pytest.mark.parametrize('field,value,code',[
    ('Tmpfs',{'/tmp':'rw,size=256m'},'container_tmpfs_mismatch'),
    ('LogConfig',{'Type':'json-file','Config':{}},'container_logging_mismatch'),
    ('Dns',['1.1.1.1'],'container_dns_mismatch'),
    ('RestartPolicy',{'Name':'always','MaximumRetryCount':0},'container_restart_mismatch'),
])
def test_host_policy_drift_refused(setup,field,value,code):
    runtime,fake,leases,owner,d,spec,payload,*_=setup
    resources=runtime.create(spec,payload,deadline=time.monotonic()+10)
    fake.objects[resources.provider_id]['HostConfig'][field]=value
    with pytest.raises(DockerError,match=code):runtime.inspect(spec,resources,deadline=time.monotonic()+5)


@pytest.mark.parametrize('change,code',[
 ('token_public','unsafe_credential_mount'),('token_hardlink','unsafe_credential_mount'),
 ('token_wrong_group','credential_unreadable_by_provider'),('token_too_large','unsafe_credential_mount'),
 ('profile_write','unsafe_profile'),('profile_private','profile_unreadable_by_provider'),
 ('profile_hardlink','unsafe_profile'),('profile_changed','profile_file_changed'),
 ('staging_shared','unsafe_staging_root'),('parent_writable','unsafe_mount_parent'),
])
def test_file_guard_negative_cases_are_metadata_only(tmp_path,monkeypatch,change,code):
    from types import SimpleNamespace
    import stat
    from cloudworkbench.inference_transport import PinnedCLI
    from cloudworkbench.profile_binding import canonical_pinned_profile_digest
    profile=tmp_path/'profile.json';value={'path':'/usr/bin/claude','sha256':'a'*64,'version':'2.1.274','native_model':'claude-fable-5-1','effort':'high'}
    profile.write_text(json.dumps(value));token=tmp_path/'synthetic';token.write_bytes(b'synthetic');root=tmp_path/'private';root.mkdir(mode=0o700)
    digest=canonical_pinned_profile_digest(PinnedCLI(**{**value,'path':Path(value['path'])}))
    config=DockerConfig('sha256:'+'a'*64,profile,hashlib.sha256(profile.read_bytes()).hexdigest(),digest,token,root,('example.com',))
    original=Path.lstat;read=Path.read_bytes
    def metadata(path):
        if path in (profile,token):
            mode=0o444 if path==profile else 0o640;nlink=1;group=959;size=path.stat().st_size
            if path==token:
                if change=='token_public':mode=0o644
                if change=='token_hardlink':nlink=2
                if change=='token_wrong_group':group=1
                if change=='token_too_large':size=4097
            else:
                if change=='profile_write':mode=0o664
                if change=='profile_private':mode=0o400
                if change=='profile_hardlink':nlink=2
            return SimpleNamespace(st_mode=stat.S_IFREG|mode,st_uid=0,st_gid=group,st_nlink=nlink,st_size=size)
        info=original(path)
        if (path==root and change=='staging_shared') or (path==tmp_path and change=='parent_writable'):
            return SimpleNamespace(st_mode=stat.S_IFDIR|0o770,st_uid=os.geteuid())
        return info
    def guarded_read(path):
        assert path!=token,'controller must never open token'
        return b'changed' if change=='profile_changed' and path==profile else read(path)
    monkeypatch.setattr(Path,'lstat',metadata);monkeypatch.setattr(Path,'read_bytes',guarded_read)
    with pytest.raises(DockerError,match=code):ProviderDocker(config,cancel_check=lambda _:False,command=FakeDocker())
