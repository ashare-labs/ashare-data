"""Offline subprocess/credential regressions; never access the user's Keychain."""

import fcntl
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import warnings

import pytest

from ashare_data.financials import tushare_reference as keychain

SECRET = "synthetic-credential-only-for-offline-regression"


@pytest.fixture
def runner(monkeypatch):
    scripts = Path(__file__).parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("startup_fixture", scripts / "run_tushare_reference.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fresh_interpreter_receives_only_private_pipe_not_argv_or_environment(runner, tmp_path, monkeypatch):
    child = tmp_path / "child.py"
    child.write_text("""import json,os,sys,time
args=dict(zip(sys.argv[1::2],sys.argv[2::2]))
secret=os.read(int(args['--credential-fd']),4097)
assert secret==b'synthetic-credential-only-for-offline-regression'
assert secret.decode() not in ' '.join(sys.argv)
assert 'TUSHARE_TOKEN' not in os.environ
assert os.getsid(0)==os.getpid()
with os.fdopen(int(args['--ready-fd']),'wb') as output:
 output.write(json.dumps(dict(pid=os.getpid(),state='worker_initialized_authentication_unverified')).encode())
time.sleep(.3)
""")
    monkeypatch.setenv("TUSHARE_TOKEN", "unrelated-synthetic-env-secret")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        result = runner.spawn_with_credential([sys.executable, "-B", str(child)], SECRET, tmp_path)
    assert result["state"] == "worker_initialized_authentication_unverified"
    assert result["fresh_interpreter"] and not result["authentication_verified"]
    _, status = os.waitpid(result["started_pid"], 0)
    assert status == 0
    assert SECRET.encode() not in (tmp_path / "worker.log").read_bytes()


def test_uncertain_startup_preserves_child_and_does_not_retry(runner, tmp_path):
    child = tmp_path / "silent.py"
    child.write_text("import time; time.sleep(30)\n")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        result = runner.spawn_with_credential([sys.executable, "-B", str(child)], SECRET, tmp_path, startup_timeout=.05)
    try:
        assert result["state"] == "startup_unconfirmed_inspect_before_retry"
        os.kill(result["started_pid"], 0)
        assert not result["existing_collector_interrupted"]
    finally:
        os.kill(result["started_pid"], signal.SIGTERM)
        os.waitpid(result["started_pid"], 0)


def test_private_credential_reader_refuses_files_and_oversize(runner, tmp_path):
    path = tmp_path / "synthetic.txt"
    path.write_text(SECRET)
    with path.open("rb") as stream, pytest.raises(ValueError, match="private pipe"):
        runner.read_private_credential(stream.fileno())
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"x" * 4097)
    os.close(write_fd)
    with pytest.raises(ValueError, match="bounded"):
        runner.read_private_credential(read_fd)


def test_keychain_mode_execs_without_secret_transfer(runner, tmp_path):
    child = tmp_path / "keychain_child.py"
    child.write_text("""import json,os,sys,time
assert '--keychain' in sys.argv and '--credential-fd' not in sys.argv
assert 'TUSHARE_TOKEN' not in os.environ
fd=int(sys.argv[sys.argv.index('--ready-fd')+1])
with os.fdopen(fd,'wb') as output:
 output.write(json.dumps(dict(pid=os.getpid(),state='worker_initialized_authentication_unverified')).encode())
time.sleep(.3)
""")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        result = runner.spawn_with_credential([sys.executable, "-B", str(child), "--keychain"], None, tmp_path)
    assert result["credential_transfer"] == "scoped_macos_keychain" and result["credential_saved"]
    os.waitpid(result["started_pid"], 0)


def test_keychain_store_uses_stdin_and_exact_nonsecret_ack(monkeypatch, tmp_path):
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        assert SECRET not in " ".join(args) and SECRET not in str(kwargs["env"])
        assert kwargs["input"] == SECRET.encode()
        return subprocess.CompletedProcess(args, 0, json.dumps(dict(stored_in_keychain=True,
                   service=keychain.SERVICE, account=keychain.ACCOUNT)).encode(), b"")
    monkeypatch.setattr(keychain.subprocess, "run", run)
    result = keychain.store_token(tmp_path / "helper", SECRET)
    assert result["stored_in_keychain"] and calls[0][0][-1] == "store"
    assert not list(tmp_path.iterdir())


