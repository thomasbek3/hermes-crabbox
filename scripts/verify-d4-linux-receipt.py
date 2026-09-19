"""Independent local checks of machine receipts; no remote operations."""
from pathlib import Path
import hashlib
import json
import sys

root=Path(sys.argv[1])
receipt=json.loads((root/'d4-linux-corrected-run.json').read_text())
cleanup=json.loads((root/'d4-linux-corrected-staging-cleanup.json').read_text())
staging=json.loads((root/'d4-linux-corrected-staging.json').read_text())
snapshot=root/'d4-linux-corrected-source-snapshot'
manifest=json.loads((snapshot/'manifest.json').read_text())
sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()
assert receipt['passed'] and receipt['host']=='omarchy' and receipt['image_verified']
assert receipt['image']=='sha256:5a03c9d2d1fc20683029c47683a3b0470ad2d7ce79eeb43ced1bdfba10770693'
assert receipt['source_sha256']==manifest
assert sha(snapshot/'manifest.json')==staging['manifest_sha256']
for path,digest in manifest.items():assert sha(snapshot/path)==digest
assert cleanup['run_id']==receipt['run_id'] and cleanup['host']=='omarchy'
assert cleanup['remaining_exact_run_containers']==[]
assert cleanup['effective_uid']==0
assert {entry['path'] for entry in cleanup['paths']}=={'/tmp/'+staging['stage'],'/var/tmp/'+staging['stage']}
for entry in cleanup['paths']:
    assert entry['uid']==(1000 if entry['path'].startswith('/tmp/') else 0)
    assert entry['mode']=='0o700' and entry['manifest_sha256']==staging['manifest_sha256'] and entry['removed']
assert cleanup['command']==['python3','/var/tmp/'+staging['stage']+'/cleanup.py',staging['stage'],staging['manifest_sha256'],receipt['run_id']]
assert receipt['effective_driver_uid']==0 and receipt['executing_driver_sha256']==manifest['driver.py']
assert receipt['observed_non_uds_connect_attempts']==0 and receipt['all_inspected_network_modes_none']
assert receipt['cleanup']=={'created_container_count':9,'private_root_removed':True,'remaining_owned_container_ids':[],'removal_failures':[]}
assert receipt['permission_metadata']=={'config':{'gid':27002,'mode':'0o440','uid':0},'directory':{'gid':27002,'mode':'0o750','uid':27001},'socket':{'gid':27002,'mode':'0o660','uid':27001}}
expected={'permitted':(27003,27002),'permitted-rw':(27003,27002),'wrong-config-owner':(27003,27002),'wrong-config-mode':(27003,27002),'foreign':(27004,27004),'permitted-uid-wrong-group':(27003,27004),'same-group-other-uid':(27004,27002),'no-mount':(27003,27002)}
ids={receipt['controller']['container_id']}
for name,identity in expected.items():
    case=receipt['cases'][name];assert (case['uid'],case['gid'])==identity
    assert case['container_id'] not in ids;ids.add(case['container_id'])
    assert case['host_cgroup']=='0::/system.slice/docker-'+case['container_id']+'.scope'
    assert case['limits']=={'cpu.max':'25000 100000','memory.max':'134217728','pids.max':'32'}
    assert case['inspected_isolation']=={'CapDrop':['ALL'],'Memory':134217728,'MemorySwap':134217728,'NanoCpus':250000000,'NetworkMode':'none','PidsLimit':32,'ReadonlyRootfs':True,'SecurityOpt':['no-new-privileges']}
    assert all(m['rw'] is (name=='permitted-rw' and m['destination']=='/run/cloud-role-broker') for m in case['mounts'])
    assert case['exit_code']==0
    for path,digest in case['imported_source_sha256'].items():assert manifest['source/'+path.removeprefix('/proof/source/')]==digest
    if name in {'permitted','permitted-rw','same-group-other-uid'}:
        assert case['pending_retry_identical'] and case['result_read'] and case['controller_db_absent'] and case['docker_socket_absent']
        assert len(case['tamper_denied'])==6 and set(case['tamper_denied'].values()) <= {1,13,30}
    elif name in {'foreign','permitted-uid-wrong-group'}:
        assert case['config_read_denied'] and case['socket_connect_denied']==13 and case['fixed_client_error']
    elif name in {'wrong-config-owner','wrong-config-mode'}:
        assert case['config_read_succeeded'] and case['fixed_client_refusal']
        assert (case['config_uid'],case['config_mode'],case['observed_invalid_property'])==((27001,'0o440','owner') if name=='wrong-config-owner' else (0,'0o444','mode'))
    else:assert case['role_directory_absent'] and case['docker_socket_absent']
controller=receipt['controller'];assert (controller['uid'],controller['gid'])==(27001,27002)
assert controller['host_cgroup']=='0::/system.slice/docker-'+controller['container_id']+'.scope'
assert len(ids)==9 and receipt['synthetic_capability_not_in_receipt']
shutdown=receipt['controller_shutdown'];assert shutdown['exit_code']==0 and shutdown['stopped'] and shutdown['health']['state']=='stopped'
assert shutdown['pending_count']==3 and receipt['controller_socket_removed']
assert shutdown['inspected_isolation']==receipt['cases']['permitted']['inspected_isolation']
assert len({receipt['cases'][name]['request_id'] for name in ['permitted','permitted-rw','same-group-other-uid']})==3
assert set(receipt['cases']['permitted-rw']['tamper_denied'].values()) <= {1,13}
for path,digest in shutdown['imported_source_sha256'].items():assert manifest['source/'+path.removeprefix('/proof/source/')]==digest
print(json.dumps({'passed':True,'host':'omarchy','run_id':receipt['run_id'],'case_count':8,'distinct_container_cgroups':9,'all_sources_match':True,'all_created_resources_removed':True,'permission_layout_matches':True,'aggregate_cgroup_budget_proven':False,'receipt_sha256':sha(root/'d4-linux-corrected-run.json'),'cleanup_sha256':sha(root/'d4-linux-corrected-staging-cleanup.json')}))
