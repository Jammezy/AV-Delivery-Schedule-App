"""Single isolated solver process; never serves HTTP or starts another runner."""
import os
import signal
import sys

if __name__ == '__main__':
    if sys.platform.startswith('linux'):
        # Kill the child even if Gunicorn is abruptly killed before its exit hook.
        import ctypes
        parent = os.getppid()
        ctypes.CDLL(None).prctl(1, signal.SIGTERM)
        if os.getppid() != parent or parent == 1:
            sys.exit(1)
        os.nice(5)
    from generation_jobs import run_job
    run_job(sys.argv[1], sys.argv[2])
