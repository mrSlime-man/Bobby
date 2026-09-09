"""Generic, privacy-safe primitives for external applicant tracking systems.

This module deliberately contains deterministic helpers only.  Browser Use is
still responsible for the live page interaction, while this layer supplies a
bounded state machine, semantic control discovery, navigation intent, and
recovery bookkeeping that can be shared by Workday, Greenhouse, Lever,
Ashby, SmartRecruiters, iCIMS, Taleo, and ordinary company-hosted forms.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from html.parser import HTMLParser
from urllib.parse import urlparse


class ATSStage(str, Enum):
    """A stable, observable application-flow stage.

    The longer names are the canonical vocabulary used for new diagnostics.
    The older names remain aliases so existing callers and any in-flight
    worker code can keep using them.
    """

    LANDING = "landing"
    APPLY_ENTRY = "apply_entry"
    AUTH = "auth"
    ACCOUNT_CREATION = "account_creation"
    PERSONAL_INFORMATION = "personal_information"
    PERSONAL_INFO = PERSONAL_INFORMATION
    CONTACT_INFORMATION = "contact_information"
    RESUME = "resume"
    EMPLOYMENT_HISTORY = "employment_history"
    EXPERIENCE = EMPLOYMENT_HISTORY
    EDUCATION = "education"
    SKILLS = "skills"
    SCREENING_QUESTIONS = "screening_questions"
    QUESTIONS = SCREENING_QUESTIONS
    EEO = "eeo"
    VOLUNTARY_DISCLOSURE = "voluntary_disclosure"
    REVIEW = "review"
    SUBMIT = "submit"
    FINAL_SUBMIT = SUBMIT
    CONFIRMATION = "confirmation"

    @classmethod
    def _missing_(cls, value: object):
        """Accept stage values written by earlier bounded-worker runs."""
        legacy_values = {
            "personal_info": cls.PERSONAL_INFORMATION,
            "experience": cls.EMPLOYMENT_HISTORY,
            "questions": cls.SCREENING_QUESTIONS,
            "final_submit": cls.SUBMIT,
        }
        return legacy_values.get(str(value))


class SiteFamily(str, Enum):
    WORKDAY = "workday"
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    SMARTRECRUITERS = "smartrecruiters"
    ICIMS = "icims"
    TALEO = "taleo"
    ORACLE = "oracle"
    SUCCESSFACTORS = "successfactors"
    JOBVITE = "jobvite"
    ADP = "adp"
    BAMBOOHR = "bamboohr"
    UKG = "ukg"
    GENERIC = "generic"


class ControlKind(str, Enum):
    BUTTON = "button"
    LINK = "link"
    TEXT = "text"
    NUMBER = "number"
    EMAIL = "email"
    PHONE = "phone"
    DATE = "date"
    TEXTAREA = "textarea"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    SELECT = "select"
    COMBOBOX = "combobox"
    AUTOCOMPLETE = "autocomplete"
    FILE = "file"
    DIALOG = "dialog"
    VALIDATION = "validation"


class ConstraintType(str, Enum):
    BOOLEAN = "boolean"
    INTEGER = "integer"
    DECIMAL = "decimal"
    SHORT_TEXT = "short_text"
    LONG_TEXT = "long_text"
    DATE = "date"
    PHONE = "phone"
    EMAIL = "email"
    SALARY = "salary"
    YEARS_EXPERIENCE = "years_experience"
    DAYS_AVAILABILITY = "days_availability"
    ENUM = "enum"
    RADIO = "radio"
    DROPDOWN = "dropdown"
    AUTOCOMPLETE = "autocomplete"


@dataclass(frozen=True)
class FieldConstraint:
    """DOM-derived constraints; values are never logged by this module."""

    type: ConstraintType = ConstraintType.SHORT_TEXT
    required: bool = False
    min_value: float | None = None
    max_value: float | None = None
    max_length: int | None = None
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class ControlDescriptor:
    kind: ControlKind
    label: str = ""
    name: str = ""
    role: str = ""
    required: bool = False
    visible: bool = True
    constraint: FieldConstraint = field(default_factory=FieldConstraint)
    option_count: int = 0


@dataclass(frozen=True)
class NavigationIntent:
    action: str
    final_candidate: bool = False
    confidence: int = 0


@dataclass(frozen=True)
class ApplicationEntryCandidate:
    """A safe, pre-application control candidate discovered from page semantics."""

    label: str
    href: str = ""
    kind: ControlKind = ControlKind.BUTTON
    confidence: int = 0
    reason: str = ""


@dataclass(frozen=True)
class ApplicationEntryProgress:
    """Privacy-safe result of comparing an entry action's before/after state."""

    progressed: bool
    stage: ATSStage
    site_family: SiteFamily
    reason: str


