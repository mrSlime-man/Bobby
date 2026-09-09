import pytest

from src.llm.ats_engine import (
    ATSRunState,
    ATSStage,
    StageEvidence,
    classify_application_entry_progress,
    ConstraintType,
    ControlKind,
    SiteFamily,
    detect_site_family,
    discover_controls,
    infer_stage,
    navigation_intent,
    validation_repair_plan,
    FieldConstraint,
    rank_application_entry_candidates,
    stage_evidence_from_visible_controls,
)


def test_site_family_detection_uses_hostname_and_dom_markers():
    assert detect_site_family("https://boards.greenhouse.io/example") is SiteFamily.GREENHOUSE
    assert (
        detect_site_family("https://jobs.example.test/app", "<div>Workday application</div>")
        is SiteFamily.WORKDAY
    )
    assert detect_site_family("https://example.test/app") is SiteFamily.GENERIC


def test_stage_detection_covers_resume_review_and_confirmation():
    assert infer_stage("https://example.test", "Upload your resume") is ATSStage.RESUME
    assert (
        infer_stage("https://example.test", "Review application and submit application")
        is ATSStage.SUBMIT
    )
    assert infer_stage("https://example.test", "Thank you for applying") is ATSStage.CONFIRMATION


@pytest.mark.parametrize(
    "job_description_boilerplate",
    (
        "Equal Employment Opportunity information and accommodations",
        "Please upload your resume when you begin the application",
    ),
)
def test_stage_detection_keeps_apply_entry_ahead_of_job_description_boilerplate(
    job_description_boilerplate,
):
    body = f"{job_description_boilerplate}. Apply Now"
    assert infer_stage("https://careers.example.test/job/1", body, ATSStage.LANDING) is ATSStage.APPLY_ENTRY


def test_stage_detection_keeps_established_eeo_stage_after_apply_entry():
    body = "Equal Employment Opportunity questionnaire. Apply Now"
    assert infer_stage("https://careers.example.test/application", body, ATSStage.EEO) is ATSStage.EEO


def test_stage_detection_keeps_account_registration_ahead_of_apply_entry():
    body = "Create an account with email and password. Apply Now"
    assert (
        infer_stage("https://careers.example.test/application", body, ATSStage.LANDING)
        is ATSStage.ACCOUNT_CREATION
    )


def test_stage_evidence_prioritizes_strong_entry_cta_over_job_description_prose():
    evidence = stage_evidence_from_visible_controls(
        [{"label": "Sign up to apply", "kind": "button", "visible": True}],
        editable_field_count=0,
    )

    assert evidence.has_strong_application_entry is True
    assert (
        infer_stage(
            "https://jobs.example.test/role",
            "Technical skills and education requirements. Sign up to apply.",
            ATSStage.LANDING,
            evidence=evidence,
        )
        is ATSStage.APPLY_ENTRY
    )


def test_stage_evidence_keeps_real_registration_form_out_of_entry_state():
    evidence = StageEvidence(has_strong_application_entry=True, editable_field_count=2)

    assert (
        infer_stage(
            "https://jobs.example.test/role",
            "Sign up to apply with your email and password.",
            ATSStage.APPLY_ENTRY,
            evidence=evidence,
        )
        is ATSStage.ACCOUNT_CREATION
    )


def test_stage_evidence_keeps_static_submit_on_unfinished_form_out_of_submit_state():
    evidence = StageEvidence(editable_field_count=4)

    assert (
        infer_stage(
            "https://job-boards.example.test/application",
            "Submit application. Personal information: legal name and preferred name.",
            ATSStage.LANDING,
            evidence=evidence,
        )
        is ATSStage.PERSONAL_INFORMATION
    )


