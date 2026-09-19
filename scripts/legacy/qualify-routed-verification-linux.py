#!/usr/bin/env python3
"""Opt-in frozen wrapper: actual caller/collector/verifier, synthetic inference only."""

# Archived one-off operation; use the supported host installer instead.
if __name__ == '__main__':
    raise SystemExit('Archived operation is disabled. See docs/AGENT-SETUP.md for supported installation.')

import argparse
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import types
import uuid

ROOT = Path(__file__).resolve().parents[2]
DRIVER = 'qualify-routed-driver-linux.py'
IMAGE = 'sha256:5a03c9d2d1fc20683029c47683a3b0470ad2d7ce79eeb43ced1bdfba10770693'
MODES = ('passed', 'rejected', 'empty')
SEEDS = ('routed_verification', 'routed_verifier_runtime', 'routed_verification_policy')
READBACK_ACTIONS = frozenset({'load','publish','cleanup_inspect'})


def sha(raw): return hashlib.sha256(raw).hexdigest()


def driver_module(bundle=None):
    source = bundle['driver_harness'] if bundle else (ROOT / 'scripts/legacy' / DRIVER).read_text()
    module = types.ModuleType('frozen_verification_driver')
    module.__file__ = str(Path(__file__).resolve())
    exec(compile(source, DRIVER, 'exec'), module.__dict__)
    module.ROOT = ROOT
    module.START_MODULES = (*module.START_MODULES, *SEEDS)
    return module


def validate_bundle(bundle):
    if (bundle.get('verification_version') != 1 or type(bundle.get('verification_version')) is not int
            or bundle.get('verification_mode') not in MODES or bundle.get('image') != IMAGE
            or bundle.get('scenario') != 'results'
            or type(bundle.get('driver_harness')) is not str
            or sha(bundle['driver_harness'].encode()) != bundle.get('driver_harness_sha256')):
        raise ValueError('verification_bundle_invalid')
    driver_module(bundle).validate_bundle(bundle)


def prepare(path, mode):
    if mode not in MODES: raise ValueError('verification_mode_invalid')
    driver = driver_module(); sources = driver.source_closure(ROOT)
    support = (ROOT / 'scripts/legacy' / driver.SUPPORT).read_text()
    harness = Path(__file__).read_text(); base = (ROOT / 'scripts/legacy' / DRIVER).read_text()
    bundle = {'version': 2, 'scenario': 'results', 'verification_version': 1, 'verification_mode': mode,
        'run_id': 'driverqual-' + uuid.uuid4().hex[:16], 'image': IMAGE, 'profile': list(driver.PROFILE),
        'sources': sources, 'hashes': {key: sha(value.encode()) for key, value in sources.items()},
        'support': support, 'support_sha256': sha(support.encode()),
        'harness': harness, 'harness_sha256': sha(harness.encode()),
        'driver_harness': base, 'driver_harness_sha256': sha(base.encode())}
    validate_bundle(bundle)
    with Path(path).open('x') as stream: json.dump(bundle, stream, indent=2); stream.write('\n')
    return bundle


def protected_script(mode):
    if mode not in ('passed', 'rejected'): raise ValueError('verification_mode_invalid')
    return ('''import errno,json,os,sys
from pathlib import Path
assert os.getuid()==1000 and os.getgid()==1000
source=Path('/workspace/fixture.txt')
assert source.read_bytes()==b'SYNTHETIC_ROUTED_CALLER_FILE\\n'
try: source.write_bytes(b'must not write')
except OSError as error:
    assert error.errno in (errno.EROFS,errno.EACCES)
else: raise AssertionError('candidate writable')
Path('/tmp/private-check').write_text('scratch works')
print(json.dumps({'uid':os.getuid(),'gid':os.getgid(),'candidate_read':True,'candidate_readonly':True,'scratch_writable':True},sort_keys=True))
sys.exit(EXIT)
'''.replace('EXIT', '0' if mode == 'passed' else '7')).encode()


def environment(mode, image):
    scripts = {} if mode == 'empty' else {'protected': protected_script(mode)}
    checks = [] if mode == 'empty' else [{'id': 'protected', 'description': 'Read immutable candidate as tool UID',
        'argv': ['/opt/hermes/venv/bin/python', '-I', '-B', '/run/task/check.py'],
        'script_id': 'protected', 'script_name': 'check.py', 'script_sha256': sha(scripts['protected']),
        'timeout_seconds': 15}]
    return {'project_id': 'synthetic', 'version': 'qualification-v1', 'architecture': 'amd64',
        'base_image_digest': image, 'image_digest': image, 'cli_versions': {'python': 'pinned-image'},
        'readiness_probes': [{'id': 'python', 'argv': ['/opt/hermes/venv/bin/python', '--version']}],
        'checks': checks}, scripts