@dataclass(frozen=True)
class StageEvidence:
    """Privacy-safe DOM structure used to disambiguate stage prose.

    Employer job descriptions often contain words such as ``skills``,
    ``education``, or ``submit application`` before the application has even
    started.  The state machine may use only these counts and semantic-control
    flags; it never receives a populated field value.
    """

    has_strong_application_entry: bool = False
    editable_field_count: int = 0

    @property
    def has_visible_form(self) -> bool:
        return self.editable_field_count > 0


def classify_application_entry_progress(
    previous_url: str,
    previous_body: str,
    next_url: str,
    next_body: str,
    *,
    previous_stage: ATSStage = ATSStage.LANDING,
    previous_site_family: SiteFamily = SiteFamily.GENERIC,
    next_evidence: StageEvidence | None = None,
) -> ApplicationEntryProgress:
    """Classify only meaningful entry transitions.

    Dynamic text changes on a landing page are not enough to reset loop
    detection.  A navigation, a newly inferred application stage, or a site
    family reclassification is a useful transition; everything else remains
    unchanged until the next bounded recovery attempt.
    """

    previous_url_key = normalize_text(previous_url).split("#", 1)[0]
    next_url_key = normalize_text(next_url).split("#", 1)[0]
    next_stage = infer_stage(
        next_url,
        next_body,
        previous_stage,
        evidence=next_evidence,
    )
    next_site_family = detect_site_family(next_url, next_body)
    if next_url_key != previous_url_key:
        return ApplicationEntryProgress(True, next_stage, next_site_family, "navigation")
    if next_stage is not previous_stage:
        return ApplicationEntryProgress(True, next_stage, next_site_family, "stage")
    if (
        next_site_family is not SiteFamily.GENERIC
        and next_site_family is not previous_site_family
    ):
        return ApplicationEntryProgress(True, next_stage, next_site_family, "site_family")
    return ApplicationEntryProgress(False, next_stage, next_site_family, "unchanged")


_APPLICATION_ENTRY_EXCLUSIONS = (
    "search",
    "filter",
    "share",
    "newsletter",
    "recommended jobs",
    "talent community",
    "join our talent",
    "subscribe",
    "save job",
    "job alert",
)


