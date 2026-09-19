#!/usr/bin/env python3
"""Isolated official subscription login; never print the resulting token."""
import fcntl, os, pty, re, select, struct, subprocess, sys, termios, time
from pathlib import Path

DEST = Path('/var/lib/cloud-workbench/auth/claude-token')
if DEST.exists():
    raise SystemExit('Cloud token already exists; refusing replacement')
master, slave = pty.openpty()
fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 180, 0, 0))
cmd = ['docker','run','--rm','-i','-t','--name','cloud-workbench-login',
       '--cap-drop','ALL','--security-opt','no-new-privileges','--pids-limit','128','--memory','512m','--cpus','1',
       '--read-only','--tmpfs','/home/agent:uid=1000,gid=1000,mode=0700,size=64m',
       '--tmpfs','/tmp:mode=1777,size=32m','--entrypoint','/usr/bin/claude',
       'sha256:b08595b4ff44def60a66aa0bbd5614cdee71f2852088ed27f774af7f7d94e1f6','setup-token']
p = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
os.close(slave)
buffer=''; seen=set(); saved=False
print('Waiting for official Claude subscription authorization URL.',flush=True)
try:
    deadline=time.monotonic()+3600
    while time.monotonic()<deadline:
        ready,_,_=select.select([master,sys.stdin],[],[],1)
        if sys.stdin in ready:
            line=sys.stdin.readline()
            if line: os.write(master,(line.rstrip('\r\n')+'\r').encode())
        if master in ready:
            try: data=os.read(master,65536)
            except OSError: break
            if not data:break
            buffer += data.decode(errors='replace')
            if len(buffer)>1048576:raise RuntimeError('Unexpected login output size')
            plain=re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]','',buffer)
            for hint in ['Choose the text style', 'Dark mode', 'Light mode', 'Press Enter', 'Paste code here', 'Browser didn’t open', 'Login successful', 'OAuth error', 'trust this folder']:
                if hint.lower() in plain.lower() and hint not in seen:
                    seen.add(hint); print('LOGIN_PROMPT '+hint,flush=True)
            for url in re.findall(r'https://(?:claude\.ai|claude\.com|console\.anthropic\.com|platform\.claude\.com)/(?:cai/)?oauth/authorize[A-Za-z0-9%=&_:/?.-]+',plain):
                if url not in seen:
                    seen.add(url);print('AUTHORIZE_URL '+url,flush=True)
            match=re.search(r'(sk-ant-oat\d+-[A-Za-z0-9_-]{80,})(?=\s)',plain)
            if match and not saved:
                DEST.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                fd=os.open(DEST,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'w') as f:f.write(match.group(1)+'\n');f.flush();os.fsync(f.fileno())
                saved=True;print('CLOUD_LOGIN_SAVED: protected token file; value suppressed.',flush=True)
        if p.poll() is not None:break
    if not saved: print('Login incomplete. No token stored.',flush=True)
finally:
    if p.poll() is None:p.terminate()
    try:p.wait(timeout=10)
    except subprocess.TimeoutExpired:p.kill()
    os.close(master)
sys.exit(0 if saved else 1)
