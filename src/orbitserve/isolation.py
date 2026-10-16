"""Fail-closed Linux namespace launcher; each child sees only its own data mount.

The outer unshare creates user+network namespaces without configuring loopback
(the host denies NETLINK_ROUTE). Bubblewrap then creates mount/PID/IPC/UTS
namespaces. No host /proc, home, workspace, data roots, or network is mounted.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def sandbox_command(node_root: Path, module: str = "orbitserve.worker_node", probe: str | None = None, source_root: Path | None = None) -> list[str]:
    if not shutil.which("unshare") or not shutil.which("bwrap"):
        raise RuntimeError("BLOCKED: unshare and bubblewrap are required for enforced isolation")
    package_src = source_root or Path(__file__).resolve().parents[1]
    venv = Path(sys.prefix).resolve()
    executable = Path(sys.executable).absolute()
    base = Path(sys.base_prefix).resolve()
    cmd = ["unshare", "--user", "--map-root-user", "--net", "bwrap",
           "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc", "--unshare-uts"]
    for p in [Path("/usr"), Path("/lib"), Path("/lib64"), base]:
        if p.exists():
            cmd += ["--ro-bind", str(p), str(p)]
    if venv != base:
        cmd += ["--ro-bind", str(venv), str(venv)]
    cmd += ["--ro-bind", str(package_src), "/app", "--bind", str(node_root.resolve()), "/node",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/etc",
            "--chdir", "/node", "--clearenv", "--setenv", "PATH", f"{venv}/bin:/usr/bin:/bin",
            "--setenv", "PYTHONPATH", "/app", "--setenv", "HOME", "/node",
            "--setenv", "TMPDIR", "/tmp", "--setenv", "MPLCONFIGDIR", "/node/.mplconfig",
            "--setenv", "OPENBLAS_NUM_THREADS", "1", "--setenv", "OMP_NUM_THREADS", "1",
            "--setenv", "PYTHONDONTWRITEBYTECODE", "1", "--", str(executable), "-u"]
    return cmd + (["-c", probe] if probe is not None else ["-m", module])


def isolation_probe(node_root: Path, source_root: Path | None = None) -> dict:
    """Execute real negative read/write/network probes in the exact worker sandbox."""
    code = '''import json,os,socket,pathlib
r={"pid":os.getpid(),"root_entries":os.listdir("/"),"node_writable":False}
p=pathlib.Path("/node/isolation_probe.txt");p.write_text("own root writable");r["node_writable"]=p.read_text()=="own root writable"
r["host_workspace_visible"]=pathlib.Path("/workspace/scratch").exists()
r["raw_data_visible"]=pathlib.Path("/workspace/shared/orbitserve-data").exists()
r["other_nodes_visible"]=pathlib.Path("/node/../B").exists()
try:
 pathlib.Path("/app/orbitserve/forbidden_probe.txt").write_text("forbidden");r["source_readonly"]=False
except OSError as e:r["source_readonly"]=True;r["readonly_errno"]=e.errno
try:
 s=socket.socket();s.settimeout(.1);s.connect(("1.1.1.1",80));r["network_denied"]=False
except OSError as e:r["network_denied"]=True;r["network_errno"]=e.errno
r["mountinfo"]=pathlib.Path("/proc/self/mountinfo").read_text()
r["pid_namespace"]=os.readlink("/proc/self/ns/pid")
r["net_namespace"]=os.readlink("/proc/self/ns/net")
r["mount_namespace"]=os.readlink("/proc/self/ns/mnt")
print(json.dumps(r))'''
    result = subprocess.run(sandbox_command(node_root, probe=code, source_root=source_root), text=True, capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError(f"BLOCKED: namespace isolation probe failed ({result.returncode}): {result.stderr}")
    evidence = json.loads(result.stdout)
    checks = [evidence["node_writable"], evidence["source_readonly"], evidence["network_denied"],
              not evidence["host_workspace_visible"], not evidence["raw_data_visible"], not evidence["other_nodes_visible"]]
    if not all(checks):
        raise RuntimeError(f"BLOCKED: namespace isolation failed negative probes: {evidence}")
    evidence["method"] = "user+mount+pid+ipc+uts+network_namespaces_bubblewrap"
    evidence["passed"] = True
    return evidence
