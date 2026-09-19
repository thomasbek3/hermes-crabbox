from pathlib import Path
import os,secrets
from cloudworkbench.store import Store
os.umask(0o007)
root=Path('/var/lib/cloud-workbench/control')
token=root/'client.token'
store=Store(root/'state.db',shared_group=True)
if token.exists():
    value=token.read_text().strip()
else:
    value=secrets.token_urlsafe(32)
    with token.open('x') as f:f.write(value+'\n')
    token.chmod(0o600)
if not store.authenticate(value):
    store.add_client('Thomas cloud2',value,['submit','observe','retrieve','cancel'],['sample-web'])
print('Named client ready; token value suppressed.')
