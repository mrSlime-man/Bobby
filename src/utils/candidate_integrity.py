"""Fail-closed validation for candidate identity inputs."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from zipfile import ZipFile
from pathlib import Path
from typing import Any

import yaml
from pypdf import PdfReader


class CandidateIntegrityError(RuntimeError):
    """Raised when candidate source-of-truth files disagree."""


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _structured_name(resume_structured: dict[str, Any]) -> str:
    personal = resume_structured.get("personal_information") or {}
    return " ".join(
        part.strip()
        for part in (
            str(personal.get("first_name") or ""),
            str(personal.get("last_name") or ""),
        )
        if part.strip()
    )


def _profile_name(application_profile_path: Path) -> str:
    if not application_profile_path.is_file():
        raise CandidateIntegrityError(
            f"Candidate application profile is missing: {application_profile_path}"
        )
    profile = yaml.safe_load(application_profile_path.read_text(encoding="utf-8")) or {}
    candidate = profile.get("candidate") or {}
    return str(
        candidate.get("full_name")
        or candidate.get("legal_name")
        or " ".join(
            part
            for part in (candidate.get("first_name"), candidate.get("last_name"))
            if part
        )
    ).strip()


def _pdf_text(pdf_path: Path) -> str:
    try:
        return "\n".join(page.extract_text() or "" for page in PdfReader(pdf_path).pages)
    except Exception as exc:
        raise CandidateIntegrityError("Candidate resume PDF could not be read") from exc


def _docx_text(docx_path: Path) -> str:
    """Read the document body without executing macros or resolving links."""
    word_namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with ZipFile(docx_path) as archive:
            member = archive.getinfo("word/document.xml")
            if member.file_size > 10 * 1024 * 1024:
                raise ValueError("Resume document XML is too large")
            document = ET.fromstring(archive.read(member))
        if document.tag != f"{word_namespace}document":
            raise ValueError("Invalid Word document root")
        body = document.find(f"{word_namespace}body")
        if body is None:
            raise ValueError("Missing Word document body")
        paragraphs = []
        for paragraph in body.iter(f"{word_namespace}p"):
            # Formatting can split a name across adjacent runs. Preserve those
            # runs, while keeping explicit Word line breaks and tabs as spaces.
            paragraphs.append(
                "".join(
                    element.text or "" if element.tag == f"{word_namespace}t" else " "
                    for element in paragraph.iter()
                    if element.tag
                    in {f"{word_namespace}t", f"{word_namespace}tab", f"{word_namespace}br"}
                )
            )
        return "\n".join(paragraphs)
    except Exception as exc:
        raise CandidateIntegrityError("Candidate resume DOCX could not be read") from exc


def _resume_document_text(resume_path: Path) -> str:
    if resume_path.suffix.lower() == ".pdf":
        return _pdf_text(resume_path)
    if resume_path.suffix.lower() == ".docx":
        return _docx_text(resume_path)
    raise CandidateIntegrityError("Candidate resume must be a PDF or DOCX document")


def validate_candidate_identity(
    *,
    resume_text: str,
    resume_structured: dict[str, Any],
    application_profile_path: Path,
    resume_pdf_path: Path,
) -> str:
    """Require the same name in all sources, including the PDF/DOCX upload.

    ``resume_pdf_path`` retains its existing caller API while accepting DOCX.
    """
    expected_name = _structured_name(resume_structured)
    profile_name = _profile_name(application_profile_path)

    if not expected_name or len(expected_name.split()) < 2:
        raise CandidateIntegrityError(
            "Structured resume does not contain a complete candidate name"
        )

    expected = _normalized(expected_name)
    if _normalized(profile_name) != expected:
        raise CandidateIntegrityError(
            "Candidate identity mismatch between structured resume and application profile"
        )

    if expected not in _normalized(resume_text):
        raise CandidateIntegrityError(
            "Candidate identity mismatch between structured resume and resume_text.txt"
        )

    if not resume_pdf_path.is_file():
        raise CandidateIntegrityError("Candidate resume document is missing")
    if expected not in _normalized(_resume_document_text(resume_pdf_path)):
        raise CandidateIntegrityError(
            "Candidate identity mismatch between structured resume and production resume document"
        )

    return expected_name
