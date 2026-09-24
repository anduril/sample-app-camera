"""Registry-drift guard for the custom task definitions.

The daemon keys both its task dispatch (``tasking/handler.py``) and its
advertised ``task_catalog`` on ``TASK_PACKAGE`` (default in ``config.py``,
overridable in ``.env``). The Lattice Schema Registry, in turn, identifies the
task types by the ``package`` declared under ``task-def/``. When the two drift
apart the Lattice UI offers task types the agent rejects as unsupported, so the
defaults are pinned to the definitions here.
"""

from __future__ import annotations

import re
from pathlib import Path

from lattice_cam.config import Config

REPO_ROOT = Path(__file__).resolve().parents[1]
TASK_DEF_ROOT = REPO_ROOT / "task-def"

_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
_MODULE_RE = re.compile(r"^\s*name:\s*\S+/([^/\s]+)/([^/\s]+)\s*$", re.MULTILINE)
_ENV_TASK_PACKAGE_RE = re.compile(r"^TASK_PACKAGE=(.+)$", re.MULTILINE)


def _task_proto() -> Path:
    """The single custom-task .proto file shipped with the repo."""
    protos = sorted(TASK_DEF_ROOT.rglob("camera_tasks.proto"))
    assert len(protos) == 1, f"expected exactly one camera_tasks.proto, found {protos}"
    return protos[0]


def _declared_package(proto_file: Path) -> str:
    match = _PACKAGE_RE.search(proto_file.read_text(encoding="utf-8"))
    assert match is not None, f"no package declaration in {proto_file}"
    return match.group(1)


def test_proto_file_lives_under_its_package_path():
    """The <owner>.<repository>.<package>.<version> convention maps 1:1 to the path."""
    proto_file = _task_proto()
    package = _declared_package(proto_file)
    expected = TASK_DEF_ROOT.joinpath(*package.split(".")) / proto_file.name
    assert proto_file == expected


def test_declared_package_matches_the_buf_module_name():
    module = _MODULE_RE.search((TASK_DEF_ROOT / "buf.yaml").read_text(encoding="utf-8"))
    assert module is not None, "could not read the module name from task-def/buf.yaml"
    owner, repository = module.groups()
    # Buf writes hyphens in the repository name as underscores in the package.
    assert _declared_package(_task_proto()).startswith(f"{owner}.{repository.replace('-', '_')}.")


def test_default_task_package_matches_the_proto_package():
    assert Config().task_package == _declared_package(_task_proto())


def test_env_example_task_package_matches_the_default():
    match = _ENV_TASK_PACKAGE_RE.search((REPO_ROOT / ".env.example").read_text(encoding="utf-8"))
    assert match is not None, "TASK_PACKAGE is not documented in .env.example"
    assert match.group(1).strip() == Config().task_package
