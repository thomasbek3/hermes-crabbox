import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

path=Path(__file__).resolve().parents[1]/'scripts/qualify-real-provider-guarded.py'
spec=importlib.util.spec_from_file_location('real_provider_qualification',path)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


def manifest():
    return {'version':1,'host':'omarchy','authorization':{'scope':'single_real_provider_canary','reference':'explicit-synthetic-test-approval',
        'expires_at':2000,'quiescence_window':True,'other_credential_consumers_fenced':True},
        'worker_config_sha256':'a'*64,'state_root':'/var/lib/cloud-workbench/worker','database':'/var/lib/cloud-workbench/control/state.db',
        'lock_device':32,'lock_inode':1234,'worker_uid':959,'worker_gid':960,'provider_image':'sha256:'+'a'*64,
        'profile_path':'/root/profile.json','profile_sha256':'b'*64,'model_identifier_evidence':'/root/model-proof.json',
        'model_identifier_evidence_sha256':'c'*64,'billing_evidence_path':'/root/billing-proof.json','billing_evidence_sha256':'e'*64,'approved_domains':['api.anthropic.com'],
        'controller_source_sha256':{name:'a'*64 for name in module.MODULES},'harness_sha256':'d'*64}


@pytest.mark.parametrize('change',['expired','no_window','other_consumers','scope','tag','profile_relative','domain','missing_source','source_changed','path','extra','short_window'])
def test_unapproved_or_unbound_manifest_refused(change):
    value=manifest()
    if change=='expired':value['authorization']['expires_at']=900
    elif change=='short_window':value['authorization']['expires_at']=1001
    elif change=='no_window':value['authorization']['quiescence_window']=False
    elif change=='other_consumers':value['authorization']['other_credential_consumers_fenced']=False
    elif change=='scope':value['authorization']['scope']='synthetic_only'
    elif change=='tag':value['provider_image']='latest'
    elif change=='profile_relative':value['profile_path']='profile.json'
    elif change=='domain':value['approved_domains']=['example.com']
    elif change=='missing_source':value['controller_source_sha256'].pop('provider_docker.py')
    elif change=='source_changed':value['controller_source_sha256']['provider_docker.py']='unverified'
    elif change=='path':value['state_root']='/tmp/unrelated'
    else:value['extra']=True
    with pytest.raises(module.QualificationRefused):module.validate_manifest(value,now=1000)


def test_valid_manifest_requires_explicit_bounded_authorization():
    assert module.validate_manifest(manifest(),now=1000)['authorization']['scope']=='single_real_provider_canary'


def test_token_is_only_canonical_and_never_in_manifest():
    assert str(module.CANONICAL_TOKEN)=='/var/lib/cloud-workbench/auth/claude-token'
    assert 'token' not in manifest()


@pytest.mark.parametrize('field',['native_model','profile_digest','provider_image','identifier_verified'])
def test_exact_model_evidence_must_match_profile(monkeypatch,field):
    from cloudworkbench.inference_transport import PinnedCLI
    from cloudworkbench.profile_binding import canonical_pinned_profile_digest
    value=manifest();profile={'path':'/usr/bin/claude','sha256':'a'*64,'version':'2.1.274','native_model':'claude-fable-5-1','effort':'max'}
    evidence={'native_model':profile['native_model'],'profile_digest':canonical_pinned_profile_digest(PinnedCLI(**{**profile,'path':Path(profile['path'])})),
        'provider_image':value['provider_image'],'source_reference':'SYNTHETIC-SOURCE-ONLY','reviewed_at':'synthetic','identifier_verified':True}
    evidence[field]=False if field=='identifier_verified' else 'wrong'
    monkeypatch.setattr(module,'trusted_json',lambda p:(profile,value['profile_sha256']) if str(p)==value['profile_path'] else (evidence,value['model_identifier_evidence_sha256']))
    with pytest.raises(module.QualificationRefused,match='exact_model_identifier_unverified'):module.load_profile(value)


class Guard:
    instances=[]
    def __init__(self,*args,**kwargs):self.fd=None;self.started=False;self.settled=False;self.closed=False;self.__class__.instances.append(self)
    def acquire(self):self.fd=1;return self
    def close(self):self.closed=True;self.fd=None


