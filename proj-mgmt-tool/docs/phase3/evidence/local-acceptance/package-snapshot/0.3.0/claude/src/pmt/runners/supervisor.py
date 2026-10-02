"""Hidden, bounded-output runner supervisor used by the file operation adapter."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import tempfile
import time
from datetime import datetime, timezone


_MAX_OUTPUT = 8 * 1024 * 1024
_STATE_LOCK = threading.RLock()


def _checked_path(path):
    from ..resources import _reject_links
    value = Path(path)
    _reject_links(value)
    return value


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _atomic_json(path, value):
    with _STATE_LOCK:
        return _publish_json(path, value)


def _publish_json(path, value):
    target = _checked_path(path)
    fd, name = tempfile.mkstemp(prefix=".pmt-state-", suffix=".tmp", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            stream.flush()
            os.fsync(stream.fileno())
        _checked_path(target)
        for attempt in range(5):
            try:
                os.replace(temporary, target)
                break
            except PermissionError as exc:
                if os.name != "nt" or getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 4:
                    raise
                time.sleep(0.01 * (2 ** attempt))
    finally:
        temporary.unlink(missing_ok=True)


def _safe_write_bytes(path, value):
    target = _checked_path(path)
    target.write_bytes(value)
    _checked_path(target)


def _safe_read_bytes(path):
    return _checked_path(path).read_bytes()


def _state(cfg, state, **extra):
    with _STATE_LOCK:
        _atomic_json(cfg["state_path"], {"state": state, "heartbeat_at": _now(),
                                         "heartbeat_epoch": time.time(), **extra})


def _heartbeat(cfg, stop):
    while not stop.wait(1):
        try:
            with _STATE_LOCK:
                state = json.loads(_checked_path(cfg["state_path"]).read_text(encoding="utf-8"))
                state.update(heartbeat_at=_now(), heartbeat_epoch=time.time())
                _atomic_json(cfg["state_path"], state)
        except (OSError, ValueError, json.JSONDecodeError):
            continue


def _bounded_read(stream, output):
    while True:
        data = stream.read(64 * 1024)
        if not data:
            return
        room = _MAX_OUTPUT - len(output)
        if room > 0:
            output.extend(data[:room])


def _win_job(process):
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class BasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [("ReadOperationCount", ctypes.c_ulonglong), ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong), ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong), ("OtherTransferCount", ctypes.c_ulonglong)]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateJobObjectW
    create.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    create.restype = wintypes.HANDLE
    job = create(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
    info = ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        kernel.CloseHandle(job)
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
    if not kernel.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
        kernel.CloseHandle(job)
        raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
    return job


def _terminate_tree(process, job):
    if os.name == "nt":
        if job:
            import ctypes
            ctypes.WinDLL("kernel32", use_last_error=True).TerminateJobObject(job, 1)
        else:
            process.terminate()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "nt" and job:
            import ctypes
            ctypes.WinDLL("kernel32", use_last_error=True).TerminateJobObject(job, 1)
        elif os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=5)


def run(cfg, prompt):
    """Run one exact selected route and write a final factual receipt."""
    state_path = Path(cfg["state_path"])
    control_path = Path(cfg["control_path"])
    Path(cfg["stdout_path"]).parent.mkdir(parents=True, exist_ok=True)
    _state(cfg, "starting", supervisor_pid=os.getpid())

    def canceled():
        try:
            control = json.loads(_checked_path(control_path).read_text(encoding="utf-8"))
            return control.get("cancel_requested") is True
        except (OSError, ValueError, json.JSONDecodeError):
            return False

    started = _now()
    exit_code = None
    output_state = "unknown"
    error_code = None
    if cfg.get("contract_fixture") and cfg.get("fixture_process"):
        fixture_stdout = cfg.get("fixture_stdout", "")
        fixture_stderr = cfg.get("fixture_stderr", "")
        fixture_delay = cfg.get("fixture_delay", 0.1)
        if (not isinstance(fixture_stdout, str) or len(fixture_stdout.encode("utf-8")) > _MAX_OUTPUT
                or not isinstance(fixture_stderr, str) or len(fixture_stderr.encode("utf-8")) > _MAX_OUTPUT
                or type(fixture_delay) not in {int, float} or fixture_delay < 0 or fixture_delay > 30):
            raise ValueError("invalid local process fixture")
        flags = ((getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                  | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                  | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)) if os.name == "nt" else 0)
        child_code = ("import sys,time;sys.stdout.write(sys.argv[1]);sys.stderr.write(sys.argv[2]);"
                      "sys.stdout.flush();sys.stderr.flush();time.sleep(float(sys.argv[3]))")
        child = subprocess.Popen([sys.executable, "-c", child_code, fixture_stdout, fixture_stderr, str(fixture_delay)], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
                                 creationflags=flags, start_new_session=(os.name != "nt"))
        try:
            job = _win_job(child)
        except OSError:
            child.kill(); child.wait(timeout=5); raise
        stdout, stderr = bytearray(), bytearray()
        out_thread = threading.Thread(target=_bounded_read, args=(child.stdout, stdout), daemon=True)
        err_thread = threading.Thread(target=_bounded_read, args=(child.stderr, stderr), daemon=True)
        out_thread.start(); err_thread.start()
        _state(cfg, "running", supervisor_pid=os.getpid(), child_pid=child.pid, fixture=True)
        while child.poll() is None:
            if canceled():
                _terminate_tree(child, job)
                output_state, exit_code = "canceled", child.returncode
                break
            time.sleep(0.05)
        if exit_code is None:
            exit_code = child.wait()
            output_state = "completed" if exit_code == 0 else "failed"
        out_thread.join(timeout=2); err_thread.join(timeout=2)
        _safe_write_bytes(cfg["stdout_path"], stdout)
        _safe_write_bytes(cfg["stderr_path"], stderr)
        if job:
            import ctypes
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(job)
    else:
        flags = 0
        startupinfo = None
        if os.name == "nt":
            flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                     | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                     | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000))
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        try:
            child = subprocess.Popen([cfg["executable"], *cfg["command"]], cwd=cfg["workspace"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     close_fds=True, creationflags=flags, startupinfo=startupinfo,
                                     start_new_session=(os.name != "nt"))
            try:
                job = _win_job(child)
            except OSError:
                child.kill()
                child.wait(timeout=5)
                raise
            stdout, stderr = bytearray(), bytearray()
            out_thread = threading.Thread(target=_bounded_read, args=(child.stdout, stdout), daemon=True)
            err_thread = threading.Thread(target=_bounded_read, args=(child.stderr, stderr), daemon=True)
            out_thread.start(); err_thread.start()
            try:
                child.stdin.write(prompt.encode("utf-8"))
                child.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            _state(cfg, "running", supervisor_pid=os.getpid(), child_pid=child.pid)
            while child.poll() is None:
                if canceled():
                    _terminate_tree(child, job)
                    output_state = "canceled"
                    break
                _state(cfg, "running", supervisor_pid=os.getpid(), child_pid=child.pid)
                time.sleep(0.5)
            exit_code = child.wait()
            out_thread.join(timeout=3); err_thread.join(timeout=3)
            if out_thread.is_alive() or err_thread.is_alive():
                # The CLI process exited while a descendant still held its pipes.
                # The route owns this process group, so stop descendants and retain
                # the outcome as unresolved rather than silently dropping them.
                _terminate_tree(child, job)
                out_thread.join(timeout=3); err_thread.join(timeout=3)
                output_state = "unknown"
                error_code = "runner_descendant_process_unresolved"
            _safe_write_bytes(cfg["stdout_path"], stdout)
            _safe_write_bytes(cfg["stderr_path"], stderr)
            if output_state not in {"canceled", "unknown"}:
                output_state = "completed" if exit_code == 0 else "failed"
            if job:
                import ctypes
                ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(job)
        except OSError:
            output_state, exit_code, error_code = "failed", None, "runner_process_start_failed"

    completed = _now()
    stdout_path, stderr_path = Path(cfg["stdout_path"]), Path(cfg["stderr_path"])
    stdout = _safe_read_bytes(stdout_path) if stdout_path.exists() else b""
    stderr = _safe_read_bytes(stderr_path) if stderr_path.exists() else b""
    try:
        from .service import _extract_text, _parse_batch_report, _parse_report, _scrub_sensitive
        final_message = _extract_text(stdout.decode("utf-8", errors="replace"), cfg.get("agent"))
        if cfg.get("batch_report_schema") == "pmt-batch-report-v1":
            binding = {"batch_id": cfg.get("batch_ref"), "members": cfg.get("batch_members", [])}
            report = _parse_batch_report(final_message, binding)
        else:
            report = _parse_report(final_message, cfg.get("criteria_ids", []))
        safe_output = {"final_message": _scrub_sensitive(final_message[:16000]),
                       "model_report": report, "report_valid": report is not None,
                       "stderr_note": {"present": bool(stderr), "byte_count": len(stderr),
                                       "classification": "redacted_runner_stderr" if stderr else "none"}}
        _atomic_json(cfg["output_path"], safe_output)
    except Exception:
        # The result may be unavailable, but raw process traces are never kept as a fallback.
        try:
            Path(cfg["output_path"]).unlink(missing_ok=True)
        except OSError:
            pass
    receipt = {"run_id": cfg["run_id"], "runner_kind": cfg["runner_kind"],
               "state": output_state, "exit_code": exit_code,
               "supervisor_pid": os.getpid(),
               "started_at": started, "completed_at": completed,
               "contract_fixture": bool(cfg.get("contract_fixture")), "error_code": error_code}
    _atomic_json(cfg["receipt_path"], receipt)
    _state(cfg, output_state, supervisor_pid=os.getpid(), completed_at=completed, exit_code=exit_code)
    stdout_path.unlink(missing_ok=True)
    stderr_path.unlink(missing_ok=True)
    return receipt


def _load_config(path):
    cfg = json.loads(_checked_path(path).read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError("runner config must be an object")
    raw_root = Path(cfg.get("spool_root", ""))
    _checked_path(raw_root)
    root = raw_root.resolve()
    if not root.is_absolute():
        raise ValueError("runner spool root must be absolute")
    for key in ("state_path", "receipt_path", "stdout_path", "stderr_path", "output_path", "control_path"):
        raw_candidate = Path(cfg.get(key, ""))
        _checked_path(raw_candidate)
        candidate = raw_candidate.resolve()
        if not candidate.is_relative_to(root):
            raise ValueError("runner file path must stay inside spool")
        _checked_path(candidate)
    return cfg


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        return 2
    cfg = None
    stop = threading.Event()
    heartbeat = None
    try:
        cfg = _load_config(args[0])
        heartbeat = threading.Thread(target=_heartbeat, args=(cfg, stop), daemon=True)
        heartbeat.start()
        prompt = sys.stdin.buffer.read(1024 * 1024 + 1).decode("utf-8")
        if len(prompt.encode("utf-8")) > 1024 * 1024:
            raise ValueError("prompt exceeds limit")
        run(cfg, prompt)
        return 0
    except Exception:
        # No exception or response body is written to diagnostic output.
        try:
            if cfg is None:
                cfg = _load_config(args[0])
            Path(cfg["stdout_path"]).unlink(missing_ok=True)
            Path(cfg["stderr_path"]).unlink(missing_ok=True)
            Path(cfg["output_path"]).unlink(missing_ok=True)
            stop.set()
            if heartbeat:
                heartbeat.join(timeout=2)
            _state(cfg, "unknown", supervisor_pid=os.getpid(), completed_at=_now())
            _atomic_json(cfg["receipt_path"], {"run_id": cfg.get("run_id"), "runner_kind": cfg.get("runner_kind"),
                "state": "unknown", "exit_code": None, "completed_at": _now(), "error_code": "runner_outcome_unknown"})
        except Exception:
            pass
        return 1
    finally:
        stop.set()
        if heartbeat:
            heartbeat.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
