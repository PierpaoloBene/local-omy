"""Documents on the Mac: found, read, classified, findable, sendable.

Files are never copied. What is kept: the path, the size and mtime, the sha256
of the content, the extracted text in chunks (for search) and a card in the
vault (documents/). The sha256 is the document's identity:
- same path, same size and mtime: nothing to do;
- a known sha256 at a new path, and the old path gone: moved (the card follows);
- same path, different content: modified (a new version; the old one is kept
  as "modified" so that what was said about it still makes sense);
- a path gone and its content nowhere: missing.

Only files under doc_roots, outside doc_exclude, with an extension of doc_exts
and at most doc_max_mb are considered: allowed() is the one rule, used by the
scan and by the API before serving a file. Skipped as well: hidden folders,
folders of code (with a .git), dependencies and app bundles.

Reading: MD and TXT as text, DOCX from its XML (standard library), PDF and
images with helpers/extract (PDFKit, and Vision OCR for scanned pages).
Classifying: the model reads the beginning and fills the card (kind, date,
year, counterparts, a sentence), as JSON constrained by a schema.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import email
import email.policy
import zipfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

from brain import dates, vault
from brain.config import Settings, private_dir
from brain.llm import LLM
from brain.summarize import slug

REPO = Path(__file__).resolve().parent.parent
HELPER_SRC = REPO / "helpers" / "extract" / "main.swift"
SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", "build", "DerivedData", "Pods", "site-packages",
             "dist", "target"}
# Bundles: folders that are one thing to the Finder (iWork packages and RTFD
# included: the single-file iWork format is read, the package is not).
SKIP_SUFFIXES = (".app", ".photoslibrary", ".musiclibrary", ".tvlibrary", ".xcodeproj", ".xcworkspace", ".bundle",
                 ".framework", ".pkg", ".lproj", ".imovielibrary", ".fcpbundle", ".pages", ".numbers", ".key",
                 ".rtfd", ".playground", ".xcassets", ".pkpass", ".epub")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".heic", ".tiff", ".tif", ".webp"}
SF_DATALESS = 0x40000000
CHUNK = 1500
OVERLAP = 200
KINDS = ["contratto", "fattura", "ricevuta", "bolletta", "estratto conto", "busta paga", "dichiarazione dei redditi",
         "modello F24", "documento d'identità", "tessera", "certificato", "attestato o titolo di studio",
         "referto medico", "prescrizione medica", "lettera", "email", "modulo", "preventivo", "polizza", "manuale",
         "appunti", "articolo", "libro", "presentazione", "foglio di calcolo", "curriculum", "piano o programma",
         "screenshot", "altro"]
# Areas of life, as the folders of a tidy Documents folder would be.
AREAS = ["casa", "salute", "lavoro", "finanze e tasse", "identità e anagrafica", "auto e trasporti", "studio",
         "famiglia", "viaggi", "progetti personali", "acquisti", "svago", "altro"]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS documents(
  sha256 TEXT PRIMARY KEY, path TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
  ext TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('new', 'ok', 'moved', 'modified', 'missing', 'unreadable')),
  title TEXT, kind TEXT, doc_date TEXT, ref_year INTEGER, counterparts TEXT, summary TEXT,
  pages INTEGER, ocr INTEGER, extracted INTEGER NOT NULL DEFAULT 0, classified INTEGER NOT NULL DEFAULT 0,
  previous_path TEXT, error TEXT, first_seen TEXT NOT NULL, updated TEXT NOT NULL,
  -- area: see AREAS. text_hash: of the extracted text, normalized; the same text in
  -- another file (a compressed copy, a re-export) makes it a duplicate_of that one.
  area TEXT, text_hash TEXT, duplicate_of TEXT);
CREATE INDEX IF NOT EXISTS documents_path ON documents(path);
CREATE INDEX IF NOT EXISTS documents_text_hash ON documents(text_hash);
-- Every path where a known document was last seen (copies included).
CREATE TABLE IF NOT EXISTS doc_paths(
  path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS doc_chunks(
  id INTEGER PRIMARY KEY, doc TEXT NOT NULL, ord INTEGER NOT NULL, page INTEGER, text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS doc_chunks_doc ON doc_chunks(doc);
CREATE VIRTUAL TABLE IF NOT EXISTS doc_chunks_fts USING fts5(
  text, content='doc_chunks', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS doc_chunks_ai AFTER INSERT ON doc_chunks BEGIN
  INSERT INTO doc_chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS doc_chunks_ad AFTER DELETE ON doc_chunks BEGIN
  INSERT INTO doc_chunks_fts(doc_chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
-- The card's words (title, kind, counterparts, summary, file name), for find_document.
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
  sha256 UNINDEXED, text, tokenize='unicode61 remove_diacritics 2');
"""


class DocError(Exception):
    pass


# Columns added after the first version, for an index created before them.
ADDED = {"area": "TEXT", "text_hash": "TEXT", "duplicate_of": "TEXT"}


def init(conn: sqlite3.Connection) -> None:
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'documents'").fetchone()
    if exists:
        have = {r[1] for r in conn.execute("PRAGMA table_info(documents)")}
        for col, kind in ADDED.items():
            if col not in have:
                conn.execute(f"ALTER TABLE documents ADD COLUMN {col} {kind}")
    conn.executescript(SCHEMA)


