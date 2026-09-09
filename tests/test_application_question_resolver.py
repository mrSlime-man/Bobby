from src.job_manager.application_question_resolver import (
    AnswerType,
    CANONICAL_CATEGORIES,
    canonical_category,
    field_spec_from_snapshot,
    match_available_option,
    normalize_answer_for_field,
    normalize_question,
    parse_radio_snapshot,
    resolve_canonical_answer,
    resolve_canonical_text_answer,
    resolve_structured_answer,
    validate_answer_for_field,
)


RESUME = {
    "education_details": [
        {"education_level": "High School Diploma", "year_of_completion": 2026}
    ],
    "legal_authorization": {"us_work_authorization": True},
}

PROFILE = {
    "screening": {
        "legally_authorized_to_work_in_us": {"answer": True},
        "sponsorship_required_now": {"answer": False},
        "willing_to_relocate": {"answer": True},
        "willing_to_travel": {"answer": True},
        "willing_to_work_weekends": {"answer": True},
        "willing_to_work_nights": {"answer": True},
        "willing_to_work_flexible_schedule": {"answer": True},
        "willing_to_undergo_background_check": {"answer": True},
        "willing_to_undergo_drug_test": {"answer": True},
    }
}


def test_normal_yes_no_dom_snapshot_separates_question_labels_and_values():
    field = parse_radio_snapshot(
        {
            "question": "Are you legally authorized to work in the US?",
            "required": True,
            "options": [
                {"label": "Yes", "value": "true"},
                {"label": "No", "value": "false"},
            ],
        }
    )
    assert field.question == "Are you legally authorized to work in the US?"
    assert field.options == ("Yes", "No")
    assert field.values == ("true", "false")
    assert field.required is True


def test_required_marker_and_spaced_options_are_removed_from_flattened_text():
    field = parse_radio_snapshot(
        {"options": [{"label": "Yes"}, {"label": "No"}]},
        "Have you completed High School Diploma? * Yes No",
    )
    assert field.question == "Have you completed High School Diploma?"
    assert field.options == ("Yes", "No")
    assert field.required is True


def test_concatenated_yes_no_is_recovered_from_flattened_text():
    field = parse_radio_snapshot(None, "Have you completed High School Diploma?*yesno")
    assert field.question == "Have you completed High School Diploma?"
    assert field.options == ("Yes", "No")
    assert field.required is True


def test_nested_label_span_dom_snapshot_uses_extracted_leaf_labels():
    field = parse_radio_snapshot(
        {
            "question": "Are you available for weekends? *",
            "options": [
                {"label": "\n  Yes \n", "value": "YES"},
                {"label": "\n No\t", "value": "NO"},
            ],
        },
        "Are you available for weekends? * Yes No",
    )
    assert field.question == "Are you available for weekends?"
    assert field.options == ("Yes", "No")


def test_high_school_completion_resolves_yes_from_resume():
    assert resolve_canonical_answer(
        "Have you completed the following level of education: High School Diploma?",
        ["Yes", "No"],
        RESUME,
        PROFILE,
    ) == "Yes"


def test_different_shift_question_resolves_yes_from_profile():
    assert resolve_canonical_answer(
        "Are you okay with working different shift times to accommodate a customer's needs?",
        ["Yes", "No"],
        RESUME,
        PROFILE,
    ) == "Yes"


def test_canonical_profile_schema_resolves_administrative_answers():
    profile = {
        "work_authorization": {
            "authorized_to_work_in_us": True,
            "requires_sponsorship_now": False,
        },
        "availability": {
            "available_to_start_immediately": True,
            "notice_period_days": 0,
            "schedule": {"weekends": True, "overnight": True, "flexible_schedule": True},
        },
        "business_travel": {"willing_to_travel": True},
        "screening": {"willing_to_background_check": True, "willing_to_drug_screen": True},
        "work_arrangement": {"remote": {"willing": True}},
        "location_preferences": {"willing_to_relocate": True},
        "candidate": {"email": "person@example.test", "phone": "555-555-5555"},
        "compensation": {"minimum_hourly_usd": 25},
    }
    assert resolve_canonical_answer(
        "Are you legally authorized to work in the United States?", ["Yes", "No"], {}, profile
    ) == "Yes"
    assert resolve_canonical_answer(
        "Will you now or in the future require sponsorship?", ["Yes", "No"], {}, profile
    ) == "No"
    assert resolve_canonical_answer(
        "Are you willing to work weekends?", ["Yes", "No"], {}, profile
    ) == "Yes"
    assert resolve_canonical_answer(
        "Are you willing to work overnight shifts?", ["Yes", "No"], {}, profile
    ) == "Yes"
    assert resolve_canonical_answer(
        "Are you willing to travel?", ["Yes", "No"], {}, profile
    ) == "Yes"
    assert resolve_canonical_answer(
        "Are you willing to work remotely?", ["Yes", "No"], {}, profile
    ) == "Yes"
    assert resolve_canonical_answer(
        "Are you willing to relocate?", ["Yes", "No"], {}, profile
    ) == "Yes"
    assert resolve_structured_answer(
        field_spec_from_snapshot("Email address", {"type": "email"}), {}, profile
    ) == "person@example.test"
    assert resolve_structured_answer(
        field_spec_from_snapshot("Phone number", {"type": "tel"}), {}, profile
    ) == "555-555-5555"
    assert resolve_structured_answer(
        field_spec_from_snapshot("Minimum annual salary", {}), {}, profile
    ) == "52000"