def test_stage_evidence_keeps_populated_form_out_of_apply_entry_recovery():
    """A Greenhouse-style full form can expose both Apply and Submit labels."""

    evidence = StageEvidence(has_strong_application_entry=True, editable_field_count=15)

    assert (
        infer_stage(
            "https://job-boards.example.test/application",
            "Apply. Submit application. Personal information: legal name and preferred name.",
            ATSStage.LANDING,
            evidence=evidence,
        )
        is ATSStage.PERSONAL_INFORMATION
    )


def test_entry_progress_uses_post_click_form_structure_before_static_submit_text():
    """Post-entry stage classification must retain real form semantics."""

    transition = classify_application_entry_progress(
        "https://careers.example.test/jobs/1",
        "Sign up to apply.",
        "https://careers.example.test/jobs/1",
        "Submit application. Personal information: legal name and preferred name.",
        previous_stage=ATSStage.APPLY_ENTRY,
        next_evidence=StageEvidence(editable_field_count=4),
    )

    assert transition.progressed is True
    assert transition.stage is ATSStage.PERSONAL_INFORMATION


def test_stage_evidence_does_not_promote_weak_application_navigation_to_entry():
    evidence = stage_evidence_from_visible_controls(
        [{"label": "Application status", "kind": "link", "visible": True}],
        editable_field_count=0,
    )

    assert evidence.has_strong_application_entry is False


@pytest.mark.parametrize(
    ("body", "expected"),
    (
        ("Create an account with your email and password", ATSStage.ACCOUNT_CREATION),
        ("Sign in with your email address and password", ATSStage.AUTH),
        (
            "Contact information: First name, last name, and phone number",
            ATSStage.CONTACT_INFORMATION,
        ),
        ("Personal information: legal name and preferred name", ATSStage.PERSONAL_INFORMATION),
        ("Employment history and professional experience", ATSStage.EMPLOYMENT_HISTORY),
        ("Education history: university and degree", ATSStage.EDUCATION),
        ("Technical skills and competencies", ATSStage.SKILLS),
        ("Additional screening questions about work authorization", ATSStage.SCREENING_QUESTIONS),
        ("Equal Employment Opportunity (EEO) questionnaire", ATSStage.EEO),
        ("Voluntary self-identification of disability status", ATSStage.VOLUNTARY_DISCLOSURE),
        ("Review your application", ATSStage.REVIEW),
    ),
)
def test_stage_detection_distinguishes_observable_application_sections(body, expected):
    assert infer_stage("https://example.test", body) is expected


def test_stage_aliases_and_legacy_stage_values_remain_compatible():
    assert ATSStage.PERSONAL_INFO is ATSStage.PERSONAL_INFORMATION
    assert ATSStage.EXPERIENCE is ATSStage.EMPLOYMENT_HISTORY
    assert ATSStage.QUESTIONS is ATSStage.SCREENING_QUESTIONS
    assert ATSStage.FINAL_SUBMIT is ATSStage.SUBMIT
    assert ATSStage("personal_info") is ATSStage.PERSONAL_INFORMATION
    assert ATSStage("experience") is ATSStage.EMPLOYMENT_HISTORY
    assert ATSStage("questions") is ATSStage.SCREENING_QUESTIONS
    assert ATSStage("final_submit") is ATSStage.SUBMIT


def test_semantic_control_discovery_handles_common_fixture_controls():
    html = """
    <label for="email">Email</label><input id="email" type="email" required>
    <label for="years">Years</label><input id="years" type="number" min="0" step="1">
    <textarea aria-label="Cover letter"></textarea>
    <select name="country" required><option value="us">United States</option></select>
    <input type="checkbox" aria-label="Terms" required>
    <input type="radio" name="work" aria-label="Remote">
    <input type="file" aria-label="Resume">
    <div role="combobox" aria-label="School" aria-autocomplete="list"></div>
    <button>Next</button>
    """
    controls = discover_controls(html)
    kinds = {control.kind for control in controls}
    assert {
        ControlKind.EMAIL,
        ControlKind.NUMBER,
        ControlKind.TEXTAREA,
        ControlKind.SELECT,
        ControlKind.CHECKBOX,
        ControlKind.RADIO,
        ControlKind.FILE,
        ControlKind.AUTOCOMPLETE,
        ControlKind.BUTTON,
    } <= kinds
    select = next(control for control in controls if control.kind is ControlKind.SELECT)
    assert select.option_count == 1
    assert select.constraint.type is ConstraintType.DROPDOWN


