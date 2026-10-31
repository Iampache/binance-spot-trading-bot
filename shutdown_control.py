"""Announce a cooperative stop before waiting for network calls/worker journals."""

import os
import signal


def install(on_stop):
    previous = {}

    def handler(signum, frame):
        on_stop()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, handler)
    native = None
    if os.name == "nt":
        # Python's main-thread signal handler can wait for a blocking Winsock
        # read. The Windows console callback runs on its own OS thread instead.
        import ctypes
        from ctypes import wintypes

        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def console_event(event):
            if event in (0, 1):  # CTRL_C_EVENT / CTRL_BREAK_EVENT only.
                on_stop()
                return True
            return False

        callback = callback_type(console_event)
        register = ctypes.WinDLL("kernel32", use_last_error=True).SetConsoleCtrlHandler
        register.argtypes = (callback_type, wintypes.BOOL)
        register.restype = wintypes.BOOL
        if register(callback, True):
            native = (register, callback)  # Keep the callback alive until cleanup.

    def restore():
        if native:
            native[0](native[1], False)
        for signum, old in previous.items():
            signal.signal(signum, old)

    return restore
