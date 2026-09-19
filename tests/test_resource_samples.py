import json
import sys
import time
import pytest
from cloudworkbench.runtime import Runtime
from cloudworkbench import resource_samples as rs

RID = 'b' * 64
IMAGE = 'sha256:' + 'a' * 64

def raw(**overrides):
    return json.dumps({'ID': RID, 'CPUPerc': '101.25%', 'MemUsage': '2.352MiB / 128MiB', 'PIDs': '3', **overrides}).encode()

def test_units_are_approximate_and_cpu_can_exceed_one_core_or_be_idle():
    assert rs.parse_stats(raw(), RID) == {'cpu_percent': 101.25, 'memory_bytes_approx': 2466250, 'memory_limit_bytes_approx': 134217728, 'pids': 3}
    value=rs.parse_stats(raw(CPUPerc='0.00%', MemUsage='1 kB / 1GB'), RID)
    assert value['cpu_percent'] == 0 and value['memory_bytes_approx'] == 1000

@pytest.mark.parametrize('change', [
    {'CPUPerc': 'NaN%'}, {'CPUPerc': 'inf%'}, {'CPUPerc': '-1%'}, {'CPUPerc': '1,2%'},
    {'CPUPerc': True}, {'CPUPerc': '100001%'}, {'MemUsage': '0B / 128MiB'},
    {'MemUsage': '1MiB / 0B'}, {'MemUsage': '1MiB / 2MiB / 3MiB'},
    {'MemUsage': '1PB / 2PB'}, {'MemUsage': '1e30B / 1e31B'},
    {'MemUsage': '1MiB'}, {'PIDs': '0'}, {'PIDs': '-1'}, {'PIDs': '2.0'},
    {'PIDs': '2147483648'}, {'PIDs': '--'}, {'PIDs': None},
])
def test_invalid_or_unavailable_values_are_not_zeroed(change):
    with pytest.raises(rs.SampleError, match='stats_malformed'): rs.parse_stats(raw(**change), RID)

@pytest.mark.parametrize('data,reason', [
    (b'', 'stats_empty'), (b'x' * 4097, 'stats_output_limit'),
    (b'\xff', 'stats_malformed'), (b'[]', 'stats_malformed'), (b'{}\n{}', 'stats_malformed'),
    (b'{"ID":"x","ID":"y"}', 'stats_malformed'),
    (raw(ID=RID[:12]), 'stats_identity_mismatch'), (raw(ID='c' * 64), 'stats_identity_mismatch'),
])
def test_bad_rows_fail_closed(data, reason):
    with pytest.raises(rs.SampleError, match=reason): rs.parse_stats(data, RID)

def test_ignored_name_or_error_fields_never_exposed():
    assert 'SYNTHETIC_SECRET' not in json.dumps(rs.parse_stats(raw(Name='SYNTHETIC_SECRET', error='SYNTHETIC_SECRET'), RID))

@pytest.fixture
def observed_runtime(tmp_path, monkeypatch):
    runtime = Runtime({'root': tmp_path, 'image': IMAGE, 'owner': 'sample-test'})
    calls=[];state={'running': True, 'generation': '7', 'owned': True}
    def run(self, args):
        calls.append((args, self.owner))
        assert args[0] == 'inspect' and args[-1] == RID
        assert 'Health' not in args[4] and 'json .Config.Labels' not in args[4]
        return json.dumps({'id': RID, 'managed': 'true', 'owner': self.owner if state['owned'] else 'another',
                           'attempt': 'attempt', 'generation': state['generation'], 'running': state['running']})
    monkeypatch.setattr(rs._DeadlineRuntime, '_run', run)
    monkeypatch.setattr(rs, '_stats', lambda *args: raw())
    return runtime,calls,state

def test_owned_generation_checked_before_and_after_sample(observed_runtime):
    runtime,calls,_=observed_runtime
    value=rs.sample_runtime(runtime,RID,expected_generation=7)
    assert value['status']=='observed' and value['reason'] is None
    assert value['runtime_owner']=='sample-test' and len(value['source_policy_sha256'])==64
    assert len(calls)==2
    assert runtime._run.__func__ is Runtime._run

@pytest.mark.parametrize('rid,generation', [('$(touch /tmp/owned)',7),(RID[:12],7),(RID,True),(RID,0),(RID,'7')])
def test_bad_identity_never_queries_docker(observed_runtime,rid,generation):
    runtime,calls,_=observed_runtime
    value=rs.sample_runtime(runtime,rid,expected_generation=generation)
    assert value['reason']=='invalid_identity' and not calls

