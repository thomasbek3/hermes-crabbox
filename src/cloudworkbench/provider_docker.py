"""Credential-blind Docker adapter for the controller-only D9 dispatcher.

No daemon ownership, service activation or account selection. Every mutating RPC
has a private durable intent/ack journal; an unanswered RPC remains unknown.
"""
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import time

from .egress import hostname
from .profile_binding import canonical_pinned_profile_digest
from .inference_transport import PinnedCLI
from .native_responses import NativeProfile
from .provider_dispatch import Resources, ObservedResource, OperationResolution
from .provider_leases import CleanupReceipt, LeaseError, _HEX


class DockerError(LeaseError):
    pass


class NoDockerRPC(DockerError):
    """Trusted command runner proves the CLI process was never spawned."""
    pass


@dataclass(frozen=True)
class DockerConfig:
    image: str
    profile_path: Path
    profile_sha256: str
    profile_digest: str
    credential_path: Path
    staging_root: Path
    allowed_domains: tuple[str, ...]
    docker: str = '/usr/bin/docker'
    uid: int = 958
    gid: int = 959
    inspector_id: str = 'provider-docker-v1'

    def __post_init__(self):
        if not isinstance(self.image,str) or not self.image.startswith('sha256:') or not _HEX.fullmatch(self.image[7:]):raise DockerError('invalid_image')
        if any(not isinstance(v,str) or not _HEX.fullmatch(v) for v in (self.profile_sha256,self.profile_digest)):raise DockerError('invalid_profile_binding')
        if type(self.uid) is not int or self.uid!=958 or type(self.gid) is not int or self.gid!=959:raise DockerError('invalid_provider_identity')
        if not isinstance(self.docker,str) or not Path(self.docker).is_absolute():raise DockerError('invalid_docker_path')
        for path in (self.profile_path,self.credential_path,self.staging_root):
            if type(path) is not type(Path()) or not path.is_absolute() or path.resolve()!=path or any(c in str(path) for c in ',\r\n\x00'):raise DockerError('invalid_mount_path')
        if type(self.allowed_domains) is not tuple or not 1<=len(self.allowed_domains)<=32 or len(set(self.allowed_domains))!=len(self.allowed_domains):raise DockerError('invalid_domains')
        try:
            if any(hostname(domain)!=domain for domain in self.allowed_domains):raise ValueError
        except (ValueError,TypeError,AttributeError):raise DockerError('invalid_domains') from None


# Static code, never interpolated with request input. Provider main consumes fd0.
_STDIN_WRAPPER = "import os,sys; f=os.open('/run/provider/request.json',os.O_RDONLY); os.dup2(f,0); os.close(f); os.execv(sys.executable,[sys.executable,'-m','cloudworkbench.provider_main',*sys.argv[1:]])"
_RESOURCE_KEYS=('provider_id','gateway_id','internal_network_id','external_network_id')


class BoundedDocker:
    """Argv-only, bounded stdout/stderr, no ambient Docker context or credentials."""
    def __init__(self,executable,config_dir):
        self.executable=executable;self.config_dir=Path(config_dir)
        self.config_dir.mkdir(mode=0o700,exist_ok=True)
        info=self.config_dir.lstat()
        if self.config_dir.resolve()!=self.config_dir or not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)&0o777!=0o700 or any(self.config_dir.iterdir()):raise DockerError('unsafe_docker_cli_config')

    def __call__(self,args,*,deadline,limit=1024*1024):
        if time.monotonic()>=deadline:raise NoDockerRPC('docker_timeout')
        try:
            process=subprocess.Popen([self.executable,'--host','unix:///var/run/docker.sock','--config',str(self.config_dir),*args],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},start_new_session=True)
        except OSError:raise NoDockerRPC('docker_unavailable') from None
        streams=selectors.DefaultSelector();data={};total=0
        try:
            for pipe in (process.stdout,process.stderr):
                os.set_blocking(pipe.fileno(),False);streams.register(pipe,selectors.EVENT_READ);data[pipe]=bytearray()
            while streams.get_map():
                if time.monotonic()>=deadline:raise DockerError('docker_timeout')
                for key,_ in streams.select(min(.1,max(0,deadline-time.monotonic()))):
                    part=os.read(key.fileobj.fileno(),32768)
                    if not part:streams.unregister(key.fileobj);continue
                    total+=len(part)
                    if total>limit:raise DockerError('docker_output_limit')
                    data[key.fileobj].extend(part)
            remaining=deadline-time.monotonic()
            if remaining<=0:raise DockerError('docker_timeout')
            try:code=process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:raise DockerError('docker_timeout') from None
            if code:raise DockerError('docker_command_failed')
            return bytes(data[process.stdout])
        finally:
            if process.poll() is None:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait(timeout=2)
            streams.close();process.stdout.close();process.stderr.close()


