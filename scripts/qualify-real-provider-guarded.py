#!/usr/bin/env python3
"""Prepared one-request qualification. No service changes; execution is opt-in.

Run on the verified host from a frozen source snapshot. Default is preflight.
The approval manifest is controller configuration, never a model request.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import socket
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid

from cloudworkbench.qualification_guard import LegacyRunnerFence,QualificationRefused,read_bounded

CANONICAL_TOKEN=Path('/var/lib/cloud-workbench/auth/claude-token')
WORKER_CONFIG=Path('/etc/cloud-workbench/worker.json')
SERVICE='cloud-workbench-worker'
MODULES=('__init__.py','qualification_guard.py','store.py','provider_leases.py','provider_dispatch.py','provider_docker.py',
    'provider_executor.py','supervised_executor.py','provider_protocol.py','native_responses.py','provider_recovery.py','cancellation_supervisor.py',
    'inference_budget.py','budget_authority.py','inference_relay.py','inference_service.py',
    'hermes_inference_protocol.py','inference_transport.py','profile_binding.py','models.py','retention.py','egress.py')


def trusted_json(path):
    body,info=read_bounded(Path(path))
    if info.st_uid!=0 or info.st_mode&0o022:raise QualificationRefused('root_owned_manifest_required')
    return json.loads(body),hashlib.sha256(body).hexdigest()


def validate_manifest(manifest,*,now):
    expected={'version','host','authorization','worker_config_sha256','state_root','database','lock_device','lock_inode',
        'worker_uid','worker_gid','provider_image','profile_path','profile_sha256','model_identifier_evidence',
        'model_identifier_evidence_sha256','billing_evidence_path','billing_evidence_sha256','approved_domains','controller_source_sha256','harness_sha256'}
    if type(manifest) is not dict or set(manifest)!=expected or manifest['version']!=1 or manifest['host']!='omarchy':
        raise QualificationRefused('invalid_qualification_manifest')
    authorization=manifest['authorization']
    if (type(authorization) is not dict or set(authorization)!={'scope','reference','expires_at','quiescence_window','other_credential_consumers_fenced'}
            or authorization['scope']!='single_real_provider_canary' or not isinstance(authorization['reference'],str)
            or not 1<=len(authorization['reference'])<=256 or authorization['quiescence_window'] is not True
            or authorization['other_credential_consumers_fenced'] is not True
            or type(authorization['expires_at']) not in (int,float) or not now+600<=authorization['expires_at']<=now+3600):
        raise QualificationRefused('qualification_authorization_missing_or_expired')
    hashes=[manifest[k] for k in ('worker_config_sha256','profile_sha256','model_identifier_evidence_sha256','billing_evidence_sha256','harness_sha256')]
    sources=manifest['controller_source_sha256']
    if type(sources) is not dict or set(sources)!=set(MODULES):raise QualificationRefused('source_snapshot_required')
    hashes.extend(sources.values())
    if any(type(v) is not str or len(v)!=64 or set(v)-set('0123456789abcdef') for v in hashes):
        raise QualificationRefused('invalid_manifest_hash')
    if (type(manifest['provider_image']) is not str or not manifest['provider_image'].startswith('sha256:')
            or len(manifest['provider_image'])!=71 or set(manifest['provider_image'][7:])-set('0123456789abcdef')):
        raise QualificationRefused('immutable_provider_image_required')
    if manifest['state_root']!='/var/lib/cloud-workbench/worker' or manifest['database']!='/var/lib/cloud-workbench/control/state.db':
        raise QualificationRefused('legacy_paths_require_separate_review')
    if type(manifest['approved_domains']) is not list or not manifest['approved_domains'] or set(manifest['approved_domains'])-{'api.anthropic.com','claude.ai','platform.claude.com'}:
        raise QualificationRefused('unapproved_provider_domains')
    for key in ('profile_path','model_identifier_evidence','billing_evidence_path'):
        value=manifest[key]
        if type(value) is not str or not Path(value).is_absolute() or Path(value).resolve()!=Path(value):
            raise QualificationRefused('noncanonical_profile_evidence')
    return manifest


def service_snapshot():
    properties=('LoadState','ActiveState','SubState','MainPID','UnitFileState','Restart')
    result=subprocess.run(['systemctl','show',SERVICE,*['--property='+key for key in properties]],
        capture_output=True,text=True,timeout=3,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
    values=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    if result.returncode or set(values)!=set(properties):return None
    return values


def service_inactive():
    value=service_snapshot()
    return bool(value and value['LoadState']=='loaded' and value['ActiveState']=='inactive'
                and value['SubState']=='dead' and value['MainPID']=='0')


def credential_metadata():
    value=CANONICAL_TOKEN.lstat()
    if (not stat.S_ISREG(value.st_mode) or value.st_nlink!=1 or value.st_uid!=959 or value.st_gid!=959
            or stat.S_IMODE(value.st_mode)!=0o640 or not 1<=value.st_size<=4096):
        raise QualificationRefused('canonical_credential_metadata_invalid')
    return {key:getattr(value,'st_'+key) for key in ('dev','ino','uid','gid','mode','nlink','size','mtime_ns','ctime_ns')}


def validate_sources(manifest):
    import cloudworkbench
    root=Path(cloudworkbench.__file__).parent
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest()!=manifest['harness_sha256']:
        raise QualificationRefused('harness_changed')
    if any(hashlib.sha256((root/name).read_bytes()).hexdigest()!=digest for name,digest in manifest['controller_source_sha256'].items()):
        raise QualificationRefused('controller_source_changed')


def load_profile(manifest):
    from cloudworkbench.inference_transport import PinnedCLI
    from cloudworkbench.profile_binding import canonical_pinned_profile_digest
    profile_data,digest=trusted_json(manifest['profile_path'])
    if digest!=manifest['profile_sha256'] or set(profile_data)!={'path','sha256','version','native_model','effort'}:
        raise QualificationRefused('profile_changed')
    profile=PinnedCLI(**{**profile_data,'path':Path(profile_data['path'])})
    if profile.native_model!='claude-fable-5-1' or profile.effort!='max':
        raise QualificationRefused('fixed_fable_model_policy_mismatch')
    evidence,evidence_hash=trusted_json(manifest['model_identifier_evidence'])
    # Root-reviewed external identifier provenance; this is NOT account entitlement.
    if (evidence_hash!=manifest['model_identifier_evidence_sha256'] or type(evidence) is not dict
            or set(evidence)!={'native_model','profile_digest','provider_image','source_reference','reviewed_at','identifier_verified'}
            or evidence['native_model']!=profile.native_model or evidence['profile_digest']!=canonical_pinned_profile_digest(profile)
            or evidence['provider_image']!=manifest['provider_image'] or evidence['identifier_verified'] is not True
            or type(evidence['source_reference']) is not str or not evidence['source_reference']
            or type(evidence['reviewed_at']) is not str or not evidence['reviewed_at']):
        raise QualificationRefused('exact_model_identifier_unverified')
    billing,billing_hash=trusted_json(manifest['billing_evidence_path'])
    if (billing_hash!=manifest['billing_evidence_sha256'] or type(billing) is not dict
            or set(billing)!={'account_identity','native_model','billing_basis','source_reference','verified_at','entitlement_verified','credential_identity'}
            or billing['account_identity']!='dedicated-cloud-claude' or billing['native_model']!=profile.native_model
            or billing['billing_basis']!='existing_entitlement_no_incremental_spend' or billing['entitlement_verified'] is not True
            or type(billing['source_reference']) is not str or not billing['source_reference']
            or type(billing['verified_at']) not in (int,float) or not time.time()-86400<=billing['verified_at']<=time.time()):
        raise QualificationRefused('account_entitlement_without_incremental_spend_unverified')
    if billing['credential_identity']!=credential_metadata():
        raise QualificationRefused('entitlement_credential_changed')
    return profile


def run_one(manifest,profile,guard,folder,receipt,*,credential_identity,cancel_event):
    from cloudworkbench.store import Store
    from cloudworkbench.inference_budget import InferenceBudget,BudgetPolicy,RootScope,AttemptScope,ensure_schema
    from cloudworkbench.provider_leases import ProviderLeases
    from cloudworkbench.provider_dispatch import ProviderDispatch
    from cloudworkbench.provider_docker import DockerConfig,ProviderDocker
    from cloudworkbench.provider_executor import ProviderExecutor
    from cloudworkbench.supervised_executor import SupervisedProviderExecutor
    from cloudworkbench.inference_relay import AttemptBinding,DispatchContext,_json
    from cloudworkbench.profile_binding import canonical_pinned_profile_digest
    run_id='realqual-'+uuid.uuid4().hex;staging=folder/'staging';staging.mkdir(mode=0o700)
    holder={};digest=canonical_pinned_profile_digest(profile)
    runtime=ProviderDocker(DockerConfig(manifest['provider_image'],Path(manifest['profile_path']),manifest['profile_sha256'],digest,
        CANONICAL_TOKEN,staging,tuple(manifest['approved_domains'])),cancel_check=lambda spec:holder['wrapper'].cancel_check(spec) if 'wrapper' in holder else True)
    original_command=runtime.command
    def guarded_command(args,**kwargs):
        admission=args[0] in ('create','start') or args[:2] in (['network','create'],['network','connect'])
        if admission:
            guard.check()
            if credential_metadata()!=credential_identity:raise QualificationRefused('entitlement_credential_changed')
        else:guard.check_lock()
        return original_command(args,**kwargs)
    runtime.command=guarded_command
    existing=runtime._run(['ps','-aq','--no-trunc'],time.monotonic()+5).decode().splitlines()
    for identity in existing:
        if len(identity)!=64 or set(identity)-set('0123456789abcdef'):raise QualificationRefused('invalid_container_inventory')
        item=json.loads(runtime._run(['inspect',identity],time.monotonic()+5))[0]
        if any(mount.get('Source')==str(CANONICAL_TOKEN) for mount in item.get('Mounts',[])):
            raise QualificationRefused('canonical_credential_already_mounted')
    # A separate qualification DB. The live DB is never opened through Store.
    store=Store(folder/'qualification.db');principal=store.add_client('qualification',uuid.uuid4().hex+uuid.uuid4().hex,['submit','observe','cancel'],[run_id])
    leases=ProviderLeases(store,cleanup_verifier=runtime.cleanup,inspector_id=runtime.config.inspector_id)
    leases.register_account('dedicated-cloud-claude',legacy_agent='claude',persistent_owner_id=run_id)
    reservation=leases.reserve('dedicated-cloud-claude',persistent_owner_id=run_id)
    attempt=store.create_session(principal,{'project_id':run_id,'agent':'hermes','goal':'one bounded provider qualification'},run_id)
    claimed=store.claim_next()
    if not claimed or claimed['id']!=attempt['attempt_id']:raise QualificationRefused('qualification_claim_mismatch')
    with store._connect() as db:
        row=db.execute('SELECT a.session_id,a.turn_id,s.owner_id,s.project_id FROM attempts a JOIN sessions s ON s.id=a.session_id WHERE a.id=?',(attempt['attempt_id'],)).fetchone()
    root_scope=RootScope(row['owner_id'],row['project_id'],row['session_id'],row['turn_id'],attempt['attempt_id'],attempt['generation'])
    scope=AttemptScope(root_scope,attempt['attempt_id'],attempt['generation']);budget=InferenceBudget()
    with store._tx() as db:
        ensure_schema(db);budget.register_root(db,root_scope,admitted_at=time.time(),policy=BudgetPolicy(root_requests=1,attempt_requests=1));budget.register_attempt(db,scope)
    grant=leases.issue_grant(reservation,attempt_id=attempt['attempt_id'],generation=attempt['generation'])
    dispatch=ProviderDispatch(leases,runtime,budget=budget,budget_scope=scope)
    binding=AttemptBinding(attempt['attempt_id'],attempt['generation'],digest)
    wrapper=SupervisedProviderExecutor(ProviderExecutor(dispatch,reservation=reservation,grant_id=grant,binding=binding,profile=profile,remaining_seconds=lambda:max(0,min(600,manifest['authorization']['expires_at']-time.time()))))
    holder['wrapper']=wrapper
    payload={'model':profile.native_model,'messages':[{'role':'user','content':'Reply exactly CWB_REAL_PROVIDER_QUALIFICATION_OK. Do not request tools.'}],
        'tools':[],'stream':False,'reasoning_effort':profile.effort}
    context=DispatchContext(binding,uuid.uuid4().hex+uuid.uuid4().hex,hashlib.sha256(_json(payload)).hexdigest())
    receipt.update(reservation_id=reservation.reservation_id,attempt_id=attempt['attempt_id'],generation=attempt['generation'],request_nonce=context.request_nonce,profile_digest=digest)
    guard.begin_execution()
    try:
        result=wrapper(context,payload,cancel_event)
        receipt.update(response_status=result.response.status,outer_cleanup_confirmed=result.outer_cleanup_confirmed,
            supervisor=asdict(wrapper.last_receipt) if wrapper.last_receipt else None,recovery_error=wrapper.last_recovery_error)
        if result.response.status==200:
            value=json.loads(result.response.body)
            receipt['canary_matched']=value['choices'][0]['message']['content'].strip()=='CWB_REAL_PROVIDER_QUALIFICATION_OK'
        else:receipt['canary_matched']=False
        # No raw CLI stream, response text or token is serialized into the receipt.
        with store._connect() as db:rows=[dict(r) for r in db.execute('SELECT request_id,state,uncertain,response FROM provider_dispatch')]
        receipt['requests']=[{k:r[k] for k in ('request_id','state','uncertain')} for r in rows]
        if len(rows)==1 and rows[0]['response']:
            envelope=json.loads(rows[0]['response']);reported=envelope.get('result') or {}
            receipt['identity_reported_by_worker']=reported.get('reported_identity')
            receipt['usage_status']=reported.get('usage_status',{'state':'unknown','reason':'missing_result'})
        receipt['model_observed']=(receipt.get('identity_reported_by_worker') or {}).get('model')==profile.native_model
        receipt['passed']=result.response.status==200 and receipt['canary_matched'] and result.outer_cleanup_confirmed and receipt['model_observed']
    finally:
        def settled():
            if wrapper.last_receipt is not None and not wrapper.last_receipt.stopped:return False
            with store._connect() as db:
                if db.execute("SELECT COUNT(*) FROM provider_dispatch WHERE state NOT IN ('cleaned','delivered') OR uncertain!=0").fetchone()[0]:return False
            selector='label=io.cloudworkbench.provider-reservation='+reservation.reservation_id
            for args in (['ps','-aq','--no-trunc','--filter',selector],['network','ls','-q','--no-trunc','--filter',selector]):
                if runtime._run(args,time.monotonic()+5).strip():return False
            return True
        guard.confirm_settled(settled)
    return receipt


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--manifest',required=True);parser.add_argument('--execute-approved',action='store_true')
    args=parser.parse_args(argv)
    if socket.gethostname()!='omarchy' or os.geteuid()!=0:raise QualificationRefused('verified_omarchy_root_required')
    manifest,_=trusted_json(args.manifest);validate_manifest(manifest,now=time.time());validate_sources(manifest);profile=load_profile(manifest)
    config,_=trusted_json(WORKER_CONFIG)
    if config.get('claude_token')!=str(CANONICAL_TOKEN):raise QualificationRefused('canonical_credential_binding_changed')
    guard=LegacyRunnerFence(WORKER_CONFIG,config_sha256=manifest['worker_config_sha256'],state_root=manifest['state_root'],database=manifest['database'],
        lock_device=manifest['lock_device'],lock_inode=manifest['lock_inode'],worker_uid=manifest['worker_uid'],worker_gid=manifest['worker_gid'],service_inactive=service_inactive)
    receipt={'host':'omarchy','executed':False,'passed':False,'real_provider_requested':args.execute_approved}
    folder=None;old_signals={};cancel_event=threading.Event()
    billing,_=trusted_json(manifest['billing_evidence_path'])
    try:
        validate_manifest(manifest,now=time.time())
        guard.acquire()
        receipt['service_snapshot']=service_snapshot()
        receipt['runner_lock']={'device':manifest['lock_device'],'inode':manifest['lock_inode'],'held':True}
        if args.execute_approved:
            folder=Path(tempfile.mkdtemp(prefix='cwb2-real-provider-qualification-',dir='/root')).resolve();folder.chmod(0o700)
            for signum in (signal.SIGINT,signal.SIGHUP,signal.SIGTERM):
                old_signals[signum]=signal.signal(signum,lambda *unused:cancel_event.set())
            receipt['executed']=True
            run_one(manifest,profile,guard,folder,receipt,credential_identity=billing['credential_identity'],cancel_event=cancel_event)
        else:receipt['preflight_only']=True
    except BaseException:
        receipt.update(passed=False,error='qualification_aborted')
        raise
    finally:
        if folder:(folder/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
        if guard.fd is not None and guard.started and not guard.settled:
            receipt.update(passed=False,lock_retained=True,error='cleanup_unconfirmed_operator_recovery_required')
            if folder:(folder/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
            print(json.dumps(receipt),flush=True)
            # No timed unlock or automatic worker restart after unknown effects.
            # Root may externally kill this process only under the held quiescence window.
            while True:time.sleep(30)
        try:
            if guard.fd is not None:guard.close()
        finally:
            for signum,handler in old_signals.items():signal.signal(signum,handler)
    if folder:(folder/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt))
    return 0 if receipt.get('passed') or receipt.get('preflight_only') else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        code=str(error) if type(error) is QualificationRefused else 'qualification_setup_failed'
        print(json.dumps({'passed':False,'error':code}));raise SystemExit(1)