def rank_application_entry_candidates(
    controls: list[dict[str, object]] | tuple[dict[str, object], ...],
    *,
    stage: ATSStage = ATSStage.LANDING,
) -> tuple[ApplicationEntryCandidate, ...]:
    """Rank semantic Apply controls without relying on an LLM or coordinates.

    ``controls`` is intentionally a small JSON-compatible representation so the
    resolver can be exercised with fixtures and fed by either browser backend.
    This function only identifies *pre-application entry* controls.  Final
    submit, account creation, resume upload, and security controls remain
    guarded by their dedicated actions.
    """

    if stage not in {ATSStage.LANDING, ATSStage.APPLY_ENTRY}:
        return ()
    candidates: list[ApplicationEntryCandidate] = []
    seen: set[tuple[str, str]] = set()
    for raw in controls:
        label = normalize_text(raw.get("label", ""))
        href = str(raw.get("href", "") or "")
        kind_value = normalize_text(raw.get("kind", "button"))
        semantic_fields = normalize_text(
            " ".join(
                str(raw.get(key, "") or "")
                for key in ("label", "aria_label", "title", "value", "name", "id")
            )
        )
        semantic = normalize_text(f"{semantic_fields} {href}")
        # Exclude controls whose visible semantics are search/share/newsletter
        # actions.  Do not inspect the href for these exclusions: legitimate
        # Apply links commonly live under a careers search route whose URL
        # contains ``search`` while its accessible label is still Apply.
        if not semantic or any(term in semantic_fields for term in _APPLICATION_ENTRY_EXCLUSIONS):
            continue
        # A final submit is never selected by this resolver.  An employer page
        # may use "Submit Your Application" as its entry CTA, but it is safe
        # only before a form/application stage is visible.
        if stage is ATSStage.APPLY_ENTRY and re.search(
            r"\b(?:submit|finish|complete)\b", semantic
        ):
            continue
        exact = bool(
            re.search(
                r"\b(?:apply(?: now| here| manually| for (?:this )?(?:job|position))?|"
                r"start application|begin application|continue application|candidate login|"
                r"(?:sign up|register|create (?:an? )?account) to apply)\b",
                semantic,
            )
        )
        path_hint = bool(re.search(r"/(?:apply|application|candidate|careers?)(?:[/?#]|$)", href))
        generic_apply = "apply" in semantic or "application" in semantic
        if not (exact or path_hint or generic_apply):
            continue
        confidence = 90 if exact else 78 if path_hint else 70
        if kind_value in {"input", "submit"} and "apply" not in semantic:
            confidence -= 15
        key = (label or semantic, href)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            ApplicationEntryCandidate(
                label=label or semantic[:120],
                href=href,
                kind=ControlKind.LINK if kind_value in {"a", "link"} else ControlKind.BUTTON,
                confidence=confidence,
                reason="exact semantic application entry"
                if exact
                else "application path hint"
                if path_hint
                else "application semantic marker",
            )
        )
    candidates.sort(key=lambda item: (-item.confidence, item.label, item.href))
    return tuple(candidates)


def stage_evidence_from_visible_controls(
    controls: list[dict[str, object]] | tuple[dict[str, object], ...],
    *,
    editable_field_count: int = 0,
) -> StageEvidence:
    """Build a structural stage hint from non-sensitive live DOM metadata.

    Only an exact, pre-application CTA is strong enough to outrank job-
    description prose, and only while no editable application form is visible.
    This deliberately leaves weaker links and full forms to their normal
    semantic stage handling.
    """

    visible = [item for item in controls if isinstance(item, dict) and item.get("visible")]
    candidates = rank_application_entry_candidates(visible, stage=ATSStage.LANDING)
    try:
        count = max(0, int(editable_field_count))
    except (TypeError, ValueError):
        count = 0
    return StageEvidence(
        has_strong_application_entry=any(candidate.confidence >= 90 for candidate in candidates),
        editable_field_count=count,
    )


SITE_FAMILY_PATTERNS: dict[SiteFamily, tuple[str, ...]] = {
    SiteFamily.WORKDAY: ("myworkdayjobs.com", "workday.com", "workdayjobs.com"),
    SiteFamily.GREENHOUSE: ("greenhouse.io", "boards.greenhouse.io"),
    SiteFamily.LEVER: ("lever.co", "jobs.lever.co"),
    SiteFamily.ASHBY: ("ashbyhq.com", "jobs.ashbyhq.com"),
    SiteFamily.SMARTRECRUITERS: ("smartrecruiters.com",),
    SiteFamily.ICIMS: ("icims.com",),
    SiteFamily.TALEO: ("taleo.net", "taleo.com"),
    SiteFamily.ORACLE: ("oraclecloud.com", "oracle.com"),
    SiteFamily.SUCCESSFACTORS: ("successfactors",),
    SiteFamily.JOBVITE: ("jobvite.com",),
    SiteFamily.ADP: ("adp.com",),
    SiteFamily.BAMBOOHR: ("bamboohr.com",),
    SiteFamily.UKG: ("ukg.com", "ultipro.com"),
}