def test_keychain_read_uses_a_private_pipe_and_errors_never_echo_secret(tmp_path):
    helper = tmp_path / "native-fixture"
    helper.write_text(f"#!{sys.executable}\nimport os,sys\nassert sys.argv[1]=='read'\nos.write(int(sys.argv[2]),{SECRET.encode()!r})\n")
    helper.chmod(0o700)
    assert keychain.read_token(helper) == SECRET
    helper.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write('keychain_osstatus=-25308 '+{SECRET!r})\nsys.exit(1)\n")
    with pytest.raises(RuntimeError, match="requires_user_unlock") as error:
        keychain.read_token(helper)
    assert SECRET not in str(error.value)


def test_active_worker_is_rejected_before_credential_prompt(runner, tmp_path, monkeypatch):
    root = tmp_path / "batch"
    root.mkdir()
    monkeypatch.setattr(runner.ts, "prepare_calendar", lambda *a: {})
    monkeypatch.setattr(runner.sys, "argv", ["run", "--root", str(root), "--baseline", str(tmp_path), "--prompt-token", "--detach"])
    monkeypatch.setattr(runner.getpass, "getpass", lambda _: pytest.fail("Duplicate startup requested a secret"))
    with (root / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(SystemExit) as result:
            runner.main()
    assert result.value.code == 2


def test_no_python_level_fork_or_shell_credential_command_remains(runner):
    source = Path(runner.__file__).read_text()
    assert "os.fork(" not in source and "preexec_fn=" not in source
    native = (Path(runner.__file__).parent / "tushare-keychain.swift").read_text()
    assert '"ashare-data.tushare"' in native and '"tushare"' in native
    assert "kSecValueData" in native and 'operation == "read"' in native
    assert "SecItemUpdate" not in native and "SecAccess" not in native


def test_keychain_helper_policy_binds_exact_service_account_and_binary(tmp_path):
    helper, policy = tmp_path / "helper", tmp_path / "policy.json"
    helper.write_bytes(b"synthetic helper, not an executable")
    identity = dict(schema="ad.tushare-credential-policy.v1", service=keychain.SERVICE, account=keychain.ACCOUNT,
                    helper=str(helper), helper_sha256=hashlib.sha256(helper.read_bytes()).hexdigest())
    policy.write_text(json.dumps(identity))
    keychain.verify_helper(helper, policy)
    for wrong in (identity | {"service": "another-service"}, identity | {"account": "another-account"}, identity | {"helper_sha256": "changed"}):
        policy.write_text(json.dumps(wrong))
        with pytest.raises(ValueError, match="credential not read"):
            keychain.verify_helper(helper, policy)


def test_keychain_collector_reads_only_bound_helper_and_reports_persistent_origin(runner, tmp_path, monkeypatch):
    root, helper = tmp_path / "batch", tmp_path / "native-helper"
    root.mkdir()
    helper.write_bytes(b"synthetic helper; subprocess is mocked")
    (root / "credential-policy.json").write_text(json.dumps(dict(schema="ad.tushare-credential-policy.v1",
        service=keychain.SERVICE, account=keychain.ACCOUNT, helper=str(helper),
        helper_sha256=hashlib.sha256(helper.read_bytes()).hexdigest())))
    monkeypatch.setattr(runner.ts, "prepare_calendar", lambda *a: {})
    calls = []
    monkeypatch.setattr(keychain, "read_token", lambda path: calls.append(path) or SECRET)
    captured = []
    monkeypatch.setattr(runner, "collect", lambda *args, **kwargs: captured.append(args))
    monkeypatch.setattr(runner.sys, "argv", ["run", "--root", str(root), "--baseline", str(tmp_path),
                                           "--keychain", "--keychain-helper", str(helper)])
    assert runner.main() == 0 and calls == [helper]
    assert captured[0][-1] == "macos_keychain" and SECRET not in repr(captured[0][2])
    assert all(SECRET.encode() not in p.read_bytes() for p in root.iterdir() if p.is_file())


def test_manual_keychain_item_is_preserved_without_a_second_secret_prompt(runner, tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("setup_fixture", Path(runner.__file__).with_name("configure_tushare_keychain.py"))
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    helper = tmp_path / "helper"
    helper.write_bytes(b"synthetic helper; keychain status is mocked")
    status_file = tmp_path / "keychain-status.json"
    monkeypatch.setattr(keychain, "status", lambda _: {"configured": True})
    monkeypatch.setattr(keychain, "store_token", lambda *a: pytest.fail("Existing item must not be overwritten"))
    monkeypatch.setattr(setup.getpass, "getpass", lambda _: pytest.fail("Existing item must not prompt again"))
    monkeypatch.setattr(setup.sys, "argv", ["setup", "--helper", str(helper), "--status-file", str(status_file)])
    assert setup.main() == 0
    assert json.loads(capsys.readouterr().out)["existing_item_preserved"]
    keychain.verify_helper(helper, tmp_path / "credential-policy.json")
