#!/usr/bin/env python3
"""Rehearse an offline release on a fresh, network-isolated Colima daemon.

This host utility never uses the existing daemon for application operations.
It requires an already running Linux Colima VM and passwordless VM sudo.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path


class RehearsalError(RuntimeError):
    pass


STORAGE_DRIVERS = ("vfs", "overlay2")


def canonical_architecture(value: str) -> str:
    """Compare reported architecture names without relabelling another ISA."""
    aliases = {"amd64": "amd64", "x86_64": "amd64", "arm64": "arm64", "aarch64": "arm64"}
    if not isinstance(value, str) or value.lower() not in aliases:
        raise RehearsalError("unsupported_reported_architecture")
    return aliases[value.lower()]


def validate_expected_platform(runtime: dict, expected_platform: str | None) -> None:
    if expected_platform is None:
        return
    if expected_platform not in {"linux/amd64", "linux/arm64"}:
        raise ValueError("unsupported_expected_platform")
    if runtime.get("os") != "linux" or runtime.get("architecture") != expected_platform.split("/")[1]:
        raise RehearsalError("release_target_platform_mismatch")


def execution_platform(runtime: dict, application: dict, daemon: dict, vm: dict,
                       client: dict | None = None, *, version: str) -> dict:
    """Bind target/image, native VM diagnostics and actual application metadata.

    Cross-ISA host/guest execution is recorded as such. The facts here do not
    promote an emulated guest into a native bare-metal performance claim.
    """
    target = canonical_architecture(runtime.get("architecture"))
    if runtime.get("os") != "linux" or application.get("platform") != "Linux":
        raise RehearsalError("application_operating_system_mismatch")
    app_arch = canonical_architecture(application.get("architecture"))
    if app_arch != target or application.get("software_version") != version:
        raise RehearsalError("application_target_or_version_mismatch")
    if not isinstance(application.get("python"), str) or not re.fullmatch(r"3\.11\.\d+", application["python"]):
        raise RehearsalError("application_python_version_mismatch")
    if vm.get("system") != "Linux" or vm.get("kernel", {}).get("system") != "Linux":
        raise RehearsalError("vm_operating_system_mismatch")
    vm_arch = canonical_architecture(vm.get("machine"))
    kernel_arch = canonical_architecture(vm.get("kernel", {}).get("machine"))
    daemon_arch = canonical_architecture(daemon.get("Architecture"))
    if kernel_arch != vm_arch or daemon_arch != vm_arch or daemon.get("OSType") != "linux":
        raise RehearsalError("vm_kernel_or_daemon_architecture_mismatch")
    client = client or {"system": platform.system(), "machine": platform.machine()}
    try:
        client_arch = canonical_architecture(client.get("machine"))
    except RehearsalError:
        client_arch = None
    return {
        "target_platform": "linux/" + target,
        "client": client,
        "vm": vm,
        "daemon": {"os": daemon["OSType"], "architecture": daemon["Architecture"]},
        "application": {key: application[key] for key in ("platform", "architecture", "python", "software_version")},
        "cross_architecture_client_to_vm": None if client_arch is None else client_arch != vm_arch,
        "cross_architecture_vm_to_application": vm_arch != app_arch,
        "scope": "Observed client, in-VM kernel/daemon and application architecture; cross-architecture execution is not native bare-metal evidence.",
    }


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write_json(path: Path, value) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")


def isolated_environment(socket: Path, config: Path) -> dict[str, str]:
    env = dict(os.environ)
    for name in ("DOCKER_CONTEXT", "DOCKER_HOST", "FIN_AUDIT_DOCKER_CONTEXT", "DOCKER_TLS_VERIFY",
                 "DOCKER_CERT_PATH", "DOCKER_API_VERSION"):
        env.pop(name, None)
    env.update(DOCKER_HOST=f"unix://{socket}", DOCKER_CONFIG=str(config), PYTHONDONTWRITEBYTECODE="1")
    return env


def validate_container(value: dict, image_id: str) -> dict:
    config = value.get("HostConfig", {})
    expected = {"NetworkMode": "none", "ReadonlyRootfs": True, "Memory": 4 * 1024**3,
                "MemorySwap": 4 * 1024**3, "NanoCpus": 2_000_000_000, "PidsLimit": 64}
    if value.get("Image") != image_id or any(config.get(key) != item for key, item in expected.items()):
        raise RehearsalError("unexpected_application_container_limits")
    additions = config.get("CapAdd", [])
    if not isinstance(additions, list) or not all(isinstance(item, str) for item in additions):
        raise RehearsalError("unexpected_application_capabilities")
    normalized = {item.removeprefix("CAP_") for item in additions}
    if config.get("CapDrop") != ["ALL"] or normalized != {"SYS_CHROOT", "SETUID", "SETGID", "KILL"}:
        raise RehearsalError("unexpected_application_capabilities")
    if not any(item in {"no-new-privileges", "no-new-privileges=true"} for item in config.get("SecurityOpt", [])):
        raise RehearsalError("missing_no_new_privileges")
    if config.get("Privileged") or config.get("PidMode") or config.get("IpcMode") == "host":
        raise RehearsalError("unexpected_host_namespace")
    declared = config.get("Mounts")
    if (config.get("Binds") or config.get("VolumesFrom") or not isinstance(declared, list) or len(declared) != 1
            or not isinstance(declared[0], dict) or declared[0].get("Type") != "volume"
            or declared[0].get("Target") != "/output" or declared[0].get("Source")
            or declared[0].get("ReadOnly", False) is not False
            or declared[0].get("BindOptions") or not isinstance(declared[0].get("VolumeOptions"), dict)
            or set(declared[0]["VolumeOptions"]) != {"NoCopy"} or declared[0]["VolumeOptions"]["NoCopy"] is not True
            or config.get("Tmpfs") != {"/tmp": "rw,noexec,nosuid,size=256m"}):
        raise RehearsalError("unexpected_application_mount_declaration")
    mounts = value.get("Mounts")
    if not isinstance(mounts, list) or len(mounts) != 1 or not isinstance(mounts[0], dict):
        raise RehearsalError("unexpected_application_mounts")
    output = mounts[0]
    if (output.get("Type") != "volume" or output.get("Destination") != "/output"
            or output.get("Driver") != "local" or output.get("RW") is not True
            or not isinstance(output.get("Name"), str) or not re.fullmatch(r"[0-9a-f]{64}", output["Name"])):
        raise RehearsalError("unexpected_application_output_volume")
    return {"Id": value["Id"], "Image": value["Image"], "HostConfig": config,
            "Mounts": [{key: output[key] for key in ("Type", "Name", "Destination", "Driver", "RW")}],
            "Config": {key: value.get("Config", {}).get(key) for key in ("Cmd", "Entrypoint", "WorkingDir")},
            "State": value.get("State", {})}


def volume_inventory(before: list[str], observed: list[dict], after: list[str]) -> dict:
    """Normal verification must remove its anonymous volume without rescue."""
    names = sorted({mount["Name"] for container in observed for mount in container["Mounts"]})
    if before or after or len(names) != 1:
        raise RehearsalError("private_output_volume_not_cleaned")
    return {"scope": "private clean daemon anonymous output volumes", "before_verification": before,
            "observed_output_volumes": names, "after_verification": after, "passed": True}


def validate_loaded_image(installed: dict, runtime: dict) -> str:
    """Docker image stores may expose the OCI manifest or config digest."""
    identifier = installed.get("Id")
    if identifier not in {runtime.get("image_id"), runtime.get("config_id")}:
        raise RehearsalError("loaded_image_identity_mismatch")
    if (installed.get("Os") != runtime.get("os") or installed.get("Architecture") != runtime.get("architecture")
            or installed.get("RootFS", {}).get("Layers") != runtime.get("rootfs_diff_ids")):
        raise RehearsalError("loaded_image_contents_mismatch")
    return identifier


_REMOTE_START = r'''
import grp,json,os,pathlib,shutil,subprocess,sys
c=json.loads(sys.argv[1]); tag=c['tag']
if len(tag)!=32 or any(x not in '0123456789abcdef' for x in tag): raise RuntimeError('bad_tag')
storage_driver=c.get('storage_driver','vfs')
if storage_driver not in ('vfs','overlay2'): raise RuntimeError('unsupported_storage_driver')
root=pathlib.Path('/var/lib/fin-audit-clean-'+tag); run=pathlib.Path('/run/fin-audit-clean-'+tag)
if shutil.disk_usage('/var/lib').free < 2*1024**3: raise RuntimeError('insufficient_vm_root_disk')
if root.exists() or run.exists(): raise RuntimeError('owned_root_exists')
root.mkdir(mode=0o700); run.mkdir(mode=0o750); os.chown(run,0,grp.getgrnam('docker').gr_gid)
config={'data-root':str(root/'data'),'exec-root':str(run/'exec'),'pidfile':str(run/'dockerd.pid'),
 'containerd':str(run/'containerd.sock'),
 'hosts':['unix://'+str(run/'docker.sock')],'group':'docker','bridge':'none','iptables':False,
 'ip6tables':False,'ip-forward':False,'ip-masq':False,'userland-proxy':False,
 'storage-driver':storage_driver,'features':{'containerd-snapshotter':False},'shutdown-timeout':5}
(root/'daemon.json').write_text(json.dumps(config)); (root/'owner.json').write_text(json.dumps({'tag':tag}))
(root/'containerd.toml').write_text('version = 2\nroot = '+json.dumps(str(root/'containerd'))+'\nstate = '+json.dumps(str(run/'containerd'))+'\n[grpc]\naddress = '+json.dumps(str(run/'containerd.sock'))+'\n')
launcher="""import os,pathlib,subprocess,sys,time
root=pathlib.Path(sys.argv[1]);run=pathlib.Path(sys.argv[2])
log=open(root/'containerd.log','ab',buffering=0)
p=subprocess.Popen(['/usr/bin/containerd','--config',str(root/'containerd.toml')],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,close_fds=True)
(root/'containerd.pid').write_text(str(p.pid))
for _ in range(300):
 if (run/'containerd.sock').exists(): break
 if p.poll() is not None: raise RuntimeError('private_containerd_failed')
 time.sleep(.1)