_FAMILY_MARKERS = {
    SiteFamily.WORKDAY: ("workday", "wd-", "wd1.myworkdayjobs"),
    SiteFamily.GREENHOUSE: ("greenhouse", "gh-application"),
    SiteFamily.LEVER: ("lever", "lever-application"),
    SiteFamily.ASHBY: ("ashby", "jobs.ashby"),
    SiteFamily.SMARTRECRUITERS: ("smartrecruiters", "smart recruiters"),
    SiteFamily.ICIMS: ("icims", "iCIMS"),
    SiteFamily.TALEO: ("taleo", "oracle recruiting"),
    SiteFamily.ORACLE: ("oracle candidate experience", "oracle recruiting cloud", "oracle cloud"),
    SiteFamily.SUCCESSFACTORS: ("successfactors", "sap successfactors"),
    SiteFamily.JOBVITE: ("jobvite",),
    SiteFamily.ADP: ("adp workforce now", "adp recruiting"),
    SiteFamily.BAMBOOHR: ("bamboohr",),
    SiteFamily.UKG: ("ukg", "ultipro"),
}

_CONFIRMATION_TERMS = (
    "thank you for applying",
    "application submitted",
    "application received",
    "successfully applied",
    "application complete",
)

_ACCOUNT_CREATION_PATTERN = re.compile(
    r"\b(?:create (?:an? )?account|register(?: for)?|sign up|"
    r"new (?:candidate|user)(?: registration)?|set up (?:an? )?account)\b"
)
_VOLUNTARY_DISCLOSURE_PATTERN = re.compile(
    r"\b(?:voluntary (?:self[- ]?identification|disclosure)|self[- ]?identify|"
    r"disability (?:self[- ]?identification|status)|protected veteran(?: status)?|"
    r"veteran self[- ]?identification)\b"
)
_EEO_PATTERN = re.compile(
    r"\b(?:eeo(?:[- ]?1)?|equal employment opportunity|race(?:/| and )ethnicity|"
    r"gender identity|ethnic(?:ity| origin))\b"
)
_CONTACT_INFORMATION_PATTERN = re.compile(
    r"\b(?:contact (?:information|details)|email address|phone (?:number)?|"
    r"mailing address|first name|last name)\b"
)
_PERSONAL_INFORMATION_PATTERN = re.compile(
    r"\b(?:personal (?:information|details)|legal name|preferred name|date of birth|"
    r"nationality|pronouns)\b"
)
_EMPLOYMENT_HISTORY_PATTERN = re.compile(
    r"\b(?:employment|work|professional|career) (?:history|experience)\b|\bexperience\b"
)
_EDUCATION_PATTERN = re.compile(
    r"\b(?:education(?:al)?(?: history)?|academic (?:history|background)|school|"
    r"university|college|degree)\b"
)
_SKILLS_PATTERN = re.compile(
    r"\b(?:skills?(?: and qualifications)?|competenc(?:y|ies)|technical skills|"
    r"proficienc(?:y|ies))\b"
)
_SCREENING_QUESTIONS_PATTERN = re.compile(
    r"\b(?:screening(?: questions?)?|additional questions?|pre[- ]?screen(?:ing)?|"
    r"questionnaire|work authorization|availability|eligibility questions?)\b"
)
_AUTH_PATTERN = re.compile(
    r"\b(?:sign in|log in|login|password|forgot password|existing (?:candidate|user|account))\b"
)


