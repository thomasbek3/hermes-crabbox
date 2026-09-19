from dataclasses import FrozenInstanceError
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from cloudworkbench.routed_export_protocol import (
    ExportError, ExportLimits, ExportFile, ExportTree, collector_program,
    decode_export, max_stream_bytes, normalize_selectors,
)


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()+b'\n'


def wire(selected, records):
    raw=canonical({'type':'header','version':1,'selected_paths':list(normalize_selectors(selected))})
    raw+=b''.join(canonical(record) for record in records)
    return raw+canonical({'type':'terminal','entries':len(records),
        'bytes':sum(record.get('size',0) for record in records if record.get('type')=='file'),
        'sha256':hashlib.sha256(raw).hexdigest()})


def file(path='file', data=b'hello', executable=False):
    return {'type':'file','path':path,'size':len(data),'data_b64':base64.b64encode(data).decode(),
            'sha256':hashlib.sha256(data).hexdigest(),'executable':executable}


def collect(root, selected, limits=ExportLimits(), instrument=None):
    program=collector_program(selected,limits,workspace=str(root.resolve()))
    if instrument:program=program.replace('try:run()',instrument+'\ntry:run()')
    return subprocess.run([sys.executable,'-I','-S','-c',program],capture_output=True,timeout=5)


def test_actual_standalone_collector_roundtrip_selected_private_files(tmp_path):
    (tmp_path/'src').mkdir();(tmp_path/'src/empty').mkdir();(tmp_path/'src/private.txt').write_bytes(b'private\x00\xff')
    (tmp_path/'src/private.txt').chmod(0o600)
    (tmp_path/'src/run.sh').write_text('echo test\n');(tmp_path/'src/run.sh').chmod(0o700)
    (tmp_path/'.github').mkdir();(tmp_path/'.github/workflow.yml').write_text('fixture: true')
    (tmp_path/'.env').write_text('must never traverse unselected')
    selected=('src','.github')
    result=collect(tmp_path,selected)
    assert result.returncode==0,result.stderr
    assert not result.stderr and len(result.stdout)<=max_stream_bytes()
    tree=decode_export(result.stdout,selected_paths=selected)
    assert tree.directories==('.github','src','src/empty')
    assert tree.files==(ExportFile('.github/workflow.yml',b'fixture: true',False),
        ExportFile('src/private.txt',b'private\x00\xff',False),ExportFile('src/run.sh',b'echo test\n',True))
    assert '.env' not in result.stdout.decode()
    assert tree==decode_export(collect(tmp_path,selected).stdout,selected_paths=selected)
    with pytest.raises(FrozenInstanceError):tree.sha256='bad'
    with pytest.raises(FrozenInstanceError):tree.files[0].data=b'changed'


def test_file_selectors_share_bounded_parent_directories(tmp_path):
    (tmp_path/'parent').mkdir();(tmp_path/'parent/a').write_bytes(b'a');(tmp_path/'parent/b').write_bytes(b'b')
    (tmp_path/'parent/unselected').symlink_to('/etc/passwd')
    selected=('parent/b','parent/a')
    result=collect(tmp_path,selected,ExportLimits(max_entries=3))
    assert result.returncode==0,result.stderr
    tree=decode_export(result.stdout,selected_paths=selected)
    assert tree.directories==('parent',) and len(tree.files)==2


def test_unicode_quote_and_newline_injection_selectors_are_safe(tmp_path):
    name='quote\";__import__(\"sys\").exit(7)#😀'
    (tmp_path/name).write_text('safe')
    result=collect(tmp_path,(name,))
    assert result.returncode==0,result.stderr
    assert decode_export(result.stdout,selected_paths=(name,)).files[0].path==name


@pytest.mark.parametrize('value',[(),[],('..',),('.',),('/etc',),('a//b',),('a/../b',),('a\\b',),('a\n',),('\ud800',),('x'*1025,),('a','a'),('a','a-foo','a/b')])
def test_selector_refusals(value):
    with pytest.raises(ExportError):normalize_selectors(value)


