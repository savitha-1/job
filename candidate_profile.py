"""Local resume text extraction and provenance-aware candidate facts.

No network calls are made. Extraction is deliberately conservative: facts are
suggestions until explicitly approved by the candidate.
"""
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree


def extract_text(path):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md"):
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".docx":
        with zipfile.ZipFile(path) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        return "\n".join("".join(node.itertext()) for node in root.findall(".//w:p", ns))
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("PDF text extraction requires optional pypdf; install it locally") from exc
        return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    raise ValueError("supported resume formats: PDF, DOCX, TXT, MD")


def suggest_facts(text, source):
    facts = {}
    patterns = {"email": r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "phone": r"(?:\+?\d[\d ()-]{7,}\d)"}
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.I)
        if match:
            facts[key] = {"value": match.group(0).strip(), "source": source, "approved": False}
    for key, pattern in (("linkedin", r"https?://(?:www\.)?linkedin\.com/in/[^\s)]+"), ("github", r"https?://(?:www\.)?github\.com/[^\s)]+")):
        match = re.search(pattern, text, re.I)
        if match:
            facts[key] = {"value": match.group(0).rstrip(".,"), "source": source, "approved": False}
    return facts


def profile_from_resume(path):
    path = Path(path).expanduser().resolve()
    return {"source": str(path), "facts": suggest_facts(extract_text(path), str(path)), "unknown": ["work_authorization", "salary_expectation", "experience", "education", "skills"], "approved": False}