def normalize_text(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def detect_site_family(url: str, body: str = "") -> SiteFamily:
    """Use hostname first, then bounded DOM markers as a compatibility hint."""
    host = (urlparse(str(url)).hostname or "").casefold()
    for family, patterns in SITE_FAMILY_PATTERNS.items():
        if any(pattern in host for pattern in patterns):
            return family
    text = normalize_text(body)
    for family, markers in _FAMILY_MARKERS.items():
        if any(normalize_text(marker) in text for marker in markers):
            return family
    return SiteFamily.GENERIC


def page_fingerprint(url: str, body: str) -> str:
    """Return a stable, non-content identifier for loop detection."""
    normalized_url = normalize_text(url).split("#", 1)[0]
    # Keep only structural text; the digest is safe to write to diagnostics.
    structure = normalize_text(body)[:12000]
    return hashlib.sha256(f"{normalized_url}|{structure}".encode()).hexdigest()[:16]


def infer_stage(
    url: str,
    body: str,
    previous: ATSStage | None = None,
    *,
    evidence: StageEvidence | None = None,
) -> ATSStage:
    """Infer a current stage from visible structural markers.

    The result is a hint, never proof of submission.  Confirmation remains
    governed by the strict independent verification path in ``ApplyAgent``.
    """
    text = normalize_text(body)
    if any(term in text for term in _CONFIRMATION_TERMS):
        return ATSStage.CONFIRMATION

    # A high-confidence entry CTA on a page without editable application
    # fields is stronger evidence than terms embedded in a job description.
    # This is intentionally structural: an actual registration/application
    # form keeps its normal stage even if its button says "Sign up to apply".
    if (
        evidence is not None
        and evidence.has_strong_application_entry
        and not evidence.has_visible_form
        and previous in {None, ATSStage.LANDING, ATSStage.APPLY_ENTRY}
    ):
        return ATSStage.APPLY_ENTRY

    # Submission controls take precedence only after the form has been
    # completed/reviewed.  Greenhouse and other ATS families render their
    # static final button beside an unfinished form, where treating its label
    # as a late stage prevents ordinary field handling.  A final click remains
    # independently guarded at every stage.
    if re.search(
        r"\b(?:submit (?:your )?application|finish application|complete application)\b", text
    ) and (
        evidence is None
        or not evidence.has_visible_form
        or previous in {ATSStage.REVIEW, ATSStage.SUBMIT}
    ):
        return ATSStage.SUBMIT
    if re.search(r"\b(?:review (?:your )?application|check your application)\b", text):
        return ATSStage.REVIEW
    if re.search(r"\b(?:submit|finish)\b", text) and previous in {
        ATSStage.REVIEW,
        ATSStage.SUBMIT,
    }:
        return ATSStage.SUBMIT

    # An account-registration page can also contain email and password
    # controls.  It must be distinguished from sign-in before the generic
    # auth classifier gets a chance to consume it.
    if _ACCOUNT_CREATION_PATTERN.search(text):
        return ATSStage.ACCOUNT_CREATION

    # Employer job descriptions routinely include EEO and resume language.
    # On a first landing-page observation, an explicit Apply Now / Start
    # Application control is stronger evidence that the page still needs its
    # application-entry transition than those boilerplate words are that a
    # form section is active.  Keep established form stages unchanged and let
    # the bounded entry resolver verify a real transition after the click.
    if (
        previous in {None, ATSStage.LANDING, ATSStage.APPLY_ENTRY}
        and (evidence is None or not evidence.has_visible_form)
        and re.search(r"\b(?:apply now|apply for this job|start application)\b", text)
    ):
        return ATSStage.APPLY_ENTRY

    # These disclosures are more specific than the general question stage.
    if _VOLUNTARY_DISCLOSURE_PATTERN.search(text):
        return ATSStage.VOLUNTARY_DISCLOSURE
    if _EEO_PATTERN.search(text):
        return ATSStage.EEO
    if re.search(r"\b(resume|résumé|cv|upload document)\b", text) or "type=file" in text:
        return ATSStage.RESUME
    if _EDUCATION_PATTERN.search(text):
        return ATSStage.EDUCATION
    if _SKILLS_PATTERN.search(text):
        return ATSStage.SKILLS
    if _EMPLOYMENT_HISTORY_PATTERN.search(text):
        return ATSStage.EMPLOYMENT_HISTORY
    if _SCREENING_QUESTIONS_PATTERN.search(text):
        return ATSStage.SCREENING_QUESTIONS
    # Check sign-in before generic email/phone labels; otherwise a login page
    # is easily mistaken for the contact-information step.
    if _AUTH_PATTERN.search(text):
        return ATSStage.AUTH
    if _CONTACT_INFORMATION_PATTERN.search(text):
        return ATSStage.CONTACT_INFORMATION
    if _PERSONAL_INFORMATION_PATTERN.search(text):
        return ATSStage.PERSONAL_INFORMATION
    if re.search(r"\b(apply now|apply for this job|start application)\b", text):
        return ATSStage.APPLY_ENTRY
    return previous or ATSStage.LANDING


def navigation_intent(label: str, stage: ATSStage) -> NavigationIntent:
    """Map a visible control label to a bounded navigation intent."""
    text = normalize_text(label)
    if not text:
        return NavigationIntent("unknown")
    if any(term in text for term in ("review application", "review", "save and continue")):
        return NavigationIntent("review", confidence=90)
    if any(term in text for term in ("next", "continue", "save & continue")):
        return NavigationIntent("next", confidence=88)
    if any(term in text for term in ("submit application", "submit", "finish", "complete")):
        final = stage in {ATSStage.REVIEW, ATSStage.FINAL_SUBMIT}
        return NavigationIntent(
            "submit" if final else "next", final_candidate=final, confidence=95 if final else 58
        )
    if any(term in text for term in ("apply now", "start application", "apply")):
        return NavigationIntent("apply_entry", confidence=82)
    return NavigationIntent("unknown")


def validation_repair_plan(message: str, constraint: FieldConstraint) -> str | None:
    """Return one deterministic repair strategy, or ``None`` when unsupported."""
    text = normalize_text(message)
    if "required" in text or "must select" in text:
        return "fill_required_or_select_option"
    if "email" in text or constraint.type is ConstraintType.EMAIL:
        return "repair_email_format"
    if "phone" in text or constraint.type is ConstraintType.PHONE:
        return "repair_phone_format"
    if any(word in text for word in ("number", "numeric", "integer", "decimal")):
        return "repair_numeric_constraint"
    if "date" in text or constraint.type is ConstraintType.DATE:
        return "repair_date_constraint"
    if "maximum" in text or "maxlength" in text:
        return "repair_max_length"
    if constraint.type in {ConstraintType.DROPDOWN, ConstraintType.AUTOCOMPLETE}:
        return "select_verified_option"
    return None


class _ControlParser(HTMLParser):
    """Small semantic parser used by tests and deterministic recovery helpers."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.controls: list[ControlDescriptor] = []
        self._labels: dict[str, str] = {}
        self._label_stack: list[str] = []
        self._select_options: list[str] = []
        self._select_attrs: dict[str, str] | None = None
        self._select_label = ""

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {str(key).casefold(): str(value or "") for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        data = self._attrs(attrs)
        if tag == "label":
            self._label_stack.append(data.get("for", ""))
            return
        if tag == "select":
            self._select_attrs = data
            self._select_options = []
            self._select_label = self._labels.get(data.get("id", ""), "")
            return
        if tag == "option" and self._select_attrs is not None:
            self._select_options.append(data.get("value", ""))
            return
        if tag not in {"input", "textarea", "button", "a", "[dialog]"} and data.get("role") not in {
            "combobox",
            "dialog",
            "button",
            "link",
            "listbox",
            "option",
        }:
            return
        kind = self._kind(tag, data)
        if kind is None:
            return
        label = data.get("aria-label", "") or data.get("placeholder", "") or data.get("name", "")
        labelled_by = data.get("aria-labelledby", "")
        if labelled_by:
            label = self._labels.get(labelled_by, label)
        if data.get("id") in self._labels:
            label = self._labels[data["id"]]
        required = "required" in data or data.get("aria-required", "").casefold() == "true"
        constraint = _constraint_for(kind, data, ())
        self.controls.append(
            ControlDescriptor(
                kind=kind,
                label=normalize_text(label),
                name=data.get("name", ""),
                role=data.get("role", ""),
                required=required,
                constraint=constraint,
            )
        )

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "label" and self._label_stack:
            self._label_stack.pop()
        elif tag == "select" and self._select_attrs is not None:
            data = self._select_attrs
            required = "required" in data or data.get("aria-required", "").casefold() == "true"
            self.controls.append(
                ControlDescriptor(
                    kind=ControlKind.SELECT,
                    label=normalize_text(
                        self._select_label or data.get("aria-label", "") or data.get("name", "")
                    ),
                    name=data.get("name", ""),
                    role=data.get("role", "select"),
                    required=required,
                    constraint=_constraint_for(
                        ControlKind.SELECT, data, tuple(self._select_options)
                    ),
                    option_count=len(self._select_options),
                )
            )
            self._select_attrs = None
            self._select_options = []

    def handle_data(self, data: str) -> None:
        if self._label_stack:
            target = self._label_stack[-1]
            self._labels[target] = normalize_text(self._labels.get(target, "") + " " + data)

    @staticmethod
    def _kind(tag: str, data: dict[str, str]) -> ControlKind | None:
        role = data.get("role", "").casefold()
        input_type = data.get("type", "text").casefold()
        if role == "dialog" or tag == "[dialog]":
            return ControlKind.DIALOG
        if role in {"combobox", "listbox"} or data.get("aria-autocomplete"):
            return (
                ControlKind.AUTOCOMPLETE if data.get("aria-autocomplete") else ControlKind.COMBOBOX
            )
        if tag == "button" or role == "button":
            return ControlKind.BUTTON
        if tag == "a" or role == "link":
            return ControlKind.LINK
        if tag == "textarea":
            return ControlKind.TEXTAREA
        if tag != "input":
            return None
        return {
            "email": ControlKind.EMAIL,
            "number": ControlKind.NUMBER,
            "tel": ControlKind.PHONE,
            "date": ControlKind.DATE,
            "checkbox": ControlKind.CHECKBOX,
            "radio": ControlKind.RADIO,
            "file": ControlKind.FILE,
        }.get(input_type, ControlKind.TEXT)


def _constraint_for(
    kind: ControlKind, data: dict[str, str], options: tuple[str, ...]
) -> FieldConstraint:
    semantic = normalize_text(
        " ".join(data.get(key, "") for key in ("name", "id", "aria-label", "placeholder"))
    )
    mapping = {
        ControlKind.EMAIL: ConstraintType.EMAIL,
        ControlKind.PHONE: ConstraintType.PHONE,
        ControlKind.NUMBER: (
            ConstraintType.INTEGER if data.get("step", "1") == "1" else ConstraintType.DECIMAL
        ),
        ControlKind.DATE: ConstraintType.DATE,
        ControlKind.CHECKBOX: ConstraintType.BOOLEAN,
        ControlKind.RADIO: ConstraintType.RADIO,
        ControlKind.SELECT: ConstraintType.DROPDOWN,
        ControlKind.COMBOBOX: ConstraintType.DROPDOWN,
        ControlKind.AUTOCOMPLETE: ConstraintType.AUTOCOMPLETE,
        ControlKind.TEXTAREA: ConstraintType.LONG_TEXT,
    }
    kind_value = mapping.get(kind, ConstraintType.SHORT_TEXT)
    if any(term in semantic for term in ("salary", "compensation", "pay")):
        kind_value = ConstraintType.SALARY
    elif any(
        term in semantic for term in ("years experience", "years of experience", "experience years")
    ):
        kind_value = ConstraintType.YEARS_EXPERIENCE
    elif any(term in semantic for term in ("availability", "start days", "notice period")):
        kind_value = ConstraintType.DAYS_AVAILABILITY
    return FieldConstraint(
        type=kind_value,
        required="required" in data or data.get("aria-required", "").casefold() == "true",
        min_value=float(data["min"]) if data.get("min", "").replace(".", "", 1).isdigit() else None,
        max_value=float(data["max"]) if data.get("max", "").replace(".", "", 1).isdigit() else None,
        max_length=int(data["maxlength"]) if data.get("maxlength", "").isdigit() else None,
        options=options,
    )


def discover_controls(html: str) -> tuple[ControlDescriptor, ...]:
    parser = _ControlParser()
    parser.feed(str(html or ""))
    return tuple(parser.controls)


@dataclass
class ATSRunState:
    """Bounded per-worker state and failure memory."""

    site_family: SiteFamily = SiteFamily.GENERIC
    stage: ATSStage = ATSStage.LANDING
    max_recovery_attempts: int = 2
    attempted_actions: set[str] = field(default_factory=set)
    failed_selectors: set[str] = field(default_factory=set)
    validation_repairs: int = 0
    provider_failures: int = 0
    upload_attempted: bool = False
    final_submit_attempted: bool = False
    _fingerprint: str = ""
    _repeat_count: int = 0

    def observe(
        self,
        url: str,
        body: str,
        *,
        evidence: StageEvidence | None = None,
    ) -> ATSStage:
        fingerprint = page_fingerprint(url, body)
        self._repeat_count = self._repeat_count + 1 if fingerprint == self._fingerprint else 0
        self._fingerprint = fingerprint
        self.stage = infer_stage(url, body, self.stage, evidence=evidence)
        return self.stage

    def record_progress(self, url: str, body: str, stage: ATSStage | None = None) -> None:
        """Reset loop memory after a verified meaningful page transition."""
        self._fingerprint = page_fingerprint(url, body)
        self._repeat_count = 0
        if stage is not None:
            self.stage = stage

    def reset_loop_memory_for_provider_handoff(self) -> None:
        """Allow one fresh page observation after a safe provider handoff.

        A provider switch keeps the same live ATS page and all irreversible
        guards. Reusing the failed provider's fingerprint would therefore
        stop the replacement agent before it can inspect that page. The
        caller still has the bounded provider candidate list and may invoke
        this only before a handoff, so the normal same-page guard remains
        active for the replacement provider.
        """

        self._fingerprint = ""
        self._repeat_count = 0
        # Ordinary form actions are safe to replay when a replacement model
        # inherits the same live page. Keeping their DOM-index memory makes a
        # fallback appear stalled after the first provider partially fills a
        # static form, even though the replacement may need to correct or
        # complete those same fields. Irreversible and repair guards remain.
        self.attempted_actions = {
            action
            for action in self.attempted_actions
            if not action.startswith("semantic_progress:")
        }

    @property
    def loop_detected(self) -> bool:
        return self._repeat_count >= 2

    def record_action(self, action: str) -> bool:
        """Record once; false means the same action must not be replayed."""
        key = normalize_text(action)
        if not key or key in self.attempted_actions:
            return False
        self.attempted_actions.add(key)
        return True

    def allow_repair(self, strategy: str) -> bool:
        if self.validation_repairs >= self.max_recovery_attempts:
            return False
        if not self.record_action(f"repair:{strategy}"):
            return False
        self.validation_repairs += 1
        return True
