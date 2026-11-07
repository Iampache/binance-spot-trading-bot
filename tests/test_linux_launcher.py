"""Local launcher validation does not contact providers or disclose credentials."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import linux_launcher as launcher


@pytest.fixture
def config(tmp_path):
    def write(extra=""):
        path = tmp_path / ".env"
        path.write_text(
            "API_KEY=dummy-key\nAPI_SECRET=dummy-secret\nTELEGRAM_TOKEN=dummy-token\nTELEGRAM_CHAT_ID=123\n"
            + extra
        )
        return tmp_path

    return write


def test_defaults_ignore_inherited_live_settings(config, monkeypatch):
    monkeypatch.setenv("USE_TESTNET", "false")
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "true")
    result = launcher.configuration(config())
    assert result["USE_TESTNET"] == "true"
    assert result["ALLOW_LIVE_TRADING"] == "false"


def test_explicit_live_configuration_and_private_permissions(config):
    folder = config("USE_TESTNET=false\nALLOW_LIVE_TRADING=true\nMOMENTUM_ALERTS=false\n")
    result = launcher.configuration(folder)
    assert result["USE_TESTNET"] == result["MOMENTUM_ALERTS"] == "false"
    if os.name != "nt":
        assert (folder / ".env").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "extra",
    [
        "USE_TESTNET=false\n",
        "AUTO_SCAN=maybe\n",
        "API_KEY=\n",
        "TELEGRAM_CHAT_ID=-123\n",
        "TOP_N=NaN\n",
    ],
)
def test_invalid_configuration_fails_without_values(config, extra):
    with pytest.raises(ValueError) as caught:
        launcher.configuration(config(extra))
    assert not any(
        value in str(caught.value) for value in ("dummy-key", "dummy-secret", "dummy-token")
    )


def test_missing_config_does_not_create_credentials(tmp_path):
    with pytest.raises(ValueError, match="Copy .env.example"):
        launcher.configuration(tmp_path)
    assert not (tmp_path / ".env").exists()


def test_dotenv_is_data_and_does_not_expand_shell_or_environment(config, monkeypatch):
    monkeypatch.setenv("SECRET_REFERENCE", "should-not-expand")
    result = launcher.configuration(config("API_SECRET='${SECRET_REFERENCE}$(touch unwanted)'\n"))
    assert result["API_SECRET"] == "${SECRET_REFERENCE}$(touch unwanted)"


@pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"), reason="Linux launcher")
def test_shell_start_and_reattach_from_a_directory_with_spaces(tmp_path):
    folder = tmp_path / "NEXVANT installation"
    folder.mkdir()
    script = Path(launcher.__file__).with_name("bot-loader.sh")
    shutil.copyfile(script, folder / script.name)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    venv = folder / ".venv" / "bin"
    venv.mkdir(parents=True)
    python = venv / "python"
    python.write_text('#!/bin/sh\necho "validated" >> "$TEST_LOG"\n')
    python.chmod(0o700)
    tmux = bin_dir / "tmux"
    tmux.write_text("""#!/bin/sh
printf '%s\\n' "$*" >> "$TEST_LOG"
case "$1" in
has-session) test -f "$TEST_SESSION" ;;
new-session) touch "$TEST_SESSION" ;;
esac
""")
    tmux.chmod(0o700)
    env = {
        **os.environ,
        "PATH": str(bin_dir) + ":" + os.environ["PATH"],
        "TEST_LOG": str(tmp_path / "calls"),
        "TEST_SESSION": str(tmp_path / "session"),
    }
    env.pop("TMUX", None)
    for _ in range(2):
        subprocess.run(
            ["bash", str(folder / script.name)],
            cwd=tmp_path,
            env=env,
            check=True,
            capture_output=True,
            timeout=10,
        )
    calls = (tmp_path / "calls").read_text()
    assert calls.count("validated") == 1
    assert calls.count("new-session") == 1
    assert calls.count("attach-session") == 2
    assert "--run" in calls
