import json
import pytest
from olbot import trial


def test_status_save_retries_transient_windows_reader_lock(tmp_path,monkeypatch):
    path=tmp_path/'status.json';path.write_text('{"old":true}')
    real=trial.os.replace;calls=[]
    def replace(a,b):
        calls.append(1)
        if len(calls)<3:raise PermissionError('sharing violation')
        real(a,b)
    monkeypatch.setattr(trial.os,'replace',replace)
    monkeypatch.setattr(trial.time,'sleep',lambda seconds:None)
    trial.save(path,{'checked':1})
    assert len(calls)==3 and json.loads(path.read_text())=={'checked':1}


def test_status_save_surfaces_persistent_error_and_preserves_both_records(tmp_path,monkeypatch):
    path=tmp_path/'status.json';path.write_text('{"old":true}')
    def replace(a,b):raise PermissionError('persistent lock')
    monkeypatch.setattr(trial.os,'replace',replace)
    monkeypatch.setattr(trial.time,'sleep',lambda seconds:None)
    with pytest.raises(PermissionError):trial.save(path,{'checked':1})
    assert json.loads(path.read_text())=={'old':True}
    assert json.loads(path.with_suffix('.tmp').read_text())=={'checked':1}
