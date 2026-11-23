"""Second brain: organizer (transcripts -> notes), vault (Markdown), index.

Interfaces between modules are fixed in INTERFACES.md.
"""

from omy.config import ARCHIVE

BRAIN = ARCHIVE / "brain"                 # the Obsidian-style vault (user-visible)
NOTES = BRAIN / "note"                    # note/AAAA/MM/GG/<HHMM>-<slug>-<id8>.{md,json}
WORK = ARCHIVE / "cache" / "organize"     # per-session checkpoints of the organizer (parts)
SCHEMA_VERSION = 1