def compose_verification(driver, original, bundle, *args):
    from cloudworkbench.routed_results import publish_child_results
    from cloudworkbench.routed_export_protocol import ExportLimits
    from cloudworkbench.routed_verification import prepare_verification, run_verification, reconcile_verification, _runtime_specs
    from cloudworkbench.routed_verification_policy import VerificationLimits
    from cloudworkbench.routed_verifier_runtime import VerifierRuntime
    scheduler, routed, stage, caller, quiesced, profile, service, provider, folder, calls, support = args
    result = original(*args)
    recovered = publish_child_results(scheduler, routed, stage, caller, replace(quiesced, observation=None),
        execution_profile=profile, services=(service,), publication_root=folder/'published',
        revision_root=folder/'output-revisions', selected_paths=('fixture.txt',), forbidden_values=(),
        export_limits=ExportLimits(max_bytes=4096, max_entries=10, max_depth=4, max_seconds=10))
    storage = folder/'verification-storage'; storage.mkdir(mode=0o700)
    material = folder/'verification-material'; material.mkdir(mode=0o2750); os.chown(material, 0, 1000); material.chmod(0o2750)
    env, scripts = environment(bundle['verification_mode'], bundle['image'])
    prepared = prepare_verification(scheduler, routed, caller, recovered, services=(service,),
        environment=env, scripts=scripts, storage_root=storage, material_root=material,
        forbidden_values=(), required_tool_gid=1000, limits=VerificationLimits(max_seconds=45, cpus=1, memory_mib=512, pids=64))
    specs = _runtime_specs(routed, prepared)
    identities = [{'spec': spec.document, 'digest': spec.digest} for spec in specs]
    (folder/'verification-identities.json').write_text(json.dumps(identities))
    verifier = VerifierRuntime(routed.runtime, journal_root=folder/'verifier-journal')
    original_rpc = verifier._rpc; rpcs = []
    def observed(action, argv, **kwargs):
        rpcs.append(argv[0]); return original_rpc(action, argv, **kwargs)
    verifier._rpc = observed
    before_state = driver.composition_state(scheduler, caller.attempt_id)
    source = prepared.materialization.source/'fixture.txt'; before_hash = sha(source.read_bytes())
    try:
        first = run_verification(scheduler, routed, caller, recovered, prepared, verifier, services=(service,), forbidden_values=())
        boundary = len(rpcs)
        second = run_verification(scheduler, routed, caller, recovered, prepared, verifier, services=(service,), forbidden_values=())
        if first != second or any(v in ('create', 'start') for v in rpcs[boundary:]):
            raise RuntimeError('verification_replay_unproven')
        after_state = driver.composition_state(scheduler, caller.attempt_id)
        with scheduler.store._connect() as db:
            metadata = json.loads(db.execute('SELECT metadata FROM artifacts WHERE id=?', (first.artifact_id,)).fetchone()[0])
            events = [json.loads(row[0]) for row in db.execute("SELECT payload FROM events WHERE type='artifact.created' AND json_extract(payload,'$.id')=?", (first.artifact_id,))]
        raw = Path(metadata['storage_path']).read_bytes(); body = json.loads(raw)
        expected = 'needs_review' if bundle['verification_mode'] == 'empty' else bundle['verification_mode']
        if (first.outcome != expected or metadata['sha256'] != sha(raw) or metadata['bytes'] != len(raw)
                or body['plan'] != prepared.plan.to_dict() or body['assessment']['outcome'] != expected
                or len(events) != 1 or 'storage_path' in events[0]
                or before_state['root'] != after_state['root'] or before_state['counts'] != after_state['counts']
                or len(after_state['artifacts']) != 3 or sha(source.read_bytes()) != before_hash):
            raise RuntimeError('verification_composition_unproven')
        records = []
        for spec in specs:
            value = verifier.load(spec, authorize=lambda action: action in READBACK_ACTIONS, forbidden_values=())
            observed_log = json.loads(value.logs)
            if observed_log != {'uid':1000,'gid':1000,'candidate_read':True,'candidate_readonly':True,'scratch_writable':True}:
                raise RuntimeError('verification_uid_mount_unproven')
            records.append({'spec_sha256':spec.digest,'runtime_id':value.runtime_id,'exit_code':value.exit_code,
                'cleanup_confirmed':value.cleanup_confirmed,'logs_sha256':value.logs_sha256,'log_observation':observed_log})
        result['protected_verification'] = {'version':1,'mode':bundle['verification_mode'],'outcome':first.outcome,
            'driver_harness_sha256':bundle['driver_harness_sha256'],'final_artifact_count':len(after_state['artifacts']),
            'plan_sha256':prepared.plan.digest,'receipt_sha256':first.receipt_sha256,
            'artifact_id':first.artifact_id,'artifact_bytes':len(raw),'candidate_sha256':recovered.candidate.revision.sha256,
            'candidate_file_sha256':before_hash,'input_revision_sha256':recovered.candidate.input_revision_sha256,
            'cleanup_sha256':recovered.cleanup.evidence_sha256,'records':records,
            'create_calls':rpcs.count('create'),'start_calls':rpcs.count('start'),
            'replay_equal':True,'replay_no_create_start':True,'root_occupancy_retained':True,
            'no_gate_or_seat_release':True,'candidate_unchanged':True,'event_storage_path_absent':True,
            'required_tool_gid':prepared.materialization.required_tool_gid,
            'source_gid':source.stat().st_gid,'source_directory_gid':prepared.materialization.source.stat().st_gid}
    finally:
        cleanup = reconcile_verification(scheduler, routed, caller, recovered, prepared, verifier, services=(service,), forbidden_values=())
        if not all(item.get('cleanup_confirmed') is True for item in cleanup):
            raise RuntimeError('verification_cleanup_unconfirmed')
    return result


