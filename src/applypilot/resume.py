"""Resume file parsing: turn an uploaded PDF/DOCX into the text the AI tailors.

You upload the resume you already have. This extracts its text so scoring and
per-job tailoring work from your real document instead of something retyped
into a textarea.

Extraction is a cascade, so it works on as many machines as possible:

  .pdf   pypdf (pure Python)  ->  pdftotext CLI (poppler)  ->  clear error
  .docx  stdlib zipfile + XML (no dependency at all)  ->  python-docx
  .txt   read directly
  .md    read directly

The original file is kept and uploaded to employers as-is, so your formatting
and layout survive. The extracted text is what the AI reads and rewrites.
"""

import logging
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = (".pdf", ".docx", ".txt", ".md", ".rtf")

# Below this, extraction almost certainly failed — a real resume is longer.
MIN_USABLE_CHARS = 200


class ResumeParseError(RuntimeError):
    """Raised when a resume file cannot be turned into usable text."""


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def _safe_import_pdfreader():
    """Import a PDF reader without letting a broken install kill the process.

    A damaged native dependency (mismatched cryptography/cffi ABI) can raise
    a Rust-level PanicException, which inherits from BaseException and so
    slips straight past `except Exception`. Everything except the signals
    that must always propagate is caught, so a bad environment degrades to
    the next extractor instead of taking down the run.
    """
    for module in ("pypdf", "PyPDF2"):
        try:
            return __import__(module, fromlist=["PdfReader"]).PdfReader
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            logger.debug("%s unavailable or broken", module, exc_info=True)
    return None


def _pdf_via_pypdf(path: Path) -> str | None:
    PdfReader = _safe_import_pdfreader()
    if PdfReader is None:
        return None

    try:
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        logger.warning("pypdf could not read %s, trying next extractor", path.name)
        return None


def _pdf_via_pdftotext(path: Path) -> str | None:
    """poppler-utils, commonly present on Linux and via Homebrew on macOS."""
    if not shutil.which("pdftotext"):
        return None
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True, text=True, timeout=60, check=False,
        )
        return result.stdout if result.returncode == 0 else None
    except (subprocess.SubprocessError, OSError):
        logger.exception("pdftotext failed on %s", path.name)
        return None


def extract_pdf(path: Path) -> str:
    for extractor in (_pdf_via_pypdf, _pdf_via_pdftotext):
        text = extractor(path)
        if text and text.strip():
            return text

    raise ResumeParseError(
        "Could not read text from this PDF. Either no PDF reader is installed "
        "(fix with: pip install pypdf) or the file is a scan/image with no "
        "embedded text. If it's a scan, export a text-based PDF from Word or "
        "Google Docs, or upload a .docx instead."
    )


# ---------------------------------------------------------------------------
# DOCX — a .docx is a zip archive containing word/document.xml
# ---------------------------------------------------------------------------

_PARA_END = re.compile(r"</w:p>")
_TAB = re.compile(r"<w:tab[^>]*/>")
_BREAK = re.compile(r"<w:br[^>]*/>")
_TAG = re.compile(r"<[^>]+>")