def place(path: str) -> str:
    """Where a file is, as the Finder would say it: iCloud Drive/Documenti/…, ~/Downloads/…"""
    home = str(Path.home())
    mobile = f"{home}/Library/Mobile Documents/"
    if path.startswith(mobile + "com~apple~CloudDocs/"):
        return "iCloud Drive/" + path[len(mobile + "com~apple~CloudDocs/"):]
    if path.startswith(mobile):
        app, _, rest = path[len(mobile):].partition("/")
        return f"iCloud Drive/{app.split('~')[-1].capitalize()}/" + rest.removeprefix("Documents/")
    cloud = f"{home}/Library/CloudStorage/"
    if path.startswith(cloud):
        provider, _, rest = path[len(cloud):].partition("/")
        return f"{provider.split('-')[0]}/{rest}"
    return "~" + path[len(home):] if path.startswith(home + "/") else path


def text_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.casefold().split())[:50000].encode()).hexdigest()[:24]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- what is allowed ------------------------------------------------------------------------

class Rules:
    def __init__(self, settings: Settings):
        self.roots = [Path(r).expanduser().resolve() for r in settings.doc_roots]
        self.exclude = [Path(e).expanduser().resolve() for e in settings.doc_exclude]
        self.exts = {e.lower() for e in settings.doc_exts}
        if not settings.doc_images:
            self.exts -= IMAGE_EXTS
        self.max_bytes = settings.doc_max_mb * 2**20
        self.downloads = settings.doc_icloud_downloads

    def _root_of(self, d: Path) -> Path | None:
        """The deepest root holding d: ~/Library/Mobile Documents rather than ~."""
        inside = [r for r in self.roots if r == d or r in d.parents]
        return max(inside, key=lambda r: len(r.parts)) if inside else None

    def excluded(self, d: Path) -> bool:
        """An exclusion covers d unless d sits in a root that is itself inside the
        exclusion: ~/Library is excluded, ~/Library/Mobile Documents is a root."""
        root = self._root_of(d)
        return any((d == e or e in d.parents) and not (root is not None and e in root.parents) for e in self.exclude)

    def skip_dir(self, d: Path) -> bool:
        name = d.name
        return (name.startswith(".") or name in SKIP_DIRS or name.lower().endswith(SKIP_SUFFIXES)
                or (d / ".git").exists() or self.excluded(d))

    def allowed(self, path: str | Path) -> bool:
        """The one rule: may brain read this file, and serve it to the phone?"""
        try:
            p = Path(path)
            if p.is_symlink():
                return False
            real = p.resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        if real.suffix.lower() not in self.exts or not real.is_file():
            return False
        root = self._root_of(real)
        if root is None:
            return False
        for parent in real.parents:
            if parent == root:
                break
            if self.skip_dir(parent):
                return False
        return real.stat().st_size <= self.max_bytes

    def walk(self):
        """Every allowed file under the roots: (path, stat). A root inside another
        is walked on its own, once."""
        for root in self.roots:
            if not root.is_dir() or self.excluded(root):
                continue
            stack = [root]
            while stack:
                d = stack.pop()
                try:
                    entries = list(os.scandir(d))
                except OSError:
                    continue
                for e in entries:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir():
                            sub = Path(e.path)
                            if sub not in self.roots and not self.skip_dir(sub):
                                stack.append(sub)
                        elif e.is_file() and Path(e.name).suffix.lower() in self.exts and not e.name.startswith("."):
                            st = e.stat()
                            if st.st_size <= self.max_bytes:
                                yield e.path, st
                    except OSError:
                        continue


def dataless(st: os.stat_result) -> bool:
    """An iCloud file whose content is not on this Mac: reading it downloads it."""
    return bool(getattr(st, "st_flags", 0) & SF_DATALESS)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


# --- scanning --------------------------------------------------------------------------------