def read_verifier_specs(folder, bundle, identity):
    from cloudworkbench.routed_verifier_runtime import CheckRuntimeSpec
    path = folder/'verification-identities.json'
    if not path.exists(): return ()
    if path.is_symlink() or not 0 < path.stat().st_size <= 65536: raise RuntimeError('verification_cleanup_identity_invalid')
    entries = json.loads(path.read_bytes())
    if type(entries) is not list or len(entries) > 1: raise RuntimeError('verification_cleanup_identity_invalid')
    specs = []
    for item in entries:
        raw = dict(item['spec'])
        for key in ('candidate_path','task_path'): raw[key] = Path(raw[key])
        for key in ('candidate_identity','task_identity','argv'): raw[key] = tuple(raw[key])
        spec = CheckRuntimeSpec(**raw)
        if (spec.digest != item['digest'] or spec.owner != bundle['run_id'] or spec.image != bundle['image']
                or spec.attempt_id != identity['spec']['attempt_id'] or spec.generation != 1
                or spec.uid != 1000 or spec.gid != 1000 or spec.check_id != 'protected'
                or not spec.candidate_path.is_relative_to(folder/'verification-material')
                or not spec.task_path.is_relative_to(folder/'verification-material')):
            raise RuntimeError('verification_cleanup_identity_invalid')
        specs.append(spec)
    return tuple(specs)


def reconcile_verifiers(driver, original, bundle, folder, actual_bundle, identity):
    from cloudworkbench.runtime import Runtime
    from cloudworkbench.routed_verifier_runtime import VerifierRuntime
    specs = read_verifier_specs(folder, bundle, identity)
    receipts = []
    if specs:
        base = Runtime({'root':folder,'image':bundle['image'],'owner':bundle['run_id'],
            'approved_mount_roots':[folder],'approved_writable_mount_roots':[folder]})
        verifier = VerifierRuntime(base, journal_root=folder/'verifier-journal')
        for spec in specs:
            value = verifier.reconcile(spec, authorize=lambda action: action in
                {'reconcile','cleanup_inspect','cleanup_logs','stop','remove','status'}, forbidden_values=())
            if value.get('cleanup_confirmed') is not True: raise RuntimeError('verification_cleanup_unconfirmed')
            receipts.append({'spec_sha256':spec.digest,'cleanup_confirmed':True})
    command = driver.docker_command(folder)
    remaining = command(['ps','-aq','--no-trunc','--filter','label=io.cloudworkbench.owner='+bundle['run_id'],
        '--filter','label=io.cloudworkbench.role=routed-verifier']).splitlines()
    if remaining: raise RuntimeError('verification_cleanup_unconfirmed')
    result = original(folder, actual_bundle, identity)
    result['verifier_cleanup'] = {'remaining':remaining,'receipts':receipts,'version':1}
    return result


