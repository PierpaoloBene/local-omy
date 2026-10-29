"""Every test runs against a throwaway home and archive (mode 700, deleted at
the end): never ~/OmiArchive or ~/.config/local-omy. Set before omy is
imported, since its paths are read at import."""

import os
import shutil
import tempfile
from pathlib import Path

_root = Path(tempfile.mkdtemp(prefix="omy-tests-"))
_root.chmod(0o700)
os.environ["HOME"] = str(_root / "home")
os.environ["OMY_ARCHIVE"] = str(_root / "archive")
os.environ["OMY_TZ"] = "Europe/Rome"
(_root / "home").mkdir(mode=0o700)
(_root / "archive").mkdir(mode=0o700)
# Names in the archive in Italian, whatever the Mac's language.
(_root / "home" / ".config" / "local-omy").mkdir(mode=0o700, parents=True)
(_root / "home" / ".config" / "local-omy" / "config.toml").write_text('language = "it"\n')
os.environ.pop("OMY_LANG", None)

import pytest  # noqa: E402

ARCHIVE = _root / "archive"


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_root, ignore_errors=True)


@pytest.fixture
def archive():
    """The archive, emptied after the test."""
    yield ARCHIVE
    for p in ARCHIVE.iterdir():
        shutil.rmtree(p) if p.is_dir() else p.unlink()