def prepared_main(monkeypatch):
    value=manifest();Guard.instances=[]
    monkeypatch.setattr(module.socket,'gethostname',lambda:'omarchy');monkeypatch.setattr(module.os,'geteuid',lambda:0)
    monkeypatch.setattr(module.time,'time',lambda:1000)
    monkeypatch.setattr(module,'trusted_json',lambda p:({'claude_token':str(module.CANONICAL_TOKEN)},'hash') if p==module.WORKER_CONFIG else ({'credential_identity':{}},'hash') if p==value['billing_evidence_path'] else (value,'hash'))
    monkeypatch.setattr(module,'validate_sources',lambda _:None);monkeypatch.setattr(module,'load_profile',lambda _:object())
    monkeypatch.setattr(module,'LegacyRunnerFence',Guard)
    monkeypatch.setattr(module,'service_snapshot',lambda:{'LoadState':'loaded','ActiveState':'inactive','SubState':'dead','MainPID':'0','UnitFileState':'enabled','Restart':'always'})


def test_default_preflight_never_executes_provider(monkeypatch,capsys):
    prepared_main(monkeypatch)
    monkeypatch.setattr(module,'run_one',lambda *args:(_ for _ in ()).throw(AssertionError('no execution')))
    assert module.main(['--manifest','/root/approved.json'])==0
    receipt=json.loads(capsys.readouterr().out)
    assert receipt['preflight_only'] and receipt['executed'] is False
    assert Guard.instances[0].closed


def test_unknown_cleanup_parks_without_unlock_or_success(monkeypatch,tmp_path,capsys):
    prepared_main(monkeypatch)
    monkeypatch.setattr(module.tempfile,'mkdtemp',lambda **kwargs:str(tmp_path))
    def unknown(manifest,profile,guard,folder,receipt,**kwargs):guard.started=True;raise RuntimeError('private-error')
    monkeypatch.setattr(module,'run_one',unknown)
    class StopPark(BaseException):pass
    monkeypatch.setattr(module.time,'sleep',lambda seconds:(_ for _ in ()).throw(StopPark()))
    with pytest.raises(StopPark):module.main(['--manifest','/root/approved.json','--execute-approved'])
    receipt=json.loads(capsys.readouterr().out)
    assert receipt['lock_retained'] and not receipt['passed']
    assert Guard.instances[0].fd==1 and not Guard.instances[0].closed
    assert 'private-error' not in json.dumps(receipt)


def test_verified_settlement_allows_close(monkeypatch,tmp_path,capsys):
    prepared_main(monkeypatch);monkeypatch.setattr(module.tempfile,'mkdtemp',lambda **kwargs:str(tmp_path))
    def settled(manifest,profile,guard,folder,receipt,**kwargs):guard.started=True;guard.settled=True;receipt['passed']=True
    monkeypatch.setattr(module,'run_one',settled)
    assert module.main(['--manifest','/root/approved.json','--execute-approved'])==0
    assert Guard.instances[0].closed and json.loads(capsys.readouterr().out)['passed']


@pytest.mark.parametrize('field',['account_identity','native_model','billing_basis','entitlement_verified','verified_at'])
def test_account_specific_billing_proof_required(monkeypatch,field):
    from cloudworkbench.inference_transport import PinnedCLI
    from cloudworkbench.profile_binding import canonical_pinned_profile_digest
    value=manifest();profile={'path':'/usr/bin/claude','sha256':'a'*64,'version':'2.1.274','native_model':'claude-fable-5-1','effort':'max'}
    evidence={'native_model':profile['native_model'],'profile_digest':canonical_pinned_profile_digest(PinnedCLI(**{**profile,'path':Path(profile['path'])})),
        'provider_image':value['provider_image'],'source_reference':'SYNTHETIC-SOURCE-ONLY','reviewed_at':'synthetic','identifier_verified':True}
    billing={'account_identity':'dedicated-cloud-claude','native_model':profile['native_model'],'billing_basis':'existing_entitlement_no_incremental_spend',
        'source_reference':'SYNTHETIC-ACCOUNT-PROOF','verified_at':1000,'entitlement_verified':True,'credential_identity':{}}
    billing[field]=False if field=='entitlement_verified' else (1001 if field=='verified_at' else 'wrong')
    def trusted(path):
        if path==value['profile_path']:return profile,value['profile_sha256']
        if path==value['model_identifier_evidence']:return evidence,value['model_identifier_evidence_sha256']
        return billing,value['billing_evidence_sha256']
    monkeypatch.setattr(module,'trusted_json',trusted);monkeypatch.setattr(module.time,'time',lambda:1000)
    with pytest.raises(module.QualificationRefused,match='account_entitlement_without_incremental_spend_unverified'):module.load_profile(value)


