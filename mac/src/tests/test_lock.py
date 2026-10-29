"""One writer at a time, across processes; reentrant within one."""

import subprocess
import sys


def test_reentrant_and_exclusive(archive):
    from omy import lock
    probe = ("import fcntl, sys; f = open(sys.argv[1], 'w')\n"
             "try:\n    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\nexcept BlockingIOError:\n    sys.exit(3)")
    with lock.writing():
        with lock.writing():
            held = subprocess.run([sys.executable, "-c", probe, str(lock.LOCK_FILE)])
            assert held.returncode == 3
    free = subprocess.run([sys.executable, "-c", probe, str(lock.LOCK_FILE)])
    assert free.returncode == 0


def test_time_zone_from_the_environment(monkeypatch):
    from omy_receiver import localtime
    monkeypatch.setenv("OMY_TZ", "America/New_York")
    localtime.local_tz.cache_clear()
    try:
        assert str(localtime.local_tz()) == "America/New_York"
    finally:
        localtime.local_tz.cache_clear()
