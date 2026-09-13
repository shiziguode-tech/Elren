"""Windows wrapper errors remain clean in shell-significant directory names."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows command processor")
@pytest.mark.parametrize("directory", ["space folder", "QA [中文] & O'Brien", "parent (copy) & child"])
@pytest.mark.parametrize("java_home", ["", "missing & java (17)"])
def test_gradle_no_java_preserves_special_paths(tmp_path, directory, java_home):
    project = tmp_path / directory
    project.mkdir()
    source = Path(__file__).resolve().parents[1] / "mobile/ElrenMobile/gradlew.bat"
    shutil.copyfile(source, project / "gradlew.bat")
    environment = dict(os.environ)
    environment["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    environment["JAVA_HOME"] = str(project / java_home) if java_home else ""
    result = subprocess.run(["cmd.exe", "/d", "/c", "gradlew.bat", "--version"],
                            cwd=project, env=environment, capture_output=True, timeout=15,
                            text=True, errors="replace", check=False)
    output = (result.stdout + result.stderr).casefold()
    assert result.returncode != 0
    assert output.count("error: java_home") == 1
    assert "not recognized" not in output
    assert "不是内部或外部命令" not in output
    assert "此时不应有" not in output
    assert "was unexpected" not in output