@pytest.mark.parametrize('reported_model',[True,False])
def test_one_request_harness_composes_real_ledger_budget_supervision_with_fake_runtime(tmp_path,monkeypatch,reported_model):
    import time
    from dataclasses import asdict,replace
    from test_provider_dispatch import FakeRuntime
    from test_qualification_guard import fixture
    from test_hermes_inference_protocol import PROFILE,result
    import cloudworkbench.provider_docker as docker_module
    from cloudworkbench.qualification_guard import LegacyRunnerFence
    instances=[]
    class Runtime(FakeRuntime):
        def __init__(self,config,*,cancel_check):
            super().__init__();self.config=config;self.cancel_check=cancel_check;self.command=lambda args,**kwargs:b'';instances.append(self)
            value=result(decision={'kind':'final','text':'CWB_REAL_PROVIDER_QUALIFICATION_OK','tool_calls':[]},
                reported_identity={'model':PROFILE.native_model if reported_model else None,'effort':None,'status':'reported' if reported_model else 'unknown'})
            self.hooks['collect']=lambda *args:json.dumps({'version':1,'status':'ok','error':None,'result':asdict(value),
                'container_cleanup_required':True,'credential_reuse_authorized':False}).encode()
        def cleanup(self,target):return replace(super().cleanup(target),inspector_id=self.config.inspector_id,observed_at=time.time())
        def _run(self,args,deadline):
            self.command(args,deadline=deadline)
            return b'owned-objects' if self.objects else b''
    monkeypatch.setattr(docker_module,'ProviderDocker',Runtime)
    monkeypatch.setattr(module,'CANONICAL_TOKEN',module.CANONICAL_TOKEN.resolve())
    monkeypatch.setattr(module,'credential_metadata',lambda:{})
    args,path,database=fixture(tmp_path);folder=tmp_path/'qualification';folder.mkdir(mode=0o700)
    guard=LegacyRunnerFence(**args).acquire();receipt={}
    try:
        value=manifest();value['authorization']['expires_at']=time.time()+900
        module.run_one(value,PROFILE,guard,folder,receipt,credential_identity={},cancel_event=__import__('threading').Event())
        assert guard.started and guard.settled and receipt['outer_cleanup_confirmed']
        assert receipt['passed'] is reported_model and receipt['model_observed'] is reported_model
        assert len([e for e in instances[0].events if e[0]=='create'])==1
        assert not instances[0].objects and instances[0].config.credential_path==module.CANONICAL_TOKEN
    finally:guard.close()


@pytest.mark.parametrize('change',[{'native_model':'claude-opus-4-6'},{'effort':'high'}])
def test_fixed_fable_policy_cannot_be_silently_substituted(monkeypatch,change):
    value=manifest();profile={'path':'/usr/bin/claude','sha256':'a'*64,'version':'2.1.274','native_model':'claude-fable-5-1','effort':'max',**change}
    monkeypatch.setattr(module,'trusted_json',lambda p:(profile,value['profile_sha256']))
    with pytest.raises(module.QualificationRefused,match='fixed_fable_model_policy_mismatch'):module.load_profile(value)

@pytest.mark.parametrize('change',[{'LoadState':'not-found'},{'MainPID':'42'},{'ActiveState':'active'},{'SubState':'auto-restart'}])
def test_quiescence_requires_loaded_dead_zero_pid(monkeypatch,change):
    state={'LoadState':'loaded','ActiveState':'inactive','SubState':'dead','MainPID':'0','UnitFileState':'enabled','Restart':'always',**change}
    monkeypatch.setattr(module,'service_snapshot',lambda:state)
    assert not module.service_inactive()


def test_exception_after_settlement_keeps_safe_durable_receipt(monkeypatch,tmp_path):
    prepared_main(monkeypatch);monkeypatch.setattr(module.tempfile,'mkdtemp',lambda **kwargs:str(tmp_path))
    def failed(manifest,profile,guard,folder,receipt,**kwargs):
        guard.started=True;guard.settled=True;raise RuntimeError('SECRET-SENTINEL')
    monkeypatch.setattr(module,'run_one',failed)
    with pytest.raises(RuntimeError):module.main(['--manifest','/root/approved.json','--execute-approved'])
    receipt=json.loads((tmp_path/'receipt.json').read_text())
    assert receipt['error']=='qualification_aborted' and not receipt['passed']
    assert 'SECRET-SENTINEL' not in (tmp_path/'receipt.json').read_text() and Guard.instances[0].closed
