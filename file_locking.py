"""OS-backed locks on Windows and POSIX; lock files are never deleted.

Keep the lock object alive until the protected work finishes. The descriptor is
non-inheritable, and the OS releases its locks when the process terminates,
including termination that bypasses Python finally blocks.
"""

import os

import portalocker


class ProcessLock:
    def __init__(self, path, *, blocking=False):
        self.fp = path.open("a+b")
        try:
            os.set_inheritable(self.fp.fileno(), False)
            flags = portalocker.LOCK_EX
            if not blocking:
                flags |= portalocker.LOCK_NB
            portalocker.lock(self.fp, flags)
        except portalocker.exceptions.AlreadyLocked:
            self.fp.close()
            raise FileExistsError("Another instance holds the account lock") from None
        except BaseException:
            self.fp.close()
            raise

    def release(self):
        if not self.fp.closed:
            try:
                portalocker.unlock(self.fp)
            finally:
                # Closing the handle also releases the OS lock if unlock fails.
                self.fp.close()

    def unlink(self, missing_ok=True):
        """Compatibility alias: release the lock, never unlink the lock file."""
        self.release()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
