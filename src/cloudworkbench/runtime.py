"""Restricted Docker execution. Network access is deliberately fail-closed."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import selectors
import tempfile
import time
from typing import Any


RUNTIME_ERROR_CODES = frozenset({
    "runtime_error", "docker_unavailable", "docker_timeout", "docker_daemon_unavailable",
    "docker_image_missing", "docker_mount_invalid", "docker_name_conflict",
    "docker_start_failed", "docker_command_failed",
})
DOCKER_OPERATIONS = frozenset({
    "create", "start", "inspect", "stop", "rm", "container_list", "network_create",
    "network_connect", "network_list", "network_inspect", "network_rm", "exec", "logs",
})


class RuntimeError(Exception):
    def __init__(self, message: str, *, code: str = "runtime_error", operation: str | None = None):
        super().__init__(message)
        self.code = code if code in RUNTIME_ERROR_CODES else "runtime_error"
        self.operation = operation if operation in DOCKER_OPERATIONS else None


def _docker_operation(args: list[str]) -> str | None:
    if len(args) >= 2 and args[0] in ("network", "container"):
        verb = "list" if args[1] == "ls" else args[1]
        operation = args[0] + "_" + verb
    else:
        operation = args[0] if args else None
    return operation if operation in DOCKER_OPERATIONS else None


def _docker_failure_code(stderr: str, operation: str | None) -> str:
    # Only fixed categories leave this function. Docker stderr can contain
    # argv, paths or supplied values, so it must never become an event message.
    detail = stderr[:8192].lower()
    if any(part in detail for part in ("cannot connect to the docker daemon", "is the docker daemon running", "error during connect", "permission denied while trying to connect")):
        return "docker_daemon_unavailable"
    if any(part in detail for part in ("no such image", "unable to find image", "manifest unknown", "pull access denied")):
        return "docker_image_missing"
    if any(part in detail for part in ("invalid mount config", "bind source path does not exist", "invalid volume specification")):
        return "docker_mount_invalid"
    if "already in use" in detail or "already exists" in detail:
        return "docker_name_conflict"
    return "docker_start_failed" if operation == "start" else "docker_command_failed"


_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}\Z")
_IMAGE = re.compile(r"(?:sha256:[0-9a-f]{64}|[^\s]+@sha256:[0-9a-f]{64})\Z")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
LABEL = "io.cloudworkbench.managed"


def _id(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise RuntimeError("invalid runtime identity")
    return value


def _path(value: Any) -> Path:
    p = Path(value)
    if not p.is_absolute() or any(c in str(p) for c in (",", "\n", "\x00")):
        raise RuntimeError("mount paths must be absolute and contain no Docker separators")
    if p != p.resolve():
        raise RuntimeError("symlink or non-canonical mount path")
    return p


class Runtime:
    def __init__(self, config: dict):
        self.config = dict(config)
        self.image = config.get("image", "")
        if not _IMAGE.fullmatch(self.image):
            raise RuntimeError("runtime image must use an immutable sha256 digest")
        self.root = _path(config["root"])
        self.docker = str(config.get("docker", "/usr/bin/docker"))
        if not Path(self.docker).is_absolute():
            raise RuntimeError("Docker executable must be absolute")
        self.uid = int(config.get("uid", 1000))
        self.gid = int(config.get("gid", self.uid))
        if self.uid <= 0 or self.gid <= 0:
            raise RuntimeError("jobs must use a nonroot numeric UID/GID")
        self.cpus = float(config.get("cpus", 2))
        self.memory_mib = int(config.get("memory_mib", 4096))
        self.pids = int(config.get("pids", 512))
        self.workspace_mib = int(config.get("workspace_mib", 20480))
        if not (0 < self.cpus <= 6 and 128 <= self.memory_mib <= 16384 and 16 <= self.pids <= 1024 and 64 <= self.workspace_mib <= 102400):
            raise RuntimeError("runtime limits outside supported range")
        self.test_workspace = config.get("test_path_workspace", False) is True
        self.owner = _id(config.get("owner", "primary"))
        self.network_enabled = config.get("network_enabled", False) is True
        self.egress_image = config.get("egress_image", self.image)
        self.allowed_domains = list(config.get("allowed_domains", []))
        if self.network_enabled:
            from .egress import hostname
            if not _IMAGE.fullmatch(self.egress_image) or not self.allowed_domains:
                raise RuntimeError("network profile requires immutable gateway image and domain allowlist")
            self.allowed_domains = [hostname(h) for h in self.allowed_domains]
        self.allowed_roots = [_path(p) for p in config.get("approved_mount_roots", [])]
        self.writable_roots = [_path(p) for p in config.get("approved_writable_mount_roots", [])]

    @property
    def release_ready(self) -> bool:
        return not self.test_workspace

    def _run(self, args: list[str], *, timeout: int = 30) -> str:
        operation = _docker_operation(args)
        try:
            result = subprocess.run([self.docker, *args], capture_output=True, text=True,
                                    timeout=timeout, check=False, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        except subprocess.TimeoutExpired:
            raise RuntimeError("Docker operation timed out; reconcile runtime identity",
                               code="docker_timeout", operation=operation) from None
        except OSError:
            raise RuntimeError("Docker executable unavailable", code="docker_unavailable",
                               operation=operation) from None
        if result.returncode:
            code = _docker_failure_code(result.stderr or "", operation)
            raise RuntimeError(f"Docker {operation or 'operation'} failed (exit {result.returncode}; {code})",
                               code=code, operation=operation)
        return result.stdout.strip()

    def make_workspace(self, session_id: str) -> Path:
        path = self.root / _id(session_id)
        if self.test_workspace:
            path.mkdir(parents=True, exist_ok=True)
            for child in ("work", "native"):
                (path / child).mkdir(exist_ok=True)
            return _path(path / "work")
        helper = self.config.get("workspace_helper")
        if helper:
            helper = _path(helper)
            try:
                subprocess.run(["/usr/bin/sudo", "-n", str(helper), session_id,
                                str(self.workspace_mib), str(self.uid), str(self.gid)],
                               check=True, capture_output=True, timeout=120)
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError("bounded workspace provisioning failed") from exc
        _path(path)
        try:
            result = subprocess.run(["/usr/bin/findmnt", "--json", "--mountpoint", str(path),
                                     "--output", "TARGET,SOURCE,FSTYPE,OPTIONS,SIZE", "--bytes"],
                                    check=True, capture_output=True, text=True, timeout=5)
            mounts = json.loads(result.stdout)["filesystems"]
            mount = mounts[0]
            opts = set(mount["options"].split(","))
            if len(mounts) != 1 or mount["target"] != str(path) or mount["fstype"] != "ext4" or not re.fullmatch(r"/dev/loop[0-9]+", mount["source"]) or not {"nosuid", "nodev"} <= opts:
                raise ValueError("invalid bounded filesystem")
            if not 0 < int(mount["size"]) <= self.workspace_mib * 1024 * 1024:
                raise ValueError("workspace exceeds configured size")
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("workspace hard quota unverified; runtime launch blocked") from exc
        if not (path / "work").is_dir() or not (path / "native").is_dir():
            raise RuntimeError("bounded workspace missing work/native directories")
        return path / "work"

    def native_state(self, session_id: str) -> Path:
        return self.make_workspace(session_id).parent / "native"

    def _mounts(self, mounts: list[dict] | None) -> list[str]:
        result = []
        destinations = set()
        for mount in mounts or []:
            source = _path(mount["source"])
            target = str(mount["target"])
            target_path = Path(target)
            if not target_path.is_absolute() or ".." in target_path.parts or any(c in target for c in (",", "\n", "\x00")) or target in destinations:
                raise RuntimeError("invalid or duplicate mount destination")
            if target in ("/", "/workspace") or target.startswith(("/proc", "/sys", "/dev", "/etc", "/bin", "/usr")):
                raise RuntimeError("protected mount destination")
            readonly = mount.get("readonly", True)
            if not isinstance(readonly, bool):
                raise RuntimeError("readonly must be boolean")
            roots = self.allowed_roots if readonly else self.writable_roots
            if not any(source.is_relative_to(root) for root in roots):
                raise RuntimeError("mount source not approved in worker configuration")
            if not source.exists() or (not source.is_file() and not source.is_dir()):
                raise RuntimeError("mount source must be an existing regular file or directory")
            if source.is_file() and source.stat().st_nlink != 1:
                raise RuntimeError("hardlinked mount source rejected")
            destinations.add(target)
            result += ["--mount", f"type=bind,src={source},dst={target}" + (",readonly" if readonly else "")]
        return result

    def launch(self, attempt_id: str, session_id: str, argv: list[str], env: dict,
               mounts: list[dict] | None = None, *, generation: int = 1) -> str:
        _id(attempt_id)
        _id(session_id)
        if not isinstance(generation, int) or generation < 1:
            raise RuntimeError("invalid fencing generation")
        if not argv or not all(isinstance(a, str) and "\x00" not in a for a in argv) or sum(len(a) for a in argv) > 131072:
            raise RuntimeError("invalid or oversized command argv")
        if not isinstance(env, dict) or any(not isinstance(k, str) or not _ENV.fullmatch(k) or not isinstance(v, str) or any(c in v for c in ("\x00", "\n", "\r")) for k, v in env.items()):
            raise RuntimeError("invalid runtime environment")
        if sum(len(k) + len(v) for k, v in env.items()) > 65536:
            raise RuntimeError("oversized runtime environment")
        workspace = self.make_workspace(session_id)
        mount_args = self._mounts(mounts)
        name = f"cwb2-{self.owner}-{attempt_id}"
        network = "none"
        if self.network_enabled:
            network, proxy = self._network(attempt_id, session_id, generation)
            env = {**env, "HTTPS_PROXY": proxy, "https_proxy": proxy,
                   "HTTP_PROXY": proxy, "http_proxy": proxy, "ALL_PROXY": proxy,
                   "NO_PROXY": "", "no_proxy": ""}
        args = ["create", "--name", name, "--label", f"{LABEL}=true",
                "--label", f"io.cloudworkbench.owner={self.owner}",
                "--label", f"io.cloudworkbench.attempt={attempt_id}",
                "--label", "io.cloudworkbench.role=job",
                "--label", f"io.cloudworkbench.generation={generation}",
                "--label", f"io.cloudworkbench.session={session_id}",
                "--label", f"io.cloudworkbench.image={self.image}",
                "--user", f"{self.uid}:{self.gid}", "--init", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "--network", network, "--dns", "127.0.0.1", "--ipc", "private", "--pids-limit", str(self.pids),
                "--cpus", str(self.cpus), "--memory", f"{self.memory_mib}m",
                "--memory-swap", f"{self.memory_mib}m", "--ulimit", "nofile=1024:1024",
                "--log-driver", "local", "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
                "--tmpfs", f"/tmp:rw,nosuid,nodev,size=256m,uid={self.uid},gid={self.gid},mode=1777",
                "--tmpfs", f"/home/agent:rw,nosuid,nodev,size=128m,uid={self.uid},gid={self.gid},mode=0700",
                "--workdir", "/workspace", "--mount", f"type=bind,src={workspace},dst=/workspace" + (",readonly" if self.config.get("workspace_readonly", False) else ""),
                *mount_args]
        # Env file prevents secrets appearing in the Docker client's process argv.
        with tempfile.NamedTemporaryFile(mode="w", prefix="cw-env-", delete=True) as envfile:
            os.chmod(envfile.name, 0o600)
            envfile.write("HOME=/home/agent\nTMPDIR=/tmp\n")
            for key, value in env.items():
                envfile.write(f"{key}={value}\n")
            envfile.flush()
            args += ["--env-file", envfile.name, "--entrypoint", argv[0], self.image, *argv[1:]]
            runtime_id = self._run(args)
        if not re.fullmatch(r"[a-f0-9]{64}", runtime_id):
            raise RuntimeError("unexpected Docker create response; reconcile by attempt label")
        self._run(["start", runtime_id])
        return runtime_id

    def _owned(self, runtime_id: str, expected_generation: int | None = None) -> dict | None:
        _id(runtime_id)
        output = self._run(["container", "ls", "--all", "--no-trunc", "--filter", f"id={runtime_id}",
                            "--filter", f"label={LABEL}=true", "--filter", f"label=io.cloudworkbench.owner={self.owner}",
                            "--format", "{{.ID}}"])
        if not output:
            return None
        ids = output.splitlines()
        if len(ids) != 1 or ids[0] != runtime_id:
            raise RuntimeError("runtime must be addressed by complete owned container ID")
        labels = json.loads(self._run(["inspect", "--format", "{{json .Config.Labels}}", runtime_id]))
        if not labels.get("io.cloudworkbench.attempt") or not labels.get("io.cloudworkbench.generation"):
            raise RuntimeError("runtime lacks attempt provenance or generation")
        if expected_generation is not None and str(expected_generation) != labels["io.cloudworkbench.generation"]:
            raise RuntimeError("stale runtime fencing generation")
        data = self._run(["inspect", "--format", "{{json .State}}", runtime_id])
        return json.loads(data)

    def status(self, runtime_id: str, expected_generation: int | None = None) -> dict:
        state = self._owned(runtime_id, expected_generation)
        if state is None:
            return {"state": "missing", "exit_code": None, "oom": False}
        execution_state = "running" if state.get("Running") else ("created" if state.get("Status") == "created" else "exited")
        return {"state": execution_state,
                "exit_code": state.get("ExitCode") if execution_state == "exited" else None,
                "oom": bool(state.get("OOMKilled")), "started_at": state.get("StartedAt")}

    def stop(self, runtime_id: str, *, expected_generation: int) -> None:
        state = self._owned(runtime_id, expected_generation)
        if state and state.get("Running"):
            self._run(["stop", "--time", "10", runtime_id], timeout=20)

    def cleanup(self, runtime_id: str, *, expected_generation: int) -> None:
        state = self._owned(runtime_id, expected_generation)
        if state and state.get("Running"):
            raise RuntimeError("refusing cleanup of live runtime")
        if state:
            labels = json.loads(self._run(["inspect", "--format", "{{json .Config.Labels}}", runtime_id]))
            self._run(["rm", runtime_id])
            if labels.get("io.cloudworkbench.role") == "job":
                self.cleanup_infrastructure(labels["io.cloudworkbench.attempt"], expected_generation=expected_generation)

    def list_owned(self) -> list[dict]:
        output = self._run(["container", "ls", "--all", "--no-trunc", "--filter", f"label={LABEL}=true",
                            "--filter", f"label=io.cloudworkbench.owner={self.owner}",
                            "--filter", "label=io.cloudworkbench.role=job", "--format", "{{json .}}"])
        results = []
        for line in output.splitlines():
            record = json.loads(line)
            labels = self._run(["inspect", "--format", "{{json .Config.Labels}}", record["ID"]])
            results.append({"runtime_id": record["ID"], "labels": json.loads(labels), **self.status(record["ID"])})
        return results

    def logs(self, runtime_id: str, max_bytes: int = 65536) -> bytes:
        if not 1 <= max_bytes <= 1048576:
            raise RuntimeError("log request must be between 1 byte and 1 MiB")
        if not self._owned(runtime_id):
            raise RuntimeError("unknown owned runtime")
        # Bound memory, time AND total bytes consumed from the daemon, even
        # when one log line is huge. This is a diagnostic tail, not event replay.
        process = None
        try:
            process = subprocess.Popen([self.docker, "logs", "--tail", "200", runtime_id],
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + 10
                consumed = 0
                tail = bytearray()
                while time.monotonic() < deadline and consumed < 8 * 1024 * 1024:
                    if not selector.select(timeout=min(0.5, max(0, deadline-time.monotonic()))):
                        if process.poll() is not None:
                            break
                        continue
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    consumed += len(chunk)
                    tail.extend(chunk)
                    if len(tail) > max_bytes:
                        del tail[:-max_bytes]
                return bytes(tail)
        except OSError as exc:
            raise RuntimeError("Docker logs unavailable") from exc
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                process.stdout.close()

    def _network(self, attempt_id: str, session_id: str, generation: int) -> tuple[str, str]:
        try:
            return self._create_network(attempt_id, session_id, generation)
        except Exception:
            # Discovery uses immutable labels, not the lost creation response.
            # If cleanup is uncertain, propagate that fact and retain the DB lease.
            try:
                self.cleanup_attempt(attempt_id, expected_generation=generation)
            except Exception as cleanup_error:
                raise RuntimeError("egress setup failed; attempt cleanup incomplete, reconcile before releasing reservation") from cleanup_error
            raise

    def _create_network(self, attempt_id: str, session_id: str, generation: int) -> tuple[str, str]:
        """Docker 28+ isolated bridge removes the host gateway address itself.

        The job has no external NIC or NET_ADMIN. The only dual-homed peer is
        a nonforwarding CONNECT gateway with a numeric internal bind address.
        Never downgrade 'isolated' if the Docker engine rejects the option.
        """
        prefix = f"cwb2-{self.owner}-{attempt_id}"
        labels = ["--label", f"{LABEL}=true", "--label", f"io.cloudworkbench.owner={self.owner}",
                  "--label", f"io.cloudworkbench.attempt={attempt_id}",
                  "--label", f"io.cloudworkbench.session={session_id}",
                  "--label", f"io.cloudworkbench.generation={generation}"]
        internal = prefix + "-internal"
        external = prefix + "-egress"
        self._run(["network", "create", *labels, "--internal", "--driver", "bridge",
                   "--opt", "com.docker.network.bridge.gateway_mode_ipv4=isolated",
                   "--opt", "com.docker.network.bridge.gateway_mode_ipv6=isolated", internal])
        config = json.loads(self._run(["network", "inspect", "--format", "{{json .}}", internal]))
        if not config.get("Internal") or config.get("Options", {}).get("com.docker.network.bridge.gateway_mode_ipv4") != "isolated":
            raise RuntimeError("Docker did not apply isolated internal network; refusing launch")
        self._run(["network", "create", *labels, "--driver", "bridge", external])
        # Create on internal network first so its address can be supplied to
        # the process without binding an externally reachable interface.
        # A fixed gateway address belongs only to this attempt network.
        subnet = config["IPAM"]["Config"][0]["Subnet"]
        import ipaddress
        net = ipaddress.ip_network(subnet)
        if net.version != 4 or net.num_addresses < 8:
            raise RuntimeError("unsupported internal gateway subnet")
        internal_ip = str(net.network_address + 2)
        args = ["create", "--name", prefix + "-proxy", *labels, "--label", "io.cloudworkbench.role=egress",
                "--network", internal, "--ip", internal_ip, "--user", "65534:65534",
                "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "--pids-limit", "64", "--memory", "128m", "--memory-swap", "128m", "--cpus", "0.25",
                "--sysctl", "net.ipv4.ip_forward=0", "--sysctl", "net.ipv6.conf.all.forwarding=0",
                "--log-driver", "local", "--log-opt", "max-size=1m", "--log-opt", "max-file=2",
                "--entrypoint", "python3", self.egress_image, "/opt/cloudworkbench/egress.py",
                "--listen", internal_ip]
        for domain in self.allowed_domains:
            args += ["--allow", domain]
        proxy_id = self._run(args)
        if not re.fullmatch(r"[a-f0-9]{64}", proxy_id):
            raise RuntimeError("unexpected proxy identity")
        self._run(["network", "connect", "--gw-priority", "1", external, proxy_id])
        self._run(["start", proxy_id])
        ready = False
        for _ in range(10):
            try:
                self._run(["exec", proxy_id, "python3", "-c",
                           "import socket,sys; socket.create_connection((sys.argv[1],8080),1).close()", internal_ip], timeout=3)
                ready = True
                break
            except RuntimeError:
                time.sleep(0.1)
        if not ready:
            raise RuntimeError("egress gateway readiness failed")
        return internal, f"http://{internal_ip}:8080"

    def _attempt_filters(self, attempt_id: str, expected_generation: int) -> list[str]:
        _id(attempt_id)
        if isinstance(expected_generation, bool) or not isinstance(expected_generation, int) or expected_generation < 1:
            raise RuntimeError("invalid fencing generation")
        return ["--filter", f"label={LABEL}=true", "--filter", f"label=io.cloudworkbench.owner={self.owner}",
                "--filter", f"label=io.cloudworkbench.attempt={attempt_id}",
                "--filter", f"label=io.cloudworkbench.generation={expected_generation}"]

    @staticmethod
    def _resource_ids(output: str) -> list[str]:
        values = output.splitlines()
        if len(values) > 16 or any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in values) or len(set(values)) != len(values):
            raise RuntimeError("unexpected attempt resource inventory; manual reconciliation required")
        return values

    def cleanup_attempt(self, attempt_id: str, *, expected_generation: int) -> None:
        """Retryable terminal cleanup, including a lost create/start response.

        Refuse every live main container. Repeated calls remain useful after
        any earlier job/proxy/network removal succeeded but a later one failed.
        """
        filters = self._attempt_filters(attempt_id, expected_generation)
        jobs = self._resource_ids(self._run(["container", "ls", "--all", "--no-trunc", *filters,
                                             "--filter", "label=io.cloudworkbench.role=job", "--format", "{{.ID}}"] ))
        for job in jobs:
            if self.status(job, expected_generation)["state"] == "running":
                raise RuntimeError("cannot clean attempt while job is running")
        for job in jobs:
            self.cleanup(job, expected_generation=expected_generation)
        self.cleanup_infrastructure(attempt_id, expected_generation=expected_generation)

    def cleanup_infrastructure(self, attempt_id: str, *, expected_generation: int) -> None:
        """Retryable cleanup independent of existence of a main container.

        The runner must call this before terminal release even when its saved
        runtime ID is missing: a prior cleanup may have removed the job and
        failed while retiring the proxy or its networks. No global prune.
        """
        filters = self._attempt_filters(attempt_id, expected_generation)
        live = self._run(["container", "ls", *filters, "--filter", "label=io.cloudworkbench.role=job", "--format", "{{.ID}}"])
        if live:
            raise RuntimeError("cannot remove infrastructure while attempt is running")
        proxies = self._resource_ids(self._run(["container", "ls", "--all", "--no-trunc", *filters,
                                               "--filter", "label=io.cloudworkbench.role=egress", "--format", "{{.ID}}"] ))
        for proxy_id in proxies:
            self.stop(proxy_id, expected_generation=expected_generation)
            # stop() must confirm the original generation before rm; a timeout
            # raises and leaves the reservation for a subsequent reconciliation.
            if self.status(proxy_id, expected_generation)["state"] == "running":
                raise RuntimeError("egress proxy did not stop")
            self._run(["rm", proxy_id])
        networks = self._resource_ids(self._run(["network", "ls", "--no-trunc", *filters, "--format", "{{.ID}}"] ))
        for network in networks:
            self._run(["network", "rm", network])
