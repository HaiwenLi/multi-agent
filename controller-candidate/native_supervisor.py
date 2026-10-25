"""Candidate-only local Windows Job supervision; no native agents launched on import."""
import _winapi
import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path
import subprocess
import time
from dataclasses import dataclass


def native_argv(seat: str, session: str, prompt: str) -> list[str]:
    if not session or not prompt:
        raise ValueError('explicit session and brief required')
    if seat in ('M', 'E1'):
        return ['opencode', 'run', '--standalone', '--format', 'json',
                '--session', session, prompt]
    if seat in ('E2', 'R'):
        return ['pi', '--print', '--mode', 'json',
                '--session-id', session, prompt]
    raise ValueError('unselected seat')


class Basic(c.Structure):
    _fields_ = [('ProcessTime', c.c_int64), ('JobTime', c.c_int64), ('Flags', w.DWORD),
                ('MinWS', c.c_size_t), ('MaxWS', c.c_size_t), ('Processes', w.DWORD),
                ('Affinity', c.c_size_t), ('Priority', w.DWORD), ('Scheduling', w.DWORD)]


class IO(c.Structure):
    _fields_ = [(name, c.c_uint64) for name in ('ReadOps', 'WriteOps', 'OtherOps', 'ReadBytes', 'WriteBytes', 'OtherBytes')]


class Limits(c.Structure):
    _fields_ = [('Basic', Basic), ('IO', IO), ('ProcessMemory', c.c_size_t),
                ('JobMemory', c.c_size_t), ('PeakProcessMemory', c.c_size_t), ('PeakJobMemory', c.c_size_t)]


class Accounting(c.Structure):
    _fields_ = [('UserTime', c.c_int64), ('KernelTime', c.c_int64), ('PeriodUserTime', c.c_int64),
                ('PeriodKernelTime', c.c_int64), ('PageFaults', w.DWORD), ('TotalProcesses', w.DWORD),
                ('ActiveProcesses', w.DWORD), ('TerminatedProcesses', w.DWORD)]


k = c.WinDLL('kernel32', use_last_error=True)
k.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
k.CreateJobObjectW.restype = w.HANDLE
k.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
k.QueryInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p]
k.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
k.CloseHandle.argtypes = [w.HANDLE]
k.ResumeThread.argtypes = [w.HANDLE]
k.ResumeThread.restype = w.DWORD


def checked(value):
    if not value:
        raise c.WinError(c.get_last_error())
    return value


class Job:
    def __init__(self, argv, cwd):
        self.handle = checked(k.CreateJobObjectW(None, None))
        self.process = None
        try:
            limits = Limits()
            limits.Basic.Flags = 0x8 | 0x200 | 0x2000  # active-process, job memory, kill-on-close
            limits.Basic.Processes = 64
            limits.JobMemory = 4 * 1024**3
            checked(k.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)))
            # Suspend before assignment: no child can escape between create and Job attach.
            with open(os.devnull, 'rb') as inp, open(os.devnull, 'wb') as out:
                for fd in (inp.fileno(), out.fileno()):
                    os.set_inheritable(fd, True)
                info = subprocess.STARTUPINFO()
                info.dwFlags = subprocess.STARTF_USESTDHANDLES
                import msvcrt
                info.hStdInput = msvcrt.get_osfhandle(inp.fileno())
                info.hStdOutput = info.hStdError = msvcrt.get_osfhandle(out.fileno())
                hp, ht, self.pid, _ = _winapi.CreateProcess(
                    None, subprocess.list2cmdline(list(map(str, argv))), None, None,
                    True, 0x4 | 0x08000000, dict(os.environ), str(cwd), info)
                self.process = hp
                try:
                    checked(k.AssignProcessToJobObject(self.handle, hp))
                    if k.ResumeThread(ht) == 0xFFFFFFFF:
                        raise c.WinError(c.get_last_error())
                except Exception:
                    _winapi.TerminateProcess(hp, 125)
                    raise
                finally:
                    _winapi.CloseHandle(ht)
        except Exception:
            self.close()
            raise

    def active(self):
        value = Accounting()
        checked(k.QueryInformationJobObject(self.handle, 1, c.byref(value), c.sizeof(value), None))
        return value.ActiveProcesses

    def stop(self):
        checked(k.TerminateJobObject(self.handle, 130))
        deadline = time.monotonic() + 15
        while self.active() and time.monotonic() < deadline:
            time.sleep(.05)
        return self.active()

    def close(self):
        if self.handle:
            k.CloseHandle(self.handle)
            self.handle = None
        if self.process:
            _winapi.CloseHandle(self.process)
            self.process = None


@dataclass(frozen=True)
class StopEvidence:
    seat: str
    reason: str
    freeze_seconds: float
    active: int | None
    stable: bool


class Supervisor:
    def __init__(self):
        self.jobs = {}
        self.frozen = set()
        self.verified = set()
        self.frozen_at = {}
        self.freeze_samples = []

    def launch(self, seat: str, argv: list[str], cwd: Path):
        if seat in self.frozen or (seat in self.jobs and self.jobs[seat].active()):
            raise ValueError('seat frozen or already busy')
        if seat in self.jobs:
            self.jobs[seat].close()
        self.jobs[seat] = Job(argv, cwd)

    def freeze(self, seat: str, reason: str):
        receipt = time.monotonic()
        self.frozen.add(seat)
        self.frozen_at[seat] = (receipt, reason)
        latency = time.monotonic() - receipt
        self.freeze_samples.append(latency)
        return latency

    def mark_verified(self, seat, *, active, children_stopped, services_stopped):
        if (seat in self.frozen and active == 0 and children_stopped is True
                and services_stopped is True):
            self.verified.add(seat)
            return True
        return False

    def request_stop(self, seat: str, reason: str):
        latency = self.freeze(seat, reason)
        job = self.jobs.get(seat)
        if job is None:
            return StopEvidence(seat, reason, latency, None, False)
        active = job.stop()
        # Real pilot must separately verify any run-scoped external service/listener.
        stable = active == 0
        if stable:
            time.sleep(5)
            stable = job.active() == 0
        if self.mark_verified(seat, active=active, children_stopped=stable,
                              services_stopped=False):  # unknown service => block transfer
            pass
        return StopEvidence(seat, reason, latency, active, stable)

    def can_reassign(self, task: str, state=None):
        # A verified outgoing seat is necessary, never sufficient by itself.
        if not state or task not in state.get('tasks', {}):
            return False
        assignment = state['tasks'][task]
        return (assignment.get('previous_owner') in self.verified and assignment.get('stopped') is True
                and assignment.get('epoch', 0) > 1
                and assignment.get('ack') == [task, assignment.get('owner'),
                                               assignment['epoch'], assignment.get('revision')])

    def close(self):
        for job in self.jobs.values():
            job.close()
