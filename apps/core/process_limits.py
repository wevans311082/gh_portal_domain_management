"""Bounded child processes for document engines; no shell or secret environment."""
import os
import signal
import subprocess
import threading
import time


class ProcessLimitError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _WindowsJob:
    """Kill-on-close job: descendants cannot keep pipes/processes alive."""
    def __init__(self, process):
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t), ("max_working", ctypes.c_size_t),
                        ("active_limit", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                        ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", Counters), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ProcessLimitError("containment_unavailable")
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            self.close()
            raise ProcessLimitError("containment_unavailable")

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def renderer_environment():
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL", "FONTCONFIG_PATH", "FONTCONFIG_FILE"}
    result = {name: value for name, value in os.environ.items() if name.upper() in allowed}
    result.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return result


def run_bounded(command, input_bytes=b"", *, timeout=15, max_input=1024 * 1024,
                max_output=10 * 1024 * 1024, max_stderr=64 * 1024, cwd=None):
    if not isinstance(input_bytes, bytes) or len(input_bytes) > max_input:
        raise ProcessLimitError("input_limit")
    if timeout <= 0 or min(max_output, max_stderr) < 1:
        raise ValueError("Process bounds must be positive")
    output, errors = bytearray(), bytearray()
    exceeded, io_error = threading.Event(), threading.Event()
    process, job = None, None
    threads = []
    deadline = time.monotonic() + timeout
    def drain(stream, buffer, limit):
        try:
            with stream:
                while True:
                    chunk = stream.read(16 * 1024)
                    if not chunk:
                        return
                    remaining = limit - len(buffer)
                    buffer.extend(chunk[:max(remaining, 0)])
                    if len(chunk) > remaining:
                        exceeded.set()
                        return
        except (OSError, ValueError):
            io_error.set()
    def feed():
        try:
            with process.stdin:
                process.stdin.write(input_bytes)
        except (OSError, ValueError):
            io_error.set()
    try:
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                shell=False, cwd=cwd, env=renderer_environment(), start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError:
            raise ProcessLimitError("engine_unavailable") from None
        if os.name == "nt":
            job = _WindowsJob(process)
        # Input is not released until containment is established.
        threads = [threading.Thread(target=drain, args=(process.stdout, output, max_output), daemon=True),
                   threading.Thread(target=drain, args=(process.stderr, errors, max_stderr), daemon=True),
                   threading.Thread(target=feed, daemon=True)]
        for thread in threads:
            thread.start()
        while process.poll() is None or any(thread.is_alive() for thread in threads):
            if exceeded.is_set():
                raise ProcessLimitError("output_limit")
            if time.monotonic() >= deadline:
                raise ProcessLimitError("timeout")
            time.sleep(0.01)
        if exceeded.is_set():
            raise ProcessLimitError("output_limit")
        if process.returncode != 0 or io_error.is_set():
            raise ProcessLimitError("engine_failed")
        return bytes(output)
    finally:
        if process is not None:
            if job is not None:
                job.close()
            elif os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=1)
            if not any(thread.is_alive() for thread in threads):
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