@pytest.mark.parametrize('timeout',[float('nan'),float('inf'),-1,0,3.01,10,True,'3'])
def test_bad_deadline_is_unknown(observed_runtime,timeout):
    runtime,calls,_=observed_runtime
    value=rs.sample_runtime(runtime,RID,expected_generation=7,timeout_seconds=timeout)
    assert value['reason']=='invalid_configuration' and not calls

def test_generation_mismatch_never_calls_stats(observed_runtime,monkeypatch):
    runtime,_,state=observed_runtime;state['generation']='8'
    monkeypatch.setattr(rs,'_stats',lambda *args:pytest.fail('stale stats'))
    value=rs.sample_runtime(runtime,RID,expected_generation=7)
    assert value['reason']=='ownership_unconfirmed' and value['memory_bytes_approx'] is None

def test_unowned_runtime_never_calls_stats(observed_runtime,monkeypatch):
    runtime,_,state=observed_runtime;state['owned']=False
    monkeypatch.setattr(rs,'_stats',lambda *args:pytest.fail('unowned stats'))
    assert rs.sample_runtime(runtime,RID,expected_generation=7)['reason']=='runtime_missing'

def test_exit_during_stats_is_unknown_not_fake_zero(observed_runtime,monkeypatch):
    runtime,_,state=observed_runtime
    def stop(*args):state['running']=False;return raw()
    monkeypatch.setattr(rs,'_stats',stop)
    value=rs.sample_runtime(runtime,RID,expected_generation=7)
    assert value['reason']=='runtime_not_running' and value['cpu_percent'] is None

def test_observer_policy_copied_from_runtime(observed_runtime,monkeypatch):
    runtime,calls,_=observed_runtime
    def mutate(*args):runtime.config['owner']='another';return raw()
    monkeypatch.setattr(rs,'_stats',mutate)
    value=rs.sample_runtime(runtime,RID,expected_generation=7)
    assert value['runtime_owner']=='sample-test' and all(owner=='sample-test' for _,owner in calls)

def fake_docker(tmp_path, program):
    path=tmp_path/'docker';path.write_text('#!'+sys.executable+'\n'+program);path.chmod(0o700);return str(path)

def test_identity_inspection_has_overall_deadline(tmp_path):
    runtime=Runtime({'root':tmp_path,'image':IMAGE,'docker':fake_docker(tmp_path,'import time\ntime.sleep(5)\n')})
    started=time.monotonic();value=rs.sample_runtime(runtime,RID,expected_generation=1,timeout_seconds=.15)
    assert value['reason']=='deadline_exceeded' and time.monotonic()-started<.5

def test_stats_targeted_nonstreaming_no_trunc_no_stderr(tmp_path):
    program='import sys\nassert sys.argv[1:]=='+repr(['stats','--no-stream','--no-trunc','--format','{{json .}}',RID])+'\nprint('+repr(raw().decode())+')\nprint("SYNTHETIC_SECRET",file=sys.stderr)\n'
    output=rs._stats(fake_docker(tmp_path,program),RID,time.monotonic()+2)
    assert rs.parse_stats(output,RID)['pids']==3 and b'SYNTHETIC_SECRET' not in output

@pytest.mark.parametrize('program,reason', [
    ('import os\nos.write(1,b"x"*10000)\n','stats_output_limit'),
    ('import sys\nprint("SYNTHETIC_SECRET",file=sys.stderr)\nsys.exit(1)\n','stats_failed'),
    ('import time\ntime.sleep(5)\n','deadline_exceeded'),
])
def test_stats_limits_and_safe_errors(tmp_path,program,reason):
    started=time.monotonic()
    with pytest.raises(rs.SampleError,match=reason) as exc:rs._stats(fake_docker(tmp_path,program),RID,started+(.2 if reason=='deadline_exceeded' else 3))
    assert 'SYNTHETIC_SECRET' not in str(exc.value) and time.monotonic()-started < (.5 if reason=='deadline_exceeded' else 3.25)

def test_window_bound_max_is_retained_observation(observed_runtime):
    runtime,_,_=observed_runtime;sample=rs.sample_runtime(runtime,RID,expected_generation=7);window=rs.SampleWindow(2)
    window.add({**sample,'cpu_percent':500});window.add({**sample,'cpu_percent':10})
    window.add({**sample,'status':'unknown','reason':'stats_empty','cpu_percent':999,'untrusted':'SECRET'})
    summary=window.summary()
    assert summary['true_peak'] is False and summary['scope']=='retained_sample_window'
    assert summary['dropped_samples']==1 and summary['observed_max']['cpu_percent']==10
    assert summary['observed_samples']==summary['unknown_samples']==1
    assert summary['samples'][-1]['cpu_percent'] is None and 'SECRET' not in json.dumps(summary)
    summary['samples'][0]['cpu_percent']=99
    assert window.summary()['observed_max']['cpu_percent']==10

