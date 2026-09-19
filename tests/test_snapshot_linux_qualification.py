import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

PATH=Path(__file__).resolve().parents[1]/'scripts/qualify-snapshot-docker-linux.py'
spec=importlib.util.spec_from_file_location('snapshot_linux_qualification',PATH)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


@pytest.fixture
def bundle(tmp_path,monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError('prepare must not execute a process')
    monkeypatch.setattr(module.subprocess,'run',forbidden)
    return module.prepare(PATH.parents[1],tmp_path/'prepared.json','sha256:'+'a'*64)


def test_prepare_is_offline_and_binds_exact_source_set(bundle):
    assert module.validate_bundle(bundle) is None
    assert bundle['base_image']=='sha256:'+'a'*64
    assert 'provider_auth_snapshot.py' in bundle['sources']
    assert 'snapshot_provider_docker.py' in bundle['sources']
    assert 'native-login-' not in json.dumps(bundle['sources'])


@pytest.mark.parametrize('change',['source','hash','harness','image','extra'])
def test_bundle_drift_refused(bundle,change):
    value=copy.deepcopy(bundle)
    if change=='source':value['sources']['provider_docker.py']+='\nchanged'
    elif change=='hash':value['hashes']['provider_docker.py']='b'*64
    elif change=='harness':value['harness']+='\nchanged'
    elif change=='image':value['base_image']='latest'
    else:value['sources']['unexpected.py']=''
    with pytest.raises(ValueError):module.validate_bundle(value)


def test_prepared_destination_never_overwritten(bundle,tmp_path):
    path=tmp_path/'existing';path.write_text('keep')
    with pytest.raises(FileExistsError):module.prepare(PATH.parents[1],path,bundle['base_image'])
    assert path.read_text()=='keep'


def test_source_closure_imports_in_isolated_local_package(tmp_path):
    package=tmp_path/'cloudworkbench';package.mkdir()
    for name in module.MODULES:
        (package/name).write_bytes((PATH.parents[1]/'src/cloudworkbench'/name).read_bytes())
    script="import sys;sys.path.insert(0,sys.argv[1]);from cloudworkbench.snapshot_provider_docker import SnapshotProviderDocker;from cloudworkbench.provider_dispatch import ProviderDispatch;from cloudworkbench.store import Store"
    result=subprocess.run([sys.executable,'-I','-c',script,str(tmp_path)],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr


def good_receipt(bundle):
    output={'fixture_only':True,'provider_calls':False,'uid':958,'gid':959,'source_uid':959,
            'source_gid':959,'mode':0o440,'sha256':'b'*64,'write_errno':30,'chmod_errno':30}
    return {'run_id':bundle['run_id'],'host':'omarchy','source_hashes':bundle['hashes'],
            'base_image':bundle['base_image'],'harness_sha256':bundle['harness_sha256'],
            'passed':True,'provider_calls':False,'real_credentials':False,
            'services_before':{unit:{'ActiveState':'active','MainPID':123+i} for i,unit in enumerate(module.SERVICES)},
            'services_after':{unit:{'ActiveState':'active','MainPID':123+i} for i,unit in enumerate(module.SERVICES)},
            'worker_diagnostic':{'phase':'complete','error':None},'worker_exit_code':0,'direct_source_errno':13,
            'base_alias':module.base_alias_name(bundle['run_id']),'base_alias_removed':True,'base_layers_verified':True,
            'build':{'exit_code':0,'error':None,'truncated':False},
            'cleanup':{'complete':True,'remaining':[]},'worker':{'uid':959,'gid':960,'groups':[959,960],
            'synthetic_source_sha256':'b'*64,'synthetic_results':[dict(output) for _ in range(4)],
            'checks':{k:True for k in ('normal_release_removes_snapshot','missing_auth_recovery',
                'newer_owner_preserved','source_original_unchanged','readonly_mounts','no_owned_resources')}}}


def test_complete_fixture_receipt_only_accepts_exact_bindings(bundle):
    assert module.validate_receipt(good_receipt(bundle),bundle)


@pytest.mark.parametrize('case',['uid','mode','source_readable','cleanup','newer_owner','provider','real_credentials','host','hash','result_count','services'])
def test_missing_proof_never_counts_as_pass(bundle,case):
    receipt=good_receipt(bundle)
    if case=='uid':receipt['worker']['synthetic_results'][0]['uid']=959
    elif case=='mode':receipt['worker']['synthetic_results'][0]['mode']=0o600
    elif case=='source_readable':receipt['direct_source_errno']=0
    elif case=='cleanup':receipt['cleanup']['remaining']=['still-owned']
    elif case=='newer_owner':receipt['worker']['checks']['newer_owner_preserved']=False
    elif case=='provider':receipt['provider_calls']=True
    elif case=='real_credentials':receipt['real_credentials']=True
    elif case=='host':receipt['host']='mac-mini'
    elif case=='hash':receipt['harness_sha256']='f'*64
    elif case=='result_count':receipt['worker']['synthetic_results'].pop()
    elif case=='services':receipt['services_after']=['inactive']*3
    with pytest.raises(ValueError):module.validate_receipt(receipt,bundle)


def test_cleanup_rejects_unrelated_object_and_requires_exact_scope():
    scope={'lease':{'reservation':{'reservation_id':'reservation','epoch':1},'request_id':'request'},
           'launch_nonce':'launch','provider_name':'provider','gateway_name':'gateway',
           'internal_network_name':'internal','external_network_name':'external'}
    labels={'io.cloudworkbench.provider-reservation':'reservation','io.cloudworkbench.provider-epoch':'1',
            'io.cloudworkbench.provider-request':'request','io.cloudworkbench.provider-launch':'launch'}
    obj={'Id':'a'*64,'Image':'sha256:'+'b'*64,'Name':'/provider','Config':{'Labels':labels}}
    assert module.owned_object(obj,scope,obj['Image'])=='a'*64
    for field,value in [('Name','/another'),('Id','short'),('Image','sha256:'+'c'*64)]:
        changed={**obj,field:value}
        with pytest.raises(ValueError):module.owned_object(changed,scope,obj['Image'])
    changed=copy.deepcopy(obj);changed['Config']['Labels']['io.cloudworkbench.provider-request']='newer'
    with pytest.raises(ValueError):module.owned_object(changed,scope,obj['Image'])


def test_cli_requires_explicit_mode_without_connecting():
    result=subprocess.run([sys.executable,str(PATH)],capture_output=True,text=True,timeout=5)
    assert result.returncode==2 and 'required' in result.stderr


def test_fallback_selects_request_within_shared_reservation():
    objects=[{'id':'a'*64,'reservation':'shared','request':'first'},
             {'id':'b'*64,'reservation':'shared','request':'second'},
             {'id':'c'*64,'reservation':'other','request':'first'}]
    calls=[]
    def docker(args):
        calls.append(args)
        filters=[args[i+1] for i,arg in enumerate(args) if arg=='--filter']
        assert len(filters)==2
        reservation=next(f.split('=',2)[-1] for f in filters if 'provider-reservation=' in f)
        request=next(f.split('=',2)[-1] for f in filters if 'provider-request=' in f)
        return '\n'.join(o['id'] for o in objects if o['reservation']==reservation and o['request']==request).encode()
    for request,expected in [('first','a'*64),('second','b'*64)]:
        scope={'lease':{'reservation':{'reservation_id':'shared'},'request_id':request}}
        assert module.scoped_ids(docker,scope)==[expected]
        assert module.scoped_ids(docker,scope,network=True)==[expected]
    assert len(calls)==4


def test_service_snapshot_records_exact_unit_names_and_pids():
    def run(args,**kwargs):
        assert args[:2]==['systemctl','show'] and '--property=MainPID' in args
        unit=args[2]
        return SimpleNamespace(returncode=0,stdout=f'Id={unit}\nActiveState=active\nMainPID=123\n')
    assert module.service_snapshot(run)=={unit:{'ActiveState':'active','MainPID':123} for unit in module.SERVICES}


@pytest.mark.parametrize('case',['zero','bool','missing','anonymous','inactive','restarted'])
def test_service_proof_rejects_missing_identity_or_changed_pid(bundle,case):
    receipt=good_receipt(bundle);unit=module.SERVICES[0]
    if case=='zero':receipt['services_before'][unit]['MainPID']=0;receipt['services_after'][unit]['MainPID']=0
    elif case=='bool':receipt['services_before'][unit]['MainPID']=True;receipt['services_after'][unit]['MainPID']=True
    elif case=='missing':receipt['services_before'].pop(unit);receipt['services_after'].pop(unit)
    elif case=='anonymous':receipt['services_before']=['active']*3;receipt['services_after']=['active']*3
    elif case=='inactive':receipt['services_before'][unit]['ActiveState']='inactive';receipt['services_after'][unit]['ActiveState']='inactive'
    else:receipt['services_after'][unit]['MainPID']+=1
    with pytest.raises(ValueError):module.validate_receipt(receipt,bundle)


def test_worker_diagnostics_are_fixed_and_do_not_include_exception_text(tmp_path):
    (tmp_path/'work').mkdir()
    failure=module.safe_worker_failure(PermissionError('SECRET-SENTINEL'))
    assert failure=='permission_denied'
    module.worker_mark(tmp_path,'normal_request',failure)
    raw=(tmp_path/'work/worker-diagnostic.json').read_text()
    assert json.loads(raw)=={'phase':'normal_request','error':'permission_denied'}
    assert 'SECRET-SENTINEL' not in raw and len(raw)<1024
    with pytest.raises(ValueError):module.worker_mark(tmp_path,'SECRET-SENTINEL')
    with pytest.raises(ValueError):module.worker_mark(tmp_path,'setup','SECRET-SENTINEL')


@pytest.mark.parametrize('field,value',[('worker_exit_code',1),('worker_diagnostic',{'phase':'normal_request','error':'lease_error'})])
def test_worker_failure_cannot_count_as_complete(bundle,field,value):
    receipt=good_receipt(bundle);receipt[field]=value
    with pytest.raises(ValueError):module.validate_receipt(receipt,bundle)


def test_alias_creation_requires_absence_and_cleanup_only_removes_proven_tag():
    image='sha256:'+'a'*64;alias=module.base_alias_name('snapshotqual-'+'b'*32);calls=[]
    present=True
    def docker(args):
        nonlocal present
        calls.append(args)
        if args[:2]==['image','ls']:return b'{"Repository":"owned"}' if present else b''
        if args[:2]==['image','inspect']:return json.dumps([{'Id':image,'RepoTags':[alias,'existing:keep']}]).encode()
        if args[:2]==['image','rm']:
            assert args==['image','rm',alias];present=False;return b''
        raise AssertionError(args)
    with pytest.raises(RuntimeError,match='base_alias_exists'):module.assert_alias_absent(docker,alias)
    module.cleanup_base_alias(docker,alias,image)
    assert calls[-1]==['image','inspect',image]
    assert not present
    module.cleanup_base_alias(docker,alias,image)
    assert len([c for c in calls if c[:2]==['image','rm']])==1


def test_changed_alias_is_never_removed():
    image='sha256:'+'a'*64;alias=module.base_alias_name('snapshotqual-'+'b'*32);calls=[]
    def docker(args):
        calls.append(args)
        if args[:2]==['image','ls']:return b'present'
        return json.dumps([{'Id':'sha256:'+'c'*64,'RepoTags':[alias]}]).encode()
    with pytest.raises(RuntimeError,match='base_alias_changed'):module.cleanup_base_alias(docker,alias,image)
    assert not any(c[:2]==['image','rm'] for c in calls)


@pytest.mark.parametrize('actual',[None,[],['changed'],['one']])
def test_base_layer_prefix_required(actual):
    with pytest.raises(RuntimeError,match='base_layers_changed'):
        module.verify_base_layers({'RootFS':{'Layers':['one','two']}},{'RootFS':{'Layers':actual}})
    module.verify_base_layers({'RootFS':{'Layers':['one','two']}},{'RootFS':{'Layers':['one','two','extra']}})


def fake_build(tmp_path, code):
    executable=tmp_path/'fake-docker'
    executable.write_text('#!'+sys.executable+'\n'+code)
    executable.chmod(0o700)
    folder=tmp_path/'run';folder.mkdir();build=folder/'build';build.mkdir()
    return folder,build,str(executable)


def test_synthetic_build_captures_bounded_diagnostic_and_private_environment(tmp_path,monkeypatch):
    monkeypatch.setenv('SECRET_AMBIENT','must-not-inherit')
    folder,build,exe=fake_build(tmp_path,"import os,sys,json\nassert 'SECRET_AMBIENT' not in os.environ\nassert os.environ['HOME'].endswith('/build-home')\nassert os.environ['DOCKER_CONFIG'].endswith('/build-cli')\nassert sys.argv[1:3]==['--host','unix:///var/run/docker.sock']\nassert '--pull=false' in sys.argv and '--progress' in sys.argv\nprint('fixture build stdout')\nprint('fixture build stderr',file=sys.stderr)\nsys.exit(17)\n")
    result=module.synthetic_build(folder,build,'synthetic:test','snapshotqual-'+'a'*32,executable=exe)
    assert result['exit_code']==17 and result['error']=='build_command_failed'
    assert result['stdout']=='fixture build stdout\n' and result['stderr']=='fixture build stderr\n'
    assert not result['truncated']


def test_synthetic_build_output_is_bounded(tmp_path):
    folder,build,exe=fake_build(tmp_path,"import os,time\nos.write(2,b'x'*70000)\ntime.sleep(10)\n")
    result=module.synthetic_build(folder,build,'synthetic:test','snapshotqual-'+'a'*32,executable=exe)
    assert result['error']=='build_output_limit' and result['truncated']
    assert len(result['stdout'].encode())+len(result['stderr'].encode())==65536
    assert result['exit_code']!=0


def test_synthetic_build_timeout_retains_diagnostic(tmp_path):
    folder,build,exe=fake_build(tmp_path,"import os,time\nos.write(2,b'build waiting')\ntime.sleep(10)\n")
    result=module.synthetic_build(folder,build,'synthetic:test','snapshotqual-'+'a'*32,executable=exe,timeout=1)
    assert result['error']=='build_timeout' and result['stderr']=='build waiting'
    assert result['exit_code']!=0


@pytest.mark.parametrize('field',['base_alias','base_alias_removed','base_layers_verified','build'])
def test_receipt_requires_successful_bound_build_and_alias_cleanup(bundle,field):
    receipt=good_receipt(bundle);receipt.pop(field)
    with pytest.raises(ValueError):module.validate_receipt(receipt,bundle)


@pytest.mark.parametrize('tags',[None,[],['<none>:<none>']])
def test_untagged_base_refused_before_temporary_alias(tags):
    with pytest.raises(RuntimeError,match='base_existing_tag_required'):
        module.require_existing_base_tag({'RepoTags':tags})
    module.require_existing_base_tag({'RepoTags':['existing:keep']})


def fixture_store_response(tmp_path, *, state='collected', raw=None, response_digest=None):
    import sqlite3
    response={'fixture_only':True,'uid':958,'gid':959,'source_uid':959,'source_gid':959,
              'mode':0o440,'sha256':'a'*64,'write_errno':30,'chmod_errno':30,'provider_calls':False}
    body=json.dumps(response).encode() if raw is None else raw
    db=sqlite3.connect(tmp_path/'synthetic.db');db.row_factory=sqlite3.Row
    db.execute('CREATE TABLE provider_dispatch(request_id TEXT,state TEXT,response BLOB,response_digest TEXT)')
    db.execute('INSERT INTO provider_dispatch VALUES(?,?,?,?)',('exact-request',state,body,response_digest or module.digest(body)))
    db.commit()
    return SimpleNamespace(_connect=lambda:db),response


def test_boolean_collect_uses_exact_durable_synthetic_response(tmp_path):
    store,response=fixture_store_response(tmp_path);calls=[]
    def collect(request):calls.append(request);return True
    dispatch=SimpleNamespace(collect=collect)
    with pytest.raises(TypeError):json.loads(dispatch.collect('exact-request'))  # historical root cause
    assert module.synthetic_collected_result(dispatch,store,'exact-request')==response
    assert calls==['exact-request','exact-request']
    with pytest.raises(RuntimeError,match='fixture_not_collected'):
        module.synthetic_collected_result(dispatch,store,'another-request')


@pytest.mark.parametrize('case',['hash','malformed','oversize','wrong_state','wrong_shape','refused'])
def test_synthetic_response_binding_and_shape_fail_closed(tmp_path,case):
    kwargs={}
    if case=='hash':kwargs['response_digest']='f'*64
    elif case=='malformed':kwargs['raw']=b'{'
    elif case=='oversize':kwargs['raw']=b'x'*4097
    elif case=='wrong_state':kwargs['state']='running'
    elif case=='wrong_shape':kwargs['raw']=b'{"fixture_only":true,"provider_calls":false,"unexpected":"value"}'
    store,_=fixture_store_response(tmp_path,**kwargs)
    dispatch=SimpleNamespace(collect=lambda request:False if case=='refused' else True)
    with pytest.raises((RuntimeError,ValueError)):
        module.synthetic_collected_result(dispatch,store,'exact-request')


def test_worker_error_diagnostic_preserves_fixed_type_and_subphase(tmp_path):
    (tmp_path/'work').mkdir()
    module.worker_mark(tmp_path,'normal_request','unexpected_error',subphase='collect',error_type='TypeError')
    data=json.loads((tmp_path/'work/worker-diagnostic.json').read_text())
    assert data=={'phase':'normal_request','error':'unexpected_error','subphase':'collect','error_type':'TypeError'}
    for kwargs in ({'subphase':'SECRET-SENTINEL'},{'error_type':'SECRET-SENTINEL'}):
        with pytest.raises(ValueError):module.worker_mark(tmp_path,'normal_request',**kwargs)