def scan(conn: sqlite3.Connection, settings: Settings, dry_run: bool = False) -> dict:
    """Bring the documents table up to date with the disk. No model, no reading
    of the content beyond hashing what changed."""
    init(conn)
    rules = Rules(settings)
    report = {"seen": 0, "new": 0, "moved": 0, "modified": 0, "missing": 0, "copies": 0}
    known = {r["path"]: r for r in conn.execute("SELECT * FROM doc_paths")}
    seen: set[str] = set()
    found: list[tuple[str, os.stat_result]] = []
    downloads = 0
    for path, st in rules.walk():
        report["seen"] += 1
        seen.add(path)
        k = known.get(path)
        if k is None or (k["size"], k["mtime_ns"]) != (st.st_size, st.st_mtime_ns):
            if dataless(st):
                # In iCloud only: hashing it downloads it. A few per scan, the rest later.
                downloads += 1
                if downloads > rules.downloads:
                    continue
            found.append((path, st))
    if downloads > rules.downloads:
        report["icloud_later"] = downloads - rules.downloads
    if dry_run:
        report["to_hash"] = len(found)
        return report
    t = now()
    for path, st in found:
        try:
            sha = sha256_file(path)
        except OSError:
            continue
        old = known.get(path)
        with conn:
            conn.execute("INSERT OR REPLACE INTO doc_paths(path, sha256, size, mtime_ns) VALUES (?, ?, ?, ?)",
                         (path, sha, st.st_size, st.st_mtime_ns))
            doc = conn.execute("SELECT * FROM documents WHERE sha256 = ?", (sha,)).fetchone()
            if old is not None and old["sha256"] != sha:
                # Same path, new content: the previous version is kept, marked.
                conn.execute("UPDATE documents SET status = 'modified', updated = ? WHERE sha256 = ? AND path = ?",
                             (t, old["sha256"], path))
                report["modified"] += 1
            if doc is None:
                conn.execute("INSERT INTO documents(sha256, path, size, mtime_ns, ext, status, first_seen, updated) "
                             "VALUES (?, ?, ?, ?, ?, 'new', ?, ?)",
                             (sha, path, st.st_size, st.st_mtime_ns, Path(path).suffix.lower(), t, t))
                report["new"] += 1
            elif doc["path"] != path:
                if not Path(doc["path"]).exists():
                    conn.execute("UPDATE documents SET path = ?, previous_path = ?, mtime_ns = ?, "
                                 "status = CASE WHEN status = 'new' THEN 'new' ELSE 'moved' END, updated = ? "
                                 "WHERE sha256 = ?", (path, doc["path"], st.st_mtime_ns, t, sha))
                    report["moved"] += 1
                    if doc["classified"]:
                        index_card(conn, sha)
                else:
                    report["copies"] += 1
            elif doc["status"] == "missing":
                conn.execute("UPDATE documents SET status = 'ok', updated = ? WHERE sha256 = ?", (t, sha))
    with conn:
        for path in set(known) - seen:
            conn.execute("DELETE FROM doc_paths WHERE path = ?", (path,))
        # A document is missing when none of its paths is still there.
        for doc in conn.execute("SELECT sha256, path, classified FROM documents "
                                "WHERE status NOT IN ('missing', 'modified')"):
            if doc["path"] not in seen:
                other = conn.execute("SELECT path FROM doc_paths WHERE sha256 = ? LIMIT 1", (doc["sha256"],)).fetchone()
                if other:
                    conn.execute("UPDATE documents SET path = ?, previous_path = ?, status = 'moved', updated = ? "
                                 "WHERE sha256 = ?", (other["path"], doc["path"], t, doc["sha256"]))
                    report["moved"] += 1
                    if doc["classified"]:
                        index_card(conn, doc["sha256"])
                else:
                    conn.execute("UPDATE documents SET status = 'missing', updated = ? WHERE sha256 = ?",
                                 (t, doc["sha256"]))
                    report["missing"] += 1
    return report


# --- reading ---------------------------------------------------------------------------------

def helper(settings: Settings) -> Path:
    """helpers/extract, compiled once (and again when the source changes)."""
    exe = settings.data_dir / "bin" / "brain-extract"
    if not exe.exists() or exe.stat().st_mtime < HELPER_SRC.stat().st_mtime:
        private_dir(exe.parent)
        r = subprocess.run(["swiftc", "-O", "-o", str(exe), str(HELPER_SRC)], capture_output=True, text=True)
        if r.returncode != 0:
            raise DocError(f"building brain-extract failed: {r.stderr.strip()[-300:]}")
    return exe


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
# A spreadsheet or a CSV can be huge and mostly numbers: what is read of it, at most.
MAX_TABLE_CHARS = 60000
# Below this much text a file says nothing searchable.
MIN_TEXT = 20


def read_docx(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    paragraphs = []
    for p in root.iter(f"{W}p"):
        parts = []
        for node in p.iter():
            if node.tag == f"{W}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{W}tab":
                parts.append("\t")
            elif node.tag in (f"{W}br", f"{W}cr"):
                parts.append("\n")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs)


def read_pptx(path: str) -> list[tuple[int | None, str, bool]]:
    """One page per slide, in order."""
    with zipfile.ZipFile(path) as z:
        names = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide[0-9]+\.xml", n)),
                       key=lambda n: int(re.search(r"([0-9]+)\.xml$", n).group(1)))
        pages = []
        for i, n in enumerate(names, 1):
            root = ElementTree.fromstring(z.read(n))
            lines = ["".join(t.text or "" for t in p.iter(f"{A}t")) for p in root.iter(f"{A}p")]
            pages.append((i, "\n".join(x for x in lines if x.strip()), False))
    return pages


