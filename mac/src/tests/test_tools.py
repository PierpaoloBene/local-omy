"""tools/verify_archive.py and tools/vault_mirror.sh."""

import json
import subprocess
import sys
from pathlib import Path

from helpers import write_segments

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def run_verify(archive, *extra):
    p = subprocess.run([sys.executable, str(TOOLS / "verify_archive.py"), str(archive), *extra],
                       capture_output=True, text=True)
    return p.returncode, json.loads(p.stdout)


def test_verify_counts_and_finds_corruption(archive):
    ids = write_segments(archive, n=3)
    rc, out = run_verify(archive)
    assert rc == 0 and out["bin"] == 3 and out["json"] == 3 and out["days"] == 1 and out["problems"] == []
    same = run_verify(archive)[1]["manifest_sha256"]
    assert same == out["manifest_sha256"]
    bin_path = next((archive / "raw" / "audio").rglob(f"{ids[1]}.bin"))
    bin_path.write_bytes(b"corrupt")
    rc, out = run_verify(archive)
    assert rc == 1 and out["problems"] == [{"segment": ids[1], "problem": "sha256 differs from the sidecar"}]
    assert out["manifest_sha256"] != same
    bin_path.with_suffix(".json").unlink()
    assert run_verify(archive)[1]["problems"][0]["problem"] == "no readable sidecar"


def test_vault_mirror_is_read_only_and_keeps_obsidian_config(tmp_path):
    """The script, with a local path standing in for hub:path (a leading "/" after an empty host is a local path to rsync -e)."""
    src, dest = tmp_path / "hub" / "brain", tmp_path / "OmiBrain"
    (src / "note").mkdir(parents=True)
    (src / "note" / "a.md").write_text("# a")
    (dest / ".obsidian").mkdir(parents=True)
    (dest / ".obsidian" / "app.json").write_text("{}")
    (dest / "stale.md").write_text("deleted on the hub")
    # a fake ssh-free rsync: the script calls `rsync ... "$HUB:$SRC" "$DEST"`; a shim drops the "host:" part
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "rsync").write_text('#!/bin/bash\nargs=()\nfor a in "$@"; do args+=("${a#*:}"); done\n'
                                'for i in "${!args[@]}"; do [[ "${args[$i]}" == -e ]] && unset "args[$i]" "args[$((i+1))]"; done\n'
                                'exec /usr/bin/rsync "${args[@]}"\n')
    (shim / "rsync").chmod(0o755)
    (shim / "ssh").write_text('#!/bin/bash\nexec bash -c "${@: -1}"\n')   # "ssh host CMD" runs CMD here
    (shim / "ssh").chmod(0o755)
    env = {"HUB": "nobody@nowhere", "SRC": f"{src}/", "DEST": f"{dest}/", "PATH": f"{shim}:/usr/bin:/bin", "HOME": str(tmp_path)}
    script = TOOLS / "vault_mirror.sh"
    assert "BatchMode=yes" in script.read_text()
    subprocess.run(["bash", str(script)], check=True, env=env, capture_output=True)
    assert (dest / "note" / "a.md").read_text() == "# a" and not (dest / "stale.md").exists()
    assert (dest / ".obsidian" / "app.json").exists()
    assert not (dest / "note" / "a.md").stat().st_mode & 0o222          # read-only
    (src / "note" / "a.md").write_text("# a, changed")                   # the next run still replaces it
    subprocess.run(["bash", str(script)], check=True, env=env, capture_output=True)
    assert (dest / "note" / "a.md").read_text() == "# a, changed"
    (dest / "note" / "a.md").chmod(0o644)
    (dest / "note" / "a.md").write_text("edited in Obsidian")            # what you change here is overwritten
    subprocess.run(["bash", str(script)], check=True, env=env, capture_output=True)
    assert (dest / "note" / "a.md").read_text() == "# a, changed"
    # an empty vault on the hub leaves the copy alone
    (src / "note" / "a.md").unlink()
    (src / "note").rmdir()
    failed = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert failed.returncode == 1 and (dest / "note" / "a.md").exists()
