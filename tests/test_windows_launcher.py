"""Exercise the real batch file on Windows with a harmless stub, never the bot."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Requires native Windows cmd.exe")


@pytest.fixture
def launcher(tmp_path):
    project = tmp_path / "project folder with spaces"
    project.mkdir()
    source = Path(__file__).resolve().parents[1] / "Start_Live_Bot.bat"
    shutil.copyfile(source, project / source.name)
    # Whitelist OS bootstrap variables; never inherit production credentials.
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "COMSPEC")
        if key in os.environ
    }
    environment.update(
        PYTHON_DOTENV_DISABLED="1",
        USE_TESTNET="true",
        ALLOW_LIVE_TRADING="false",
        AUTO_SCAN="false",
    )
    return project, environment


def install_stub(project, environment):
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(project / ".venv")],
        env=environment,
        check=True,
        capture_output=True,
        timeout=60,
    )
    (project / "telegram_assistant.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "keys = ['USE_TESTNET','ALLOW_LIVE_TRADING','AUTO_SCAN','PYTHON_DOTENV_DISABLED','VIRTUAL_ENV']\n"
        "result = {key: os.getenv(key) for key in keys}\n"
        "result.update(cwd=os.getcwd(), executable=sys.executable)\n"
        "Path('launcher-result.json').write_text(json.dumps(result), encoding='utf-8')\n"
        "sys.exit(17)\n",
        encoding="utf-8",
    )


def run_launcher(project, environment, cwd):
    return subprocess.run(
        [environment.get("COMSPEC", "cmd.exe"), "/d", "/c", str(project / "Start_Live_Bot.bat")],
        env=environment,
        cwd=cwd,
        input="\n",
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_launcher_sets_live_auto_mode_and_holds_correct_environment(launcher, tmp_path):
    project, environment = launcher
    install_stub(project, environment)
    (project / ".env").write_text("LAUNCHER_TEST_ONLY=1\n", encoding="utf-8")
    result = run_launcher(project, environment, tmp_path)
    assert result.returncode == 17, (result.stdout, result.stderr)
    state = json.loads((project / "launcher-result.json").read_text(encoding="utf-8"))
    assert state["USE_TESTNET"] == "false"
    assert state["ALLOW_LIVE_TRADING"] == "true"
    assert state["AUTO_SCAN"] == "true"
    assert state["PYTHON_DOTENV_DISABLED"] is None
    assert Path(state["VIRTUAL_ENV"]).resolve() == (project / ".venv").resolve()
    assert Path(state["cwd"]).resolve() == project.resolve()
    assert Path(state["executable"]).resolve() == (project / ".venv/Scripts/python.exe").resolve()
    assert "Press any key" in result.stdout


def test_launcher_refuses_missing_venv(launcher, tmp_path):
    project, environment = launcher
    result = run_launcher(project, environment, tmp_path)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "needs its own Windows .venv" in result.stdout
    assert not (project / "launcher-result.json").exists()


def test_launcher_refuses_missing_local_configuration(launcher, tmp_path):
    project, environment = launcher
    install_stub(project, environment)
    result = run_launcher(project, environment, tmp_path)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "Local .env is missing" in result.stdout
    assert not (project / "launcher-result.json").exists()
