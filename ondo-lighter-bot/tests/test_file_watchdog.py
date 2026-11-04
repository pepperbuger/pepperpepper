from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import json
import subprocess

from olbot import file_watchdog as w


def test_healthy_and_old_positions_do_not_trigger_routine_model_work():
    healthy = {"checked_ts": 100, "phase": "running", "hedge_state": {"ETH": {"pending": False}}}
    assert w.classify(healthy, 101, 1, True) == []
    healthy["checked_ts"] = 0
    assert w.classify(healthy, 200, 1, True) == ["status_stale"]


def test_absent_process_and_pending_are_not_treated_as_flat():
    assert "trial_process_absent" in w.classify(None, 200, None, False)
    status = {"checked_ts": 100, "hedge_state": {"ETH": {"pending": True}}}
    assert "uncertain_or_pending" in w.classify(status, 100, 0, True)


def test_completion_is_one_terminal_event_not_process_failure():
    assert w.classify({"ended_ts": 99, "phase": "halted"}, 100, None, False) == ["execution_ended:halted"]


def test_delivery_has_fixed_arguments_no_shell_or_raw_log_and_never_retries():
    runner = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr="warning: cannot resume"))
    result = w.send_to_chat("codex.exe", "thread-id", Path("event.json"), ["status_stale"], runner=runner)
    command = runner.call_args.args[0]
    assert command[:4] == ["codex.exe", "queue", "--thread", "thread-id"]
    assert "NOT new approval" in command[5] and "--resume" in command
    assert not result["queued_without_warning"] and result["retry"] is False
    runner.assert_called_once()


def test_unknown_delivery_is_not_retried():
    runner = Mock(side_effect=subprocess.TimeoutExpired("codex", 25))
    result = w.send_to_chat("codex.exe", "thread", Path("e.json"), ["status_stale"], runner=runner)
    assert result["error_type"] == "TimeoutExpired" and result["retry"] is False
    runner.assert_called_once()


def test_handover_deduplicates_known_alert_and_repeated_poll(tmp_path, monkeypatch):
    (tmp_path / "launch.json").write_text(json.dumps({"parent_pid": 1}))
    (tmp_path / "status.json").write_text(json.dumps({"checked_ts": 1, "phase": "running"}))
    (tmp_path / "engine.log").write_text("old error already reported")
    (tmp_path / "monitor-status.json").write_text(json.dumps({"notification_keys": ["status_stale"]}))
    monkeypatch.setattr(w, "process_alive", lambda pid: True)
    monkeypatch.setattr(w, "desktop_alert", Mock())
    send = Mock(); monkeypatch.setattr(w, "send_to_chat", send)
    w.run(tmp_path, codex_exe="codex", thread="same", once=True)
    w.run(tmp_path, codex_exe="codex", thread="same", once=True)
    send.assert_not_called()
    state = json.loads((tmp_path / "local-watchdog.json").read_text())
    assert state["routine_model_calls"] == state["exchange_requests"] == 0
    assert not (tmp_path / "local-watchdog.lock").exists()


def test_new_error_is_delivered_once_even_after_watchdog_restart(tmp_path, monkeypatch):
    (tmp_path / "launch.json").write_text(json.dumps({"parent_pid": 1}))
    (tmp_path / "status.json").write_text(json.dumps({"checked_ts": 1}))
    (tmp_path / "engine.log").write_text("")
    monkeypatch.setattr(w, "process_alive", lambda pid: True)
    monkeypatch.setattr(w, "desktop_alert", Mock())
    send=Mock(return_value={"attempted": True});monkeypatch.setattr(w, "send_to_chat", send)
    w.run(tmp_path, codex_exe="codex", thread="same", once=True)
    w.run(tmp_path, codex_exe="codex", thread="same", once=True)
    assert send.call_count == 1


def test_missing_engine_log_notifies_instead_of_crashing_monitor(tmp_path,monkeypatch):
    (tmp_path/'launch.json').write_text(json.dumps({'parent_pid':1}))
    (tmp_path/'status.json').write_text(json.dumps({'checked_ts':1}))
    monkeypatch.setattr(w,'process_alive',lambda pid:True)
    monkeypatch.setattr(w,'desktop_alert',Mock())
    send=Mock(return_value={'attempted':True});monkeypatch.setattr(w,'send_to_chat',send)
    w.run(tmp_path,codex_exe='codex',thread='same',once=True)
    assert 'engine_log_stale' in send.call_args.args[3]


def test_external_finalizer_terminal_state_takes_precedence_over_stale_status(tmp_path,monkeypatch):
    (tmp_path/'launch.json').write_text(json.dumps({'parent_pid':1}))
    (tmp_path/'status.json').write_text(json.dumps({'checked_ts':1,'phase':'running'}))
    (tmp_path/'stop-finalization.json').write_text(json.dumps({'ended_ts':99,'phase':'resolved'}))
    (tmp_path/'engine.log').write_text('')
    monkeypatch.setattr(w,'process_alive',lambda pid:False)
    monkeypatch.setattr(w,'desktop_alert',Mock())
    send=Mock(return_value={'attempted':True});monkeypatch.setattr(w,'send_to_chat',send)
    w.run(tmp_path,codex_exe='codex',thread='same',once=True)
    assert send.call_args.args[3]==['execution_ended:resolved']


def test_atomic_receipt_retries_windows_reader_lock_without_losing_state(tmp_path, monkeypatch):
    original = w.os.replace
    calls=[]
    def locked_twice(a,b):
        calls.append(1)
        if len(calls)<3:raise PermissionError("reader lock")
        original(a,b)
    monkeypatch.setattr(w.os,"replace",locked_twice)
    monkeypatch.setattr(w.time,"sleep",lambda seconds:None)
    w.atomic_save(tmp_path/'receipt.json', {'attempted':True})
    assert len(calls)==3 and json.loads((tmp_path/'receipt.json').read_text())['attempted']
