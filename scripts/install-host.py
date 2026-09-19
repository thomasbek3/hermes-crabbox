#!/usr/bin/env python3
"""Install a new private Linux worker. Default is a read-only JSON plan; --apply writes.

Dependencies are never installed with a remote shell script. Install Docker Engine,
Tailscale, Python 3.11+ with venv, git, sudo, e2fsprogs and util-linux first. Join
Tailscale and enable its HTTPS certificates. Provider login remains interactive;
--grok-auth must name a dedicated Grok CLI OIDC auth JSON, not a personal home.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
TARGET = Path('/opt/cloud-workbench')
STATE = Path('/var/lib/cloud-workbench')
ETC = Path('/etc/cloud-workbench')
VERSION = 'hermes-tasks-desktop-soul-v1'
PROJECT = 'hermes-tasks'
CRABBOX_URL = 'https://github.com/openclaw/crabbox/releases/download/v0.61.0/crabbox_0.61.0_linux_amd64.tar.gz'
CRABBOX_SHA = '56ba9df85ff3832b05dad9f5c41e8c4c3fdd9fcf3dddc90063f64175e21684cc'
REQUIRED = ('docker','tailscale','systemctl','git','sudo','visudo','groupadd','useradd','usermod','mkfs.ext4','findmnt','mount','losetup')

class InstallError(Exception):
    pass

def run(argv, *, timeout=120, cwd=None):
    result = subprocess.run([str(v) for v in argv], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Command output can contain config/credentials. Keep failures nonsecret.
        raise InstallError('command_failed:' + Path(str(argv[0])).name)
    return result.stdout.strip()

def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2)+'\n').encode()

def origin(value):
    if not re.fullmatch(r'https://[a-z0-9][a-z0-9-]*\.[a-z0-9-]+\.ts\.net', value or ''):
        raise InstallError('origin_must_be_https_tailscale_dns_without_path')
    return value

def source_files(root=ROOT):
    tracked = run(['git','-C',root,'ls-files','-z']).split('\0')
    allowed = {'src','deploy','integrations','scripts','LICENSES','pyproject.toml','uv.lock','README.md','LICENSE','THIRD_PARTY_NOTICES.md'}
    result=[]
    for name in sorted(filter(None,tracked)):
        path=root/name
        if Path(name).parts[0] not in allowed:
            continue
        if path.is_symlink() or not path.is_file():
            raise InstallError('source_must_be_clean_tracked_files')
        result.append(path)
    return result

def source_hash(root=ROOT):
    digest = hashlib.sha256()
    for path in source_files(root):
        digest.update(str(path.relative_to(root)).encode()+b'\0'+path.read_bytes())
    return digest.hexdigest()

def write_new(path, data, mode=0o640, uid=0, gid=0):
    path = Path(path)
    if path.is_symlink() or path.parent.resolve() != path.parent:
        raise InstallError('unsafe_destination')
    if path.exists():
        if not path.is_file() or path.read_bytes() != data:
            raise InstallError('existing_file_differs:' + path.name)
        return
    fd = os.open(path, os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW, mode)
    with os.fdopen(fd,'wb') as out:
        os.fchmod(out.fileno(), mode)
        if os.geteuid() == 0:
            os.fchown(out.fileno(),uid,gid)
        out.write(data)

def auth_bytes(path):
    if not path or path.is_symlink():
        raise InstallError('dedicated_grok_auth_unavailable')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as source:
        info=os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_mode&0o077 or info.st_size>65536:
            raise InstallError('dedicated_grok_auth_must_be_private_regular_file')
        raw=source.read(65537)
    if len(raw)>65536:raise InstallError('dedicated_grok_auth_oversize')
    sys.path.insert(0,str(ROOT/'src'))
    from cloudworkbench.hermes_job_entrypoint import read_auth
    # A sudo invocation must accept the invoking user's dedicated login without
    # changing its ownership. Validate a private, bounded snapshot as this uid.
    with tempfile.NamedTemporaryFile() as snapshot:
        os.fchmod(snapshot.fileno(),0o600)
        snapshot.write(raw);snapshot.flush()
        read_auth(Path(snapshot.name).resolve())
    spec=importlib.util.spec_from_file_location('host_refresh_validation',ROOT/'integrations/omarchy-cloud/credential_refresh.py')
    refresh=importlib.util.module_from_spec(spec);spec.loader.exec_module(refresh)
    refresh.credential(raw)
    return raw

def auth_valid(path):
    try:
        auth_bytes(path)
        return True
    except Exception:
        return False

def owned_serve_config(value,host_origin):
    if not value:return True
    if set(value)-{'TCP','Web','AllowFunnel','Foreground'} or value.get('Foreground'):
        return False
    if any(value.get('AllowFunnel',{}).values()):return False
    if value.get('TCP',{})!={'443':{'HTTPS':True}}:return False
    web=value.get('Web',{})
    if set(web)!={host_origin.removeprefix('https://')+':443'}:return False
    routes=next(iter(web.values())).get('Handlers',{})
    allowed={'/':{'Proxy':'http://127.0.0.1:7780'},'/mcp':{'Proxy':'http://127.0.0.1:7781'}}
    return bool(routes) and all(key in allowed and row==allowed[key] for key,row in routes.items())

def preflight(args):
    missing = [name for name in REQUIRED if not shutil.which(name)]
    issues = []
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64','amd64'):
        issues.append('worker_requires_native_linux_x86_64; use_a_linux_VM_on_macOS_or_Windows')
    if shutil.disk_usage('/').free < (args.workspace_mib+20480)*1024**2:
        issues.append('disk_space_below_one_workspace_plus_20GiB_reserve')
    if Path('/proc/meminfo').is_file():
        mem = dict(line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())
        if int(mem['MemTotal'].split()[0]) < (args.memory_mib+2048)*1024:
            issues.append('memory_below_one_worker_plus_2GiB_reserve')
    if sys.version_info < (3,11):
        issues.append('python_3_11_required')
    if not Path('/run/systemd/system').is_dir():
        issues.append('systemd_required')
    issues.extend('missing_dependency:'+name for name in missing)
    if platform.system()=='Linux' and not (ETC/'install-in-progress.json').exists() and not (ETC/'install-receipt.json').exists():
        import pwd, grp
        for name in ('cloud-control','cloud-worker','cloud-job'):
            try:pwd.getpwnam(name);issues.append('existing_service_user:'+name)
            except KeyError:pass
        for name in ('cloud-workbench','cloud-job'):
            try:grp.getgrnam(name);issues.append('existing_service_group:'+name)
            except KeyError:pass
    if platform.system()=='Linux' and not (ETC/'install-in-progress.json').exists() and not (ETC/'install-receipt.json').exists():
        for port in (7780,7781):
            with socket.socket() as probe:
                probe.settimeout(0.2)
                if probe.connect_ex(('127.0.0.1',port))==0:issues.append('service_port_in_use:'+str(port))
    if not args.origin:
        issues.append('supply_--origin_https://YOUR-HOST.YOUR-TAILNET.ts.net')
    else:
        origin(args.origin)
    candidate=args.grok_auth
    pending=ETC/'install-in-progress.json'
    managed=STATE/'auth/grok/.grok/auth.json'
    if (pending.is_file() and not pending.is_symlink() and pending.stat().st_uid==0
            and not pending.stat().st_mode&0o022 and pending.stat().st_size<65536
            and json.loads(pending.read_text()).get('origin')==args.origin and managed.exists()):
        candidate=managed
    if not auth_valid(candidate):
        issues.append('dedicated_valid_private_grok_oidc_auth_file_required')
    if not missing and not any('linux' in issue for issue in issues):
        try:
            info = json.loads(run(['docker','info','--format','{{json .}}']))
            if info.get('OSType') != 'linux':
                issues.append('linux_docker_engine_required')
            tail = json.loads(run(['tailscale','status','--json']))
            if tail.get('BackendState') != 'Running':
                issues.append('tailscale_login_required')
            dns = tail.get('Self',{}).get('DNSName','').rstrip('.')
            if args.origin and args.origin != 'https://'+dns:
                issues.append('origin_does_not_match_this_tailscale_host')
            serve=json.loads(run(['tailscale','serve','status','--json']) or '{}')
            ours=(ETC/'install-receipt.json').exists() or (ETC/'install-in-progress.json').exists()
            if serve and (not ours or not owned_serve_config(serve,args.origin)):
                issues.append('existing_tailscale_serve_config_requires_operator_merge')
        except Exception:
            issues.append('docker_or_tailscale_unavailable_to_current_user')
    return {'schema_version':1,'mode':'plan','ready':not issues,'issues':issues,
        'source_sha256':source_hash(),'supported_host':'Linux x86_64 with systemd and Docker Engine',
        'origin':args.origin,'project':PROJECT,'environment':VERSION,'capacity':args.capacity,
        'provider':'dedicated Grok CLI OIDC; grok-4.6; initial login is interactive; refresh timer installed',
        'paths':{'install':str(TARGET),'config':str(ETC),'state':str(STATE)},
        'changes':['create isolated service users','build pinned public-source desktop image',
                   'install API, worker and MCP services','create immutable environment registry',
                   'configure private Tailscale Serve routes; no Funnel'],
        'next':'Fix listed prerequisites, then rerun identical arguments with sudo and --apply.'}

def configurations(image, ids, args, manifest_digest=None):
    runtime = dict(image=image,root=str(STATE/'workspaces'),uid=ids['job'],gid=ids['job_group'],owner='primary',
        workspace_mib=args.workspace_mib,memory_mib=args.memory_mib,cpus=1,pids=512,
        workspace_helper='/usr/local/libexec/cloud-workbench-volume',network_enabled=False,
        approved_mount_roots=[str(STATE/'worker/tasks'),str(STATE/'auth'),str(STATE/'inputs')],
        approved_writable_mount_roots=[str(STATE/'workspaces')])
    project = {'allowed_agents':['hermes'],'models':{'hermes':['grok-4.6']},'environment_versions':[VERSION],
        'template':str(TARGET/'empty-workspace'),'checks':[],
        'environment_resources':{VERSION:{k:runtime[k] for k in ('workspace_mib','memory_mib','cpus','pids')}}}
    if manifest_digest:
        project['operator_approved_environments']={VERSION:manifest_digest}
    common = dict(state_root=str(STATE/'worker'),database=str(STATE/'control/state.db'),input_root=str(STATE/'inputs'),
        artifact_root=str(STATE/'worker/artifacts'),environment_registry=str(STATE/'environments/registry.db'),
        projects={PROJECT:project},agents=['hermes'],hermes_enabled=True,claude_enabled=False,test_mode=False,
        capacity=args.capacity,hermes_capacity=args.capacity,execution_seconds=10800,hermes_execution_seconds=10800,
        host_memory_reserve_mib=2048,host_disk_reserve_mib=20480,bind='127.0.0.1',port=7780,
        dashboard_origin=args.origin,capabilities={'hermes':{'enabled':True,'authentication':'dedicated_grok_oidc',
        'native_resume':'supported','continuation':'native_session','live_delivery':'unsupported','structured_events':'supported'}})
    worker={**common,'runtime':runtime,'operator_approved_images':[image],
        'hermes_runtime':{'hermes_source_root':str(TARGET/'src/cloudworkbench'),
        'hermes_grok_auth':str(STATE/'auth/grok/.grok/auth.json'),'hermes_journal_root':str(STATE/'worker/hermes-journal'),
        'docker_socket':'/run/docker.sock','coordinator_uid':ids['worker'],'coordinator_gid':ids['service_group'],
        'docker_gid':ids['docker_group'],'tool_gid':ids['job_group'],'tool_shared_gid':ids['job_group'],
        'tool_network':'bridge','tool_image_allowlist':[image],'tool_network_by_profile':{'crabbox-private-tools-bridge':'bridge'},
        'environment_network_profile':'crabbox-private-tools-bridge','environment_secret_refs':['grok-dedicated-oauth'],
        'crabbox_enabled':True,'crabbox_binary':'/usr/local/bin/crabbox','crabbox_image':image,
        'crabbox_images':[image],'crabbox_desktop_images':[image],'crabbox_pstack_images':[]}}
    return common,worker

def environment_manifest(api,image):
    return {'project_id':PROJECT,'version':VERSION,'architecture':'amd64',
        'base_image_digest':image,'image_digest':image,'cli_versions':{'hermes':'0.21.3'},
        'readiness_probes':[{'id':'hermes-import','argv':['/opt/hermes/venv/bin/python','-c','import hermes_cli'],'timeout_seconds':30}],
        'checks':[], 'legacy_template_id':PROJECT,
        'resources':api['projects'][PROJECT]['environment_resources'][VERSION],
        'network_profile':'crabbox-private-tools-bridge','secret_refs':['grok-dedicated-oauth']}

def identities():
    import pwd, grp
    for group in ('cloud-workbench','cloud-job'):
        try: grp.getgrnam(group)
        except KeyError:
            if group=='cloud-job':
                used={entry.gr_gid for entry in grp.getgrall()}
                selected=next(value for value in range(200000,201000) if value not in used)
                run(['groupadd','--gid',selected,group])
            else:run(['groupadd','--system',group])
    for name,group in (('cloud-control','cloud-workbench'),('cloud-worker','cloud-workbench'),('cloud-job','cloud-job')):
        try: pwd.getpwnam(name)
        except KeyError:
            extra=[]
            if name=='cloud-job':
                used={entry.pw_uid for entry in pwd.getpwall()}
                extra=['--uid',next(value for value in range(200000,201000) if value not in used)]
            run(['useradd','--system','--no-create-home','--gid',group,'--shell',shutil.which('nologin') or '/usr/sbin/nologin',*extra,name])
    run(['usermod','-a','-G','docker,cloud-job','cloud-worker'])
    return {'control':pwd.getpwnam('cloud-control').pw_uid,'worker':pwd.getpwnam('cloud-worker').pw_uid,
        'job':pwd.getpwnam('cloud-job').pw_uid,'job_group':grp.getgrnam('cloud-job').gr_gid,
        'service_group':grp.getgrnam('cloud-workbench').gr_gid,'docker_group':grp.getgrnam('docker').gr_gid}

def install(args, plan):
    if os.geteuid() != 0:
        raise InstallError('apply_requires_root')
    os.umask(0o022)
    receipt = ETC/'install-receipt.json'
    requested = {key:plan[key] for key in ('source_sha256','origin','capacity')}
    requested.update(memory_mib=args.memory_mib,workspace_mib=args.workspace_mib)
    if receipt.exists():
        previous=json.loads(receipt.read_text())
        if previous.get('request') != requested:
            raise InstallError('existing_install_differs; upgrades_require_explicit_operator_workflow')
        return {**previous,'unchanged':True}
    if plan['issues']:
        raise InstallError('preflight_failed')
    # Resume only our own interrupted installation with exactly the same request.
    pending=ETC/'install-in-progress.json'
    resumed=(pending.is_file() and not pending.is_symlink() and pending.stat().st_uid==0
        and not pending.stat().st_mode&0o022 and pending.stat().st_size<65536
        and json.loads(pending.read_text())==requested)
    if not resumed and any(path.exists() for path in (ETC,TARGET,STATE,Path('/opt/omarchy-mcp'),Path('/usr/local/bin/crabbox'))):
        raise InstallError('existing_installation_paths; inspect_before_retry')
    if source_hash()!=plan['source_sha256']:
        raise InstallError('source_changed_since_preflight')
    ids=identities(); group=ids['service_group']
    for path,uid,gid,mode in [(TARGET,0,0,0o755),(ETC,0,group,0o750),(STATE,0,group,0o750),
        (STATE/'control',ids['control'],group,0o2770),(STATE/'inputs',ids['control'],group,0o2770),
        (STATE/'workspaces',0,group,0o750),(STATE/'worker',ids['worker'],group,0o2750),
        (STATE/'auth',ids['worker'],ids['job_group'],0o750),(STATE/'environments',0,group,0o750)]:
        if path.is_symlink() or path.resolve()!=path:
            raise InstallError('unsafe_install_directory')
        path.mkdir(parents=True,exist_ok=True);os.chown(path,uid,gid);path.chmod(mode)
    write_new(pending,encoded(requested),0o640,0,group)
    for name in ('tasks','artifacts','results','logs','hermes-journal'):
        path=STATE/'worker'/name;path.mkdir(exist_ok=True);os.chown(path,ids['worker'],group);path.chmod(0o700 if name=='hermes-journal' else 0o2750)
    for path in source_files():
        target=TARGET/path.relative_to(ROOT);target.parent.mkdir(parents=True,exist_ok=True)
        write_new(target,path.read_bytes(),0o755 if path.stat().st_mode&0o111 else 0o644)
    (TARGET/'empty-workspace').mkdir(exist_ok=True)
    for path in (STATE/'auth/grok',STATE/'auth/grok/.grok'):
        path.mkdir(mode=0o700,exist_ok=True);os.chown(path,ids['worker'],group)
    managed_auth=STATE/'auth/grok/.grok/auth.json'
    if resumed and managed_auth.exists():
        # The refresh timer may already have rotated it before a later step failed.
        # Never replace managed credentials with the initial login snapshot.
        if not auth_valid(managed_auth):raise InstallError('managed_grok_auth_needs_reauthentication')
    else:
        write_new(managed_auth,auth_bytes(args.grok_auth),0o600,ids['worker'],group)
    run([sys.executable,'-m','venv',TARGET/'.venv'])
    python=TARGET/'.venv/bin/python'
    run([python,'-m','pip','install','uv==0.12.15'],timeout=600)
    run([TARGET/'.venv/bin/uv','sync','--frozen','--no-dev','--project',TARGET],timeout=1200)
    with urllib.request.urlopen(CRABBOX_URL,timeout=120) as response:
        blob=response.read(128*1024*1024+1)
    if hashlib.sha256(blob).hexdigest()!=CRABBOX_SHA:
        raise InstallError('crabbox_download_checksum_mismatch')
    with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
        entries=[entry for entry in archive if Path(entry.name).name=='crabbox' and entry.isfile()]
        if len(entries)!=1:raise InstallError('invalid_crabbox_archive')
        write_new(Path('/usr/local/bin/crabbox'),archive.extractfile(entries[0]).read(),0o755)
    tag='hermes-crabbox:host-'+plan['source_sha256'][:12]
    run(['docker','build','--memory=8g','--cpu-period=100000','--cpu-quota=200000','--tag',tag,'--file',TARGET/'deploy/Dockerfile.portable',
         '--build-arg','JOB_UID='+str(ids['job']),'--build-arg','JOB_GID='+str(ids['job_group']),TARGET],timeout=3600)
    image=run(['docker','image','inspect',tag,'--format','{{.Id}}'])
    if not re.fullmatch('sha256:[0-9a-f]{64}',image):raise InstallError('invalid_built_image')
    api,worker=configurations(image,ids,args)
    manifest=environment_manifest(api,image)
    manifest_path=ETC/'environment.json'
    write_new(manifest_path,encoded(manifest),0o640,0,group)
    record=json.loads(run([python,'-m','cloudworkbench.environments','--registry',api['environment_registry'],'register',manifest_path]))
    registry=Path(api['environment_registry']);os.chown(registry,0,group);registry.chmod(0o640)
    api,worker=configurations(image,ids,args,record['manifest_sha256'])
    for name,config in (('api',api),('worker',worker)):
        write_new(ETC/(name+'.json'),encoded(config),0o640,0,group)
    helper=Path('/usr/local/libexec/cloud-workbench-volume');helper.parent.mkdir(parents=True,exist_ok=True)
    write_new(helper,(TARGET/'scripts/workspace-volume').read_bytes(),0o755)
    sudoers=Path('/etc/sudoers.d/cloud-workbench-volume')
    write_new(sudoers,b'cloud-worker ALL=(root) NOPASSWD: /usr/local/libexec/cloud-workbench-volume\n',0o440)
    run(['visudo','-cf',sudoers])
    mcp=Path('/opt/omarchy-mcp');mcp.mkdir(exist_ok=True)
    for source in (TARGET/'integrations/omarchy-mcp').rglob('*'):
        if source.is_file():
            dest=mcp/source.relative_to(TARGET/'integrations/omarchy-mcp')
            dest.parent.mkdir(parents=True,exist_ok=True)
            write_new(dest,source.read_bytes(),0o644)
    run([sys.executable,'-m','venv',mcp/'.venv'])
    run([mcp/'.venv/bin/python','-m','pip','install','-r',mcp/'requirements.lock'],timeout=900)
    run([mcp/'.venv/bin/python',mcp/'build_package.py'])
    (mcp/'skill.zip').chmod(0o644)
    env={'HERMES_CRABBOX_ORIGIN':args.origin,'HERMES_CRABBOX_UPSTREAM':'http://127.0.0.1:7780',
        'HERMES_CRABBOX_MCP_PORT':'7781','HERMES_CRABBOX_PROJECT':PROJECT,'HERMES_CRABBOX_ENVIRONMENT':VERSION,'HERMES_CRABBOX_MODEL':'grok-4.6'}
    write_new(ETC/'mcp.env',''.join(k+'='+v+'\n' for k,v in env.items()).encode(),0o644)
    for name in ('api','worker','mcp'):
        write_new(Path('/etc/systemd/system')/('cloud-workbench-'+name+'.service'),(TARGET/'deploy'/('cloud-workbench-'+name+'.service')).read_bytes(),0o644)
    for suffix in ('service','timer'):
        name='cloud-workbench-grok-refresh.'+suffix
        write_new(Path('/etc/systemd/system')/name,(TARGET/'integrations/omarchy-cloud'/name).read_bytes(),0o644)
    run(['systemctl','daemon-reload'])
    run(['systemctl','enable','--now','cloud-workbench-api','cloud-workbench-worker','cloud-workbench-mcp','cloud-workbench-grok-refresh.timer'])
    for name in ('api','worker','mcp'):
        if run(['systemctl','is-active','cloud-workbench-'+name])!='active':raise InstallError('service_failed:'+name)
    ready=False
    for _ in range(15):
        try:
            beat=json.loads((STATE/'worker/heartbeat.json').read_text())
            with urllib.request.urlopen('http://127.0.0.1:7780/health',timeout=2) as response:
                alive=json.load(response).get('status')=='alive'
            ready=(alive and 'hermes' in beat.get('available_agents',[])
                and not beat.get('errors') and 0<=time.time()-beat['timestamp']<15)
            if ready:break
        except (OSError,ValueError,KeyError):pass
        time.sleep(1)
    if not ready:raise InstallError('services_started_but_worker_not_ready; inspect_journal_and_retry')
    run(['tailscale','serve','--bg','--https=443','http://127.0.0.1:7780'])
    run(['tailscale','serve','--bg','--https=443','--set-path=/mcp','http://127.0.0.1:7781'])
    result={'schema_version':1,'mode':'installed','request':requested,'image':image,'origin':args.origin,
        'mcp_url':args.origin+'/mcp','project':PROJECT,'environment':VERSION,'provider_calls':0,
        'live_task_verified':False,'caller_credentials_created':False,
        'next':'Provision a named caller with scripts/provision-delegation-client.py, then run scripts/install-delegation-skill.py on its computer. Run a delegated task to verify provider access.'}
    write_new(receipt,encoded(result),0o640,0,group)
    return result

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group()
    mode.add_argument('--apply',action='store_true');mode.add_argument('--plan',action='store_true')
    p.add_argument('--origin')
    p.add_argument('--grok-auth',type=Path);p.add_argument('--capacity',type=int,choices=range(1,9),default=2)
    p.add_argument('--memory-mib',type=int,default=4096);p.add_argument('--workspace-mib',type=int,default=20480)
    args=p.parse_args(argv)
    try:
        if not 128<=args.memory_mib<=16384 or not 64<=args.workspace_mib<=102400:
            raise InstallError('resource_limits_out_of_range')
        plan=preflight(args)
        result=install(args,plan) if args.apply else plan
        print(json.dumps(result,sort_keys=True))
        return 0 if args.apply or plan['ready'] else 2
    except Exception as exc:
        code=str(exc) if isinstance(exc,InstallError) else type(exc).__name__
        print(json.dumps({'schema_version':1,'mode':'error','error':code,'secrets_printed':False}))
        return 1

if __name__=='__main__':raise SystemExit(main())