def validate_receipt(receipt, bundle):
    driver_module(bundle).validate_receipt(receipt, bundle)
    proof = receipt['worker']['composition'].get('protected_verification', {})
    cleanup = receipt['export_cleanup'].get('verifier_cleanup', {})
    expected = 'needs_review' if bundle['verification_mode'] == 'empty' else bundle['verification_mode']
    count = 0 if bundle['verification_mode'] == 'empty' else 1
    if (type(proof.get('version')) is not int or proof.get('version') != 1 or proof.get('mode') != bundle['verification_mode'] or proof.get('outcome') != expected
            or proof.get('driver_harness_sha256') != bundle['driver_harness_sha256']
            or type(proof.get('final_artifact_count')) is not int or proof['final_artifact_count'] != 3
            or any(type(proof.get(key)) is not int or proof[key] != count for key in ('create_calls','start_calls'))
            or any(proof.get(key) is not True for key in ('replay_equal','replay_no_create_start','root_occupancy_retained',
                'no_gate_or_seat_release','candidate_unchanged','event_storage_path_absent'))
            or any(type(proof.get(key)) is not int or proof[key] != 1000 for key in ('required_tool_gid','source_gid','source_directory_gid'))
            or proof.get('candidate_sha256') != receipt['worker']['composition']['candidate_sha256']
            or proof.get('candidate_file_sha256') != sha(b'SYNTHETIC_ROUTED_CALLER_FILE\n')
            or proof.get('input_revision_sha256') != receipt['worker']['composition']['input_revision_sha256']
            or proof.get('cleanup_sha256') != receipt['worker']['composition']['cleanup_sha256']
            or not re.fullmatch('verification-[0-9a-f]{64}',proof.get('artifact_id',''))
            or type(proof.get('artifact_bytes')) is not int or proof['artifact_bytes'] <= 0
            or any(not re.fullmatch('[0-9a-f]{64}',proof.get(key,'')) for key in ('plan_sha256','receipt_sha256'))
            or len(proof.get('records', [])) != count or cleanup.get('version') != 1
            or cleanup.get('remaining') != [] or len(cleanup.get('receipts', [])) != count):
        raise ValueError('verification_receipt_unproven')
    for record, closed in zip(proof['records'], cleanup['receipts']):
        if (any(not re.fullmatch('[0-9a-f]{64}',record.get(key,'')) for key in ('spec_sha256','runtime_id','logs_sha256'))
                or type(record.get('exit_code')) is not int or record['exit_code'] != (0 if expected == 'passed' else 7)
                or record.get('cleanup_confirmed') is not True
                or closed != {'spec_sha256':record['spec_sha256'],'cleanup_confirmed':True}
                or record.get('log_observation') != {'uid':1000,'gid':1000,'candidate_read':True,'candidate_readonly':True,'scratch_writable':True}):
            raise ValueError('verification_check_unproven')
    return True


def configured_driver(bundle):
    validate_bundle(bundle)
    driver = driver_module(bundle)
    compose = driver.compose_results; reconcile = driver.reconcile_fixture_export
    codes = driver.diagnostic_codes
    driver.compose_results = lambda *args: compose_verification(driver, compose, bundle, *args)
    driver.reconcile_fixture_export = lambda folder, value, identity: reconcile_verifiers(driver, reconcile, bundle, folder, value, identity)
    driver.validate_bundle = validate_bundle
    driver.validate_receipt = validate_receipt
    driver.diagnostic_codes = lambda value: codes(value) | codes({**value, 'harness':value['driver_harness']})
    driver.QUALIFICATION_BUNDLE = bundle
    return driver


def main():
    if sys.argv[1:2] == ['--worker-entry']:
        bundle = json.loads((Path(sys.argv[2])/'bundle.json').read_text())
        configured_driver(bundle).main(); return
    if sys.argv[1:2] == ['--remote-entry']:
        configured_driver(QUALIFICATION_BUNDLE).main(); return
    parser = argparse.ArgumentParser(); mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare', type=Path); mode.add_argument('--remote', type=Path)
    parser.add_argument('--mode', choices=MODES, default='passed'); parser.add_argument('--receipt', type=Path)
    args = parser.parse_args()
    if args.prepare:
        bundle = prepare(args.prepare, args.mode)
        print(json.dumps({'path':str(args.prepare),'run_id':bundle['run_id'],'mode':args.mode,'remote_executed':False})); return
    if args.receipt is None or args.receipt.exists(): parser.error('new --receipt is required')
    bundle = json.loads(args.remote.read_text()); configured_driver(bundle).main()


if __name__ == '__main__': main()
