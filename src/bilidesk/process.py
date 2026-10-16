"""Private Windows Job: child waits for stdin until attached by its parent."""
import ctypes
from ctypes import wintypes
import os


class Job:
    def __init__(self):
        self.handle=None
        if os.name!='nt':
            return
        class Basic(ctypes.Structure):
            _fields_=[('a',ctypes.c_longlong),('b',ctypes.c_longlong),('flags',wintypes.DWORD),
                      ('min',ctypes.c_size_t),('max',ctypes.c_size_t),('count',wintypes.DWORD),
                      ('affinity',ctypes.c_size_t),('priority',wintypes.DWORD),('scheduling',wintypes.DWORD)]
        class Extended(ctypes.Structure):
            _fields_=[('basic',Basic),('io',ctypes.c_ulonglong*6),('memory',ctypes.c_size_t*4)]
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype=wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        self.kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        self.kernel.OpenProcess.restype=wintypes.HANDLE
        self.handle=self.kernel.CreateJobObjectW(None,None)
        limits=Extended(); limits.basic.flags=0x2000
        if not self.handle or not self.kernel.SetInformationJobObject(self.handle,9,ctypes.byref(limits),ctypes.sizeof(limits)):
            self.close(); raise RuntimeError('job_creation_failed')

    def assign(self,pid):
        if self.handle:
            process=self.kernel.OpenProcess(0x0100|0x0001,False,pid)
            try:
                if not process or not self.kernel.AssignProcessToJobObject(self.handle,process):
                    raise RuntimeError('job_assignment_failed')
            finally:
                if process:
                    self.kernel.CloseHandle(process)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle); self.handle=None
