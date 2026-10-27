# SPDX-License-Identifier: AGPL-3.0-only

import hashlib
import os
import shutil
import tempfile

import pytest

_RUN_REGISTRY = pytest.StashKey[tuple[str, str | None]]()
UNRECORDED_BUILD_ID = "20200101T000000Z-0123abcd"


def pytest_configure(config):
    root = tempfile.mkdtemp(prefix="pocket-listen-run-")
    config.stash[_RUN_REGISTRY] = (root, os.environ.get("POCKET_LISTEN_DIR"))
    os.environ["POCKET_LISTEN_DIR"] = os.path.join(root, "registry")


def pytest_unconfigure(config):
    root, previous = config.stash.get(_RUN_REGISTRY, (None, None))
    if root is None:
        return
    if previous is None:
        os.environ.pop("POCKET_LISTEN_DIR", None)
    else:
        os.environ["POCKET_LISTEN_DIR"] = previous
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(autouse=True)
def listen_registry(tmp_path_factory, request):
    name = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
    folder = tmp_path_factory.getbasetemp() / "pocket-listen" / name
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("POCKET_LISTEN_DIR", str(folder))
        yield folder