else: raise RuntimeError('private_containerd_timeout')
os.execv('/usr/bin/dockerd',['/usr/bin/dockerd','--config-file',str(root/'daemon.json')])
"""
(root/'launch.py').write_text(launcher)
log=open(root/'daemon.log','ab',buffering=0)
p=subprocess.Popen(['/usr/bin/unshare','--net','/usr/bin/python3',str(root/'launch.py'),str(root),str(run)],
 stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,close_fds=True)
(root/'launcher.pid').write_text(str(p.pid))
print(json.dumps({'pid':p.pid,'root':str(root),'run':str(run),'socket':str(run/'docker.sock'),
 'host_netns':os.readlink('/proc/1/ns/net'),'disk_free_before':shutil.disk_usage('/var/lib').free}))
'''

_REMOTE_BASELINE = r'''
import json,pathlib,subprocess
command=['docker','--host','unix:///var/run/docker.sock']
info=json.loads(subprocess.check_output(command+['info','--format','{{json .}}']))
images=sorted(set(subprocess.check_output(command+['image','ls','-aq','--no-trunc'],text=True).splitlines()))
volumes=sorted(subprocess.check_output(command+['volume','ls','--quiet'],text=True).splitlines())
containerd=[]
for proc in pathlib.Path('/proc').iterdir():
 if not proc.name.isdecimal(): continue
 try: args=[s.decode() for s in (proc/'cmdline').read_bytes().split(b'\0') if s]
 except (OSError,UnicodeError): continue
 if args and args[0]=='/usr/bin/containerd' and len(args)==1: containerd.append({'pid':int(proc.name),'argv':args})
print(json.dumps({'daemon_id':info['ID'],'data_root':info['DockerRootDir'],
 'containerd_address':info.get('Containerd',{}).get('Address'),'images':images,'volumes':volumes,'global_containerd':containerd}))
'''

_REMOTE_INSPECT = r'''
import json,os,pathlib,platform,subprocess,sys,tomllib
c=json.loads(sys.argv[1]); root=pathlib.Path(c['root']); run=pathlib.Path(c['run']); pid=c['pid']
cmd=pathlib.Path('/proc/'+str(pid)+'/cmdline').read_bytes().split(b'\0')
if str(root/'daemon.json').encode() not in cmd: raise RuntimeError('daemon_identity_changed')
netns=os.readlink('/proc/'+str(pid)+'/ns/net')
addresses=json.loads(subprocess.check_output(['nsenter','--target',str(pid),'--net','ip','-j','address']))
routes=json.loads(subprocess.check_output(['nsenter','--target',str(pid),'--net','ip','-j','route','show','table','all']))
children=[]
for proc in pathlib.Path('/proc').iterdir():
 if not proc.name.isdecimal(): continue
 try: args=[s.decode() for s in (proc/'cmdline').read_bytes().split(b'\0') if s]
 except (OSError,UnicodeError): continue
 if args and pathlib.Path(args[0]).name=='containerd' and str(root/'containerd.toml') in args:
  conf=args[args.index('--config')+1] if '--config' in args else args[args.index('-c')+1]
  parsed=tomllib.loads(pathlib.Path(conf).read_text())
  children.append({'pid':int(proc.name),'argv':args,'config_path':conf,'root':parsed.get('root'),
                   'state':parsed.get('state'),'grpc_address':parsed.get('grpc',{}).get('address'),
                   'netns':os.readlink(str(proc/'ns/net'))})
print(json.dumps({'pid':pid,'argv':[a.decode() for a in cmd if a],'netns':netns,
 'host_netns':os.readlink('/proc/1/ns/net'),'addresses':addresses,'routes':routes,
 'containerd':children,'daemon_config':json.loads((root/'daemon.json').read_text()),
 'platform':{'system':platform.system(),'machine':platform.machine(),
  'kernel':{'system':subprocess.check_output(['uname','-s'],text=True).strip(),
            'machine':subprocess.check_output(['uname','-m'],text=True).strip(),
            'release':platform.release()}}}))
'''

_REMOTE_STOP = r'''
import json,os,pathlib,shutil,signal,subprocess,sys,time
def cleanup_owned_docker(command):
 def listing(arguments,kind):
  result=subprocess.run(command+arguments,capture_output=True,text=True,check=True,timeout=10)
  names=result.stdout.splitlines()
  if any(len(name)!=64 or any(c not in '0123456789abcdef' for c in name) for name in names):
   raise RuntimeError('unexpected_owned_'+kind+'_name')
  return sorted(set(names))
 before=listing(['volume','ls','--quiet'],'volume')
 identifiers=listing(['ps','-aq','--no-trunc'],'container')
 if identifiers: subprocess.run(command+['rm','-f','-v',*identifiers],capture_output=True,check=True,timeout=20)
 after_containers=listing(['volume','ls','--quiet'],'volume')
 if after_containers: subprocess.run(command+['volume','rm',*after_containers],capture_output=True,check=True,timeout=20)
 remaining=listing(['volume','ls','--quiet'],'volume')
 if remaining: raise RuntimeError('owned_volumes_remain')
 return {'before_container_removal':before,'after_container_removal':after_containers,
         'explicitly_removed':after_containers,'remaining_after_docker_cleanup':remaining,
         'docker_cleanup_completed':True}
c=json.loads(sys.argv[1]); tag=c['tag']; root=pathlib.Path('/var/lib/fin-audit-clean-'+tag); run=pathlib.Path('/run/fin-audit-clean-'+tag)
if len(tag)!=32 or any(x not in '0123456789abcdef' for x in tag): raise RuntimeError('bad_tag')
owner=json.loads((root/'owner.json').read_text())
if owner!= {'tag':tag}: raise RuntimeError('ownership_mismatch')
pid=int((root/'launcher.pid').read_text()); proc=pathlib.Path('/proc')/str(pid)
volume_cleanup={'before_container_removal':None,'after_container_removal':None,'explicitly_removed':[],
 'remaining_after_docker_cleanup':None,'docker_cleanup_completed':False}
if proc.exists():
 args=(proc/'cmdline').read_bytes().split(b'\0')
 if not any(str(root/name).encode() in args for name in ('daemon.json','launch.py')): raise RuntimeError('refuse_signal_changed_pid')
 # Remove only this daemon's containers before stopping its runtime. This also
 # handles an interrupted docker client without leaving a running application.
 command=['docker','--host','unix://'+str(run/'docker.sock')]
 try:
  volume_cleanup=cleanup_owned_docker(command)
 except (OSError,subprocess.SubprocessError,RuntimeError) as error:
  volume_cleanup['docker_cleanup_error_type']=type(error).__name__
 os.kill(pid,signal.SIGTERM)
 for i in range(100):
  if not proc.exists() or (proc/'stat').read_text().split()[2]=='Z': break
  time.sleep(.1)
 else:
  if not any(str(root/name).encode() in (proc/'cmdline').read_bytes().split(b'\0') for name in ('daemon.json','launch.py')): raise RuntimeError('refuse_changed_pid')
  os.killpg(pid,signal.SIGKILL)
child_file=root/'containerd.pid'
if child_file.exists():
 child=int(child_file.read_text()); child_proc=pathlib.Path('/proc')/str(child)
 if child_proc.exists() and (child_proc/'stat').read_text().split()[2]!='Z':
  if str(root/'containerd.toml').encode() not in (child_proc/'cmdline').read_bytes().split(b'\0'): raise RuntimeError('refuse_changed_containerd')
  os.kill(child,signal.SIGTERM)
  for _ in range(50):
   if not child_proc.exists() or (child_proc/'stat').read_text().split()[2]=='Z': break
   time.sleep(.1)
  else: os.kill(child,signal.SIGKILL)
survivors=[]
for p in pathlib.Path('/proc').iterdir():
 if not p.name.isdecimal(): continue
 try:
  args=(p/'cmdline').read_bytes().split(b'\0')
  if any(str(root).encode() in arg or str(run).encode() in arg for arg in args): survivors.append(int(p.name))
 except OSError: pass
if survivors: raise RuntimeError('owned_runtime_processes_remain:'+str(survivors))
log=(root/'daemon.log').read_text(errors='replace')[-200000:]
containerd_log=(root/'containerd.log').read_text(errors='replace')[-200000:] if (root/'containerd.log').exists() else ''
shutil.rmtree(root); shutil.rmtree(run)
volume_cleanup['owned_volume_root_removed']=not root.exists()
print(json.dumps({'stopped_pid':pid,'removed_owned_roots':[str(root),str(run)],'daemon_log':log,
 'containerd_log':containerd_log,'volume_cleanup':volume_cleanup}))
'''


class IsolatedDaemon:
    """A fresh VM daemon plus a private host-side Unix socket forward."""

    def __init__(self, profile: str = "genesis-fin-audit", *, storage_driver: str = "vfs"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", profile):
            raise ValueError("invalid_colima_profile")
        if storage_driver not in STORAGE_DRIVERS:
            raise ValueError("unsupported_storage_driver")
        self.profile, self.tag = profile, uuid.uuid4().hex
        self.storage_driver = storage_driver
        self.paths = None
        self.forward = None
        self.temporary = None
        self.cleanup = None

    def remote(self, source: str, value: dict, *, timeout=40) -> dict:
        command = ["colima", "ssh", "--profile", self.profile, "--", "sudo", "-n", "python3", "-c", source,
                   json.dumps(value, separators=(",", ":"))]
        completed = subprocess.run(command, text=True, capture_output=True, check=False, timeout=timeout)
        if completed.returncode:
            raise RehearsalError(f"vm_command_failed: {completed.stderr[-4000:]}")
        return json.loads(completed.stdout)

    def docker(self, *args: str, timeout=30) -> subprocess.CompletedProcess:
        return subprocess.run(["docker", *args], env=self.env, text=True, capture_output=True, timeout=timeout)

    def json(self, *args: str):
        result = self.docker(*args)
        if result.returncode:
            raise RehearsalError(f"isolated_docker_failed: {result.stderr[-2000:]}")
        return json.loads(result.stdout)

    def volume_names(self) -> list[str]:
        result = self.docker("volume", "ls", "--quiet")
        if result.returncode:
            raise RehearsalError("private_volume_inventory_unavailable")
        names = result.stdout.splitlines()
        if any(not re.fullmatch(r"[0-9a-f]{64}", name) for name in names) or len(set(names)) != len(names):
            raise RehearsalError("unexpected_private_volume_name")
        return sorted(names)

    def __enter__(self):
        try:
            self.existing_before = self.remote(_REMOTE_BASELINE, {})
            self.paths = self.remote(_REMOTE_START, {"tag": self.tag, "storage_driver": self.storage_driver})
            self.temporary = Path(tempfile.mkdtemp(prefix="fa-clean-", dir="/tmp"))
            self.temporary.chmod(0o700)
            config = self.temporary / "ssh.conf"
            result = subprocess.run(["colima", "ssh-config", self.profile], check=True, text=True, capture_output=True)
            config.write_text(result.stdout); config.chmod(0o600)
            (self.temporary / "docker-config").mkdir(mode=0o700)
            self.socket = self.temporary / "docker.sock"
            self.env = isolated_environment(self.socket, self.temporary / "docker-config")
            self.forward_log = (self.temporary / "ssh.log").open("wb")
            self.forward = subprocess.Popen(["ssh", "-F", str(config), "-o", "ControlMaster=no", "-o", "ControlPath=none",
                                            "-o", "ExitOnForwardFailure=yes", "-o", "StreamLocalBindUnlink=no",
                                            "-N", "-L", f"{self.socket}:{self.paths['socket']}", f"colima-{self.profile}"],
                                           stdin=subprocess.DEVNULL, stdout=self.forward_log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if self.forward.poll() is not None:
                    raise RehearsalError("ssh_forward_failed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RehearsalError("isolated_daemon_start_timeout")
                try:
                    result = self.docker("info", "--format", "{{json .}}", timeout=min(5, remaining))
                except subprocess.TimeoutExpired:
                    result = None
                if self.forward.poll() is not None:
                    raise RehearsalError("ssh_forward_failed")
                if time.monotonic() >= deadline:
                    raise RehearsalError("isolated_daemon_start_timeout")
                if result is not None and result.returncode == 0:
                    self.initial = json.loads(result.stdout)
                    break
                time.sleep(min(.2, max(0, deadline - time.monotonic())))
            else:
                raise RehearsalError("isolated_daemon_start_timeout")
            self.isolation = self.remote(_REMOTE_INSPECT, self.paths)
            self.initial_volumes = self.volume_names()
            self.validate_isolation()
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def validate_isolation(self):
        info, isolation, paths = self.initial, self.isolation, self.paths
        if info.get("DockerRootDir") != paths["root"] + "/data" or info.get("Images") != 0 or info.get("Containers") != 0:
            raise RehearsalError("daemon_not_fresh")
        if self.initial_volumes:
            raise RehearsalError("daemon_has_existing_volumes")
        if info.get("Driver") != self.storage_driver:
            raise RehearsalError("daemon_storage_driver_mismatch")
        if isolation["netns"] == isolation["host_netns"]:
            raise RehearsalError("daemon_not_isolated")
        if {a["ifname"] for a in isolation["addresses"]} != {"lo"} or isolation["routes"]:
            raise RehearsalError("daemon_namespace_has_network")
        children = isolation["containerd"]
        if len(children) != 1:
            raise RehearsalError("managed_containerd_not_identified")
        child = children[0]
        for field, prefix in (("root", paths["root"] + "/"), ("state", paths["run"] + "/"),
                              ("grpc_address", paths["run"] + "/"), ("config_path", paths["root"] + "/")):
            if not isinstance(child.get(field), str) or not child[field].startswith(prefix):
                raise RehearsalError("managed_containerd_not_private")
        if info.get("Containerd", {}).get("Address") != child["grpc_address"]:
            raise RehearsalError("containerd_address_mismatch")
        if child["netns"] != isolation["netns"]:
            raise RehearsalError("containerd_network_namespace_mismatch")

    def __exit__(self, exc_type, exc, traceback):
        try:
            if self.paths:
                self.cleanup = self.remote(_REMOTE_STOP, {"tag": self.tag}, timeout=55)
                self.existing_after = self.remote(_REMOTE_BASELINE, {})
                before, after = self.existing_before, self.existing_after
                preserved = all(before[key] == after[key] for key in
                                ("daemon_id", "data_root", "containerd_address", "global_containerd"))
                preserved &= set(before["images"]).issubset(after["images"])
                preserved &= set(before["volumes"]).issubset(after["volumes"])
                self.cleanup["existing_engine_preserved"] = preserved
                self.cleanup["existing_before"] = before
                self.cleanup["existing_after"] = after
                if not preserved:
                    raise RehearsalError("existing_engine_changed")
        finally:
            if self.forward:
                self.forward.terminate()
                try:
                    self.forward.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.forward.kill(); self.forward.wait()
            if hasattr(self, "forward_log"):
                self.forward_log.close()
            if self.temporary:
                shutil.rmtree(self.temporary)


def verify_run_receipts(output: Path) -> dict:
    complete = json.loads((output / "run" / "COMPLETE.json").read_text())
    if complete.get("completed") is not True or complete.get("passed") is not True or complete.get("stage") != 5:
        raise RehearsalError("stage5_receipt_incomplete")
    checksums = complete.get("sha256", {})
    required = {"summary.json", "run-manifest.json", "application/summary.json", "application/COMPLETE.json",
                "readiness/summary.json", "readiness/trajectories.jsonl", "showcase.html"}
    if set(checksums) != required:
        raise RehearsalError("stage5_receipt_inventory_mismatch")
    for name in required:
        path = output / "run" / name
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents) or digest(path) != checksums[name]:
            raise RehearsalError("stage5_receipt_hash_mismatch")
    metrics = json.loads((output / "cgroup-metrics.json").read_text())
    if metrics.get("passed") is not True or metrics.get("stage") != 5 or not metrics.get("cgroup_v2"):
        raise RehearsalError("verification_did_not_pass")
    values = metrics["values"]
    if values.get("memory.max") != 4 * 1024**3 or values.get("pids.max") != 64 or values.get("cpu.max") != "200000 100000":
        raise RehearsalError("measured_cgroup_limits_mismatch")
    if type(values.get("memory.peak")) is not int or not 0 < values["memory.peak"] <= values["memory.max"]:
        raise RehearsalError("missing_or_excess_peak_memory")
    suites = ET.parse(output / "tests.xml").getroot()
    counts = {name: sum(int(item.get(name, 0)) for item in suites.iter("testsuite"))
              for name in ("tests", "errors", "failures", "skipped")}
    if counts["tests"] == 0 or counts["errors"] or counts["failures"] or counts["skipped"]:
        raise RehearsalError("pytest_not_fully_verified")
    summary = json.loads((output / "run" / "summary.json").read_text())
    if (summary.get("passed") is not True or summary.get("stage") != 5 or summary.get("profile") != "secure"
            or not summary.get("checks") or any(value is not True for value in summary["checks"].values())):
        raise RehearsalError("stage5_summary_not_passed")
    application_manifest = json.loads((output / "run" / "run-manifest.json").read_text())
    return {"junit": counts, "metrics": metrics, "summary": summary, "application_manifest": application_manifest}


def run_clean_rehearsal(bundle: Path, output: Path, *, profile="genesis-fin-audit", timeout=1800,
                        expected_sha256: str | None = None, expected_platform: str | None = None,
                        storage_driver: str = "vfs") -> Path:
    from release_support import extract_release, verify_bundle

    bundle = bundle.resolve(strict=True)
    manifest = verify_bundle(bundle)
    validate_expected_platform(manifest["runtime"], expected_platform)
    if type(timeout) is not int or timeout <= 0:
        raise ValueError("invalid_rehearsal_timeout")
    if storage_driver not in STORAGE_DRIVERS:
        raise ValueError("unsupported_storage_driver")
    archive = bundle.parent / f"genesis-fin-audit-{manifest['software_version']}-linux-{manifest['runtime']['architecture']}.tar.gz"
    if expected_sha256 is None:
        checksum = archive.with_name(archive.name + ".sha256")
        if checksum.is_symlink() or checksum.stat().st_size > 512:
            raise RehearsalError("invalid_release_checksum_file")
        contents = checksum.read_text(encoding="ascii").strip()
        match = re.fullmatch(r"([0-9a-f]{64})  " + re.escape(archive.name), contents)
        if not match:
            raise RehearsalError("invalid_release_checksum_file")
        expected_sha256 = match[1]
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256) or digest(archive) != expected_sha256:
        raise RehearsalError("release_archive_checksum_mismatch")
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("rehearsal_output_exists")
    if any(parent.is_symlink() for parent in output.parents):
        raise RehearsalError("symlink_output_parent")
    output.parent.mkdir(parents=True, exist_ok=True)
    output = output.parent.resolve() / output.name
    if output.is_relative_to(bundle):
        raise RehearsalError("output_inside_release")
    temporary = Path(tempfile.mkdtemp(prefix=".clean-rehearsal-", dir=output.parent))
    started = time.monotonic()
    daemon = IsolatedDaemon(profile, storage_driver=storage_driver)
    try:
        transferred = temporary / "transfer" / "bundle"
        transferred.parent.mkdir(mode=0o700)
        extract_release(archive, transferred, expected_sha256)
        copied_manifest = verify_bundle(transferred)
        if copied_manifest != manifest:
            raise RehearsalError("transferred_manifest_changed")
        execution = temporary / "verification"
        with daemon:
            write_json(temporary / "initial-daemon.json", {"info": daemon.initial, "isolation": daemon.isolation,
                                                          "volumes": daemon.initial_volumes,
                                                          "requested_storage_driver": storage_driver,
                                                          "existing_engine": daemon.existing_before})
            runtime = manifest["runtime"]
            allowed_images = {runtime["image_id"], runtime["config_id"]}
            log = temporary / "verification.log"
            with log.open("wb") as handle:
                process = subprocess.Popen([sys.executable, "-B", str(transferred / "verify"), "--offline", "--output", str(execution),
                                            "--seed-file", str(temporary / "control-seed.json")],
                                           cwd=transferred, env=daemon.env, stdout=handle, stderr=subprocess.STDOUT,
                                           start_new_session=True)
                observed = {}
                deadline = time.monotonic() + timeout
                try:
                    while process.poll() is None:
                        if time.monotonic() > deadline:
                            raise RehearsalError("rehearsal_timeout")
                        ids = daemon.docker("ps", "-aq", "--no-trunc")
                        if ids.returncode:
                            raise RehearsalError("isolated_daemon_unavailable")
                        for identifier in ids.stdout.splitlines():
                            if identifier not in observed:
                                item = daemon.docker("inspect", identifier)
                                if item.returncode == 0:
                                    actual = json.loads(item.stdout)[0]
                                    if actual.get("Image") not in allowed_images:
                                        raise RehearsalError("unexpected_container_image")
                                    observed[identifier] = validate_container(actual, actual["Image"])
                        time.sleep(.1)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL); process.wait()
                if process.returncode:
                    raise RehearsalError(f"packaged_verify_failed_{process.returncode}")
            if len(observed) != 1:
                raise RehearsalError("application_container_not_observed")
            write_json(temporary / "volume-inventory.json",
                       volume_inventory(daemon.initial_volumes, list(observed.values()), daemon.volume_names()))
            loaded_id = next(iter(observed.values()))["Image"]
            installed = daemon.json("image", "inspect", loaded_id)[0]
            validate_loaded_image(installed, runtime)
            write_json(temporary / "application-containers.json", list(observed.values()))
            write_json(temporary / "loaded-image.json", {key: installed.get(key) for key in ("Id", "Architecture", "Os", "Size", "RepoTags", "RootFS")})
            verified = verify_run_receipts(execution)
            platform_proof = execution_platform(runtime, verified["application_manifest"], daemon.initial,
                                                daemon.isolation["platform"], version=manifest["software_version"])
            write_json(temporary / "execution-platform.json", platform_proof)
        write_json(temporary / "daemon-cleanup.json", daemon.cleanup)
        # This verifies that packaged verification did not modify source or add caches.
        verify_bundle(transferred)
        shutil.rmtree(temporary / "transfer")
        receipt = {"format_version": 1, "completed": True, "passed": True, "scope": "fresh-daemon-offline-transfer",
                   "runtime": manifest["runtime"], "software_version": manifest["software_version"],
                   "loaded_image_id": loaded_id,
                   "execution_platform": platform_proof,
                   "release_archive": {"filename": archive.name, "sha256": expected_sha256, "bytes": archive.stat().st_size},
                   "elapsed_seconds": time.monotonic() - started, "profile": profile,
                   "storage_driver": storage_driver,
                   "network": "Separate daemon net namespace: loopback only, no routes; application network=none.",
                   "junit": verified["junit"], "peak_cgroup_memory_bytes": verified["metrics"]["values"]["memory.peak"],
                   "sha256": {path.relative_to(temporary).as_posix(): digest(path) for path in sorted(temporary.rglob("*")) if path.is_file()}}
        write_json(temporary / "COMPLETE.json", receipt)
        output.mkdir(mode=0o700)
        try:
            os.rename(temporary, output)
        except BaseException:
            output.rmdir()
            raise
        return output
    except BaseException as error:
        if daemon.cleanup and not (temporary / "daemon-cleanup.json").exists():
            write_json(temporary / "daemon-cleanup.json", daemon.cleanup)
        write_json(temporary / "FAILED.json", {"completed": False, "passed": False, "error": str(error),
                                               "elapsed_seconds": time.monotonic() - started})
        # Keep a private failed receipt for diagnosis; never publish COMPLETE.
        raise RehearsalError(f"{error}; private diagnostic directory: {temporary}") from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", default="genesis-fin-audit")
    parser.add_argument("--storage-driver", choices=STORAGE_DRIVERS, default="vfs",
                        help="Storage driver for the private, initially empty verification daemon.")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--sha256", help="Trusted outer archive SHA-256; otherwise verify the adjacent checksum file.")
    parser.add_argument("--expected-platform", choices=["linux/amd64", "linux/arm64"],
                        help="Require this sealed target and verify actual application architecture.")
    args = parser.parse_args(argv)
    try:
        result = run_clean_rehearsal(args.bundle, args.output, profile=args.profile, timeout=args.timeout,
                                     expected_sha256=args.sha256, expected_platform=args.expected_platform,
                                     storage_driver=args.storage_driver)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Clean rehearsal failed: {error}", file=sys.stderr)
        return 1
    print(f"Clean offline rehearsal passed: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
