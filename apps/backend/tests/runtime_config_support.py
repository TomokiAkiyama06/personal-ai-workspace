"""A fake file system for the runtime configuration's checks (Decision 0093, 6).

The parser reads the host only through a :class:`~paw_backend.runtime_config.Host`;
this one answers from a dictionary, so the tests check ownership and modes
without creating a root-owned file.
"""

import errno
import os
import stat
from dataclasses import dataclass, field

ROOT = 0
PAW = 990


@dataclass
class Node:
    kind: str  # "dir", "file", "link"
    mode: int = 0o755
    uid: int = ROOT
    content: bytes = b""
    target: str = ""


@dataclass(frozen=True)
class FakeStat:
    st_mode: int
    st_uid: int
    st_size: int


def _stat(node: Node) -> FakeStat:
    kind = {"dir": stat.S_IFDIR, "file": stat.S_IFREG, "link": stat.S_IFLNK}[node.kind]
    return FakeStat(kind | node.mode, node.uid, len(node.content))


ELF = b"\x7fELF\x02\x01\x01"


@dataclass
class FakeHost:
    nodes: dict[str, Node] = field(default_factory=dict)
    groups: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.nodes.setdefault("/", Node("dir"))

    # Building the tree.
    def dir(self, path: str, *, mode: int = 0o755, uid: int = ROOT) -> None:
        self._parents(path)
        self.nodes[path] = Node("dir", mode, uid)

    def file(
        self,
        path: str,
        content: bytes | str = ELF,
        *,
        mode: int = 0o755,
        uid: int = ROOT,
    ) -> None:
        self._parents(path)
        if isinstance(content, str):
            content = content.encode()
        self.nodes[path] = Node("file", mode, uid, content)

    def link(self, path: str, target: str, *, uid: int = ROOT) -> None:
        self._parents(path)
        self.nodes[path] = Node("link", 0o777, uid, target=target)

    def _parents(self, path: str) -> None:
        parent = os.path.dirname(path)
        if parent != path and parent not in self.nodes:
            self.dir(parent)

    # The Host protocol.
    def _node(self, path: str) -> Node:
        node = self.nodes.get(path)
        if node is None:
            raise FileNotFoundError(errno.ENOENT, "missing")
        return node

    def lstat(self, path: str) -> FakeStat:
        return _stat(self._node(path))

    def readlink(self, path: str) -> str:
        node = self._node(path)
        if node.kind != "link":
            raise OSError(errno.EINVAL, "not a link")
        return node.target

    def read_file(self, path: str, limit: int) -> tuple[FakeStat, bytes]:
        node = self._node(path)
        if node.kind == "link":
            raise OSError(errno.ELOOP, "link")
        if node.kind != "file":
            return _stat(node), b""
        return _stat(node), node.content[: limit + 1]

    def can_execute(self, path: str) -> bool:
        node = self._node(path)
        return node.kind == "file" and bool(node.mode & 0o111)

    def group_id(self, name: str) -> int | None:
        return self.groups.get(name)


PYTHON = "/opt/paw/current/venv/bin/python"


def standard_host() -> FakeHost:
    """The host of the Decision's example (section 4), every part root-owned."""
    host = FakeHost(groups={"paw-sandbox": 2001})
    host.dir("/etc/paw")
    host.file("/usr/bin/systemctl")
    host.file("/usr/bin/nvidia-smi")
    host.file("/usr/bin/python3.13")
    host.link("/usr/bin/python3", "python3.13")
    host.file("/usr/local/lib/paw/paw-unit-pids", "#!/usr/bin/python3 -I\nimport sys\n")
    host.file("/usr/local/lib/paw/paw-git-wrapper", "#!/usr/bin/python3 -I\n")
    host.link("/opt/paw/current", "releases/1")
    host.dir("/opt/paw/releases/1/venv/bin")
    host.link("/opt/paw/releases/1/venv/bin/python", "/usr/bin/python3.13")
    return host


EXAMPLE = """\
version = 1

[compute]
nvidia_smi = "/usr/bin/nvidia-smi"
gpu_index = 0
restore_margin_mib = 4096

[[compute.deployments]]
name = "main"
role = "main"
weights_mib = 34286
kv_pool_mib = 19866
kv_bytes_per_token = 68279
max_sequences = 8
max_context_tokens = 131072
residency = "always"
initial = "gpu"

[compute.deployments.commands]
gpu = ["/usr/bin/systemctl", "start", "paw-llm-main.service"]
unload = ["/usr/bin/systemctl", "stop", "paw-llm-main.service"]
pids = ["/usr/local/lib/paw/paw-unit-pids", "paw-llm-main.service"]
metrics_url = "http://127.0.0.1:18080/metrics"
unit = "paw-llm-main.service"

[local_model]
base_url = "http://127.0.0.1:18080/v1"
deployment = "main"
served_model = "main"
model_id = "Qwen3.8-27B-FP8"
agent_label = "local"

[ssh]
identity_template = "/etc/paw/ssh-keys/{user}.key"

[git]
runner = "ssh"

[sandbox]
runner = "ssh"
shim_base = "/run/paw-sandbox"
shim_group = "paw-sandbox"
idle_seconds = 900

[orchestrator]
[orchestrator.ladders]
planner = ["local"]
worker = ["local"]
researcher = ["local"]
reviewer = ["local"]
"""

MINIMAL_COMPUTE = """\
version = 1

[compute]

[[compute.deployments]]
name = "main"
role = "main"
weights_mib = 1000
"""
