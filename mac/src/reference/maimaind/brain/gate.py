"""May the model be loaded now? Only when local-omy is not transcribing and
macOS is not short of memory. (M4 builds the scheduler on this.)"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from brain import omy
from brain.config import Settings

# kern.memorystatus_vm_pressure_level: 1 normal, 2 warning, 4 critical.
PRESSURE_NORMAL = 1
PRESSURE_CRITICAL = 4
# kern.memorystatus_level: the percentage of memory macOS considers available
# (what memory_pressure prints as "free percentage"). Calibrated with brain doctor.
MIN_AVAILABLE_PERCENT = 45


def _sysctl(name: str) -> int | None:
    r = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True)
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def memory() -> dict:
    vm = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    page = int(re.search(r"page size of (\d+)", vm).group(1)) if "page size of" in vm else 16384

    def pages(label: str) -> int:
        m = re.search(rf"{label}:\s+(\d+)", vm)
        return int(m.group(1)) if m else 0

    reclaimable = sum(pages(k) for k in ("Pages free", "Pages inactive", "Pages purgeable", "Pages speculative"))
    return {
        "pressure_level": _sysctl("kern.memorystatus_vm_pressure_level"),
        "available_percent": _sysctl("kern.memorystatus_level"),
        "reclaimable_gb": round(reclaimable * page / 2**30, 1),
        "total_gb": round((_sysctl("hw.memsize") or 0) / 2**30, 1),
    }


@dataclass
class Gate:
    open: bool
    reason: str
    omy: dict
    memory: dict


def check(settings: Settings, model_loaded: bool = False) -> Gate:
    try:
        progress = omy.transcription(settings)
    except omy.OmyError as e:
        # Not knowing is treated as busy: never compete with a transcription.
        return Gate(False, f"stato di local-omy sconosciuto ({e})", {}, memory())
    mem = memory()
    if progress.get("running"):
        return Gate(False, "local-omy sta trascrivendo", progress, mem)
    # With Gemma loaded on 16 GB, "warning" is the model itself (measured): only
    # critical stops the work then. Before loading, memory must be normal.
    level = mem["pressure_level"]
    if level is not None and (level >= PRESSURE_CRITICAL or (not model_loaded and level != PRESSURE_NORMAL)):
        return Gate(False, f"memoria sotto pressione (livello {level})", progress, mem)
    if not model_loaded and (mem["available_percent"] or 100) < MIN_AVAILABLE_PERCENT:
        return Gate(False, f"memoria disponibile {mem['available_percent']}% < {MIN_AVAILABLE_PERCENT}%",
                    progress, mem)
    return Gate(True, "ok", progress, mem)