def test_navigation_and_validation_are_bounded_by_context():
    assert navigation_intent("Submit application", ATSStage.REVIEW).final_candidate is True
    assert navigation_intent("Submit application", ATSStage.PERSONAL_INFO).action == "next"
    constraint = FieldConstraint(type=ConstraintType.INTEGER, required=True)
    assert (
        validation_repair_plan("This field is required and must be a number", constraint)
        == "fill_required_or_select_option"
    )


def test_application_entry_resolver_prefers_real_apply_and_rejects_noise():
    candidates = rank_application_entry_candidates(
        [
            {"kind": "button", "label": "Search jobs"},
            {"kind": "link", "label": "Join our talent community"},
            {"kind": "link", "label": "Apply Now", "href": "/jobs/1/apply"},
            {"kind": "button", "label": "Share this job"},
        ]
    )
    assert len(candidates) == 1
    assert candidates[0].label == "apply now"
    assert candidates[0].confidence >= 90


@pytest.mark.parametrize(
    "label",
    [
        "Apply",
        "Apply for this Position",
        "Start Application",
        "Continue Application",
        "Candidate Login",
    ],
)
def test_application_entry_resolver_supports_nested_entry_labels(label):
    candidates = rank_application_entry_candidates([{"kind": "button", "label": label}])
    assert candidates and candidates[0].confidence >= 70


def test_application_entry_resolver_never_selects_late_stage_submit():
    assert not rank_application_entry_candidates(
        [{"kind": "button", "label": "Submit application"}],
        stage=ATSStage.APPLY_ENTRY,
    )


def test_application_entry_resolver_allows_apply_link_under_search_route():
    candidates = rank_application_entry_candidates(
        [
            {
                "kind": "link",
                "label": "Apply",
                "href": "/careers/search/results?job=1&action=apply",
            }
        ]
    )
    assert candidates and candidates[0].label == "apply"


def test_application_entry_fixture_reaches_account_stage_after_apply_action():
    """Privacy-safe semantic replay of the observed generic landing stall."""
    landing_controls = [
        {"kind": "button", "label": "Search jobs"},
        {"kind": "link", "label": "Share this job"},
        {
            "kind": "link",
            "label": "Apply",
            "href": "/careers/search/results?job=1&action=apply",
        },
        {"kind": "link", "label": "Join our talent community"},
    ]
    candidates = rank_application_entry_candidates(landing_controls)
    assert candidates and candidates[0].label == "apply"

    transition = classify_application_entry_progress(
        "https://employer.example/careers/job/1",
        "Search jobs Share this job Apply Join our talent community",
        "https://employer.example/careers/job/1",
        "Create an account Email Password Confirm password Register",
        previous_stage=ATSStage.LANDING,
    )
    assert transition.progressed is True
    assert transition.reason == "stage"
    assert transition.stage is ATSStage.ACCOUNT_CREATION


def test_entry_progress_ignores_dynamic_landing_text_until_stage_changes():
    transition = classify_application_entry_progress(
        "https://employer.example/careers/job/1#top",
        "Apply now Search jobs",
        "https://employer.example/careers/job/1#bottom",
        "Apply now Search jobs Loading",
        previous_stage=ATSStage.APPLY_ENTRY,
    )
    assert transition.progressed is False
    assert transition.reason == "unchanged"


