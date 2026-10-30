"""Find the edital PDF in what a public body uploaded as its edital.

In the first sample from Rio de Janeiro, 10 of 25 uploads were not a PDF: 8 ZIP files
with the edital and its annexes, one 7z archive and one Word file.
"""

from __future__ import annotations

import contextlib
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

# A real edital with every annex is a few megabytes. The cap protects against a ZIP
# whose header declares a small file that inflates without limit.
MAX_PDF_BYTES = 200 * 1024 * 1024

# An annex often mentions the edital in its name ("Anexo II ao edital - Modelo de
# proposta"), and so does a draft ("Minuta Edital 26 2026"). They start with these words.
_NOT_THE_EDITAL = re.compile(r"^(anexo|apenso|apendice|minuta)")


@dataclass
class Edital:
    pdf: bytes
    title: str


def find_edital(title: str, data: bytes, *, nested: bool = False) -> Edital | str:
    """The edital PDF, or the reason it could not be found."""
    if data.startswith(b"%PDF"):
        return Edital(data, title)
    if not data.startswith(b"PK\x03\x04"):
        return "edital is not a PDF or a ZIP"
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return "edital is a corrupt ZIP"
    # A .docx is a ZIP too. Word files have no pages, and pages are the unit of evidence.
    if "word/document.xml" in archive.namelist():
        return "edital is a Word document"

    candidates = [info for info in archive.infolist() if _looks_like_edital(_name(info))]
    if not candidates:
        # Seen once: a ZIP holding only another ZIP. One level is enough.
        inner = [i for i in archive.infolist() if _name(i).lower().endswith(".zip")]
        if len(inner) == 1 and not nested:
            content = _read(archive, inner[0])
            if content is None:
                return "edital inside the ZIP is too large"
            return find_edital(f"{title}/{_name(inner[0])}", content, nested=True)
        return "no edital inside the ZIP"
    # When two files qualify, one is usually the edital alone and the other the edital
    # merged with its annexes ("Edital 26 2026_COMPLETO.pdf"). The larger one has more.
    chosen = max(candidates, key=lambda info: info.file_size)
    pdf = _read(archive, chosen)
    if pdf is None:
        return "edital inside the ZIP is too large"
    if not pdf.startswith(b"%PDF"):
        return "edital inside the ZIP is not a PDF"
    return Edital(pdf, f"{title}/{_name(chosen)}")


def _read(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes | None:
    """The member's content, or None if it is larger than the cap."""
    if info.file_size > MAX_PDF_BYTES:
        return None
    with archive.open(info) as member:
        content = member.read(MAX_PDF_BYTES + 1)
    return None if len(content) > MAX_PDF_BYTES else content


def _looks_like_edital(name: str) -> bool:
    stem = _fold(PurePosixPath(name).stem)
    if not name.lower().endswith(".pdf") or "edital" not in stem:
        return False
    # Drop leading numbering such as "1. ", "004_0_" or "3 - ".
    stem = re.sub(r"^[\d\s._-]+", "", stem)
    return not _NOT_THE_EDITAL.match(stem)


def _name(info: zipfile.ZipInfo) -> str:
    """The file name, without directories and with its accents restored.

    Without the UTF-8 flag, zipfile decodes names as cp437, but Windows in Brazil writes
    them in cp850, so "Contratação" comes out garbled unless it is decoded again. A name
    that cannot be encoded back to cp437 was not decoded as cp437 and is kept as it is.
    """
    name = info.filename
    if not info.flag_bits & 0x800:
        with contextlib.suppress(UnicodeEncodeError):
            name = name.encode("cp437").decode("cp850", errors="replace")
    return PurePosixPath(name).name


def _fold(text: str) -> str:
    """Lowercase without accents, so "Apêndice" and "APENDICE" match the same pattern."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))
