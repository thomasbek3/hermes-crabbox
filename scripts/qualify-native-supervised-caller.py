"""Two unprivileged caller roles for the isolated native/supervised proof."""
import errno, hashlib, json, os, signal, socket, subprocess, sys
from pathlib import Path


def state():
    return {'uid':os.getuid(),'gid':os.getgid(),'pid':os.getpid(),
        'status':{k:v.strip() for k,v in (line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
                  if k in ('Uid','Gid','CapEff','CapBnd','NoNewPrivs','Seccomp')},
        'cgroup':{name:Path('/sys/fs/cgroup',name).read_text() for name in ('cgroup.procs','cpu.max','memory.max','pids.max')}}


def relay():
    from cloudworkbench.inference_relay import AttemptBinding,InferenceRelay,make_loopback_service
    if os.getuid()!=1001:raise RuntimeError('wrong_relay_uid')
    config=json.loads(Path('/run/worker-inference/client.json').read_text())
    root=Path('/run/relay')
    def fence(code):
        (root/'fenced.json').write_text(json.dumps({'code':code}))
    forwarded=InferenceRelay(socket_path=Path('/run/worker-inference/socket'),journal_path=root/'relay.db',
        binding=AttemptBinding(**config['binding']),capability=config['worker_capability'],fence=fence,deadline_seconds=180)
    server=make_loopback_service(forwarded,address=('127.0.0.1',9876),
        capability_sha256=hashlib.sha256(config['http_capability'].encode()).hexdigest(),authorize=lambda:True,execution_timeout=190)
    (root/'ready.json').write_text(json.dumps(state()));(root/'ready.json').chmod(0o600)
    server.serve_forever()


def tool(relay_pid):
    if os.getuid()!=1000:raise RuntimeError('wrong_tool_uid')
    checks=[]
    def denied(name,fn,expected=(errno.EACCES,errno.EPERM)):
        try:fn()
        except OSError as exc:
            if exc.errno not in expected:raise
            checks.append({'check':name,'errno':exc.errno})
        else:raise AssertionError(name+' allowed')
    denied('worker_capability_read',lambda:Path('/run/worker-inference/client.json').read_bytes())
    def connect_worker():
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:client.connect('/run/worker-inference/socket')
    denied('worker_socket_connect',connect_worker)
    denied('relay_journal_read',lambda:Path('/run/relay/relay.db').read_bytes())
    denied('relay_signal',lambda:os.kill(relay_pid,signal.SIGTERM))
    denied('init_signal',lambda:os.kill(1,signal.SIGTERM))
    denied('relay_environ_read',lambda:Path('/proc/'+str(relay_pid)+'/environ').read_bytes())
    denied('source_readonly',lambda:Path('/proof/writable-probe/injected').write_text('test'),(errno.EROFS,))
    denied('docker_socket_absent',lambda:Path('/var/run/docker.sock').stat(),(errno.ENOENT,))
    root=Path('/run/tool')
    for key,name in [('HOME','home'),('HERMES_HOME','hermes'),('CODEX_HOME','codex'),('CLAUDE_CONFIG_DIR','claude')]:
        path=root/name;path.mkdir(mode=0o700);os.environ[key]=str(path)
    public=json.loads(Path('/proof/native-config.json').read_text())
    os.environ.update(HERMES_BUNDLED_PLUGINS='/opt/hermes/empty-bundled',HERMES_INTERACTIVE='0',
        HERMES_ENABLE_PROJECT_PLUGINS='0',TERMINAL_CWD=str(root),SYNTHETIC_HTTP_CAPABILITY=public['http_capability'],HERMES_STREAM_RETRIES='0')
    (root/'fixture.txt').write_text('SYNTHETIC_NATIVE_SUPERVISED_FILE\n')
    (root/'query.txt').write_text('Read /run/tool/fixture.txt using read_file, then report SYNTHETIC_NATIVE_SUPERVISED_OK after using its contents.')
    model=public['model']
    config={'security':{'tirith_enabled':False},'plugins':{'enabled':[]},'model':{'default':model,'provider':'synthetic-proof'},
        'providers':{'synthetic-proof':{'base_url':'http://127.0.0.1:9876/v1','transport':'chat_completions','key_env':'SYNTHETIC_HTTP_CAPABILITY','default_model':model}},
        'agent':{'max_turns':3,'api_max_retries':1},'compression':{'enabled':False},
        'auxiliary':{'transient_retries':0,'title_generation':{'enabled':False,'model_upgrade_enabled':False}},'terminal':{'backend':'local','cwd':str(root)}}
    (Path(os.environ['HERMES_HOME'])/'config.yaml').write_text(json.dumps(config))
    argv=['/opt/hermes/venv/bin/hermes','chat','--query-file',str(root/'query.txt'),'--format','stream-json','--oneshot',
          '--provider','synthetic-proof','--model',model,'--reasoning','high','--toolsets','file']
    child=subprocess.Popen(argv,cwd=root,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
    observed=state();observed['hermes_pid']=child.pid
    members=[int(value) for value in observed['cgroup']['cgroup.procs'].split()]
    assert relay_pid in members and child.pid in members
    try:stdout,stderr=child.communicate(timeout=110)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid,signal.SIGKILL);stdout,stderr=child.communicate(timeout=5)
    events=[json.loads(line) for line in stdout.splitlines()]
    terminal=[event for event in events if event.get('type')=='result']
    passed=(child.returncode==0 and len(terminal)==1 and terminal[0].get('exit_code')==0
        and 'SYNTHETIC_NATIVE_SUPERVISED_OK' in terminal[0].get('text','')
        and any(event.get('type')=='tool_result' and event.get('name')=='read_file' for event in events))
    print(json.dumps({'passed':passed,'cli_exit':child.returncode,'events':events,'stderr':stderr[-4000:],
        'isolation_checks':checks,'process':observed,'disabled_fixture_features':['plugins','tirith','compression','title_generation']}))
    return 0 if passed else 1


if __name__=='__main__':
    if sys.argv[1:]==['relay']:relay()
    elif len(sys.argv)==3 and sys.argv[1]=='tool':raise SystemExit(tool(int(sys.argv[2])))
    else:raise SystemExit('unknown caller proof role')
