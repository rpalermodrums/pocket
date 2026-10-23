# SPDX-License-Identifier: AGPL-3.0-only
"""Run the b2b page's pure model tests under Node when it is available (CI provides it)."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_page_model_rules_under_node():
    test = Path(__file__).with_name("b2b_page_model.test.mjs")
    result = subprocess.run(["node", "--test", str(test)], capture_output=True, text=True, timeout=120,
                            check=False)
    assert result.returncode == 0, result.stdout + result.stderr
