"""Real local processes prove limits/cleanup without a native PDF installation."""
import json
import os
import sys
import time

import pytest

from apps.core.process_limits import run_bounded, ProcessLimitError


def python(source):
    return [sys.executable, "-c", source]


def alive(identifier):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x00100000, False, identifier)
        if not handle:
            return False
        try:
            return api.WaitForSingleObject(handle, 0) == 258
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(identifier, 0)
        return True
    except ProcessLookupError:
        return False


def test_bounded_process_roundtrips_bytes_without_a_shell():
    assert run_bounded(python("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"), b"fixture\x00bytes") == b"fixture\x00bytes"


def test_child_does_not_inherit_application_credentials(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "fixture-private-key")
    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEYS", "fixture-encryption-keys")
    output = run_bounded(python("import os,json; print(json.dumps([os.getenv('STRIPE_SECRET_KEY'),os.getenv('INTEGRATION_ENCRYPTION_KEYS')]))"))
    assert json.loads(output) == [None, None]


def test_input_is_rejected_before_starting_a_process():
    with pytest.raises(ProcessLimitError, match="input_limit"):
        run_bounded(["an-engine-that-must-not-start"], b"too large", max_input=1)


@pytest.mark.parametrize("stream,limits", [(1, {"max_output": 1024}), (2, {"max_stderr": 1024})])
def test_output_and_diagnostic_limits_stop_a_noisy_process(stream, limits):
    start = time.monotonic()
    with pytest.raises(ProcessLimitError, match="output_limit"):
        run_bounded(python(f"import os,time; os.write({stream},b'x'*100000); time.sleep(60)"), timeout=3, **limits)
    assert time.monotonic() - start < 6


def test_nonzero_exit_never_exposes_diagnostics():
    with pytest.raises(ProcessLimitError) as error:
        run_bounded(python("import sys; sys.stderr.write('fixture-secret-diagnostic'); sys.exit(2)"))
    assert error.value.code == "engine_failed"
    assert "fixture-secret" not in str(error.value)


def tree_source(path):
    return ("import sys,subprocess,time,os,json,pathlib; sys.stdin.buffer.read(); "
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
            f"record=pathlib.Path({str(path)!r}); staging=record.with_suffix('.tmp'); "
            "staging.write_text(json.dumps([os.getpid(),child.pid])); staging.replace(record); time.sleep(60)")


def test_deadline_kills_parent_and_descendant(tmp_path):
    path = tmp_path / "processes.json"
    with pytest.raises(ProcessLimitError, match="timeout"):
        run_bounded(python(tree_source(path)), timeout=1.5)
    identifiers = json.loads(path.read_text())
    assert not any(alive(identifier) for identifier in identifiers)


def test_abort_also_cleans_up_the_process_tree(tmp_path, monkeypatch):
    path = tmp_path / "processes.json"
    original_sleep = time.sleep
    def interrupt(seconds):
        if path.exists():
            raise KeyboardInterrupt
        original_sleep(seconds)
    monkeypatch.setattr("apps.core.process_limits.time.sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run_bounded(python(tree_source(path)), timeout=3)
    assert not any(alive(identifier) for identifier in json.loads(path.read_text()))