def read_xlsx(path: str) -> list[tuple[int | None, str, bool]]:
    """One page per sheet: rows as tab-separated cells (shared strings resolved)."""
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ElementTree.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{S}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{S}t")))
        names = sorted((n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet[0-9]+\.xml", n)),
                       key=lambda n: int(re.search(r"([0-9]+)\.xml$", n).group(1)))
        pages, size = [], 0
        for i, n in enumerate(names, 1):
            rows = []
            for row in ElementTree.fromstring(z.read(n)).iter(f"{S}row"):
                cells = []
                for c in row.iter(f"{S}c"):
                    v = c.find(f"{S}v")
                    if c.get("t") == "s" and v is not None and (v.text or "").isdigit() and int(v.text) < len(shared):
                        cells.append(shared[int(v.text)])
                    elif c.get("t") == "inlineStr":
                        cells.append("".join(t.text or "" for t in c.iter(f"{S}t")))
                    elif v is not None and v.text:
                        cells.append(v.text)
                if any(x.strip() for x in cells):
                    rows.append("\t".join(cells))
            text = "\n".join(rows)[:max(0, MAX_TABLE_CHARS - size)]
            size += len(text)
            pages.append((i, text, False))
    return pages


def read_odf(path: str) -> str:
    """OpenDocument (odt, ods, odp): the text of content.xml, a line per paragraph or cell."""
    with zipfile.ZipFile(path) as z:
        root = ElementTree.fromstring(z.read("content.xml"))
    text_ns = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
    lines = ["".join(p.itertext()) for p in root.iter() if p.tag in (f"{text_ns}p", f"{text_ns}h")]
    return "\n".join(x for x in lines if x.strip())


class _HTMLText(HTMLParser):
    SKIP = {"script", "style", "head", "noscript", "svg"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "td"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_text(html: str) -> str:
    parser = _HTMLText()
    parser.feed(html)
    return re.sub(r"\n\s*\n+", "\n\n", "".join(parser.parts)).strip()


def read_eml(path: str) -> str:
    """A saved mail: who, when, subject, then the text (or the HTML, as text)."""
    with open(path, "rb") as f:
        msg = email.message_from_binary_file(f, policy=email.policy.default)
    head = [f"{k}: {msg[k]}" for k in ("From", "To", "Date", "Subject") if msg[k]]
    body = msg.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        text = body.get_content()
        if body.get_content_type() == "text/html":
            text = html_text(text)
    names = [part.get_filename() for part in msg.iter_attachments() if part.get_filename()]
    return "\n".join(head + ([f"Allegati: {', '.join(names)}"] if names else []) + ["", text])


def read_epub(path: str) -> list[tuple[int | None, str, bool]]:
    """The chapters in reading order (the OPF spine), one page each."""
    with zipfile.ZipFile(path) as z:
        container = ElementTree.fromstring(z.read("META-INF/container.xml"))
        opf_path = next(e.get("full-path") for e in container.iter() if e.tag.endswith("rootfile"))
        opf = ElementTree.fromstring(z.read(opf_path))
        base = str(Path(opf_path).parent)
        items = {e.get("id"): e.get("href") for e in opf.iter() if e.tag.endswith("item")}
        spine = [items[e.get("idref")] for e in opf.iter() if e.tag.endswith("itemref") and e.get("idref") in items]
        pages = []
        for i, href in enumerate(spine, 1):
            name = f"{base}/{href}" if base not in ("", ".") else href
            try:
                pages.append((i, html_text(z.read(name).decode("utf-8", errors="replace")), False))
            except KeyError:
                continue
    return pages


def read_text(path: str) -> str:
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def textutil(path: str) -> str:
    """Word 97 (.doc), RTF, web archives: macOS's own converter."""
    r = subprocess.run(["textutil", "-convert", "txt", "-stdout", path], capture_output=True, timeout=120)
    if r.returncode != 0:
        raise DocError(f"textutil: {r.stderr.decode(errors='replace').strip()[-200:]}")
    return r.stdout.decode("utf-8", errors="replace")


def _pdf_or_image(settings: Settings, path: str) -> list[tuple[int | None, str, bool]]:
    r = subprocess.run([str(helper(settings)), path], capture_output=True, text=True, timeout=600)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise DocError(f"brain-extract: {r.stderr.strip()[-200:] or 'no output'}")
    if out.get("error"):
        raise DocError(out["error"])
    return [(p["n"], p["text"], p["ocr"]) for p in out["pages"]]


def read_iwork(settings: Settings, path: str) -> list[tuple[int | None, str, bool]]:
    """Pages, Numbers, Keynote (single-file format): their content is in a binary
    format, but the file carries a preview, a PDF of the document in older
    versions, an image of the first page in newer ones."""
    import tempfile
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        preview = next((n for n in ("QuickLook/Preview.pdf", "preview.pdf") if n in names), None) or \
            next((n for n in ("preview.jpg", "QuickLook/Thumbnail.jpg") if n in names), None)
        if preview is None:
            raise DocError("iWork file without a preview")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / Path(preview).name
            out.write_bytes(z.read(preview))
            return _pdf_or_image(settings, str(out))


def extract(settings: Settings, path: str) -> list[tuple[int | None, str, bool]]:
    """[(page, text, ocr)]"""
    ext = Path(path).suffix.lower()
    try:
        if ext in (".md", ".txt"):
            return [(None, read_text(path), False)]
        if ext in (".csv", ".tsv"):
            return [(None, read_text(path)[:MAX_TABLE_CHARS], False)]
        if ext in (".html", ".htm"):
            return [(None, html_text(read_text(path)), False)]
        if ext == ".docx":
            return [(None, read_docx(path), False)]
        if ext == ".pptx":
            return read_pptx(path)
        if ext == ".xlsx":
            return read_xlsx(path)
        if ext in (".odt", ".ods", ".odp"):
            return [(None, read_odf(path), False)]
        if ext == ".epub":
            return read_epub(path)
        if ext == ".eml":
            return [(None, read_eml(path), False)]
        if ext in (".doc", ".rtf", ".webarchive"):
            return [(None, textutil(path), False)]
        if ext in (".pages", ".numbers", ".key"):
            return read_iwork(settings, path)
    except (zipfile.BadZipFile, KeyError, StopIteration, ElementTree.ParseError, UnicodeError) as e:
        raise DocError(f"not a readable {ext[1:].upper()}: {e!r}"[:300])
    return _pdf_or_image(settings, path)


def chunks(pages: list[tuple[int | None, str, bool]]) -> list[tuple[int | None, str]]:
    out = []
    for page, text, _ in pages:
        text = re.sub(r"[ \t]+", " ", text).strip()
        i = 0
        while i < len(text):
            piece = text[i:i + CHUNK]
            if i + CHUNK < len(text):
                cut = max(piece.rfind("\n"), piece.rfind(". "))
                if cut > CHUNK // 2:
                    piece = piece[:cut + 1]
            out.append((page, piece.strip()))
            i += max(len(piece) - OVERLAP, 1) if i + len(piece) < len(text) else len(piece)
    return [(p, t) for p, t in out if t]


def extract_next(conn: sqlite3.Connection, settings: Settings) -> str | None:
    """Read one new document; returns its sha256, or None if there is nothing to read."""
    init(conn)
    doc = conn.execute("SELECT * FROM documents WHERE extracted = 0 AND status NOT IN ('missing', 'modified') "
                       "ORDER BY first_seen LIMIT 1").fetchone()
    if doc is None:
        return None
    t = now()
    try:
        if not Rules(settings).allowed(doc["path"]):
            raise DocError("not allowed any more")
        pages = extract(settings, doc["path"])
    except (DocError, OSError, subprocess.TimeoutExpired) as e:
        with conn:
            conn.execute("UPDATE documents SET extracted = 1, classified = 1, status = 'unreadable', error = ?, "
                         "updated = ? WHERE sha256 = ?", (str(e)[:300], t, doc["sha256"]))
        return doc["sha256"]
    full = "\n".join(text for _, text, _ in pages)
    least = settings.doc_image_min_chars if doc["ext"] in IMAGE_EXTS else MIN_TEXT
    if len("".join(full.split())) < least:
        # A photo, an empty form: nothing to search in it.
        with conn:
            conn.execute("UPDATE documents SET extracted = 1, classified = 1, status = 'unreadable', "
                         "error = 'nessun testo', updated = ? WHERE sha256 = ?", (t, doc["sha256"]))
        return doc["sha256"]
    with conn:
        conn.execute("DELETE FROM doc_chunks WHERE doc = ?", (doc["sha256"],))
        for n, (page, text) in enumerate(chunks(pages)):
            conn.execute("INSERT INTO doc_chunks(doc, ord, page, text) VALUES (?, ?, ?, ?)", (doc["sha256"], n, page, text))
        conn.execute("UPDATE documents SET extracted = 1, pages = ?, ocr = ?, text_hash = ?, updated = ? "
                     "WHERE sha256 = ?", (len(pages), int(any(o for _, _, o in pages)), text_hash(full), t,
                                          doc["sha256"]))
    return doc["sha256"]


# --- classifying -----------------------------------------------------------------------------

# kind and area last: with the JSON constrained, the model picks them after writing
# the title and the summary, not before.
CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": 100},
        "summary": {"type": "string", "maxLength": 300},
        "counterparts": {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": 6},
        "doc_date": {"type": ["string", "null"], "description": "AAAA-MM-GG"},
        "ref_year": {"type": ["integer", "null"]},
        "kind": {"type": "string", "enum": KINDS},
        "area": {"type": "string", "enum": AREAS},
    },
    "required": ["title", "summary", "counterparts", "doc_date", "ref_year", "kind", "area"],
}
# The lists are in the prompt too: the schema only constrains what the model writes, it
# does not show it the choices. Without them it chose «altro» for contracts and payslips.
CLASSIFY_PROMPT = f"""Ricevi l'inizio di un documento trovato sul Mac dell'utente, con la cartella in cui si trova.
Compila la scheda in italiano, usando SOLO ciò che c'è scritto nel testo e nel percorso:
- title: un titolo chiaro e specifico, con il nome della persona se il documento è di qualcuno in particolare
  (es. «Contratto di locazione Via Roma 10», «Carta d'identità di Rosa Linda Rossi»);
- summary: una frase con le informazioni chiave (importi, scadenze, numeri, nomi): sarà usata per cercarlo;
- counterparts: le persone, aziende o enti nominati come parti o intestatari (nomi propri, non ruoli);
- doc_date: la data del documento (firma, emissione) come AAAA-MM-GG, o null;
- ref_year: l'anno a cui si riferisce (es. il periodo d'imposta, l'anno del contratto), o null;
- kind: il tipo di documento, uno di: {", ".join(KINDS)} (il più specifico; «altro» solo se nessuno calza);
- area: l'ambito della vita a cui appartiene, uno di: {", ".join(AREAS)} (la cartella aiuta: «Documenti/Salute/…» è salute)."""
# Characters of the document the model reads (~1,100 tokens): the beginning says what it is.
CLASSIFY_CHARS = 4000


def classify_next(conn: sqlite3.Connection, llm: LLM) -> str | None:
    init(conn)
    doc = conn.execute("SELECT * FROM documents WHERE extracted = 1 AND classified = 0 "
                       "AND status NOT IN ('missing', 'modified', 'unreadable') ORDER BY first_seen LIMIT 1").fetchone()
    if doc is None:
        return None
    twin = conn.execute("SELECT * FROM documents WHERE text_hash = ? AND sha256 != ? AND classified = 1 "
                        "AND status NOT IN ('unreadable') ORDER BY first_seen LIMIT 1",
                        (doc["text_hash"], doc["sha256"])).fetchone() if doc["text_hash"] else None
    if twin is not None:
        # The same text in another file: its card, no model call.
        card = {k: twin[k] for k in ("kind", "area", "title", "doc_date", "ref_year", "summary")}
        card["counterparts"] = json.loads(twin["counterparts"] or "[]")
        store_card(conn, doc["sha256"], card)
        with conn:
            conn.execute("UPDATE documents SET duplicate_of = ? WHERE sha256 = ?",
                         (twin["duplicate_of"] or twin["sha256"], doc["sha256"]))
        return doc["sha256"]
    text = "\n".join(r[0] for r in conn.execute("SELECT text FROM doc_chunks WHERE doc = ? ORDER BY ord",
                                                (doc["sha256"],)))[:CLASSIFY_CHARS]
    modified = datetime.fromtimestamp(doc["mtime_ns"] / 1e9).date().isoformat()
    r = llm.chat([{"role": "system", "content": CLASSIFY_PROMPT},
                  {"role": "user", "content": f"File: {place(doc['path'])}\nModificato il: {modified}\n\n"
                                              f"{text or '(nessun testo)'}"}],
                 max_tokens=600, temperature=0.1,
                 response_format={"type": "json_schema", "json_schema": {"name": "scheda", "schema": CLASSIFY_SCHEMA}})
    try:
        card = json.loads(r["message"].get("content") or "")
    except ValueError:
        card = {}
    store_card(conn, doc["sha256"], card)
    return doc["sha256"]


def store_card(conn: sqlite3.Connection, sha: str, card: dict) -> None:
    doc_date = card.get("doc_date") if isinstance(card.get("doc_date"), str) and \
        re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", card["doc_date"]) else None
    year = card.get("ref_year") if isinstance(card.get("ref_year"), int) and 1900 < card["ref_year"] < 2200 else None
    kind = card.get("kind") if card.get("kind") in KINDS else "altro"
    area = card.get("area") if card.get("area") in AREAS else None
    counterparts = [str(c)[:80] for c in card.get("counterparts") or [] if str(c).strip()][:6]
    path = conn.execute("SELECT path FROM documents WHERE sha256 = ?", (sha,)).fetchone()[0]
    title = " ".join(str(card.get("title") or "").split())[:100] or Path(path).stem
    summary = " ".join(str(card.get("summary") or "").split())[:300]
    with conn:
        conn.execute("UPDATE documents SET kind = ?, area = ?, title = ?, doc_date = ?, ref_year = ?, counterparts = ?, "
                     "summary = ?, classified = 1, status = CASE WHEN status = 'new' THEN 'ok' ELSE status END, "
                     "updated = ? WHERE sha256 = ?",
                     (kind, area, title, doc_date, year, json.dumps(counterparts, ensure_ascii=False), summary, now(),
                      sha))
        index_card(conn, sha)


def card_text(doc: sqlite3.Row | dict) -> str:
    """The words a document is found by, without reading it: its card and where it is."""
    counterparts = " ".join(json.loads(doc["counterparts"] or "[]"))
    folders = re.sub(r"[/_\-.]+", " ", place(doc["path"]))
    return " ".join(str(x) for x in (doc["title"] or "", doc["kind"] or "", doc["area"] or "", counterparts,
                                     doc["summary"] or "", folders, doc["ref_year"] or "", doc["doc_date"] or ""))


def index_card(conn: sqlite3.Connection, sha: str) -> None:
    doc = conn.execute("SELECT * FROM documents WHERE sha256 = ?", (sha,)).fetchone()
    conn.execute("DELETE FROM documents_fts WHERE sha256 = ?", (sha,))
    conn.execute("INSERT INTO documents_fts(sha256, text) VALUES (?, ?)", (sha, card_text(doc)))


# --- vault cards -----------------------------------------------------------------------------

def card_path(settings: Settings, doc: sqlite3.Row) -> Path:
    year = (doc["doc_date"] or "")[:4] or (str(doc["ref_year"]) if doc["ref_year"] else "senza-data")
    return settings.vault / "documents" / year / f"{slug(doc['title'] or Path(doc['path']).stem)[:50]}-{doc['sha256'][:8]}.md"


def render_cards(conn: sqlite3.Connection, settings: Settings) -> vault.Result:
    init(conn)
    result = vault.Result()
    existing = {}
    if (settings.vault / "documents").is_dir():
        for p in (settings.vault / "documents").glob("*/*.md"):
            existing[p.stem.rsplit("-", 1)[-1]] = p
    states = {"ok": "presente", "new": "presente", "moved": "spostato", "modified": "sostituito da una versione nuova",
              "missing": "non più trovato", "unreadable": "illeggibile"}
    for doc in conn.execute("SELECT * FROM documents WHERE classified = 1 AND status != 'unreadable'"):
        path = existing.get(doc["sha256"][:8]) or card_path(settings, doc)
        counterparts = json.loads(doc["counterparts"] or "[]")
        when = dates.short(doc["doc_date"]) if doc["doc_date"] else None
        lines = [f"# {doc['title'] or Path(doc['path']).name}", "",
                 f"_{doc['kind'] or 'documento'}" + (f" · {doc['area']}" if doc["area"] else "")
                 + (f" · {when}" if when else "")
                 + (f" · anno {doc['ref_year']}" if doc["ref_year"] else "")
                 + (f" · {', '.join(counterparts)}" if counterparts else "") + "_"]
        twin = conn.execute("SELECT path FROM documents WHERE sha256 = ?", (doc["duplicate_of"],)).fetchone() \
            if doc["duplicate_of"] else None
        info = [f"- File: [{Path(doc['path']).name}](<file://{doc['path']}>)", f"- Dove: {place(doc['path'])}",
                f"- Percorso: `{doc['path']}`",
                f"- Stato: {states.get(doc['status'], doc['status'])}"
                + (f" (prima in `{doc['previous_path']}`)" if doc["previous_path"] else ""),
                f"- sha256: `{doc['sha256']}`", f"- {doc['pages'] or 1} pagine" + (" · letto con OCR" if doc["ocr"] else "")]
        if twin:
            info.append(f"- Stesso testo di `{twin['path']}`")
        keys = {"type": "document", "kind": doc["kind"], "area": doc["area"], "title": doc["title"], "date": doc["doc_date"],
                "ref_year": doc["ref_year"], "counterparts": counterparts, "path": doc["path"],
                "sha256": doc["sha256"], "status": doc["status"]}
        card = {k: doc[k] for k in ("kind", "area", "title", "doc_date", "ref_year", "summary")}
        card["counterparts"] = counterparts
        vault.render_note(path, keys, [("title", "\n".join(lines)), ("summary", doc["summary"] or ""),
                                       ("file", "## File\n\n" + "\n".join(info))],
                          {"v": 1, "document": doc["sha256"], "card": card}, result)
    return result


def import_cards(conn: sqlite3.Connection, settings: Settings) -> int:
    """brain reindex: the classifications back from the cards (no model)."""
    init(conn)
    n = 0
    for p in (settings.vault / "documents").glob("*/*.md") if (settings.vault / "documents").is_dir() else []:
        data = vault.read_data(p.read_text()) or {}
        sha, card = data.get("document"), data.get("card")
        if sha and card and conn.execute("SELECT 1 FROM documents WHERE sha256 = ?", (sha,)).fetchone():
            store_card(conn, sha, card)
            n += 1
    return n


# --- the tool --------------------------------------------------------------------------------

# What search and find_document may show: present, readable, and not a copy of one shown.
SHOWN = "d.status NOT IN ('modified', 'unreadable', 'missing') AND d.classified = 1 AND d.duplicate_of IS NULL"


class DocSearch:
    """The documents' side of Tools.search: candidates, what the reranker reads,
    the line the model sees."""

    def __init__(self, conn: sqlite3.Connection, tools):
        self.conn, self.tools = conn, tools

    def fts(self, q: str) -> list[list[tuple[str, str]]]:
        chunks = [("chunk", str(r[0])) for r in self.conn.execute(
            f"SELECT c.id FROM doc_chunks_fts f JOIN doc_chunks c ON c.id = f.rowid JOIN documents d "
            f"ON d.sha256 = c.doc WHERE doc_chunks_fts MATCH ? AND {SHOWN} ORDER BY bm25(doc_chunks_fts) LIMIT 30", (q,))]
        cards = [("card", r[0]) for r in self.conn.execute(
            f"SELECT f.sha256 FROM documents_fts f JOIN documents d ON d.sha256 = f.sha256 "
            f"WHERE documents_fts MATCH ? AND {SHOWN} ORDER BY bm25(documents_fts) LIMIT 10", (q,))]
        return [chunks, cards]

    def ok(self, chunk_id: str) -> bool:
        return self.conn.execute(f"SELECT 1 FROM doc_chunks c JOIN documents d ON d.sha256 = c.doc "
                                 f"WHERE c.id = ? AND {SHOWN}", (int(chunk_id),)).fetchone() is not None

    def doc_of(self, kind: str, id_: str) -> str:
        if kind == "card":
            return id_
        return self.conn.execute("SELECT doc FROM doc_chunks WHERE id = ?", (int(id_),)).fetchone()[0]

    def _doc(self, sha: str) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM documents WHERE sha256 = ?", (sha,)).fetchone()

    def text(self, kind: str, id_: str) -> str:
        """Contextual: the card's title, kind and folder before the excerpt, so that
        «canone di 650 euro» is known to be the rent of that contract."""
        doc = self._doc(self.doc_of(kind, id_))
        head = f"Documento «{doc['title']}» ({doc['kind']}) in {place(doc['path'])}."
        if kind == "card":
            return f"{head} {doc['summary'] or ''}"
        row = self.conn.execute("SELECT page, text FROM doc_chunks WHERE id = ?", (int(id_),)).fetchone()
        return f"{head} Pagina {row['page'] or 1}:\n{row['text']}"

    def about(self, name: str, limit: int = 5) -> str:
        """Documents that name a person: in the card (holder, counterpart, title) or in the folder."""
        from brain.tools import fts_query
        q = fts_query(name, "tutte")
        if q is None:
            return ""
        rows = self.conn.execute(f"SELECT d.* FROM documents_fts f JOIN documents d ON d.sha256 = f.sha256 "
                                 f"WHERE documents_fts MATCH ? AND {SHOWN} ORDER BY bm25(documents_fts) LIMIT ?",
                                 (q, limit)).fetchall()
        return "\n".join(doc_line(self.tools, d) for d in rows)

    def line(self, kind: str, id_: str, extra: str = "") -> str:
        doc = self._doc(self.doc_of(kind, id_))
        if kind == "card":
            return doc_line(self.tools, doc, extra + (f"\n  {doc['summary']}" if doc["summary"] else ""))
        row = self.conn.execute("SELECT page, text FROM doc_chunks WHERE id = ?", (int(id_),)).fetchone()
        excerpt = " ".join(row["text"].split())[:400]
        return doc_line(self.tools, doc, f" · p. {row['page'] or 1}{extra}\n  «{excerpt}…»")


def doc_line(tools, doc: sqlite3.Row, extra: str = "") -> str:
    ref = tools.refs.add("document", doc["sha256"], dict(doc))
    counterparts = ", ".join(json.loads(doc["counterparts"] or "[]"))
    when = dates.short(doc["doc_date"]) if doc["doc_date"] else "senza data"
    state = "" if doc["status"] in ("ok", "moved", "new") else f" · ATTENZIONE: {doc['status']}"
    return (f"[{ref}] {doc['kind'] or 'documento'} · «{doc['title'] or Path(doc['path']).name}» · {when}"
            + (f" · anno {doc['ref_year']}" if doc["ref_year"] else "")
            + (f" · {counterparts}" if counterparts else "") + f" · {place(doc['path'])}{state}{extra}")


def setup_tools(conn: sqlite3.Connection, settings: Settings):
    """Registers find_document, and the documents in search, on a Tools."""
    init(conn)

    def install(tools) -> None:
        tools.doc_search = DocSearch(conn, tools)

        def find_document(query: str, kind: str | None = None, year: int | None = None, area: str | None = None,
                          limit: int = 8) -> str:
            from brain.tools import RERANK_CHARS, fts_query
            q = fts_query(query, "qualsiasi")
            if q is None:
                raise ValueError("query vuota")
            where, params = [SHOWN], []
            if kind:
                where.append("d.kind = ?")
                params.append(kind)
            if area:
                where.append("d.area = ?")
                params.append(area)
            if year:
                where.append("(d.ref_year = ? OR substr(d.doc_date, 1, 4) = ? OR d.ref_year IS NULL "
                             "AND d.doc_date IS NULL)")
                params += [int(year), str(year)]
            limit = max(1, min(int(limit or 8), 20))
            scores: dict[str, float] = {}
            for sha, score in conn.execute(
                    f"SELECT f.sha256, bm25(documents_fts) FROM documents_fts f JOIN documents d ON d.sha256 = f.sha256 "
                    f"WHERE documents_fts MATCH ? AND {' AND '.join(where)} ORDER BY 2 LIMIT 50", [q, *params]):
                scores[sha] = scores.get(sha, 0) + score * 2  # the card counts more than the text
            # The best chunk per document. In Python: bm25() cannot sit inside an aggregate,
            # and SQLite folds a subquery into the outer GROUP BY.
            best_chunk: dict[str, float] = {}
            for sha, score in conn.execute(
                    f"SELECT c.doc, bm25(doc_chunks_fts) FROM doc_chunks_fts f JOIN doc_chunks c ON c.id = f.rowid "
                    f"JOIN documents d ON d.sha256 = c.doc WHERE doc_chunks_fts MATCH ? AND {' AND '.join(where)} "
                    f"ORDER BY 2 LIMIT 200", [q, *params]):
                best_chunk[sha] = min(best_chunk.get(sha, 0.0), score)
            for sha, score in best_chunk.items():
                scores[sha] = scores.get(sha, 0) + score
            if tools.embedder is not None:
                from brain import embed
                for kind_, ref, dist in embed.knn(conn, tools.embedder.query(query), ("chunk",), (), k=30):
                    row = conn.execute("SELECT doc FROM doc_chunks WHERE id = ?", (int(ref),)).fetchone()
                    if row and conn.execute(f"SELECT 1 FROM documents d WHERE d.sha256 = ? AND {' AND '.join(where)}",
                                            [row[0], *params]).fetchone():
                        scores[row[0]] = scores.get(row[0], 0) - (1 - dist) * 10
            best = sorted(scores, key=scores.get)[:max(limit, 15)]
            if tools.reranker is not None and best:
                try:
                    rel = tools.reranker.scores(query, [tools.doc_search.text("card", sha)[:RERANK_CHARS]
                                                        for sha in best])
                    best = [sha for sha, r in sorted(zip(best, rel), key=lambda x: -x[1]) if r >= 0.02] or best[:3]
                except Exception:  # without the reranker, the order of the scores
                    pass
            best = best[:limit]
            if not best:
                return "find_document: nessun documento trovato."
            rows = [conn.execute("SELECT * FROM documents WHERE sha256 = ?", (sha,)).fetchone() for sha in best]
            return (f"find_document {query!r}: {len(rows)} documenti, dal più pertinente. Se l'utente chiede di "
                    "mandarlo, cita il riferimento [D…]: l'app lo apre e lo condivide.\n"
                    + "\n".join(doc_line(tools, d, f"\n  {d['summary']}" if d["summary"] else "") for d in rows))

        tools.register("find_document", "Cerca tra i documenti del Mac e di iCloud Drive (contratti, fatture, "
                       "bollette, referti, documenti d'identità…) per contenuto, tipo, ambito e anno. Restituisce "
                       "schede [D…] con il percorso del file.",
                       {"query": {"type": "string", "description": "cosa cercare, es. 'contratto di locazione'"},
                        "kind": {"type": "string", "enum": KINDS},
                        "area": {"type": "string", "enum": AREAS},
                        "year": {"type": "integer", "description": "anno del documento o a cui si riferisce"},
                        "limit": {"type": "integer"}}, ["query"], find_document)

    return install


# --- worker hooks ----------------------------------------------------------------------------

SCAN_EVERY = 900


def worker_hooks(settings: Settings, worker) -> None:
    """Scan every SCAN_EVERY seconds (cheap: stat, and hashing only what changed);
    extract and classify in the rounds; cards to the vault."""
    conn = worker.conn
    init(conn)
    state = {"last": None}

    def scan_job():
        t = worker.d.clock()
        if state["last"] is not None and t - state["last"] < SCAN_EVERY:
            return False
        state["last"] = t
        r = scan(conn, settings)
        if r["new"] or r["moved"] or r["missing"] or r["modified"]:
            render_cards(conn, settings)
            return True
        return False

    def extract_job(w) -> bool:
        return extract_next(conn, settings) is not None

    def classify_job(w) -> bool:
        with w.busy:
            sha = classify_next(conn, w.load())
        if sha:
            render_cards(conn, settings)
        return sha is not None

    worker.scans.append(scan_job)
    worker.jobs += [extract_job, classify_job]
    worker.ask_setup = setup_tools(conn, settings)
