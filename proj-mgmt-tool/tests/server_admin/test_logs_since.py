from datetime import datetime, timedelta, timezone
import json
import pytest
from pmt.server_admin.logging import read_logs


def _entry(at, message):
    return json.dumps({"at_utc": at.isoformat(), "message": message}) + "\n"


def test_since_reads_rotation_in_chronological_order_and_redacts(tmp_path):
    now = datetime(2026, 10, 8, 0, 30, tzinfo=timezone.utc)
    (tmp_path / "host.log.2026-10-07").write_text(
        _entry(now-timedelta(hours=3), "old") + _entry(now-timedelta(minutes=50), "credential=private-test-value"), encoding="utf-8")
    (tmp_path / "host.log").write_text(_entry(now-timedelta(minutes=10), "new"), encoding="utf-8")
    result = read_logs(tmp_path, since="1h", now=now)
    assert len(result) == 2 and "private-test-value" not in "".join(result)
    assert "[REDACTED]" in result[0] and "new" in result[1]
    assert read_logs(tmp_path, tail=1, since="1h", now=now) == result[-1:]


@pytest.mark.parametrize("value", ["", "0h", "-1h", "all", "1.5h", "366d"])
def test_invalid_duration_rejected_without_creating_log(tmp_path, value):
    with pytest.raises(ValueError):
        read_logs(tmp_path, since=value)
    assert not list(tmp_path.iterdir())


def test_log_memory_and_output_are_bounded_and_oversized_line_not_leaked(tmp_path):
    path = tmp_path / "host.log"
    path.write_text("x"*20000+"\n"+"\n".join("entry "+str(i)+" "+("z"*14000) for i in range(120))+"\n", encoding="utf-8")
    result = read_logs(tmp_path, tail=1000)
    assert sum(len(line.encode("utf-8")) for line in result) <= 1024*1024
    assert len(result) < 120 and "entry 119" in result[-1]
    assert "x"*20000 not in "".join(result)
