#!/usr/bin/env python3
"""Idempotent new-service provisioning. Never touches v1, personal auth or jobs."""
import grp
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import subprocess
import sys


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run as root')
    source = Path('/home/thomas/cloud-workbench')
    target = Path('/opt/cloud-workbench')
    root = Path('/var/lib/cloud-workbench')
    for group in ('cloud-workbench','cloud-job'):
        try: grp.getgrnam(group)
        except KeyError: run('/usr/bin/groupadd','--system',group)
    for name,group in (('cloud-control','cloud-workbench'),('cloud-worker','cloud-workbench'),('cloud-job','cloud-job')):
        try: pwd.getpwnam(name)
        except KeyError: run('/usr/bin/useradd','--system','--no-create-home','--gid',group,'--shell','/usr/bin/nologin',name)
    run('/usr/bin/usermod','-a','-G','docker,cloud-job','cloud-worker')
    gid=grp.getgrnam('cloud-workbench').gr_gid
    jobgid=grp.getgrnam('cloud-job').gr_gid
    worker=pwd.getpwnam('cloud-worker').pw_uid
    controller=pwd.getpwnam('cloud-control').pw_uid
    job=pwd.getpwnam('cloud-job').pw_uid
    root.mkdir(exist_ok=True,mode=0o750)
    os.chown(root,0,gid);root.chmod(0o750)
    for child,owner,group,mode in [('control',controller,gid,0o2770),('inputs',controller,gid,0o2770),('workspaces',0,gid,0o750),('auth',worker,jobgid,0o750),('worker',worker,gid,0o2750)]:
        path=root/child;path.mkdir(exist_ok=True);os.chown(path,owner,group);path.chmod(mode)
    for child in ('tasks','artifacts','results','logs'):
        p=root/'worker'/child;p.mkdir(exist_ok=True);os.chown(p,worker,gid);p.chmod(0o2750)
    token=root/'auth'/'claude-token'
    if token.exists():os.chown(token,worker,jobgid);token.chmod(0o640)
    # Sync source explicitly, excluding credentials, local caches and research outputs.
    target.mkdir(exist_ok=True,mode=0o755)
    for name in ('src','deploy','scripts','tests','pyproject.toml','uv.lock'):
        src=source/name;dst=target/name
        if src.is_dir():shutil.copytree(src,dst,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        else:shutil.copy2(src,dst)
    helper=Path('/usr/local/libexec/cloud-workbench-volume')
    helper.parent.mkdir(exist_ok=True)
    shutil.copyfile(source/'scripts'/'workspace-volume',helper);helper.chmod(0o755);os.chown(helper,0,0)
    sudoers=Path('/etc/sudoers.d/cloud-workbench-volume')
    content='cloud-worker ALL=(root) NOPASSWD: /usr/local/libexec/cloud-workbench-volume\n'
    temporary=sudoers.with_suffix('.candidate')
    temporary.write_text(content);temporary.chmod(0o440)
    run('/usr/bin/visudo','-cf',str(temporary));os.replace(temporary,sudoers)
    etc=Path('/etc/cloud-workbench');etc.mkdir(exist_ok=True);os.chown(etc,0,gid);etc.chmod(0o750)
    image=run('/usr/bin/docker','image','inspect','cloud-workbench:build-20260917','--format','{{.Id}}')
    template=target/'demo';template.mkdir(exist_ok=True)
    (template/'booking.py').write_text('def valid_date(value):\n    return True\n')
    shutil.copyfile(source/'scripts/demo-check-booking.py',target/'check_booking.py')
    projects={'sample-web':{'allowed_agents':['fixture','claude'],'models':{'claude':[],'fixture':[]},'environment_versions':['demo-v1'],'template':str(template),'fixture_operation':'fix_booking','checks':[{'id':'booking-validity','description':'Valid dates pass; malformed and nonexistent dates fail.','argv':['python3','/run/task/check_booking.py'],'script_name':'check_booking.py','script_source':str(target/'check_booking.py'),'timeout':30}]}}
    config={'state_root':str(root/'worker'),'database':str(root/'control/state.db'),'input_root':str(root/'inputs'),'artifact_root':str(root/'worker/artifacts'),'projects':projects,'agents':['fixture'],'test_mode':True,'claude_enabled':False,'capacity':2,'execution_seconds':3600,'claude_token':str(token),'runtime':{'image':image,'root':str(root/'workspaces'),'uid':job,'gid':jobgid,'owner':'primary','workspace_mib':256,'memory_mib':1024,'cpus':1,'pids':128,'workspace_helper':str(helper),'approved_mount_roots':[str(root/'worker/tasks'),str(root/'auth')],'approved_writable_mount_roots':[str(root/'workspaces')],'network_enabled':False}}
    # Never overwrite an operator's changed configuration on repeat provision.
    for name,data in [('worker.json',config),('api.json',{k:v for k,v in config.items() if k not in ('claude_token','runtime')})]:
        p=etc/name
        if not p.exists():p.write_text(json.dumps(data,indent=2)+'\n');os.chown(p,0,gid);p.chmod(0o640)
    for name in ('api','worker'):
        shutil.copyfile(source/'deploy'/f'cloud-workbench-{name}.service',Path('/etc/systemd/system')/f'cloud-workbench-{name}.service')
    print(json.dumps({'provisioned':True,'job_uid':job,'job_gid':jobgid,'controller_uid':controller,'worker_uid':worker,'image':image,'v1_changed':False}))


if __name__=='__main__':main()