@pytest.mark.parametrize('name',['.git','.hermes','.codex','.claude','.env','.ENV.production','native','auth.json','credentials.json','.ssh','node_modules','.venv','__pycache__'])
def test_private_path_policy_parity(name):
    from cloudworkbench.workflow_revisions import _safe_path, RevisionError
    with pytest.raises(RevisionError):_safe_path('src/'+name)
    with pytest.raises(ExportError):normalize_selectors(('src/'+name,))


def test_private_policy_set_matches_revision_policy():
    from cloudworkbench.workflow_revisions import _DENIED as revision_denied
    from cloudworkbench.routed_export_protocol import _DENIED
    assert _DENIED==revision_denied


@pytest.mark.parametrize('kind',['symlink','directory_symlink','hardlink','fifo','private','missing'])
def test_actual_collector_refuses_unsafe_entries_without_terminal(tmp_path,kind):
    target=tmp_path/'src';target.mkdir()
    if kind=='symlink':(target/'file').symlink_to('/etc/passwd')
    elif kind=='directory_symlink':target.rmdir();target.symlink_to(tmp_path,target_is_directory=True)
    elif kind=='hardlink':
        original=tmp_path/'outside';original.write_bytes(b'x');os.link(original,target/'file')
    elif kind=='fifo':os.mkfifo(target/'pipe')
    elif kind=='private':(target/'.env').write_bytes(b'fixture secret')
    else:target.rmdir()
    result=collect(tmp_path,('src',))
    assert result.returncode==2 and result.stderr==b'workspace_export_failed\n'
    assert b'fixture secret' not in result.stderr and not result.stdout


@pytest.mark.parametrize('limits',[ExportLimits(max_bytes=2),ExportLimits(max_entries=1),ExportLimits(max_depth=1)])
def test_actual_collector_limits(tmp_path,limits):
    (tmp_path/'src').mkdir();(tmp_path/'src/file').write_bytes(b'123')
    result=collect(tmp_path,('src',),limits)
    assert result.returncode==2 and not result.stdout


def test_actual_empty_directory_and_zero_byte_file(tmp_path):
    (tmp_path/'empty').mkdir();(tmp_path/'zero').write_bytes(b'')
    selected=('empty','zero');limits=ExportLimits(max_bytes=0,max_entries=2)
    result=collect(tmp_path,selected,limits)
    assert result.returncode==0,result.stderr
    value=decode_export(result.stdout,selected_paths=selected,limits=limits)
    assert value.files[0].data==b'' and value.directories==('empty',)


def test_collector_file_mutation_detected_after_read(tmp_path):
    target=tmp_path/'file';target.write_bytes(b'original')
    instrument="""original_read=os.read
mutated=False
def mutate(fd,size):
 global mutated
 data=original_read(fd,size)
 if data and not mutated:
  mutated=True
  with open(%r,'wb') as out:out.write(b'changed!')
 return data
os.read=mutate
""" % str(target.resolve())
    result=collect(tmp_path,('file',),instrument=instrument)
    assert result.returncode==2 and not result.stdout


def test_collector_directory_replacement_detected(tmp_path):
    root=tmp_path/'src';root.mkdir();(root/'file').write_bytes(b'original')
    instrument="""original_read=os.read
mutated=False
def mutate(fd,size):
 global mutated
 data=original_read(fd,size)
 if data and not mutated:
  mutated=True
  os.rename(%r,%r)
  os.mkdir(%r)
 return data
os.read=mutate
""" % (str(root.resolve()),str(tmp_path.resolve()/'old'),str(root.resolve()))
    result=collect(tmp_path,('src',),instrument=instrument)
    assert result.returncode==2 and not result.stdout


@pytest.mark.parametrize('change',['missing_terminal','trailing','hash','count','bytes','bad_json','duplicate_key','nonfinite','boolean_version','unknown_header'])
def test_decoder_rejects_bad_framing_and_terminal(change):
    raw=wire(('file',),[file()]);lines=raw.splitlines(keepends=True)
    if change=='missing_terminal':raw=b''.join(lines[:-1])
    elif change=='trailing':raw+=b'{}\n'
    elif change in ('hash','count','bytes'):
        terminal=json.loads(lines[-1]);terminal[{'hash':'sha256','count':'entries','bytes':'bytes'}[change]]='0'*64 if change=='hash' else 99
        raw=b''.join(lines[:-1])+canonical(terminal)
    elif change=='bad_json':raw=b'\xff\n'
    elif change=='duplicate_key':raw=b'{"type":"header","type":"header"}\n'
    elif change=='nonfinite':raw=b'{"type":NaN}\n'
    elif change=='boolean_version':raw=raw.replace(b'"version":1',b'"version":true')
    else:raw=raw.replace(b'"version":1',b'"extra":true,"version":1')
    with pytest.raises(ExportError):decode_export(raw,selected_paths=('file',))