def extract_docx(path: Path) -> str:
    """Extract text without any third-party dependency.

    Word stores paragraphs as <w:p> elements; converting those boundaries to
    newlines before stripping tags preserves the line structure that section
    headers depend on.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, KeyError, OSError):
        # Fall back to python-docx if it happens to be installed
        try:
            import docx
            return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
        except Exception as e:
            raise ResumeParseError(
                f"Could not read this .docx file: {e}. Try re-saving it from "
                "Word or Google Docs, or upload a PDF instead."
            ) from e

    xml = _TAB.sub("\t", xml)
    xml = _BREAK.sub("\n", xml)
    xml = _PARA_END.sub("\n", xml)
    text = _TAG.sub("", xml)

    # Unescape the XML entities Word uses
    for entity, char in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                         ("&quot;", '"'), ("&apos;", "'"), ("&#160;", " ")):
        text = text.replace(entity, char)

    return text


# ---------------------------------------------------------------------------
# Plain text / RTF
# ---------------------------------------------------------------------------

def extract_rtf(path: Path) -> str:
    """Minimal RTF handling — strip control words and groups."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"\\par[d]?", "\n", raw)
    text = re.sub(r"\\'([0-9a-fA-F]{2})", " ", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    text = text.replace("{", "").replace("}", "")
    return text


# ---------------------------------------------------------------------------
# Cleanup + public API
# ---------------------------------------------------------------------------

def clean(text: str) -> str:
    """Normalize extracted text into something an LLM reads cleanly."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\xa0", " ").replace("\u200b", "")
    # Common PDF bullet glyphs -> a plain bullet
    for glyph in ("\uf0b7", "\uf0a7", "\u2022", "\u25cf", "\u25aa", "\u2023"):
        text = text.replace(glyph, "- ")
    # Ligatures that break keyword matching
    for lig, plain in (("\ufb01", "fi"), ("\ufb02", "fl"), ("\ufb00", "ff")):
        text = text.replace(lig, plain)

    lines = [re.sub(r"[ \t]{2,}", "  ", line.rstrip()) for line in text.split("\n")]
    out: list[str] = []
    blanks = 0
    for line in lines:
        if line.strip():
            blanks = 0
            out.append(line)
        else:
            blanks += 1
            if blanks <= 1:       # collapse runs of blank lines to one
                out.append("")
    return "\n".join(out).strip()


def extract_text(path: str | Path) -> str:
    """Extract clean text from a resume file.

    Raises ResumeParseError with an actionable message when extraction fails
    or produces too little text to be a real resume.
    """
    path = Path(path)
    if not path.exists():
        raise ResumeParseError(f"File not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        raw = extract_pdf(path)
    elif suffix == ".docx":
        raw = extract_docx(path)
    elif suffix == ".rtf":
        raw = extract_rtf(path)
    elif suffix in (".txt", ".md"):
        raw = path.read_text(encoding="utf-8", errors="replace")
    elif suffix == ".doc":
        raise ResumeParseError(
            "Legacy .doc files aren't supported. Open it in Word or Google Docs "
            "and save as .docx or PDF, then upload again."
        )
    else:
        raise ResumeParseError(
            f"Unsupported file type '{suffix}'. Upload a PDF, DOCX, or TXT."
        )

    text = clean(raw)

    if len(text) < MIN_USABLE_CHARS:
        raise ResumeParseError(
            f"Only {len(text)} characters of text came out of this file, which "
            "is too little to be a resume. It is most likely a scanned image "
            "rather than a text document. Export a text-based PDF from Word or "
            "Google Docs, or upload a .docx."
        )

    return text


def analyze(text: str) -> dict:
    """Sanity-check extracted text and report what's missing.

    Surfaced in the panel so a bad upload is caught immediately rather than
    discovered later through mysteriously poor tailoring.
    """
    lowered = text.lower()
    warnings: list[str] = []

    has_email = bool(re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text))
    has_phone = bool(re.search(r"(\+?\d[\d\-.() ]{8,}\d)", text))
    sections = {
        "experience": any(k in lowered for k in
                          ("experience", "employment", "work history", "professional")),
        "education": "education" in lowered or "university" in lowered
                     or "degree" in lowered or "college" in lowered,
        "skills": "skills" in lowered or "technologies" in lowered
                  or "proficient" in lowered,
    }

    if not has_email:
        warnings.append("No email address found — check the file extracted correctly.")
    if not sections["experience"]:
        warnings.append("No work-experience section detected.")
    if not sections["skills"]:
        warnings.append("No skills section detected — skills drive keyword matching.")
    if len(text) < 800:
        warnings.append("Resume text looks short; some content may not have extracted.")

    return {
        "characters": len(text),
        "words": len(text.split()),
        "lines": len([line for line in text.split("\n") if line.strip()]),
        "has_email": has_email,
        "has_phone": has_phone,
        "sections": sections,
        "warnings": warnings,
        "looks_good": not warnings,
    }
