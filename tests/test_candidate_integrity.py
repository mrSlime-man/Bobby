from pathlib import Path
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

import pytest
import yaml

from src.utils.candidate_integrity import CandidateIntegrityError, validate_candidate_identity

CANDIDATE_NAME = "Morgan Example"
WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _write_profile(path: Path, name: str) -> None:
    path.write_text(yaml.safe_dump({"candidate": {"full_name": name}}), encoding="utf-8")


def _validate(
    tmp_path: Path, *, text_name=CANDIDATE_NAME, profile_name=CANDIDATE_NAME,
    profile_field="full_name", resume_path=None
):
    profile = tmp_path / "application_profile.yaml"
    profile.write_text(
        yaml.safe_dump({"candidate": {profile_field: profile_name}}), encoding="utf-8"
    )
    parameters = {
        "resume_text": f"{text_name}\nIT Support Specialist",
        "resume_structured": {
            "personal_information": {"first_name": "Morgan", "last_name": "Example"}
        },
        "application_profile_path": profile,
        "resume_pdf_path": resume_path or tmp_path / "resume.pdf",
    }
    if resume_path is not None:
        return validate_candidate_identity(**parameters)
    parameters["resume_pdf_path"].write_bytes(b"%PDF-test")
    page = MagicMock()
    page.extract_text.return_value = f"{CANDIDATE_NAME} resume"
    reader = MagicMock()
    reader.pages = [page]
    with patch("src.utils.candidate_integrity.PdfReader", return_value=reader):
        return validate_candidate_identity(**parameters)


def _write_docx(path, text="Morgan Example", *, xml=None):
    if xml is None:
        xml = (
            f'<w:document xmlns:w="{WORD_NAMESPACE}"><w:body><w:p>'
            f"<w:r><w:t>{text}</w:t></w:r>"
            "</w:p></w:body></w:document>"
        )
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return path


def test_matching_identity_passes(tmp_path):
    assert _validate(tmp_path) == CANDIDATE_NAME


def test_legal_name_profile_field_is_supported(tmp_path):
    resume = _validate(
        tmp_path,
        profile_field="legal_name",
        resume_path=_write_docx(tmp_path / "resume.docx"),
    )
    assert resume == CANDIDATE_NAME


def test_profile_identity_mismatch_fails_closed(tmp_path):
    with pytest.raises(CandidateIntegrityError, match="application profile"):
        _validate(tmp_path, profile_name="Different Person")


def test_resume_text_identity_mismatch_fails_closed(tmp_path):
    with pytest.raises(CandidateIntegrityError, match="resume_text"):
        _validate(tmp_path, text_name="Different Person")


def test_matching_docx_identity_passes(tmp_path):
    resume = _write_docx(tmp_path / "resume.docx")
    assert _validate(tmp_path, resume_path=resume) == CANDIDATE_NAME


def test_docx_formatting_runs_preserve_candidate_name(tmp_path):
    xml = (
        f'<w:document xmlns:w="{WORD_NAMESPACE}"><w:body><w:p>'
        "<w:r><w:t>Mor</w:t></w:r><w:r><w:t>gan</w:t><w:tab/></w:r>"
        "<w:r><w:t>Example</w:t></w:r>"
        "</w:p></w:body></w:document>"
    )
    resume = _write_docx(tmp_path / "resume.docx", xml=xml)
    assert _validate(tmp_path, resume_path=resume) == CANDIDATE_NAME


def test_docx_identity_mismatch_fails_closed(tmp_path):
    resume = _write_docx(tmp_path / "resume.docx", text="Different Person")
    with pytest.raises(CandidateIntegrityError, match="production resume document"):
        _validate(tmp_path, resume_path=resume)


@pytest.mark.parametrize(
    "content", ["not a ZIP", "missing document", "malformed XML", "wrong root"]
)
def test_malformed_docx_fails_closed_without_document_path(tmp_path, content):
    resume = tmp_path / "private-candidate-name.docx"
    if content == "not a ZIP":
        resume.write_text("not a ZIP")
    elif content == "missing document":
        with ZipFile(resume, "w") as archive:
            archive.writestr("other.xml", "<root/>")
    else:
        _write_docx(resume, xml="<malformed" if content == "malformed XML" else "<root/>")
    with pytest.raises(CandidateIntegrityError, match="DOCX could not be read") as error:
        _validate(tmp_path, resume_path=resume)
    assert str(resume) not in str(error.value)


def test_real_pdf_identity_is_supported(tmp_path):
    from reportlab.pdfgen.canvas import Canvas

    resume = tmp_path / "resume.pdf"
    canvas = Canvas(str(resume))
    canvas.drawString(72, 720, CANDIDATE_NAME)
    canvas.save()
    assert _validate(tmp_path, resume_path=resume) == CANDIDATE_NAME


def test_pdf_identity_mismatch_fails_closed(tmp_path):
    from reportlab.pdfgen.canvas import Canvas

    resume = tmp_path / "resume.pdf"
    canvas = Canvas(str(resume))
    canvas.drawString(72, 720, "Different Person")
    canvas.save()
    with pytest.raises(CandidateIntegrityError, match="production resume document"):
        _validate(tmp_path, resume_path=resume)


def test_missing_resume_fails_closed(tmp_path):
    with pytest.raises(CandidateIntegrityError, match="resume document is missing"):
        _validate(tmp_path, resume_path=tmp_path / "absent.docx")


def test_unsupported_resume_type_fails_closed(tmp_path):
    resume = tmp_path / "resume.txt"
    resume.write_text(CANDIDATE_NAME)
    with pytest.raises(CandidateIntegrityError, match="PDF or DOCX"):
        _validate(tmp_path, resume_path=resume)