def test_unsupported_bachelors_degree_is_not_fabricated():
    assert resolve_canonical_answer(
        "Have you completed a Bachelor's degree?", ["Yes", "No"], RESUME, PROFILE
    ) == "No"


def test_truly_unknown_question_returns_none_for_safe_fallback():
    assert resolve_canonical_answer(
        "Do you prefer cats or dogs?", ["Cats", "Dogs"], RESUME, PROFILE
    ) is None


def test_minimum_canonical_categories_are_registered_and_recognized():
    assert CANONICAL_CATEGORIES == {
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
    assert canonical_category("Are you willing to undergo a drug test?") == "drug_test"


def test_semantic_question_normalization_removes_concatenated_options_and_counter():
    assert normalize_question(
        "Are you authorized to work in the United States?*YesNo 0 / 200"
    ) == "Are you authorized to work in the United States?"


def test_option_matching_returns_only_actual_dom_choice():
    assert match_available_option("yes", ["Yes", "No", "Other"]) == "Yes"
    assert match_available_option("Advanced", ["Beginner", "Intermediate"]) is None


def test_changed_dropdown_options_reject_old_cached_answer():
    assert match_available_option("2 weeks", ["Immediately", "1 week", "3+ weeks"]) is None


def test_semantic_availability_variants_use_source_truth_for_enum():
    profile = {"availability": {"available_immediately": True}}
    for question in (
        "When can you start?",
        "How soon can you join?",
        "Availability to start",
        "Notice period",
    ):
        assert resolve_canonical_answer(
            question, ["Immediately", "1 week", "2 weeks"], {}, profile
        ) == "Immediately"


def test_sponsorship_variant_and_work_authorization_are_deterministic():
    assert resolve_canonical_answer(
        "Will you require visa sponsorship?", ["Yes", "No"], RESUME, PROFILE
    ) == "No"
    assert resolve_canonical_answer(
        "Are you authorized to work in the United States?", ["Yes", "No"], RESUME, PROFILE
    ) == "Yes"
    assert resolve_canonical_text_answer(
        "Do you now or in the future require sponsorship?", RESUME, PROFILE
    ) == "No"


def test_relocation_and_remote_work_resolve_only_supported_profile_flags():
    profile = {
        "screening": {
            "willing_to_relocate": True,
            "open_to_remote_work": True,
        }
    }
    assert resolve_canonical_answer(
        "Are you willing to relocate?", ["Yes", "No"], {}, profile
    ) == "Yes"
    assert resolve_canonical_answer(
        "Are you willing to work remotely?", ["Yes", "No"], {}, profile
    ) == "Yes"


def test_cached_prose_is_rejected_for_numeric_field():
    spec = field_spec_from_snapshot(
        "How many years of SaaS experience do you have?",
        {"type": "number", "required": True},
    )
    assert normalize_answer_for_field("Several years supporting SaaS", spec) is None


def test_explicit_false_required_snapshot_remains_optional():
    spec = field_spec_from_snapshot(
        "Optional explanation",
        {"type": "text", "required": False, "aria-required": "false"},
    )

    assert spec.required is False


def test_empty_html_required_attribute_is_required():
    spec = field_spec_from_snapshot("Required answer", {"required": ""})

    assert spec.required is True


def test_all_required_field_families_are_classified():
    cases = [
        ("Confirm", {"type": "checkbox"}, AnswerType.BOOLEAN),
        ("Quantity", {"type": "number"}, AnswerType.INTEGER),
        ("Quantity", {"type": "number", "step": "0.1"}, AnswerType.DECIMAL),
        ("Years of experience", {"type": "number"}, AnswerType.YEARS_EXPERIENCE),
        ("Availability (days)", {"type": "number"}, AnswerType.DAYS_AVAILABILITY),
        ("Expected annual salary", {"type": "number"}, AnswerType.SALARY),
        ("Short", {"maxlength": "20"}, AnswerType.SHORT_TEXT),
        ("Essay", {"type": "textarea"}, AnswerType.LONG_TEXT),
        ("Start date", {"type": "date"}, AnswerType.DATE),
        ("Phone", {"type": "tel"}, AnswerType.PHONE),
        ("Email", {"type": "email"}, AnswerType.EMAIL),
        ("Choice", {"role": "radio"}, AnswerType.ENUM_RADIO),
        ("Choice", {"tag": "select"}, AnswerType.DROPDOWN),
    ]
    for question, attrs, expected in cases:
        assert field_spec_from_snapshot(question, attrs).answer_type == expected


def test_immediate_availability_days_resolves_zero_with_maxlength_20():
    spec = field_spec_from_snapshot(
        "How soon you can join us (days)?",
        {"type": "text", "maxlength": "20", "required": True},
    )
    profile = {"start_availability": {"available_immediately": True}}
    answer = resolve_structured_answer(spec, {}, profile)
    assert spec.answer_type == AnswerType.DAYS_AVAILABILITY
    assert answer == "0"
    assert validate_answer_for_field(answer, spec) == ()


def test_maxlength_two_experience_is_numeric_and_prose_is_rejected():
    spec = field_spec_from_snapshot(
        "What is your experience (years) with Google Workspace Support?",
        {"type": "text", "maxlength": "2"},
    )
    assert spec.answer_type == AnswerType.YEARS_EXPERIENCE
    assert normalize_answer_for_field("Approximately 3 years", spec) == "3"
    assert normalize_answer_for_field("I have broad support experience", spec) is None


def test_google_workspace_skill_does_not_invent_years_experience():
    spec = field_spec_from_snapshot(
        "What is your experience (years) with Google Workspace Support?",
        {"maxlength": "2", "required": True},
    )
    resume = {"skills": ["Google Workspace", "Microsoft 365"]}
    assert resolve_structured_answer(spec, resume, {}) is None


def test_tenovi_saas_years_question_remains_unsupported_without_duration():
    spec = field_spec_from_snapshot(
        "How many years of work experience do you have with Software as a Service (SaaS)?",
        {"type": "number", "required": True, "min": "0", "step": "1"},
    )
    resume = {"skills": ["technical support", "software troubleshooting"]}
    assert spec.answer_type == AnswerType.YEARS_EXPERIENCE
    assert resolve_structured_answer(spec, resume, PROFILE) is None


def test_annual_numeric_salary_converts_explicit_hourly_target_to_52000():
    spec = field_spec_from_snapshot(
        "Whats your expected CTC per annum?",
        {"type": "number", "inputmode": "numeric"},
    )
    profile = {"compensation": {"target_hourly_usd": 25}}
    answer = resolve_structured_answer(spec, {}, profile)
    assert spec.answer_type == AnswerType.SALARY
    assert answer == "52000"
    assert normalize_answer_for_field("$52,000", spec) == "52000"


def test_text_answer_is_bounded_by_maxlength():
    spec = field_spec_from_snapshot("Short response", {"maxlength": "5"})
    assert normalize_answer_for_field("abcdefgh", spec) == "abcde"


def test_number_input_never_accepts_prose_or_currency_range():
    spec = field_spec_from_snapshot("Enter a quantity", {"type": "number"})
    assert normalize_answer_for_field("five", spec) is None
    assert normalize_answer_for_field("$10-$20", spec) is None
    assert normalize_answer_for_field("10", spec) == "10"


def test_pattern_min_max_and_step_constraints_are_validated():
    spec = field_spec_from_snapshot(
        "Score",
        {"type": "number", "pattern": r"\d{2}", "min": "10", "max": "20", "step": "2"},
    )
    assert validate_answer_for_field("12", spec) == ()
    assert "pattern" in validate_answer_for_field("8", spec)
    assert "min=10" in validate_answer_for_field("8", spec)
    assert "max=20" in validate_answer_for_field("22", spec)
    assert "step=2" in validate_answer_for_field("13", spec)
