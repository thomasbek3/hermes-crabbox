"""Offline qualification driver; imports only clean source and temporary profile state."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
import subprocess
import contextlib

source=Path(sys.argv[1]).resolve()
plugin=Path(sys.argv[2]).resolve()

def blocked(*args,**kwargs):raise RuntimeError('network_disabled_in_qualification')
socket.socket.connect=blocked
socket.socket.connect_ex=blocked
socket.create_connection=blocked
socket.getaddrinfo=blocked

if len(sys.argv)==3:
    # Cleanup after the child interpreter and its background/atexit work have stopped.
    with tempfile.TemporaryDirectory(prefix='cwb-pstack-discovery-') as directory:
        run=subprocess.run([sys.executable,'-I',str(Path(__file__).resolve()),str(source),str(plugin),directory],
            env={'HOME':directory,'PATH':'/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,timeout=60)
        sys.stdout.buffer.write(run.stdout);sys.stderr.buffer.write(run.stderr)
        raise SystemExit(run.returncode)

with contextlib.nullcontext(sys.argv[3]) as directory:
    base=Path(directory).resolve()
    for name in ('home','profile','empty-bundled','work','codex','claude','config','cache','data'):(base/name).mkdir()
    os.environ.clear()
    os.environ.update({'HOME':str(base/'home'),'HERMES_HOME':str(base/'profile'),
        'HERMES_BUNDLED_PLUGINS':str(base/'empty-bundled'),'HERMES_ENABLE_PROJECT_PLUGINS':'0',
        'CODEX_HOME':str(base/'codex'),'CLAUDE_CONFIG_DIR':str(base/'claude'),
        'XDG_CONFIG_HOME':str(base/'config'),'XDG_CACHE_HOME':str(base/'cache'),'XDG_DATA_HOME':str(base/'data'),
        'PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1','HERMES_REDACT_SECRETS':'1'})
    denied_reads=[]
    def audit(event,args):
        if event=='open' and isinstance(args[0],(str,bytes)):
            path=Path(os.fsdecode(args[0])).absolute()
            if path.name in {'.env','.op.env','auth.json','.credentials.json','config.yaml'} and not path.is_relative_to(base):
                denied_reads.append(path.name)
                raise PermissionError('external_sensitive_state_forbidden')
    sys.addaudithook(audit)
    sys.dont_write_bytecode=True
    os.chdir(base/'work')
    sys.path.insert(0,str(source))
    shutil.copytree(plugin,base/'profile/plugins/pstack')
    for path in (base/'profile/plugins').rglob('*'):
        if path.is_file():path.chmod(0o444)
        elif path.is_dir():path.chmod(0o555)
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
    from cloudworkbench.hermes_adapter import build_launch
    from cloudworkbench.pstack_routing import BackendProfile
    plan=build_launch(BackendProfile('source-probe','anthropic','synthetic-model','anthropic_messages','high','anthropic','a'*64,('high',)))
    config=json.loads(plan.config_json)
    config.update({'plugins':{'enabled':['pstack'],'disabled':[]},'skills':{'auto_load':[]},'cwb_unknown_probe':{'value':'ignored-not-strict'}})
    (base/'profile/config.yaml').write_text(json.dumps(config));(base/'profile/config.yaml').chmod(0o444)
    from hermes_cli.config import load_config_readonly
    os.environ['HERMES_SAFE_MODE']='1'
    parsed=load_config_readonly()
    for key in ('model','agent','terminal','memory','approvals','fallback_model','delegation'):
        for child,value in config[key].items() if isinstance(config[key],dict) else []:
            assert parsed[key][child]==value,(key,child)
    assert parsed['fallback_model']==[]
    assert parsed['cwb_unknown_probe']==config['cwb_unknown_probe'] # Loader is permissive, NOT a strict validator.
    from tools.approval_context import _get_single_query_approval_mode
    assert _get_single_query_approval_mode()=='deny'
    from tools.memory_tool import get_builtin_memory_store_flags
    assert get_builtin_memory_store_flags(parsed)==(False,False)
    assert os.environ.get('HERMES_IGNORE_USER_CONFIG') is None
    os.environ.pop('HERMES_SAFE_MODE')
    from hermes_cli.plugins import get_plugin_manager
    manager=get_plugin_manager();manager.discover_and_load()
    enabled=[x['key'] for x in manager.list_plugins() if x['enabled']]
    assert enabled==['pstack'], enabled
    skill=manager.find_plugin_skill('pstack:poteto-mode')
    assert skill and skill.is_relative_to(base/'profile/plugins/pstack')
    assert len(manager.list_plugin_skills('pstack'))==50
    from agent.skill_commands import build_preloaded_skills_prompt
    prompt,loaded,missing=build_preloaded_skills_prompt(['pstack:poteto-mode'])
    assert loaded==['pstack:poteto-mode'] and not missing and prompt
    import providers
    registry=providers.list_providers()
    imported=[str(getattr(module,'__file__','')) for key,module in sys.modules.items() if key.startswith(('plugins.model_providers.','hermes_provider_plugin_'))]
    assert all(Path(p).resolve().is_relative_to(source) for p in imported if p)
    assert not (base/'profile/plugins/model-providers').exists()
    # Exercise actual opt-in gate with installed-entrypoint metadata containing an unapproved target.
    import importlib.metadata
    original=importlib.metadata.entry_points
    class ForbiddenEntry:
        name='unapproved-provider';group='hermes_agent.plugins';value='unapproved:register'
        def load(self):raise AssertionError('unapproved_entrypoint_loaded')
    class Entries(list):
        def select(self,**kwargs):return [x for x in self if x.group==kwargs.get('group')]
    importlib.metadata.entry_points=lambda:Entries([ForbiddenEntry()])
    providers._discover_entry_point_providers()
    importlib.metadata.entry_points=original
    assert not denied_reads
    source_modules={str(Path(m.__file__).resolve().relative_to(source)):hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest()
        for m in tuple(sys.modules.values()) if getattr(m,'__file__',None) and Path(m.__file__).is_file() and Path(m.__file__).resolve().is_relative_to(source)}
    receipt={'driver_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'hermes_commit':'3b0e392e5a6922034feccac5771041ac78467757',
        'pstack_commit':'204e77a7a011c4613dc9c4913a481d77cc0ebe54',
        'loaded_source_hashes':source_modules,'external_sensitive_read_attempts':len(denied_reads),
        'enabled_plugins':enabled,'registered_skill_count':len(manager.list_plugin_skills('pstack')),
        'explicit_preload':loaded,'preload_sha256':hashlib.sha256(prompt.encode()).hexdigest(),
        'poteto_skill_sha256':hashlib.sha256(skill.read_bytes()).hexdigest(),
        'provider_names':sorted(p.name for p in registry),'provider_modules_from_clean_source':True,
        'config_loader_preserves_generated_values':True,'unknown_config_keys':'preserved_not_rejected',
        'env_safe_mode_keeps_config':True,'approval_consumer':'deny','memory_consumer':[False,False],
        'unapproved_entrypoint_refused':True,'network_disabled':True,'provider_calls':0,'personal_state_read':False,
        'scope':'actual isolated plugin/provider discovery and explicit skill preload; not full CLI startup or inference'}
    print(json.dumps(receipt,sort_keys=True))
    # Restore our own temporary copies for portable TemporaryDirectory cleanup.
    for path in (base/'profile/plugins').rglob('*'):
        if path.is_dir():path.chmod(0o700)
