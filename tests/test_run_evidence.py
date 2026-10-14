"""The rehearsal recorder must keep the full observable client stream."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from glassboard.run_evidence import capture
from glassboard.run_evidence import main


def test_capture_preserves_prompt_json_stream_and_stderr(tmp_path: Path):
    prompt = tmp_path / "input.txt"
    prompt.write_bytes(b"exact prompt\n")
    script = ("import json,sys; print(json.dumps({'type':'assistant','text':sys.stdin.read()})); "
              "print('diagnostic', file=sys.stderr)")
    record = capture(tmp_path / "private", "claude", 1, prompt, tmp_path,
                     [sys.executable, "-c", script], prompt_stdin=True,
                     native_unavailable_reason="synthetic client has no native history")
    directory = tmp_path / "private" / "claude-round01"
    assert record["status"] == "complete"
    assert record["json_events"] == 1
    assert json.loads((directory / "stdout.jsonl").read_text())["text"] == "exact prompt\n"
    assert (directory / "stderr.bin").read_bytes() == b"diagnostic\n"
    assert record["stdout_sha256"] == hashlib.sha256((directory / "stdout.jsonl").read_bytes()).hexdigest()
    assert (directory.stat().st_mode & 0o777) == 0o700
    for filename in ("prompt.txt", "stdout.jsonl", "stderr.bin", "record.json"):
        assert ((directory / filename).stat().st_mode & 0o777) == 0o600
    with pytest.raises(FileExistsError):
        capture(tmp_path / "private", "claude", 1, prompt, tmp_path, [sys.executable])


def test_capture_flags_malformed_or_missing_json(tmp_path: Path):
    prompt = tmp_path / "input.txt"
    prompt.write_text("prompt")
    record = capture(tmp_path / "private", "grok", 1, prompt, tmp_path,
                     [sys.executable, "-c", "print('not json')"],
                     native_unavailable_reason="synthetic client has no native history")
    assert record["status"] == "incomplete"
    assert record["invalid_json_lines"] == 1
    assert (tmp_path / "private" / "grok-round01" / "stdout.jsonl").read_bytes() == b"not json\n"


def test_capture_preserves_declared_native_history(tmp_path: Path):
    prompt = tmp_path / "input.txt"
    prompt.write_text("prompt")
    native = tmp_path / "native.jsonl"
    native.write_text('{"message":"native"}\n')
    record = capture(tmp_path / "private", "codex", 1, prompt, tmp_path,
                     [sys.executable, "-c", "print('{}')"], native_files=[native])
    assert record["status"] == "complete"
    assert record["native_files"][0]["status"] == "copied"
    assert (tmp_path / "private" / "codex-round01" / "native-01.jsonl").read_bytes() == native.read_bytes()
    missing = capture(tmp_path / "private", "codex", 2, prompt, tmp_path,
                      [sys.executable, "-c", "print('{}')"], native_files=[tmp_path / "missing.jsonl"])
    assert missing["status"] == "incomplete"


def test_capture_discovers_native_history_after_client_exits(tmp_path: Path):
    prompt = tmp_path / "input.txt"
    prompt.write_text("prompt")
    native = tmp_path / "client-home" / "session.jsonl"
    script = (f"from pathlib import Path; p=Path({str(native)!r}); p.parent.mkdir(); "
              "p.write_text('{\"message\":1}\\n'); print('{}')")
    record = capture(tmp_path / "private", "codex", 1, prompt, tmp_path,
                     [sys.executable, "-c", script], native_globs=[str(native.parent / "*.jsonl")])
    assert record["status"] == "complete"
    assert record["native_files"][0]["source"] == str(native)
    assert (tmp_path / "private" / "codex-round01" / "native-01.jsonl").read_bytes() == native.read_bytes()


def test_capture_command_line_uses_full_json_stream(tmp_path: Path, capsys):
    prompt = tmp_path / "input.txt"
    prompt.write_text("prompt")
    result = main(["--evidence", str(tmp_path / "private"), "--agent", "codex",
                   "--round", "1", "--prompt", str(prompt), "--cwd", str(tmp_path),
                   "--native-unavailable-reason", "synthetic client has no native history",
                   "--", sys.executable, "-c", "print('{}')"])
    assert result == 0
    assert json.loads(capsys.readouterr().out)["status"] == "complete"


def test_capture_flags_undeclared_native_history(tmp_path: Path):
    prompt = tmp_path / "input.txt"
    prompt.write_text("prompt")
    record = capture(tmp_path / "private", "codex", 1, prompt, tmp_path,
                     [sys.executable, "-c", "print('{}')"])
    assert record["status"] == "incomplete"
    assert record["native_files"] == [] and record["native_unavailable_reason"] is None
    unmatched = capture(tmp_path / "private", "codex", 2, prompt, tmp_path,
                        [sys.executable, "-c", "print('{}')"],
                        native_globs=[str(tmp_path / "does-not-exist" / "*.jsonl")])
    assert unmatched["status"] == "incomplete"


@pytest.mark.parametrize("interrupt_signal", [signal.SIGINT, signal.SIGTERM])
def test_interrupted_capture_stops_client_and_preserves_partial_record(tmp_path: Path, interrupt_signal):
    prompt = tmp_path / "input.txt"
    prompt.write_text("prompt")
    evidence = tmp_path / "private"
    client_pid = tmp_path / "client.pid"
    client = ("import json,os,time; "
              f"open({str(client_pid)!r},'w').write(str(os.getpid())); "
              "print(json.dumps({'type':'started'}), flush=True); time.sleep(60)")
    command = [sys.executable, "-m", "glassboard.run_evidence",
               "--evidence", str(evidence), "--agent", "codex", "--round", "1",
               "--prompt", str(prompt), "--cwd", str(tmp_path),
               "--native-unavailable-reason", "synthetic client has no native history",
               "--", sys.executable, "-c", client]
    recorder = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for _ in range(200):
        if client_pid.exists():
            break
        time.sleep(0.01)
    assert client_pid.exists()
    os.kill(recorder.pid, interrupt_signal)
    stdout, stderr = recorder.communicate(timeout=10)
    assert recorder.returncode == 130 and stdout == ""
    assert "partial evidence record preserved" in stderr
    record = json.loads((evidence / "codex-round01" / "record.json").read_text())
    assert record["status"] == "capture_error"
    assert record["error_type"] == "KeyboardInterrupt"
    assert record["exit_code"] == -signal.SIGTERM
    assert record["json_events"] == 1 and record["invalid_json_lines"] == 0
    with pytest.raises(ProcessLookupError):
        os.kill(int(client_pid.read_text()), 0)