def test_run_state_prevents_duplicate_actions_and_same_page_loops():
    state = ATSRunState()
    assert state.record_action("upload_resume") is True
    assert state.record_action("upload_resume") is False
    state.observe("https://example.test/app", "<form>resume</form>")
    state.observe("https://example.test/app", "<form>resume</form>")
    state.observe("https://example.test/app", "<form>resume</form>")
    assert state.loop_detected is True
    assert state.allow_repair("select_verified_option") is True
    assert state.allow_repair("select_verified_option") is False


def test_run_state_resets_loop_memory_only_after_recorded_progress():
    state = ATSRunState()
    state.observe("https://example.test/app", "Apply now")
    state.observe("https://example.test/app", "Apply now")
    state.observe("https://example.test/app", "Apply now")
    assert state.loop_detected is True
    state.record_progress(
        "https://example.test/app",
        "Create an account with your email and password",
        stage=ATSStage.ACCOUNT_CREATION,
    )
    assert state.loop_detected is False
    assert state.stage is ATSStage.ACCOUNT_CREATION


def test_run_state_resets_loop_memory_for_provider_handoff_without_clearing_safety_state():
    state = ATSRunState(stage=ATSStage.RESUME)
    state.record_action("upload_resume")
    state.record_action("semantic_progress:input:12")
    state.upload_attempted = True
    state.observe("https://careers.example.invalid/apply", "Resume upload")
    state.observe("https://careers.example.invalid/apply", "Resume upload")
    state.observe("https://careers.example.invalid/apply", "Resume upload")

    assert state.loop_detected is True
    assert "upload_resume" in state.attempted_actions
    assert state.upload_attempted is True

    state.reset_loop_memory_for_provider_handoff()

    assert state.loop_detected is False
    assert state._repeat_count == 0
    assert state._fingerprint == ""
    assert "upload_resume" in state.attempted_actions
    assert "semantic_progress:input:12" not in state.attempted_actions
    assert state.upload_attempted is True
    state.observe("https://careers.example.invalid/apply", "Resume upload")
    state.observe("https://careers.example.invalid/apply", "Resume upload")
    state.observe("https://careers.example.invalid/apply", "Resume upload")
    assert state.loop_detected is True


def test_supported_ats_family_fixtures_keep_generic_control_discovery():
    fixtures = (
        (
            "https://boards.greenhouse.io/acme/jobs/1",
            "<input type='file' aria-label='Resume'><button>Submit application</button>",
        ),
        (
            "https://jobs.lever.co/acme/1",
            "<input type='email' required><div role='combobox' aria-autocomplete='list'></div>",
        ),
        (
            "https://acme.wd5.myworkdayjobs.com/en-US/acme/job/1",
            "<select required><option>United States</option></select>",
        ),
        ("https://jobs.ashbyhq.com/acme/1", "<textarea aria-label='Question'></textarea>"),
        ("https://careers.smartrecruiters.com/acme/1", "<input type='checkbox' required>"),
        ("https://acme.icims.com/jobs/1", "<input type='text' aria-label='First name'>"),
        ("https://acme.taleo.net/careersection/1/jobdetail.ftl", "<input type='date'>"),
    )
    for url, html in fixtures:
        assert detect_site_family(url) is not SiteFamily.GENERIC
        assert discover_controls(html)


@pytest.mark.parametrize(
    ("url", "family"),
    (
        (
            "https://careers.oraclecloud.com/hcmUI/CandidateExperience/en/sites/example",
            SiteFamily.ORACLE,
        ),
        ("https://jobs.successfactors.com/job/example", SiteFamily.SUCCESSFACTORS),
        ("https://jobs.jobvite.com/example/job/1", SiteFamily.JOBVITE),
        ("https://recruiting.adp.com/srccar/public/RTI.home", SiteFamily.ADP),
        ("https://example.bamboohr.com/careers/1", SiteFamily.BAMBOOHR),
        ("https://recruiting.ultipro.com/example/jobboard", SiteFamily.UKG),
    ),
)
def test_additional_ats_families_are_detected_from_their_hosts(url, family):
    assert detect_site_family(url) is family