@pytest.mark.parametrize('change',['size','sha','base64','base64_padding','bool_size','bool_execute','extra','escape','private','missing_parent','unselected'])
def test_decoder_independent_file_validation(change):
    value=file()
    if change=='size':value['size']=100
    elif change=='sha':value['sha256']='0'*64
    elif change=='base64':value['data_b64']='!!!!!!!!'
    elif change=='base64_padding':value=file(data=b'f');value['data_b64']='Zh=='
    elif change=='bool_size':value['size']=True
    elif change=='bool_execute':value['executable']=1
    elif change=='extra':value['uid']=0
    elif change=='escape':value['path']='../file'
    elif change=='private':value['path']='.env'
    elif change=='missing_parent':value['path']='file/child'
    else:value['path']='other'
    with pytest.raises(ExportError):decode_export(wire(('file',),[value]),selected_paths=('file',))


@pytest.mark.parametrize('records,selected',[
    ([file(),file()],('file',)),
    ([file('b'),file('a')],('a','b')),
    ([file('z'),{'type':'directory','path':'a'}],('a','z')),
    ([{'type':'directory','path':'a/b'},{'type':'directory','path':'a'}],('a',)),
    ([{'type':'directory','path':'a'},file('a')],('a',)),
    ([file('a')],('a','b')),
])
def test_decoder_order_duplicates_parent_and_selector_coverage(records,selected):
    with pytest.raises(ExportError):decode_export(wire(selected,records),selected_paths=selected)


@pytest.mark.parametrize('secret,path,data',[(b'secret','file',b'secret bytes'),(b'secret','secret-file',b'other')])
def test_decoded_secret_rejects_whole_tree(secret,path,data):
    with pytest.raises(ExportError,match='secret_refused'):
        decode_export(wire((path,),[file(path,data)]),selected_paths=(path,),forbidden_values=(secret,))


def test_decoder_secret_scan_crosses_large_file_boundaries(tmp_path):
    data=b'x'*65535+b'secret'+b'y'*65536;(tmp_path/'file').write_bytes(data)
    result=collect(tmp_path,('file',));assert result.returncode==0
    with pytest.raises(ExportError,match='secret_refused'):
        decode_export(result.stdout,selected_paths=('file',),forbidden_values=(b'secret',))


@pytest.mark.parametrize('limits',[{'max_bytes':True},{'max_entries':0},{'max_depth':65},{'max_seconds':float('nan')},{'max_seconds':0}])
def test_invalid_limits(limits):
    with pytest.raises(ExportError):ExportLimits(**limits)


def test_decode_cap_and_configuration_validation():
    limits=ExportLimits(max_bytes=0,max_entries=1)
    with pytest.raises(ExportError):decode_export(b'x'*(max_stream_bytes(limits)+1),selected_paths=('file',),limits=limits)
    for forbidden in ([b'x'],(b'',),('x',)):
        with pytest.raises(ExportError):decode_export(wire(('file',),[file()]),selected_paths=('file',),forbidden_values=forbidden)


def test_bound_handles_worst_base64_rounding_and_unicode_paths():
    paths=tuple('😀'*200+str(i) for i in range(12))
    limits=ExportLimits(max_bytes=12,max_entries=12)
    raw=wire(paths,[file(path,b'x') for path in sorted(paths)])
    assert len(raw)<=max_stream_bytes(limits)
    assert len(decode_export(raw,selected_paths=paths,limits=limits).files)==12


def test_selector_program_bound_and_default_workspace():
    paths=tuple('x'*900+str(i) for i in range(30))
    assert len(collector_program(paths).encode())<128*1024
    assert '"workspace":"/workspace"' in collector_program(('file',))
    with pytest.raises(ExportError):normalize_selectors(tuple('x'*900+str(i) for i in range(100)))
