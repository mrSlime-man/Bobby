"""Normalize screening questions and resolve facts without inventing answers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence


CANONICAL_CATEGORIES = {
    "high_school_completed",
    "education_level",
    "work_authorization",
    "sponsorship_required",
    "relocation_willingness",
    "flexible_shifts",
    "night_shift",
    "weekend_availability",
    "travel_willingness",
    "background_check",
    "drug_test",
    "remote_work",
    "start_availability",
}


@dataclass(frozen=True)
class RadioField:
    question: str
    options: tuple[str, ...]
    values: tuple[str, ...]
    required: bool = False


class AnswerType(str, Enum):
    BOOLEAN = "BOOLEAN"
    INTEGER = "INTEGER"
    DECIMAL = "DECIMAL"
    SHORT_TEXT = "SHORT_TEXT"
    LONG_TEXT = "LONG_TEXT"
    DATE = "DATE"
    PHONE = "PHONE"
    EMAIL = "EMAIL"
    SALARY = "SALARY"
    YEARS_EXPERIENCE = "YEARS_EXPERIENCE"
    DAYS_AVAILABILITY = "DAYS_AVAILABILITY"
    ENUM_RADIO = "ENUM/RADIO"
    DROPDOWN = "DROPDOWN"


@dataclass(frozen=True)
class FieldSpec:
    """Normalized constraints collected from one real form control."""

    question: str
    answer_type: AnswerType
    input_type: str = "text"
    maxlength: int | None = None
    minlength: int | None = None
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    step: Decimal | None = None
    pattern: str | None = None
    inputmode: str | None = None
    required: bool = False
    placeholder: str | None = None
    aria_label: str | None = None
    aria_describedby: str | None = None
    role: str | None = None
    help_text: str | None = None
    error_text: str | None = None
    character_counter: str | None = None

    @property
    def numeric(self) -> bool:
        return self.answer_type in {
            AnswerType.INTEGER,
            AnswerType.DECIMAL,
            AnswerType.SALARY,
            AnswerType.YEARS_EXPERIENCE,
            AnswerType.DAYS_AVAILABILITY,
        }

    def prompt_context(self) -> str:
        values = {
            "answer_type": self.answer_type.value,
            "input_type": self.input_type,
            "maxlength": self.maxlength,
            "minlength": self.minlength,
            "min": str(self.minimum) if self.minimum is not None else None,
            "max": str(self.maximum) if self.maximum is not None else None,
            "step": str(self.step) if self.step is not None else None,
            "pattern": self.pattern,
            "inputmode": self.inputmode,
            "required": self.required,
            "placeholder": self.placeholder,
            "help_text": self.help_text,
            "error_text": self.error_text,
            "character_counter": self.character_counter,
        }
        return ", ".join(f"{key}={value}" for key, value in values.items() if value not in (None, ""))


def _optional_int(value: Any) -> int | None:
    try:
        return int(str(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _optional_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "", "any") else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def classify_answer_type(question: str, attributes: Mapping[str, Any] | None = None) -> AnswerType:
    attrs = attributes or {}
    q = normalize_question(question).casefold()
    input_type = str(attrs.get("type") or "text").casefold()
    inputmode = str(attrs.get("inputmode") or "").casefold()
    role = str(attrs.get("role") or "").casefold()
    tag = str(attrs.get("tag") or attrs.get("tag_name") or "").casefold()
    maxlength = _optional_int(attrs.get("maxlength"))

    if input_type == "checkbox":
        return AnswerType.BOOLEAN
    if input_type == "radio" or role in {"radio", "radiogroup"}:
        return AnswerType.ENUM_RADIO
    if tag == "select" or role in {"listbox", "combobox"}:
        return AnswerType.DROPDOWN
    if input_type == "date" or re.search(r"\b(?:start|available|availability) date\b", q):
        return AnswerType.DATE
    if input_type == "email" or re.search(r"\be-?mail\b", q):
        return AnswerType.EMAIL
    if input_type == "tel" or re.search(r"\b(?:phone|mobile)\b", q):
        return AnswerType.PHONE
    if re.search(r"\b(?:experience|worked|working)\b.*\b(?:years?|yrs?)\b|\b(?:years?|yrs?)\b.*\bexperience\b", q):
        return AnswerType.YEARS_EXPERIENCE
    if re.search(r"\b(?:join|start|availability|notice)\b.*(?:\(\s*days?\s*\)|\bdays?\b)", q):
        return AnswerType.DAYS_AVAILABILITY
    if re.search(r"\b(?:salary|compensation|ctc|pay|wage|earnings)\b", q):
        return AnswerType.SALARY
    if input_type == "number" or inputmode in {"numeric", "decimal"}:
        return AnswerType.DECIMAL if inputmode == "decimal" or _optional_decimal(attrs.get("step")) not in (None, Decimal(1)) else AnswerType.INTEGER
    if maxlength is not None and maxlength <= 120:
        return AnswerType.SHORT_TEXT
    if input_type == "textarea":
        return AnswerType.LONG_TEXT
    return AnswerType.SHORT_TEXT


def field_spec_from_snapshot(question: str, snapshot: Mapping[str, Any] | None = None) -> FieldSpec:
    snapshot = snapshot or {}
    return FieldSpec(
        question=normalize_question(question),
        answer_type=classify_answer_type(question, snapshot),
        input_type=str(snapshot.get("type") or "text").casefold(),
        maxlength=_optional_int(snapshot.get("maxlength")),
        minlength=_optional_int(snapshot.get("minlength")),
        minimum=_optional_decimal(snapshot.get("min")),
        maximum=_optional_decimal(snapshot.get("max")),
        step=_optional_decimal(snapshot.get("step")),
        pattern=str(snapshot.get("pattern")) if snapshot.get("pattern") else None,
        inputmode=str(snapshot.get("inputmode")) if snapshot.get("inputmode") else None,
        required=(
            "required" in snapshot
            and snapshot.get("required") is not False
            and str(snapshot.get("required")).casefold() != "false"
        )
        or str(snapshot.get("aria-required") or "").casefold() == "true",
        placeholder=snapshot.get("placeholder"),
        aria_label=snapshot.get("aria-label"),
        aria_describedby=snapshot.get("aria-describedby"),
        role=snapshot.get("role"),
        help_text=snapshot.get("help_text"),
        error_text=snapshot.get("error_text"),
        character_counter=snapshot.get("character_counter"),
    )


def _nested_values(data: Any, wanted_key: str) -> list[Any]:
    wanted = re.sub(r"[^a-z0-9]+", "", wanted_key.casefold())
    found: list[Any] = []
    if isinstance(data, Mapping):
        for key, value in data.items():
            normalized = re.sub(r"[^a-z0-9]+", "", str(key).casefold())
            if normalized == wanted:
                found.append(value)
            found.extend(_nested_values(value, wanted_key))
    elif isinstance(data, list):
        for value in data:
            found.extend(_nested_values(value, wanted_key))
    return found


def _first_number(values: Iterable[Any]) -> Decimal | None:
    for value in values:
        if isinstance(value, Mapping) and "answer" in value:
            value = value["answer"]
        match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
        if match:
            return Decimal(match.group())
    return None


def resolve_structured_answer(
    spec: FieldSpec,
    resume: Mapping[str, Any] | None,
    profile: Mapping[str, Any] | None,
) -> str | None:
    """Resolve only facts explicitly supported by candidate sources."""
    q = spec.question.casefold()
    resume = resume or {}
    profile = profile or {}

    if spec.answer_type == AnswerType.EMAIL:
        for value in _nested_values(profile, "email"):
            value = str(value or "").strip()
            if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
                return value
        return None

    if spec.answer_type == AnswerType.PHONE:
        for value in _nested_values(profile, "phone"):
            value = str(value or "").strip()
            if re.search(r"\d{7,}", re.sub(r"\D", "", value)):
                return value
        return None

    if spec.answer_type == AnswerType.DAYS_AVAILABILITY:
        availability_values = (
            _nested_values(profile, "available_immediately")
            + _nested_values(profile, "available_to_start_immediately")
            + _nested_values(profile, "can_start_immediately")
            + _nested_values(profile, "immediate_start")
            + _nested_values(profile, "notice_period")
            + _nested_values(profile, "notice_period_days")
            + _nested_values(resume, "notice_period")
        )
        if any(value is True or "immediate" in str(value).casefold() or str(value).casefold() == "none" for value in availability_values):
            return "0"
        return None

    if spec.answer_type == AnswerType.SALARY:
        annual = bool(re.search(r"\b(?:annual|annually|per annum|yearly|ctc)\b", q))
        hourly = bool(re.search(r"\b(?:hourly|per hour|/hr)\b", q))
        if annual:
            target = _first_number(_nested_values(profile, "target_hourly_usd"))
            if target is None:
                target = _first_number(_nested_values(profile, "if_single_hourly_number_required"))
            if target is None:
                target = _first_number(_nested_values(profile, "minimum_hourly_usd"))
            if target is None:
                target = _first_number(
                    _nested_values(profile, "minimum_hourly_compensation_usd")
                )
            if target is not None:
                return str(int(target * Decimal(2080)))
        if hourly:
            target = _first_number(_nested_values(profile, "target_hourly_usd"))
            if target is None:
                target = _first_number(_nested_values(profile, "minimum_hourly_usd"))
            if target is None:
                target = _first_number(
                    _nested_values(profile, "minimum_hourly_compensation_usd")
                )
            if target is not None:
                return str(target.normalize())
        return None

    if spec.answer_type == AnswerType.YEARS_EXPERIENCE:
        # A skills-list mention does not establish duration. Accept only an
        # explicit technology-to-years mapping/value from the resume.
        subject = re.sub(r"^.*?experience\s*(?:\(years?\)|in years?)?\s*(?:with|in|using)?\s*", "", q)
        subject = re.sub(r"[?*].*$", "", subject).strip()
        candidate_keys = [
            f"{subject}_years",
            f"years_{subject}",
            f"years_experience_{subject}",
        ]
        for key in candidate_keys:
            value = _first_number(_nested_values(resume, key))
            if value is not None:
                return str(value.normalize())
        return None
    return None


def normalize_answer_for_field(answer: Any, spec: FieldSpec) -> str | None:
    value = str(answer or "").strip()
    if not value:
        return None
    if spec.numeric:
        numbers = re.findall(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
        if len(numbers) != 1:
            return None
        value = numbers[0]
        if spec.answer_type in {AnswerType.INTEGER, AnswerType.SALARY, AnswerType.YEARS_EXPERIENCE, AnswerType.DAYS_AVAILABILITY}:
            try:
                number = Decimal(value)
            except InvalidOperation:
                return None
            if number != number.to_integral_value():
                return None
            value = str(int(number))
    if spec.maxlength is not None and len(value) > spec.maxlength:
        if spec.numeric:
            return None
        value = value[: spec.maxlength].rstrip()
    return value or None


def validate_answer_for_field(answer: Any, spec: FieldSpec) -> tuple[str, ...]:
    value = str(answer or "")
    issues: list[str] = []
    if spec.required and not value:
        issues.append("required")
    if spec.maxlength is not None and len(value) > spec.maxlength:
        issues.append(f"maxlength={spec.maxlength}")
    if spec.minlength is not None and value and len(value) < spec.minlength:
        issues.append(f"minlength={spec.minlength}")
    if spec.pattern and value:
        try:
            if re.fullmatch(spec.pattern, value) is None:
                issues.append("pattern")
        except re.error:
            pass
    if spec.numeric and value:
        try:
            number = Decimal(value)
        except InvalidOperation:
            issues.append("numeric")
        else:
            if spec.minimum is not None and number < spec.minimum:
                issues.append(f"min={spec.minimum}")
            if spec.maximum is not None and number > spec.maximum:
                issues.append(f"max={spec.maximum}")
            if spec.step is not None and spec.step != 0:
                base = spec.minimum or Decimal(0)
                if (number - base) % spec.step != 0:
                    issues.append(f"step={spec.step}")
    return tuple(issues)


def normalize_question(text: Any, options: Iterable[Any] = ()) -> str:
    value = str(text or "").replace("\\", " ")
    value = re.sub(r"[\r\n\t\u00a0]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    # LinkedIn sometimes flattens helper UI into the label. Remove only
    # well-delimited UI suffixes; never discard words from the question body.
    value = re.sub(r"\s*\d+\s*/\s*\d+\s*$", "", value).strip()
    clean_options = _deduplicate_options(options)
    if not clean_options and re.search(r"\?\s*\*?\s*(?:yes\s*/?\s*no|no\s*/?\s*yes)(?:\s*/?\s*other)?\s*$", value, re.I):
        clean_options = ("Yes", "No")
    if clean_options:
        orderings = (clean_options, tuple(reversed(clean_options)))
        for ordering in orderings:
            suffix = r"\s*\*?\s*" + r"\s*(?:/|\||,)?\s*".join(
                re.escape(option) for option in ordering
            ) + r"\s*$"
            stripped = re.sub(suffix, "", value, flags=re.I).strip()
            if stripped != value:
                value = stripped
                break
    value = re.sub(r"\s*\*+\s*$", "", value).strip()
    value = re.sub(r"\s+([?!.,:;])", r"\1", value)
    return value


def normalize_option(text: Any) -> str:
    value = normalize_question(text)
    return value.strip(" *")


def _deduplicate_options(options: Iterable[Any]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in options:
        option = normalize_option(raw)
        key = option.casefold()
        if option and key not in seen:
            seen.add(key)
            result.append(option)
    return tuple(result)


def parse_radio_snapshot(snapshot: Mapping[str, Any] | None, combined_text: Any = "") -> RadioField:
    """Convert DOM-extracted radio metadata into a clean question and option list."""
    snapshot = snapshot or {}
    raw_options = snapshot.get("options") or []
    labels: list[Any] = []
    values: list[Any] = []
    for item in raw_options:
        if isinstance(item, Mapping):
            label = item.get("label") or item.get("ariaLabel") or item.get("value")
            value = item.get("value") or label
        else:
            label = value = item
        if label:
            labels.append(label)
            values.append(value)

    options = _deduplicate_options(labels)
    option_values = _deduplicate_options(values)
    raw_question = snapshot.get("question") or ""
    required = bool(snapshot.get("required")) or bool(re.search(r"\*\s*$", str(raw_question)))
    question = normalize_question(raw_question)
    raw_combined = re.sub(r"\s+", " ", str(combined_text or "")).strip()
    combined = normalize_question(raw_combined, options)

    # Old/new LinkedIn markup can expose only the container's flattened text.
    # Remove a trailing required marker plus option labels without touching the
    # actual question punctuation. If labels collapsed, recover common booleans.
    if not options and re.search(r"\*?\s*(?:yes\s*no|no\s*yes)\s*$", raw_combined, re.I):
        options = ("Yes", "No")
        option_values = options
    if re.search(r"\*\s*(?:yes\s*no|no\s*yes)\s*$", raw_combined, re.I):
        required = True
    if not question:
        question = combined
    suffix_options = options or ("Yes", "No")
    if question:
        for ordering in (suffix_options, tuple(reversed(suffix_options))):
            suffix = r"\s*\*?\s*" + r"\s*".join(re.escape(x) for x in ordering) + r"\s*$"
            stripped = re.sub(suffix, "", question, flags=re.I).strip()
            if stripped != question:
                required = required or "*" in question[len(stripped) :]
                question = stripped
                break
    question = normalize_question(question)
    return RadioField(question, options, option_values, required)


def canonical_category(question: str) -> str | None:
    q = normalize_question(question).casefold()
    if re.search(r"high school|secondary school", q):
        return "high_school_completed"
    if re.search(r"bachelor|master|associate|doctorate|degree|education level", q):
        return "education_level"
    if re.search(r"sponsor|sponsorship", q):
        return "sponsorship_required"
    if re.search(r"authori[sz]ed.*work|legally.*work|work authori[sz]ation", q):
        return "work_authorization"
    if re.search(r"relocat", q):
        return "relocation_willingness"
    if re.search(r"flexible schedule|different shift|varied shift|varying shift", q):
        return "flexible_shifts"
    if re.search(r"night shift|overnight", q):
        return "night_shift"
    if re.search(r"weekend", q):
        return "weekend_availability"
    if re.search(r"travel", q):
        return "travel_willingness"
    if re.search(r"background (?:check|screen)", q):
        return "background_check"
    if re.search(r"drug (?:test|screen)", q):
        return "drug_test"
    if re.search(r"(?:work|working).*(?:remote|from home)|(?:remote|from home).*(?:work|working)", q):
        return "remote_work"
    if re.search(r"when can you start|how soon can you join|availability to start|notice period", q):
        return "start_availability"
    return None


def match_available_option(answer: Any, options: Sequence[str]) -> str | None:
    """Return the actual DOM option matching an answer, never invented text."""
    wanted = normalize_option(answer).casefold()
    if not wanted:
        return None
    aliases = {
        "true": "yes",
        "false": "no",
        "y": "yes",
        "n": "no",
        "immediate": "immediately",
        "available immediately": "immediately",
    }
    wanted = aliases.get(wanted, wanted)
    for option in options:
        normalized = normalize_option(option).casefold()
        if aliases.get(normalized, normalized) == wanted:
            return option
    return None


def _find_values(data: Any, keys: set[str]) -> list[Any]:
    found: list[Any] = []
    if isinstance(data, Mapping):
        for key, value in data.items():
            if str(key).casefold() in keys:
                if isinstance(value, Mapping) and "answer" in value:
                    found.append(value["answer"])
                elif not isinstance(value, (Mapping, list)):
                    found.append(value)
            found.extend(_find_values(value, keys))
    elif isinstance(data, list):
        for value in data:
            found.extend(_find_values(value, keys))
    return found


def _known_bool(profile: Mapping[str, Any] | None, *keys: str) -> bool | None:
    values = _find_values(profile or {}, {key.casefold() for key in keys})
    booleans = [value for value in values if isinstance(value, bool)]
    if not booleans:
        return None
    return booleans[0] if len(set(booleans)) == 1 else None


def _education_levels(resume: Mapping[str, Any] | None) -> list[str]:
    values = _find_values(resume or {}, {"education_level", "degree", "final_evaluation_grade"})
    return [normalize_question(value).casefold() for value in values if isinstance(value, str)]


def _match_answer(value: bool | str | None, options: Sequence[str]) -> str | None:
    if value is None:
        return None
    wanted = ("yes", "true") if value is True else ("no", "false") if value is False else (str(value).casefold(),)
    for token in wanted:
        matched = match_available_option(token, options)
        if matched is not None:
            return matched
    if isinstance(value, str):
        for option in options:
            if value.casefold() in option.casefold() or option.casefold() in value.casefold():
                return option
    return None


def resolve_canonical_answer(
    question: str,
    options: Sequence[str],
    resume: Mapping[str, Any] | None,
    profile: Mapping[str, Any] | None,
) -> str | None:
    """Resolve a screening answer only when local sources make it truthful."""
    category = canonical_category(question)
    if category is None:
        return None
    q = normalize_question(question).casefold()
    value: bool | str | None = None

    if category in {"high_school_completed", "education_level"}:
        levels = _education_levels(resume)
        asked = next(
            (level for level in ("doctorate", "master", "bachelor", "associate", "high school") if level in q),
            None,
        )
        if asked:
            value = any(asked in level for level in levels) if levels else None
        elif levels:
            value = levels[0]
    elif category == "work_authorization":
        if re.search(r"canada|canadian", q):
            value = _known_bool(resume, "canada_work_authorization", "legally_allowed_to_work_in_canada")
        elif re.search(r"(?:united kingdom|\buk\b|britain)", q):
            value = _known_bool(resume, "uk_work_authorization", "legally_allowed_to_work_in_uk")
        elif re.search(r"(?:european union|\beu\b)", q):
            value = _known_bool(resume, "eu_work_authorization", "legally_allowed_to_work_in_eu")
        else:
            value = _known_bool(
                profile,
                "legally_authorized_to_work_in_us",
                "authorized_to_work_in_us",
            )
            if value is None:
                value = _known_bool(resume, "us_work_authorization", "legally_allowed_to_work_in_us")
    elif category == "sponsorship_required":
        value = _known_bool(
            profile,
            "sponsorship_required_now",
            "sponsorship_required_future",
            "requires_sponsorship_now",
            "requires_sponsorship_future",
        )
        if value is None:
            value = _known_bool(resume, "requires_us_sponsorship")
    elif category == "start_availability":
        availability_values = (
            _nested_values(profile, "available_immediately")
            + _nested_values(profile, "available_to_start_immediately")
            + _nested_values(profile, "can_start_immediately")
            + _nested_values(profile, "immediate_start")
            + _nested_values(profile, "notice_period")
            + _nested_values(profile, "notice_period_days")
            + _nested_values(resume, "notice_period")
        )
        if any(
            value is True
            or "immediate" in str(value).casefold()
            or str(value).casefold() == "none"
            for value in availability_values
        ):
            for candidate in ("Immediately", "0", "0 days"):
                matched = match_available_option(candidate, options)
                if matched is not None:
                    return matched
            return None
    else:
        keys = {
            "relocation_willingness": ("willing_to_relocate", "open_to_relocation"),
            "flexible_shifts": ("willing_to_work_flexible_schedule", "flexible_schedule"),
            "night_shift": ("willing_to_work_nights", "willing_to_work_overnight", "overnight"),
            "weekend_availability": ("willing_to_work_weekends", "weekends"),
            "travel_willingness": ("willing_to_travel",),
            "background_check": (
                "willing_to_undergo_background_check",
                "willing_to_undergo_background_checks",
                "willing_to_background_check",
            ),
            "drug_test": (
                "willing_to_undergo_drug_test",
                "willing_to_undergo_drug_tests",
                "willing_to_drug_screen",
            ),
            "remote_work": (
                "willing_to_work_remote",
                "open_to_remote_work",
                "remote_work",
            ),
        }
        value = _known_bool(profile, *keys[category])
        if value is None and category == "remote_work":
            arrangement = (profile or {}).get("work_arrangement")
            remote = arrangement.get("remote") if isinstance(arrangement, Mapping) else None
            if isinstance(remote, Mapping) and isinstance(remote.get("willing"), bool):
                value = remote["willing"]
        if value is None:
            value = _known_bool(resume, *keys[category])
    return _match_answer(value, options)


def resolve_canonical_text_answer(
    question: str,
    resume: Mapping[str, Any] | None,
    profile: Mapping[str, Any] | None,
) -> str | None:
    """Resolve canonical free-text facts without asking a model to invent them."""
    category = canonical_category(question)
    if category is None:
        return None
    if category == "education_level":
        values = _find_values(resume or {}, {"education_level", "degree"})
        return next((normalize_question(value) for value in values if isinstance(value, str)), None)
    if category == "start_availability":
        immediate_values = (
            _nested_values(profile or {}, "available_immediately")
            + _nested_values(profile or {}, "available_to_start_immediately")
            + _nested_values(profile or {}, "can_start_immediately")
            + _nested_values(profile or {}, "immediate_start")
        )
        if any(value is True for value in immediate_values):
            return "Available immediately"
        values = (
            _nested_values(profile or {}, "notice_period")
            + _nested_values(profile or {}, "notice_period_days")
            + _nested_values(resume or {}, "notice_period")
        )
        for value in values:
            if isinstance(value, Mapping) and "answer" in value:
                value = value["answer"]
            if str(value).strip():
                return normalize_question(value)
        return None
    return resolve_canonical_answer(question, ("Yes", "No"), resume, profile)