def test_empty_or_unknown_window_not_zero_usage(observed_runtime):
    runtime,_,_=observed_runtime;window=rs.SampleWindow()
    assert window.summary()['observed_max']['cpu_percent'] is None
    window.add({**rs.sample_runtime(runtime,RID,expected_generation=7),'status':'unknown','reason':'stats_empty'})
    assert window.summary()['status']=='unknown' and window.summary()['unknown_samples']==1

@pytest.mark.parametrize('change', [
    {'runtime_id':'c'*64},{'generation':8},{'runtime_owner':'another'},{'source_policy_sha256':'d'*64},
    {'runtime_owner':'SECRET'*100},{'pids':0},{'cpu_percent':float('nan')},{'memory_bytes_approx':-1},
    {'elapsed_ms':float('inf')},{'sampled_at':'SECRET'},{'status':'unknown','reason':'SYNTHETIC_SECRET'},
])
def test_window_refuses_mixed_identity_or_unsafe_values(observed_runtime,change):
    runtime,_,_=observed_runtime;value=rs.sample_runtime(runtime,RID,expected_generation=7);window=rs.SampleWindow();window.add(value)
    with pytest.raises(ValueError):window.add({**value,**change})

@pytest.mark.parametrize('limit',[0,121,True,1.5])
def test_window_bounded(limit):
    with pytest.raises(ValueError):rs.SampleWindow(limit)


def test_ownership_inspection_output_is_also_bounded(tmp_path):
    runtime=Runtime({'root':tmp_path,'image':IMAGE,'docker':fake_docker(tmp_path,'import os\nos.write(1,b"x"*10000)\n')})
    value=rs.sample_runtime(runtime,RID,expected_generation=1)
    assert value['reason']=='inspection_output_limit' and value['cpu_percent'] is None


def test_commands_share_one_output_budget(tmp_path):
    docker=fake_docker(tmp_path,'import os\nos.write(1,b"x"*2100)\n')
    observer=rs._DeadlineRuntime(Runtime({'root':tmp_path,'image':IMAGE,'docker':docker}),time.monotonic()+3)
    assert len(observer._run(['inspect']))==2100
    with pytest.raises(rs.SampleError,match='inspection_output_limit'):
        observer._run(['inspect'])


def test_child_holding_stdout_cannot_extend_deadline(tmp_path):
    program='import os,time\nif os.fork()==0:\n time.sleep(5)\nelse:\n os._exit(0)\n'
    started=time.monotonic()
    with pytest.raises(rs.SampleError,match='deadline_exceeded'):
        rs._stats(fake_docker(tmp_path,program),RID,started+.15)
    assert time.monotonic()-started < .5


def test_inspection_json_and_missing_binary_are_diagnostic(tmp_path):
    runtime=Runtime({'root':tmp_path,'image':IMAGE,'docker':fake_docker(tmp_path,'print("not json")\n')})
    assert rs.sample_runtime(runtime,RID,expected_generation=1)['reason']=='ownership_unconfirmed'
    runtime.docker=str(tmp_path/'missing-docker')
    assert rs.sample_runtime(runtime,RID,expected_generation=1)['reason']=='runtime_unavailable'


def test_deadline_kills_and_reaps_direct_observer(tmp_path, monkeypatch):
    import os
    children=[]
    original=rs.subprocess.Popen
    def tracked(*args, **kwargs):
        process=original(*args, **kwargs);children.append(process.pid);return process
    monkeypatch.setattr(rs.subprocess,'Popen',tracked)
    with pytest.raises(rs.SampleError,match='deadline_exceeded'):
        rs._stats(fake_docker(tmp_path,'import time\ntime.sleep(10)\n'),RID,time.monotonic()+.2)
    assert len(children)==1
    with pytest.raises(ProcessLookupError):os.kill(children[0],0)
    with pytest.raises(ChildProcessError):os.waitpid(children[0],os.WNOHANG)


def test_window_rejects_over_budget_elapsed_evidence(observed_runtime):
    runtime,_,_=observed_runtime;sample=rs.sample_runtime(runtime,RID,expected_generation=7)
    with pytest.raises(ValueError,match='duration'):rs.SampleWindow().add({**sample,'elapsed_ms':3501})
