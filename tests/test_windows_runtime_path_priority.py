"""Package-first command resolution, including native Windows environment keys."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from deepdesk.tool_runtime import ToolRuntime, prepend_unique_path


@pytest.mark.parametrize('spelling', ['PATH', 'Path', 'path'])
@pytest.mark.parametrize('factory', ['prepend', 'runtime'])
def test_package_path_moves_a_late_entry_ahead_of_host_tools(tmp_path, spelling, factory):
    if os.name != 'nt' and spelling != 'PATH':
        pytest.skip('Case aliases only apply to Windows')
    package = tmp_path / 'Elren [中文] & tools'
    host = tmp_path / 'older tools'
    package.mkdir()
    host.mkdir()
    for folder in (package, host):
        (folder / 'elren-qa-command.cmd').write_text('@echo harmless probe\n', encoding='ascii')
    original = {spelling: os.pathsep.join([str(host), str(package), str(host)])}
    environment = dict(original)
    if factory == 'prepend':
        prepend_unique_path(environment, [str(package), str(package)])
    else:
        runtime = ToolRuntime(tmp_path, package, package / 'manifest.json', {}, (package,), ())
        environment = runtime.environment(environment)
    assert environment['PATH'].split(os.pathsep) == [str(package), str(host)]
    if os.name == 'nt':
        assert [key for key in environment if key.upper() == 'PATH'] == ['PATH']
        native = {**os.environ, **environment}
        # The child receives only one case-insensitive PATH key.
        for key in list(native):
            if key.upper() == 'PATH' and key != 'PATH':
                del native[key]
        result = subprocess.run([sys.executable, '-c',
                                 'import json,shutil;print(json.dumps(shutil.which("elren-qa-command.cmd")))'], env=native, capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=10,
                                creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        assert result.returncode == 0
        assert json.loads(result.stdout) == str(package / 'elren-qa-command.cmd')
    assert original == {spelling: os.pathsep.join([str(host), str(package), str(host)])}


@pytest.mark.skipif(os.name != 'nt', reason='Windows environment key equivalence')
def test_case_variants_collapse_without_losing_host_fallbacks(tmp_path):
    package, host_a, host_b = (str(tmp_path / name) for name in ('package', 'host-a', 'host-b'))
    environment = {'PATH': host_a, 'Path': host_b, 'path': package, 'KEEP': 'unchanged'}
    prepend_unique_path(environment, [package])
    assert environment == {'PATH': os.pathsep.join([package, host_a, host_b]), 'KEEP': 'unchanged'}
    prepend_unique_path(environment, [package])
    assert environment['PATH'].split(os.pathsep) == [package, host_a, host_b]


def test_empty_path_entries_do_not_inject_the_current_directory(tmp_path):
    environment = {'PATH': os.pathsep + str(tmp_path)}
    prepend_unique_path(environment, ['', str(tmp_path), ''])
    assert environment['PATH'] == str(tmp_path)