class ProviderDocker:
    def __init__(self,config: DockerConfig,*,cancel_check,command=None,
                 recovery_only=False, credential_target='/run/secrets/claude-token'):
        if type(config) is not DockerConfig or not callable(cancel_check):raise DockerError('trusted_adapter_config_required')
        if type(recovery_only) is not bool or credential_target not in ('/run/secrets/claude-token','/run/secrets/native-auth.json'):
            raise DockerError('invalid_recovery_configuration')
        self.config=config;self.cancel_check=cancel_check;self.command=command or BoundedDocker(config.docker,config.staging_root/'docker-cli')
        self.recovery_only=recovery_only
        self._credential_target=credential_target
        if not recovery_only:self._check_files()

    def _credential_path(self,spec):
        return self.config.credential_path

    @staticmethod
    def _check_parents(path):
        for parent in path.parents:
            info=parent.lstat()
            sticky_root=info.st_uid==0 and bool(info.st_mode&stat.S_ISVTX)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0,os.geteuid())
                    or (info.st_mode&0o022 and not sticky_root)):
                raise DockerError('unsafe_mount_parent')

    def _check_files(self):
        for path in (self.config.profile_path,self.config.credential_path,self.config.staging_root):self._check_parents(path)
        root=self.config.staging_root;info=root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)&0o777!=0o700:raise DockerError('unsafe_staging_root')
        profile=self.config.profile_path.lstat()
        if not stat.S_ISREG(profile.st_mode) or profile.st_uid!=0 or profile.st_mode&0o022 or profile.st_nlink!=1 or not 0<profile.st_size<=8192:raise DockerError('unsafe_profile')
        if not (profile.st_mode&0o004 or (profile.st_gid==self.config.gid and profile.st_mode&0o040)):raise DockerError('profile_unreadable_by_provider')
        body=self.config.profile_path.read_bytes()
        if hashlib.sha256(body).hexdigest()!=self.config.profile_sha256:raise DockerError('profile_file_changed')
        try:
            def unique_fields(pairs):
                value={}
                for key,item in pairs:
                    if key in value:raise ValueError
                    value[key]=item
                return value
            value=json.loads(body,object_pairs_hook=unique_fields)
            if type(value) is not dict or any(type(item) is not str for item in value.values()):raise ValueError
            if set(value)=={'transport','provider','model','effort'} and value['transport']=='native-responses-v1':
                profile=NativeProfile(value['provider'],value['model'],value['effort'])
                digest=profile.digest;target='/run/secrets/native-auth.json';credential_limit=64*1024
            elif set(value)=={'path','sha256','version','native_model','effort'}:
                value['path']=Path(value['path']);profile=PinnedCLI(**value)
                digest=canonical_pinned_profile_digest(profile);target='/run/secrets/claude-token';credential_limit=4096
            else:raise ValueError
            if digest!=self.config.profile_digest:raise ValueError
        except (ValueError,TypeError,KeyError,AttributeError,RecursionError):raise DockerError('profile_binding_mismatch') from None
        # Metadata only. Never open/read the credential in the controller.
        token=self.config.credential_path.lstat()
        if not stat.S_ISREG(token.st_mode) or token.st_nlink!=1 or token.st_mode&0o027 or not 1<=token.st_size<=credential_limit:raise DockerError('unsafe_credential_mount')
        if not ((token.st_uid==self.config.uid and token.st_mode&0o400) or (token.st_gid==self.config.gid and token.st_mode&0o040)):raise DockerError('credential_unreadable_by_provider')
        self._credential_target=target

    def _binding(self,spec):
        if spec.profile_digest!=self.config.profile_digest:raise DockerError('profile_binding_mismatch')
        return {'request_id':spec.lease.request_id,'reservation_id':spec.lease.reservation.reservation_id,'epoch':spec.lease.reservation.epoch,
                'attempt_id':spec.attempt_id,'generation':spec.generation,'launch_nonce':spec.launch_nonce,
                'payload_digest':spec.payload_digest,'profile_digest':spec.profile_digest}

    def _folder(self,spec):
        binding=self._binding(spec)
        if not isinstance(binding['launch_nonce'],str) or len(binding['launch_nonce'])!=32 or any(c not in '0123456789abcdef' for c in binding['launch_nonce']):raise DockerError('invalid_launch_nonce')
        folder=self.config.staging_root/binding['launch_nonce']
        try:info=folder.lstat()
        except FileNotFoundError:return folder
        else:
            if folder.resolve()!=folder or not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)&0o777!=0o700:raise DockerError('unsafe_request_staging')
        return folder

    def _save(self,spec,journal):
        folder=self._folder(spec);path=folder/'journal.json';temporary=folder/'journal.next'
        body=json.dumps(journal,sort_keys=True,separators=(',',':')).encode()
        if len(body)>65536:raise DockerError('journal_limit')
        try:stale=temporary.lstat()
        except FileNotFoundError:pass
        else:
            if not stat.S_ISREG(stale.st_mode) or stale.st_uid!=os.geteuid() or stat.S_IMODE(stale.st_mode)!=0o600 or stale.st_nlink!=1:raise DockerError('unsafe_journal_temporary')
            temporary.unlink()
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:
            with os.fdopen(fd,'wb',closefd=False) as stream:stream.write(body);stream.flush();os.fsync(fd)
        finally:os.close(fd)
        os.replace(temporary,path)
        fd=os.open(folder,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)

    def _load(self,spec):
        path=self._folder(spec)/'journal.json'
        info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1 or info.st_size>65536:raise DockerError('unsafe_journal')
        journal=json.loads(path.read_bytes())
        if journal.get('version')!=1:raise DockerError('journal_version_mismatch')
        if journal.get('binding')!=self._binding(spec):raise DockerError('journal_identity_mismatch')
        resources=journal.get('resources')
        if not isinstance(resources,dict) or set(resources)-set(_RESOURCE_KEYS) or any(not isinstance(v,str) or not _HEX.fullmatch(v) for v in resources.values()):raise DockerError('invalid_journal_resources')
        return journal

    def _run(self,args,deadline,limit=1024*1024):
        return self.command(args,deadline=min(deadline,time.monotonic()+5),limit=limit)

    def _mutation(self,spec,journal,args,deadline,*,key=None,removed_id=None):
        if journal['pending'] is not None:raise DockerError('docker_outcome_unknown')
        journal['pending']={'verb':args[0],'key':key};self._save(spec,journal)
        try:output=self._run(args,deadline)
        except NoDockerRPC:
            journal['pending']=None;self._save(spec,journal)
            raise
        if key:
            identity=output.decode('ascii').strip()
            if not _HEX.fullmatch(identity):raise DockerError('invalid_docker_identity')
            journal['resources'][key]=identity
        if args[0]=='start':journal['started'].append(args[-1])
        if removed_id:journal['removed'].append(removed_id)
        journal['pending']=None;self._save(spec,journal)
        return output

    def _json(self,args,deadline):
        try:
            value=json.loads(self._run(args,deadline))
            if not isinstance(value,list) or len(value)!=1 or not isinstance(value[0],dict):raise ValueError
            return value[0]
        except (ValueError,TypeError,KeyError,UnicodeError):raise DockerError('invalid_docker_inspection') from None

    @staticmethod
    def _labels(labels):
        return [item for key,value in sorted(labels.items()) for item in ('--label',key+'='+value)]

    def _container_args(self,spec,component,internal,ip=None):
        provider=component=='provider';memory='960m' if provider else '64m'
        args=['create','--name',spec.provider_name if provider else spec.gateway_name,*self._labels(spec.labels(component)),
            '--network',internal,'--user',f'{self.config.uid}:{self.config.gid}' if provider else '65534:65534',
            '--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true','--ipc','private',
            '--pids-limit','112' if provider else '16','--memory',memory,'--memory-swap',memory,'--cpus','0.4' if provider else '0.1',
            '--restart','no','--log-driver','local','--log-opt','max-size=1m','--log-opt','max-file=2',
            '--tmpfs','/tmp:rw,nosuid,nodev,noexec,size='+('256m' if provider else '16m')+',mode=1777',
            '--sysctl','net.ipv4.ip_forward=0','--sysctl','net.ipv6.conf.all.forwarding=0']
        if ip:args+=['--ip',ip]
        if provider:args+=['--dns','127.0.0.1']
        return args

    def create(self,spec,payload,*,deadline):
        if self.recovery_only:raise DockerError('recovery_adapter_cannot_execute')
        self._check_files()
        self.prepare_request_journal(spec,payload)
        return self.create_prepared(spec,payload,deadline=deadline)

    def prepare_request_journal(self,spec,payload):
        """Durable pre-RPC intent. No Docker calls or credential access."""
        if type(payload) is not bytes or len(payload)>256*1024 or hashlib.sha256(payload).hexdigest()!=spec.payload_digest:raise DockerError('invalid_payload_binding')
        self._check_parents(self.config.staging_root)
        root=self.config.staging_root.lstat()
        if not stat.S_ISDIR(root.st_mode) or root.st_uid!=os.geteuid() or stat.S_IMODE(root.st_mode)&0o777!=0o700:raise DockerError('unsafe_staging_root')
        folder=self._folder(spec);folder.mkdir(mode=0o700,exist_ok=False)
        journal={'version':1,'image_labels':{},'binding':self._binding(spec),'resources':{},'pending':None,'create_complete':False,'start_complete':False,'cleanup_complete':False,'removed':[],'started':[]}
        self._save(spec,journal)
        request=folder/'request.json';fd=os.open(request,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o640)
        try:
            os.fchown(fd,-1,self.config.gid);os.fchmod(fd,0o640)
            with os.fdopen(fd,'wb',closefd=False) as stream:stream.write(payload);stream.flush();os.fsync(fd)
        finally:os.close(fd)

    def create_prepared(self,spec,payload,*,deadline):
        if self.recovery_only:raise DockerError('recovery_adapter_cannot_execute')
        self._check_files()
        if type(payload) is not bytes or len(payload)>256*1024 or hashlib.sha256(payload).hexdigest()!=spec.payload_digest:raise DockerError('invalid_payload_binding')
        journal=self._load(spec);request=self._folder(spec)/'request.json'
        if (journal['resources'] or journal['pending'] is not None or journal['create_complete']
                or journal['start_complete'] or journal['cleanup_complete'] or journal['removed'] or journal['started']):
            raise DockerError('request_journal_not_pristine')
        info=request.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_nlink!=1
                or info.st_size!=len(payload) or info.st_mode&0o027 or request.read_bytes()!=payload):
            raise DockerError('request_payload_changed')
        try:
            engine=json.loads(self._run(['version','--format','{{json .}}'],deadline))
            for side in ('Client','Server'):
                version=engine[side]['Version']
                if not isinstance(version,str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[+][A-Za-z0-9.-]+)?',version) or int(version.split('.')[0])<28:raise ValueError
        except (ValueError,TypeError,KeyError):raise DockerError('unsupported_docker_engine') from None
        journal['engine_versions']={side:engine[side]['Version'] for side in ('Client','Server')};self._save(spec,journal)
        image=self._json(['image','inspect',self.config.image],deadline)
        image_labels=image.get('Config',{}).get('Labels') or {}
        if image.get('Id')!=self.config.image or not isinstance(image_labels,dict) or any(not isinstance(k,str) or not isinstance(v,str) or k.startswith('io.cloudworkbench.provider-') or k=='cloudd' for k,v in image_labels.items()):raise DockerError('unsafe_image_labels')
        journal['image_labels']=image_labels;self._save(spec,journal)
        self._mutation(spec,journal,['network','create','--driver','bridge','--internal',*self._labels(spec.network_labels('internal')),
            '--opt','com.docker.network.bridge.gateway_mode_ipv4=isolated','--opt','com.docker.network.bridge.gateway_mode_ipv6=isolated',spec.internal_network_name],deadline,key='internal_network_id')
        internal=journal['resources']['internal_network_id']
        net=self._json(['network','inspect',internal],deadline)
        if not net.get('Internal') or net.get('Options',{}).get('com.docker.network.bridge.gateway_mode_ipv4')!='isolated':raise DockerError('network_isolation_unconfirmed')
        try:
            subnet=ipaddress.ip_network(net['IPAM']['Config'][0]['Subnet'])
            if subnet.version!=4 or not subnet.is_private or subnet.num_addresses<8:raise ValueError
            ip=str(subnet.network_address+2)
        except (ValueError,TypeError,KeyError,IndexError):raise DockerError('invalid_internal_subnet') from None
        journal['gateway_ip']=ip;self._save(spec,journal)
        self._mutation(spec,journal,['network','create','--driver','bridge',*self._labels(spec.network_labels('external')),spec.external_network_name],deadline,key='external_network_id')
        args=self._container_args(spec,'gateway',internal,ip)
        args+=['--entrypoint','/opt/hermes/venv/bin/python',self.config.image,'-m','cloudworkbench.egress','--listen',ip]
        for domain in self.config.allowed_domains:args+=['--allow',domain]
        self._mutation(spec,journal,args,deadline,key='gateway_id')
        self._mutation(spec,journal,['network','connect','--gw-priority','1',journal['resources']['external_network_id'],journal['resources']['gateway_id']],deadline)
        args=self._container_args(spec,'provider',internal)
        for source,target in ((self.config.profile_path,'/run/provider/profile.json'),(self._credential_path(spec),self._credential_target),(request,'/run/provider/request.json')):
            args+=['--mount',f'type=bind,src={source},dst={target},readonly']
        args+=['--entrypoint','/opt/hermes/venv/bin/python',self.config.image,'-c',_STDIN_WRAPPER,'--profile','/run/provider/profile.json','--proxy','http://'+ip+':8080']
        self._mutation(spec,journal,args,deadline,key='provider_id')
        resources=Resources(*(journal['resources'][key] for key in _RESOURCE_KEYS))
        self.inspect(spec,resources,deadline=deadline)
        journal['create_complete']=True;self._save(spec,journal)
        return resources

    def inspect(self,spec,resources,*,deadline):
        journal=self._load(spec)
        if type(resources) is not Resources or any(journal['resources'].get(key)!=getattr(resources,key) for key in _RESOURCE_KEYS):raise DockerError('resource_binding_mismatch')
        result=[]
        for component,identity,name in (('provider',resources.provider_id,spec.provider_name),('gateway',resources.gateway_id,spec.gateway_name)):
            obj=self._json(['inspect',identity],deadline);host=obj.get('HostConfig',{});config=obj.get('Config',{})
            expected_user=f'{self.config.uid}:{self.config.gid}' if component=='provider' else '65534:65534'
            if (obj.get('Id')!=identity or obj.get('Name')!='/'+name or config.get('Labels')!={**journal['image_labels'],**spec.labels(component)} or obj.get('Image')!=self.config.image
                    or config.get('User')!=expected_user or not host.get('ReadonlyRootfs') or host.get('Privileged') or host.get('CapAdd')
                    or host.get('CapDrop')!=['ALL'] or not {'no-new-privileges','no-new-privileges:true'} & set(host.get('SecurityOpt',[])) or host.get('NetworkMode')!=resources.internal_network_id
                    or host.get('PortBindings') or host.get('PidMode')=='host' or host.get('IpcMode')!='private'):
                raise DockerError('container_policy_mismatch')
            if host.get('Sysctls')!={'net.ipv4.ip_forward':'0','net.ipv6.conf.all.forwarding':'0'}:raise DockerError('container_forwarding_mismatch')
            expected_networks={spec.internal_network_name:resources.internal_network_id}
            if component=='gateway':expected_networks[spec.external_network_name]=resources.external_network_id
            attached=obj.get('NetworkSettings',{}).get('Networks',{})
            matched=set()
            for key,network in attached.items():
                names=[name for name,identity in expected_networks.items() if key in (name,identity) and network.get('NetworkID') in ('',identity)]
                if len(names)!=1 or names[0] in matched:raise DockerError('container_network_mismatch')
                matched.add(names[0])
            if matched!=set(expected_networks):raise DockerError('container_network_mismatch')
            expected_tmpfs='256m' if component=='provider' else '16m'
            tmpfs=host.get('Tmpfs',{})
            if not isinstance(tmpfs,dict) or set(tmpfs)!={'/tmp'} or not isinstance(tmpfs['/tmp'],str) or set(tmpfs['/tmp'].split(','))!={'rw','nosuid','nodev','noexec','size='+expected_tmpfs,'mode=1777'}:raise DockerError('container_tmpfs_mismatch')
            if host.get('LogConfig')!={'Type':'local','Config':{'max-size':'1m','max-file':'2'}}:raise DockerError('container_logging_mismatch')
            if component=='provider' and host.get('Dns')!=['127.0.0.1']:raise DockerError('container_dns_mismatch')
            if host.get('RestartPolicy')!={'Name':'no','MaximumRetryCount':0}:raise DockerError('container_restart_mismatch')
            command=['-c',_STDIN_WRAPPER,'--profile','/run/provider/profile.json','--proxy','http://'+journal['gateway_ip']+':8080'] if component=='provider' else ['-m','cloudworkbench.egress','--listen',journal['gateway_ip'],*[part for domain in self.config.allowed_domains for part in ('--allow',domain)]]
            if config.get('Entrypoint')!=['/opt/hermes/venv/bin/python'] or config.get('Cmd')!=command:raise DockerError('container_command_mismatch')
            expected_memory=(960 if component=='provider' else 64)*1024*1024
            if (host.get('Memory')!=expected_memory or host.get('MemorySwap')!=expected_memory or host.get('PidsLimit')!=(112 if component=='provider' else 16)
                    or host.get('NanoCpus')!=(400000000 if component=='provider' else 100000000)):
                raise DockerError('container_limits_mismatch')
            expected_mounts={} if component=='gateway' else {'/run/provider/profile.json':str(self.config.profile_path),self._credential_target:str(self._credential_path(spec)),'/run/provider/request.json':str(self._folder(spec)/'request.json')}
            mounts=obj.get('Mounts',[])
            binds=[m for m in mounts if m.get('Type')!='tmpfs']
            if len(binds)!=len(expected_mounts) or {m.get('Destination') for m in binds}!=set(expected_mounts) or any(m.get('RW') or m.get('Type')!='bind' or expected_mounts.get(m.get('Destination'))!=m.get('Source') for m in binds):raise DockerError('container_mount_mismatch')
            result.append(ObservedResource(identity,name,spec.labels(component),obj['State']['Status']))
        expected_peers={resources.provider_id,resources.gateway_id}
        for role,identity,name in (('internal',resources.internal_network_id,spec.internal_network_name),('external',resources.external_network_id,spec.external_network_name)):
            obj=self._json(['network','inspect',identity],deadline)
            if (obj.get('Id')!=identity or obj.get('Name')!=name or obj.get('Labels')!=spec.network_labels(role) or obj.get('Driver')!='bridge'
                    or bool(obj.get('Internal'))!=(role=='internal') or set(obj.get('Containers',{}))-(expected_peers if role=='internal' else {resources.gateway_id})):
                raise DockerError('network_policy_mismatch')
            if role=='internal' and any(obj.get('Options',{}).get('com.docker.network.bridge.gateway_mode_'+version)!='isolated' for version in ('ipv4','ipv6')):raise DockerError('network_isolation_unconfirmed')
            result.append(ObservedResource(identity,name,obj['Labels'],'created'))
        return tuple(result)

    def start(self,spec,resources,*,deadline):
        if self.recovery_only:raise DockerError('recovery_adapter_cannot_execute')
        self._check_files()
        self.inspect(spec,resources,deadline=deadline);journal=self._load(spec)
        if not journal['create_complete'] or journal['start_complete']:raise DockerError('invalid_start_state')
        if self.cancel_check(spec):raise DockerError('request_cancelled_before_start')
        self._mutation(spec,journal,['start',resources.gateway_id],deadline)
        # Explicit readiness command has no credential mount or request data.
        ready_deadline=min(deadline,time.monotonic()+10)
        while True:
            try:
                self._run(['exec',resources.gateway_id,'/bin/bash','-c','exec 3<>/dev/tcp/"$1"/8080','gateway-ready',journal['gateway_ip']],ready_deadline)
                break
            except DockerError:
                status=self._json(['inspect',resources.gateway_id],ready_deadline)['State']
                if status.get('Status')=='exited':raise DockerError('gateway_exited') from None
                if time.monotonic()>=ready_deadline:raise DockerError('gateway_not_ready') from None
                time.sleep(.1)
        if self.cancel_check(spec):raise DockerError('request_cancelled_before_start')
        self._mutation(spec,journal,['start',resources.provider_id],deadline)
        journal['start_complete']=True;self._save(spec,journal)
        while time.monotonic()<deadline:
            if self.cancel_check(spec):
                self._mutation(spec,journal,['stop','--time','1',resources.provider_id],time.monotonic()+5);raise DockerError('provider_cancelled')
            try:state=self._json(['inspect',resources.provider_id],deadline)['State']
            except DockerError:
                if time.monotonic()>=deadline:break
                raise
            if state.get('Status')=='exited':return
            if state.get('Status')!='running':raise DockerError('provider_state_unknown')
            time.sleep(min(.1,max(0,deadline-time.monotonic())))
        self._mutation(spec,journal,['stop','--time','1',resources.provider_id],time.monotonic()+5)
        raise DockerError('provider_execution_timeout')

    def collect(self,spec,resources,*,limit,deadline):
        self.inspect(spec,resources,deadline=deadline)
        obj=self._json(['inspect',resources.provider_id],deadline)
        if obj['State'].get('Status')!='exited':raise DockerError('provider_not_exited')
        response=self._run(['logs',resources.provider_id],deadline,limit=limit)
        if len(response)>limit:raise DockerError('provider_response_limit')
        # Main serializes structured errors too. The protocol layer owns interpretation.
        return response

    def _cleanup_known(self,spec,journal,deadline):
        if journal['pending'] is not None:raise DockerError('docker_outcome_unknown')
        names={'provider_id':spec.provider_name,'gateway_id':spec.gateway_name,'internal_network_id':spec.internal_network_name,'external_network_id':spec.external_network_name}
        # Previously removed IDs require durable successful RPC acknowledgments.
        # Missing objects without such acknowledgment still fail closed.
        for key,identity in journal['resources'].items():
            if identity in journal['removed']:continue
            network='network' in key
            obj=self._json(['network','inspect',identity] if network else ['inspect',identity],deadline)
            expected=spec.network_labels('internal' if key=='internal_network_id' else 'external') if network else {**journal['image_labels'],**spec.labels('provider' if key=='provider_id' else 'gateway')}
            if obj.get('Id')!=identity or obj.get('Name')!=('' if network else '/')+names[key] or (obj.get('Labels') if network else obj.get('Config',{}).get('Labels'))!=expected:raise DockerError('cleanup_ownership_mismatch')
            if not network and obj.get('Image')!=self.config.image:raise DockerError('cleanup_image_mismatch')
        for key in _RESOURCE_KEYS:
            identity=journal['resources'].get(key)
            if identity is None or identity in journal['removed']:continue
            if 'network' not in key:
                self._mutation(spec,journal,['stop','--time','1',identity],deadline)
                self._mutation(spec,journal,['rm',identity],deadline,removed_id=identity)
            else:self._mutation(spec,journal,['network','rm',identity],deadline,removed_id=identity)
        journal['cleanup_complete']=True;self._save(spec,journal)

    def resolve(self,spec,row,*,deadline):
        journal=self._load(spec)
        if journal['pending'] is not None:return None
        operation=row['operation']
        if operation=='create' and journal['create_complete']:
            resources=Resources(*(journal['resources'][key] for key in _RESOURCE_KEYS));self.inspect(spec,resources,deadline=deadline);outcome='completed'
        elif operation=='create' and not journal['create_complete']:
            self._cleanup_known(spec,journal,deadline);resources=None;outcome='settled_cleaned'
        elif operation=='start' and journal['create_complete'] and not journal['started']:
            resources=Resources(*(journal['resources'][key] for key in _RESOURCE_KEYS));self.inspect(spec,resources,deadline=deadline);outcome='definitive_no_effect'
        elif operation=='start' and journal['start_complete']:
            resources=Resources(*(journal['resources'][key] for key in _RESOURCE_KEYS));self.inspect(spec,resources,deadline=deadline);outcome='completed'
        elif operation in ('start','cleanup') and journal['create_complete']:
            self._cleanup_known(spec,journal,deadline)
            resources=Resources(*(journal['resources'][key] for key in _RESOURCE_KEYS));outcome='settled_cleaned'
        else:return None
        digest=hashlib.sha256(json.dumps(journal,sort_keys=True).encode()).hexdigest()
        return OperationResolution(spec.lease.request_id,spec.launch_nonce,row['version'],operation,outcome,resources,digest)

    def cleanup(self,target):
        # Caller holds the account flock. Reconstruct only from the exact bound target.
        from .provider_dispatch import DispatchSpec
        from .provider_leases import RequestLease
        evidence=[]
        for binding in target.dispatches:
            row=dict(binding)
            lease=RequestLease(target.reservation,row['request_id'],target.grant_id or 'owner-cleanup','inference',0)
            spec=DispatchSpec(lease,row['attempt_id'],row['generation'],'',row['payload_digest'],row['profile_digest'],row['launch_nonce'],row['provider_name'],row['gateway_name'],row['internal_network_name'],row['external_network_name'])
            if all(row.get(key) is None for key in _RESOURCE_KEYS) and not self._folder(spec).exists():
                evidence.append({'binding':self._binding(spec),'scope':'admitted-no-create'});continue
            journal=self._load(spec)
            bound={key:row.get(key) for key in _RESOURCE_KEYS if row.get(key) is not None}
            if bound and bound!=journal['resources']:raise DockerError('cleanup_binding_mismatch')
            if not bound and not journal['cleanup_complete']:raise DockerError('unbound_cleanup_requires_reconciliation')
            if journal['pending'] is not None:raise DockerError('docker_outcome_unknown')
            self._cleanup_known(spec,journal,time.monotonic()+20)
            if set(journal['removed'])!=set(journal['resources'].values()):raise DockerError('cleanup_ack_incomplete')
            request=self._folder(spec)/'request.json'
            if request.exists():
                info=request.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_nlink!=1:raise DockerError('unsafe_request_cleanup')
                request.unlink()
            evidence.append(journal)
        if not target.dispatches:
            # Only the trusted lease layer invokes this callback after freezing
            # the exact owner and proving no outstanding requests in its ledger.
            if target.scope!='owner' or target.request_id is not None:raise DockerError('owner_scope_unconfirmed')
            evidence.append({'scope':'logical-owner-with-no-outstanding-requests','reservation':target.reservation.reservation_id,'epoch':target.reservation.epoch})
        digest=hashlib.sha256(json.dumps(evidence,sort_keys=True).encode()).hexdigest()
        return CleanupReceipt(target,self.config.inspector_id,'terminated',time.time(),digest)
