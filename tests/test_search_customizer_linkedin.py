"""Tests for src/job_manager/linkedin/search_customizer_linkedin.py"""

import json
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.job_manager.linkedin.search_customizer_linkedin import SearchCustomizer
from src.pydantic_models.config_models import SearchConfig

MODULE = "src.job_manager.linkedin.search_customizer_linkedin"


@pytest.fixture
def mock_page():
    page = AsyncMock()
    page.keyboard = AsyncMock()
    return page


@pytest.fixture
def customizer(mock_page):
    sc = SearchCustomizer(mock_page)
    sc.set_advanced_search_params(
        {
            "positions": ["Software Engineer", "Python Developer"],
            "locations": ["Germany"],
            "remote": True,
            "hybrid": True,
            "onsite": False,
            "experience_level": {"entry": True, "mid_senior_level": True, "director": False},
            "job_types": {"full_time": True, "contract": False, "internship": True},
            "date": {"24_hours": True, "week": False},
            "apply_once_at_company": True,
            "company_blacklist": ["Wayfair", "Crossover"],
            "title_blacklist": ["word1", "word2"],
            "location_blacklist": ["Brazil"],
        }
    )
    return sc


# ---------------------------------------------------------------------------
# set_advanced_search_params / is_job_blacklisted (base class, exercised here)
# ---------------------------------------------------------------------------


class TestSetAdvancedSearchParams:
    def test_sets_positions_and_locations(self, customizer):
        assert customizer.positions == ["Software Engineer", "Python Developer"]
        assert customizer.locations == ["Germany"]

    def test_sets_work_location_flags(self, customizer):
        assert customizer.remote is True
        assert customizer.hybrid is True
        assert customizer.onsite is False

    def test_sets_experience_level(self, customizer):
        assert customizer.experience_level["entry"] is True
        assert customizer.experience_level["director"] is False

    def test_sets_job_types(self, customizer):
        assert customizer.job_types["full_time"] is True
        assert customizer.job_types["contract"] is False

    def test_sets_date_posted(self, customizer):
        assert customizer.date_posted["24_hours"] is True
        assert customizer.date_posted["week"] is False

    def test_sets_blacklists(self, customizer):
        assert "Wayfair" in customizer.company_blacklist
        assert "word1" in customizer.title_blacklist
        assert "Brazil" in customizer.location_blacklist


class TestSearchDebugHelpers:
    def test_format_linkedin_keyword_query_quotes_and_ors_positions(self, mock_page):
        sc = SearchCustomizer(mock_page)
        sc.positions = ["CTO", "Chief Technology Officer", " Technical Manager ", ""]
        assert (
            sc.format_linkedin_keyword_query()
            == '"CTO" OR "Chief Technology Officer" OR "Technical Manager"'
        )


class TestBoundedSearchRotation:
    @staticmethod
    def _parameters(profile, families, location_variants):
        return {
            "positions": ["fallback position"],
            "locations": [location_variants[0]],
            "remote": profile == "remote",
            "hybrid": profile == "tampa",
            "onsite": profile == "tampa",
            "experience_level": {"entry": True, "associate": True},
            "job_types": {"full_time": True, "contract": profile == "tampa"},
            "date": {"month": True},
            "search_profile": profile,
            "query_families": families,
            "location_variants": location_variants,
        }

    def test_query_rotation_is_persistent_and_independent_by_profile(
        self, mock_page, monkeypatch, tmp_path
    ):
        rotation_path = tmp_path / "search_rotation.json"
        monkeypatch.setattr(SearchCustomizer, "SEARCH_ROTATION_PATH", rotation_path)

        remote_families = [
            {"name": "core", "positions": ["IT Support", "Help Desk"]},
            {"name": "service", "positions": ["Service Desk"]},
        ]
        tampa_families = [
            {"name": "desktop", "positions": ["Desktop Support"]},
            {"name": "network", "positions": ["Network Support"]},
        ]

        remote_first = SearchCustomizer(mock_page)
        remote_first.set_advanced_search_params(
            self._parameters("remote", remote_families, ["United States"])
        )
        remote_second = SearchCustomizer(mock_page)
        remote_second.set_advanced_search_params(
            self._parameters("remote", remote_families, ["United States"])
        )
        tampa_first = SearchCustomizer(mock_page)
        tampa_first.set_advanced_search_params(
            self._parameters("tampa", tampa_families, ["Tampa"])
        )

        assert (remote_first.active_query_family, remote_first.positions) == (
            "core",
            ["IT Support", "Help Desk"],
        )
        assert (remote_second.active_query_family, remote_second.positions) == (
            "service",
            ["Service Desk"],
        )
        assert (tampa_first.active_query_family, tampa_first.positions) == (
            "desktop",
            ["Desktop Support"],
        )
        assert json.loads(rotation_path.read_text(encoding="utf-8"))["cursors"] == {
            "remote:query": 0,
            "tampa:query": 1,
        }

    def test_location_rotation_uses_one_tampa_bay_slice_per_cycle(
        self, mock_page, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            SearchCustomizer,
            "SEARCH_ROTATION_PATH",
            tmp_path / "search_rotation.json",
        )
        parameters = self._parameters(
            "tampa",
            [{"name": "core", "positions": ["IT Support"]}],
            ["Tampa", "Wesley Chapel"],
        )

        first = SearchCustomizer(mock_page)
        first.set_advanced_search_params(parameters)
        second = SearchCustomizer(mock_page)
        second.set_advanced_search_params(parameters)

        assert first.locations == ["Tampa"]
        assert second.locations == ["Wesley Chapel"]

    def test_rotated_tampa_city_keeps_tampa_profile_identity(self, mock_page, monkeypatch, tmp_path):
        monkeypatch.setattr(
            SearchCustomizer,
            "SEARCH_ROTATION_PATH",
            tmp_path / "search_rotation.json",
        )
        parameters = self._parameters(
            "tampa",
            [{"name": "core", "positions": ["IT Support"]}],
            ["St. Petersburg"],
        )

        customizer = SearchCustomizer(mock_page)
        customizer.set_advanced_search_params(parameters)

        assert customizer.locations == ["St. Petersburg"]
        assert customizer._profile_name() == "tampa"

    def test_profile_url_uses_only_the_selected_slice_and_managed_filters(
        self, mock_page, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            SearchCustomizer,
            "SEARCH_ROTATION_PATH",
            tmp_path / "search_rotation.json",
        )
        customizer = SearchCustomizer(mock_page)
        customizer.set_advanced_search_params(
            self._parameters(
                "remote",
                [
                    {"name": "core", "positions": ["IT Support", "Help Desk"]},
                    {"name": "network", "positions": ["Network Support"]},
                ],
                ["United States"],
            )
        )

        with patch(f"{MODULE}.EASY_APPLY_ONLY_MODE", False):
            query = parse_qs(urlsplit(customizer.build_profile_search_url()).query)

        assert query["keywords"] == ['"IT Support" OR "Help Desk"']
        assert "fallback position" not in query["keywords"][0]
        assert query["location"] == ["United States"]
        assert query["f_WT"] == ["2"]
        assert query["f_E"] == ["2,3"]
        assert query["f_JT"] == ["F"]
        assert query["f_TPR"] == ["r2592000"]
        assert "f_AL" not in query

    def test_tampa_ui_allows_selected_city_rendering_but_rejects_stale_location(
        self, mock_page, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            SearchCustomizer,
            "SEARCH_ROTATION_PATH",
            tmp_path / "search_rotation.json",
        )
        customizer = SearchCustomizer(mock_page)
        customizer.set_advanced_search_params(
            self._parameters(
                "tampa",
                [{"name": "core", "positions": ["IT Support"]}],
                ["Wesley Chapel"],
            )
        )
        valid_snapshot = {
            "dialog": True,
            "f_WT": {"1": True, "2": False, "3": True},
            "f_E": {"2": True, "3": True},
            "f_JT": {"F": True, "C": True},
            "f_TPR": {"r2592000": True},
            "easy_apply": False,
            "locations": ["Wesley Chapel, Florida, United States"],
        }

        with patch(f"{MODULE}.EASY_APPLY_ONLY_MODE", False):
            assert customizer._ui_state_matches_profile(valid_snapshot) == (True, [])
            valid_snapshot["locations"] = ["United States"]
            verified, mismatches = customizer._ui_state_matches_profile(valid_snapshot)

        assert verified is False
        assert "ui_location" in mismatches

    def test_tampa_ui_accepts_linkedin_punctuation_variant_without_broadening_scope(
        self, mock_page, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            SearchCustomizer,
            "SEARCH_ROTATION_PATH",
            tmp_path / "search_rotation.json",
        )
        customizer = SearchCustomizer(mock_page)
        customizer.set_advanced_search_params(
            self._parameters(
                "tampa",
                [{"name": "core", "positions": ["IT Support"]}],
                ["St. Petersburg"],
            )
        )
        snapshot = {
            "dialog": True,
            "f_WT": {"1": True, "2": False, "3": True},
            "f_E": {"2": True, "3": True},
            "f_JT": {"F": True, "C": True},
            "f_TPR": {"r2592000": True},
            "easy_apply": False,
            "locations": ["St Petersburg, FL"],
        }

        with patch(f"{MODULE}.EASY_APPLY_ONLY_MODE", False):
            assert customizer._ui_state_matches_profile(snapshot) == (True, [])
            snapshot["locations"] = ["Tampa, FL"]
            verified, mismatches = customizer._ui_state_matches_profile(snapshot)

        assert verified is False
        assert "ui_location" in mismatches

    def test_custom_ui_accepts_exact_city_with_linkedin_state_suffix(
        self, mock_page, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            SearchCustomizer,
            "SEARCH_ROTATION_PATH",
            tmp_path / "search_rotation.json",
        )
        customizer = SearchCustomizer(mock_page)
        customizer.set_advanced_search_params(
            self._parameters(
                "custom",
                [{"name": "core", "positions": ["IT Support"]}],
                ["St. Petersburg"],
            )
        )
        snapshot = {
            "dialog": True,
            "f_WT": {"1": False, "2": False, "3": False},
            "f_E": {"2": True, "3": True},
            "f_JT": {"F": True, "C": False},
            "f_TPR": {"r2592000": True},
            "easy_apply": False,
            "locations": ["St Petersburg, FL"],
        }

        with patch(f"{MODULE}.EASY_APPLY_ONLY_MODE", False):
            assert customizer._ui_state_matches_profile(snapshot) == (True, [])

    @pytest.mark.asyncio
    async def test_pydantic_normalized_24_hour_date_maps_to_url_and_ui_selector(
        self, mock_page, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            SearchCustomizer,
            "SEARCH_ROTATION_PATH",
            tmp_path / "search_rotation.json",
        )
        parameters = SearchConfig(
            positions=["IT Support"],
            locations=["United States"],
            remote=True,
            date={"24_hours": True},
        ).model_dump()
        customizer = SearchCustomizer(mock_page)
        customizer.set_advanced_search_params(parameters)

        assert customizer._expected_url_parameters()["f_TPR"] == "r86400"
        with patch(f"{MODULE}.safe_click", new_callable=AsyncMock, return_value=True) as click:
            await customizer._set_date_posted_filter()

        assert any("Past 24 hours" in call.args[1] for call in click.call_args_list)


class TestIsJobBlacklisted:
    def test_blacklisted_company(self, customizer):
        assert customizer.is_job_blacklisted("Engineer", "Wayfair", "Germany") is True

    def test_blacklisted_company_case_insensitive(self, customizer):
        assert customizer.is_job_blacklisted("Engineer", "wayfair", "Germany") is True

    def test_blacklisted_title(self, customizer):
        assert customizer.is_job_blacklisted("word1 Developer", "Google", "Germany") is True

    def test_blacklisted_location(self, customizer):
        assert customizer.is_job_blacklisted("Engineer", "Google", "Brazil") is True

    def test_not_blacklisted(self, customizer):
        assert customizer.is_job_blacklisted("Data Scientist", "Amazon", "Germany") is False

    def test_partial_title_match(self, customizer):
        assert customizer.is_job_blacklisted("Senior word2 Engineer", "Amazon", "Germany") is True


# ---------------------------------------------------------------------------
# _set_basic_search_terms
# ---------------------------------------------------------------------------


class TestSetBasicSearchTerms:
    @pytest.mark.asyncio
    async def test_fills_keywords_with_boolean_or_query(self, customizer):
        with (
            patch(f"{MODULE}.safe_fill", new_callable=AsyncMock, return_value=True) as mock_fill,
            patch(f"{MODULE}.find_element_safely", new_callable=AsyncMock, return_value=None),
        ):
            await customizer._set_basic_search_terms()

        first_call_args = mock_fill.call_args_list[0]
        assert '"Software Engineer" OR "Python Developer"' in first_call_args[0]

    @pytest.mark.asyncio
    async def test_fills_location_and_presses_enter(self, customizer, mock_page):
        mock_element = AsyncMock()

        def fill_side_effect(page, selector, value, **kwargs):
            if "zip code" in selector or "City" in selector or "location" in selector:
                return True
            return True

        with (
            patch(f"{MODULE}.safe_fill", new_callable=AsyncMock, side_effect=fill_side_effect),
            patch(
                f"{MODULE}.find_element_safely",
                new_callable=AsyncMock,
                return_value=mock_element,
            ),
        ):
            await customizer._set_basic_search_terms()

        mock_page.keyboard.press.assert_called_once_with("Enter")

    @pytest.mark.asyncio
    async def test_logs_warning_when_keywords_not_filled(self, customizer):
        with (
            patch(f"{MODULE}.safe_fill", new_callable=AsyncMock, return_value=False),
            patch(f"{MODULE}.find_element_safely", new_callable=AsyncMock, return_value=None),
        ):
            await customizer._set_basic_search_terms()

    @pytest.mark.asyncio
    async def test_skips_keywords_when_no_positions(self, mock_page):
        sc = SearchCustomizer(mock_page)
        sc.positions = []
        sc.locations = ["Germany"]
        with (
            patch(f"{MODULE}.safe_fill", new_callable=AsyncMock, return_value=True) as mock_fill,
            patch(f"{MODULE}.find_element_safely", new_callable=AsyncMock, return_value=None),
        ):
            await sc._set_basic_search_terms()
        assert all("Software Engineer" not in str(call.args) for call in mock_fill.call_args_list)

    @pytest.mark.asyncio
    async def test_clears_location_when_no_locations_configured(self, mock_page):
        sc = SearchCustomizer(mock_page)
        sc.positions = []
        sc.locations = []
        sc._dismiss_location_typeahead = AsyncMock()

        with patch(f"{MODULE}.safe_fill", new_callable=AsyncMock, return_value=True) as mock_fill:
            await sc._set_basic_search_terms()

        mock_fill.assert_called_once()
        assert mock_fill.call_args.args[2] == ""
        mock_page.keyboard.press.assert_not_called()
        sc._dismiss_location_typeahead.assert_called_once()

    @pytest.mark.asyncio
    async def test_dismiss_location_typeahead_presses_escape_and_blurs(self, customizer, mock_page):
        with patch(f"{MODULE}.async_pause", new_callable=AsyncMock):
            await customizer._dismiss_location_typeahead()

        mock_page.keyboard.press.assert_called_once_with("Escape")
        mock_page.evaluate.assert_called_once_with(
            "document.activeElement && document.activeElement.blur()"
        )

    @pytest.mark.asyncio
    async def test_commit_basic_search_clicks_search_button(self, customizer):
        with (
            patch(f"{MODULE}.safe_click", new_callable=AsyncMock, return_value=True) as mock_click,
            patch(f"{MODULE}.async_pause", new_callable=AsyncMock),
        ):
            await customizer._commit_basic_search()

        mock_click.assert_called_once()

    @pytest.mark.asyncio
    async def test_commit_basic_search_falls_back_to_enter(self, customizer, mock_page):
        with (
            patch(f"{MODULE}.safe_click", new_callable=AsyncMock, return_value=False),
            patch(f"{MODULE}.async_pause", new_callable=AsyncMock),
        ):
            await customizer._commit_basic_search()

        mock_page.keyboard.press.assert_called_once_with("Enter")

    @pytest.mark.asyncio
    async def test_does_not_press_enter_when_no_location_element(self, customizer, mock_page):
        with (
            patch(f"{MODULE}.safe_fill", new_callable=AsyncMock, return_value=True),
            patch(f"{MODULE}.find_element_safely", new_callable=AsyncMock, return_value=None),
        ):
            await customizer._set_basic_search_terms()

        mock_page.keyboard.press.assert_not_called()


# ---------------------------------------------------------------------------
# _open_all_filters
# ---------------------------------------------------------------------------


class TestOpenAllFilters:
    @pytest.mark.asyncio
    async def test_returns_true_when_filter_button_found(self, customizer):
        with patch(f"{MODULE}.safe_click", new_callable=AsyncMock, return_value=True):
            result = await customizer._open_all_filters()
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_no_button_found(self, customizer):
        with patch(f"{MODULE}.safe_click", new_callable=AsyncMock, return_value=False):
            result = await customizer._open_all_filters()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self, customizer):
        with patch(f"{MODULE}.safe_click", new_callable=AsyncMock, side_effect=Exception("err")):
            result = await customizer._open_all_filters()
        assert result is False


# ---------------------------------------------------------------------------
# _set_date_posted_filter
# ---------------------------------------------------------------------------


class TestSetDatePostedFilter:
    @pytest.mark.asyncio
    async def test_clicks_correct_date_option(self, customizer):
        with patch(f"{MODULE}.safe_click", new_callable=AsyncMock, return_value=True) as mock_click:
            await customizer._set_date_posted_filter()

        clicked_selectors = [str(call) for call in mock_click.call_args_list]
        assert any("24 hours" in s or "24_hours" in s for s in clicked_selectors)

    @pytest.mark.asyncio
    async def test_skips_when_date_posted_empty(self, mock_page):
        sc = SearchCustomizer(mock_page)
        sc.date_posted = {}
        with patch(f"{MODULE}.safe_click", new_callable=AsyncMock) as mock_click:
            await sc._set_date_posted_filter()
        mock_click.assert_not_called()

    @pytest.mark.asyncio
    async def test_only_clicks_enabled_date(self, customizer):
        customizer.date_posted = {"week": False, "month": True, "24_hours": False}
        with patch(f"{MODULE}.safe_click", new_callable=AsyncMock, return_value=True) as mock_click:
            await customizer._set_date_posted_filter()

        clicked_selectors = [str(call) for call in mock_click.call_args_list]
        assert any("Past month" in s for s in clicked_selectors)

    @pytest.mark.asyncio
    async def test_handles_exception_gracefully(self, customizer):
        with patch(f"{MODULE}.safe_click", new_callable=AsyncMock, side_effect=Exception("err")):
            await customizer._set_date_posted_filter()


# ---------------------------------------------------------------------------
# _set_experience_level_filter
# ---------------------------------------------------------------------------


class TestSetExperienceLevelFilter:
    @pytest.mark.asyncio
    async def test_clicks_enabled_experience_levels(self, customizer):
        with patch(f"{MODULE}.safe_click", new_callable=AsyncMock, return_value=True) as mock_click:
            await customizer._set_experience_level_filter()

        assert mock_click.call_count >= 2

    @pytest.mark.asyncio
    async def test_skips_disabled_experience_levels(self, customizer):
        customizer.experience_level = {"director": False, "executive": False}
        with patch(f"{MODULE}.safe_click", new_callable=AsyncMock, return_value=True) as mock_click:
            await customizer._set_experience_level_filter()
        mock_click.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_experience_level_empty(self, mock_page):
        sc = SearchCustomizer(mock_page)
        sc.experience_level = {}
        with patch(f"{MODULE}.safe_click", new_callable=AsyncMock) as mock_click:
            await sc._set_experience_level_filter()
        mock_click.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_exception_gracefully(self, customizer):
        with patch(f"{MODULE}.safe_click", new_callable=AsyncMock, side_effect=Exception("err")):
            await customizer._set_experience_level_filter()


# ---------------------------------------------------------------------------
# _set_job_type_filter
# ---------------------------------------------------------------------------


class TestSetJobTypeFilter:
    @pytest.mark.asyncio
    async def test_clicks_enabled_job_types(self, customizer):
        with patch(f"{MODULE}.safe_click", new_callable=AsyncMock, return_value=True) as mock_click:
            await customizer._set_job_type_filter()

        assert mock_click.call_count >= 1

    @pytest.mark.asyncio
    async def test_uses_element_number_1_for_internship(self, customizer):
        customizer.job_types = {"internship": True}
        with patch(f"{MODULE}.safe_click", new_callable=AsyncMock, return_value=True) as mock_click:
            await customizer._set_job_type_filter()

        call_kwargs = mock_click.call_args_list[0][1]
        assert call_kwargs.get("element_number") == 1

    @pytest.mark.asyncio
    async def test_uses_element_number_0_for_non_internship(self, customizer):
        customizer.job_types = {"full_time": True}
        with patch(f"{MODULE}.safe_click", new_callable=AsyncMock, return_value=True) as mock_click:
            await customizer._set_job_type_filter()

        call_kwargs = mock_click.call_args_list[0][1]
        assert call_kwargs.get("element_number") == 0

    @pytest.mark.asyncio
    async def test_skips_when_job_types_empty(self, mock_page):
        sc = SearchCustomizer(mock_page)
        sc.job_types = {}
        with patch(f"{MODULE}.safe_click", new_callable=AsyncMock) as mock_click:
            await sc._set_job_type_filter()
        mock_click.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_exception_gracefully(self, customizer):
        with patch(f"{MODULE}.safe_click", new_callable=AsyncMock, side_effect=Exception("err")):
            await customizer._set_job_type_filter()


# ---------------------------------------------------------------------------
# _set_work_location_filter
# ---------------------------------------------------------------------------


class TestSetWorkLocationFilter:
    @pytest.mark.asyncio
    async def test_clicks_remote_and_hybrid(self, customizer):
        with patch(f"{MODULE}.safe_click", new_callable=AsyncMock, return_value=True) as mock_click:
            await customizer._set_work_location_filter()

        clicked_selectors = [str(call) for call in mock_click.call_args_list]
        assert any("Remote" in s for s in clicked_selectors)
        assert any("Hybrid" in s for s in clicked_selectors)

    @pytest.mark.asyncio
    async def test_does_not_click_onsite_when_disabled(self, customizer):
        with patch(f"{MODULE}.safe_click", new_callable=AsyncMock, return_value=True) as mock_click:
            await customizer._set_work_location_filter()

        clicked_selectors = [str(call) for call in mock_click.call_args_list]
        assert not any("On-site" in s for s in clicked_selectors)

    @pytest.mark.asyncio
    async def test_no_clicks_when_all_disabled(self, mock_page):
        sc = SearchCustomizer(mock_page)
        sc.remote = False
        sc.hybrid = False
        sc.onsite = False
        with patch(f"{MODULE}.safe_click", new_callable=AsyncMock) as mock_click:
            await sc._set_work_location_filter()
        mock_click.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_exception_gracefully(self, customizer):
        with patch(f"{MODULE}.safe_click", new_callable=AsyncMock, side_effect=Exception("err")):
            await customizer._set_work_location_filter()


# ---------------------------------------------------------------------------
# _apply_filters
# ---------------------------------------------------------------------------


class TestApplyFilters:
    @pytest.mark.asyncio
    async def test_returns_true_when_apply_button_found(self, customizer):
        with patch(f"{MODULE}.safe_click", new_callable=AsyncMock, return_value=True):
            result = await customizer._apply_filters()
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_no_apply_button(self, customizer):
        with patch(f"{MODULE}.safe_click", new_callable=AsyncMock, return_value=False):
            result = await customizer._apply_filters()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self, customizer):
        with patch(f"{MODULE}.safe_click", new_callable=AsyncMock, side_effect=Exception("err")):
            result = await customizer._apply_filters()
        assert result is False


# ---------------------------------------------------------------------------
# _set_easy_apply_filter
# ---------------------------------------------------------------------------


class TestSetEasyApplyFilter:
    @pytest.mark.asyncio
    async def test_external_mode_checks_and_preserves_disabled_state(self, customizer):
        mock_input = AsyncMock()
        mock_input.get_attribute = AsyncMock(return_value="false")
        with (
            patch(f"{MODULE}.EASY_APPLY_ONLY_MODE", False),
            patch(
                f"{MODULE}.find_element_safely",
                new_callable=AsyncMock,
                return_value=mock_input,
            ),
            patch(f"{MODULE}.safe_click", new_callable=AsyncMock) as mock_click,
        ):
            await customizer._set_easy_apply_filter()
        mock_click.assert_not_called()

    @pytest.mark.asyncio
    async def test_audit_removes_f_al_and_preserves_other_filters(self, customizer, mock_page):
        mock_page.url = "https://linkedin.com/jobs/search/?f_AL=true&f_E=2&keywords=help"
        disabled = AsyncMock()
        disabled.get_attribute = AsyncMock(return_value="false")

        async def navigate(url, **kwargs):
            mock_page.url = url

        mock_page.goto.side_effect = navigate
        with (
            patch(f"{MODULE}.EASY_APPLY_ONLY_MODE", False),
            patch(
                f"{MODULE}.find_element_safely",
                new_callable=AsyncMock,
                return_value=disabled,
            ),
            patch(f"{MODULE}.async_pause", new_callable=AsyncMock),
        ):
            await customizer._audit_easy_apply_filter_state()

        final_url = mock_page.goto.call_args.args[0]
        assert "f_AL" not in final_url
        assert "f_E=2" in final_url
        assert "keywords=help" in final_url

    @pytest.mark.asyncio
    async def test_easy_apply_selectors_are_semantically_scoped(self, customizer):
        with (
            patch(f"{MODULE}.EASY_APPLY_ONLY_MODE", False),
            patch(
                f"{MODULE}.find_element_safely",
                new_callable=AsyncMock,
                return_value=None,
            ) as mock_find,
        ):
            await customizer._set_easy_apply_filter()

        selectors = [call.args[1] for call in mock_find.call_args_list]
        assert all("Easy Apply" in selector or "easy-apply" in selector for selector in selectors)
        assert "input[role='switch'][data-artdeco-toggle-button='true']" not in selectors

    @pytest.mark.asyncio
    async def test_does_not_toggle_when_already_enabled(self, customizer):
        mock_input = AsyncMock()
        mock_input.get_attribute = AsyncMock(return_value="true")

        with (
            patch(f"{MODULE}.EASY_APPLY_ONLY_MODE", True),
            patch(f"{MODULE}.find_element_safely", new_callable=AsyncMock, return_value=mock_input),
            patch(f"{MODULE}.safe_click", new_callable=AsyncMock) as mock_click,
        ):
            await customizer._set_easy_apply_filter()

        mock_click.assert_not_called()

    @pytest.mark.asyncio
    async def test_toggles_when_not_enabled(self, customizer):
        mock_input = AsyncMock()
        mock_input.get_attribute = AsyncMock(return_value="false")

        with (
            patch(f"{MODULE}.EASY_APPLY_ONLY_MODE", True),
            patch(f"{MODULE}.find_element_safely", new_callable=AsyncMock, return_value=mock_input),
            patch(f"{MODULE}.safe_click", new_callable=AsyncMock, return_value=True) as mock_click,
        ):
            await customizer._set_easy_apply_filter()

        mock_click.assert_called()

    @pytest.mark.asyncio
    async def test_toggles_when_input_element_not_found(self, customizer):
        with (
            patch(f"{MODULE}.EASY_APPLY_ONLY_MODE", True),
            patch(f"{MODULE}.find_element_safely", new_callable=AsyncMock, return_value=None),
            patch(f"{MODULE}.safe_click", new_callable=AsyncMock, return_value=True) as mock_click,
        ):
            await customizer._set_easy_apply_filter()

        mock_click.assert_called()

    @pytest.mark.asyncio
    async def test_handles_exception_gracefully(self, customizer):
        with (
            patch(f"{MODULE}.EASY_APPLY_ONLY_MODE", True),
            patch(
                f"{MODULE}.find_element_safely",
                new_callable=AsyncMock,
                side_effect=Exception("err"),
            ),
        ):
            await customizer._set_easy_apply_filter()


# ---------------------------------------------------------------------------
# set_search_params (orchestration)
# ---------------------------------------------------------------------------


class TestSetSearchParams:
    @pytest.mark.asyncio
    async def test_navigates_to_linkedin_jobs(self, customizer, mock_page):
        with (
            patch(f"{MODULE}.LINKEDIN_RECOMMENDED_JOBS_MODE", False),
            patch(f"{MODULE}.LINKEDIN_TOP_APPLICANT_JOBS_MODE", False),
            patch(f"{MODULE}.async_pause"),
            patch.object(customizer, "_set_basic_search_terms", new_callable=AsyncMock),
            patch.object(customizer, "_commit_basic_search", new_callable=AsyncMock),
            patch.object(
                customizer, "_open_all_filters", new_callable=AsyncMock, return_value=True
            ),
            patch.object(customizer, "_set_date_posted_filter", new_callable=AsyncMock),
            patch.object(customizer, "_set_experience_level_filter", new_callable=AsyncMock),
            patch.object(customizer, "_set_job_type_filter", new_callable=AsyncMock),
            patch.object(customizer, "_set_work_location_filter", new_callable=AsyncMock),
            patch.object(customizer, "_set_easy_apply_filter", new_callable=AsyncMock),
            patch.object(customizer, "_apply_filters", new_callable=AsyncMock, return_value=True),
        ):
            await customizer.set_search_params()

        mock_page.goto.assert_called_once_with(
            "https://www.linkedin.com/jobs/search/", wait_until="domcontentloaded"
        )

    @pytest.mark.asyncio
    async def test_recommended_jobs_mode_ignores_position_search(self, customizer, mock_page):
        with (
            patch(f"{MODULE}.LINKEDIN_RECOMMENDED_JOBS_MODE", True),
            patch(f"{MODULE}.LINKEDIN_TOP_APPLICANT_JOBS_MODE", False),
            patch(f"{MODULE}.async_pause", new_callable=AsyncMock),
            patch.object(
                customizer, "_set_basic_search_terms", new_callable=AsyncMock
            ) as mock_basic,
            patch.object(customizer, "_commit_basic_search", new_callable=AsyncMock) as mock_commit,
            patch.object(customizer, "_open_all_filters", new_callable=AsyncMock) as mock_filters,
        ):
            await customizer.set_search_params()

        mock_page.goto.assert_called_once_with(
            "https://www.linkedin.com/jobs/collections/recommended/",
            wait_until="domcontentloaded",
        )
        mock_basic.assert_not_called()
        mock_commit.assert_not_called()
        mock_filters.assert_not_called()

    @pytest.mark.asyncio
    async def test_top_applicant_jobs_mode_ignores_position_search(self, customizer, mock_page):
        with (
            patch(f"{MODULE}.LINKEDIN_RECOMMENDED_JOBS_MODE", False),
            patch(f"{MODULE}.LINKEDIN_TOP_APPLICANT_JOBS_MODE", True),
            patch(f"{MODULE}.async_pause", new_callable=AsyncMock),
            patch.object(
                customizer, "_set_basic_search_terms", new_callable=AsyncMock
            ) as mock_basic,
            patch.object(customizer, "_commit_basic_search", new_callable=AsyncMock) as mock_commit,
            patch.object(customizer, "_open_all_filters", new_callable=AsyncMock) as mock_filters,
        ):
            await customizer.set_search_params()

        mock_page.goto.assert_called_once_with(
            "https://www.linkedin.com/jobs/collections/top-applicant/",
            wait_until="domcontentloaded",
        )
        mock_basic.assert_not_called()
        mock_commit.assert_not_called()
        mock_filters.assert_not_called()

    @pytest.mark.asyncio
    async def test_calls_all_filter_setters_when_filters_open(self, customizer):
        with (
            patch(f"{MODULE}.LINKEDIN_RECOMMENDED_JOBS_MODE", False),
            patch(f"{MODULE}.LINKEDIN_TOP_APPLICANT_JOBS_MODE", False),
            patch(f"{MODULE}.async_pause"),
            patch.object(customizer, "_set_basic_search_terms", new_callable=AsyncMock),
            patch.object(customizer, "_commit_basic_search", new_callable=AsyncMock),
            patch.object(
                customizer, "_open_all_filters", new_callable=AsyncMock, return_value=True
            ),
            patch.object(
                customizer, "_set_date_posted_filter", new_callable=AsyncMock
            ) as mock_date,
            patch.object(
                customizer, "_set_experience_level_filter", new_callable=AsyncMock
            ) as mock_exp,
            patch.object(
                customizer, "_set_job_type_filter", new_callable=AsyncMock
            ) as mock_job_type,
            patch.object(
                customizer, "_set_work_location_filter", new_callable=AsyncMock
            ) as mock_loc,
            patch.object(customizer, "_set_easy_apply_filter", new_callable=AsyncMock) as mock_easy,
            patch.object(customizer, "_apply_filters", new_callable=AsyncMock, return_value=True),
        ):
            await customizer.set_search_params()

        mock_date.assert_called_once()
        mock_exp.assert_called_once()
        mock_job_type.assert_called_once()
        mock_loc.assert_called_once()
        mock_easy.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_filter_setters_when_filters_not_open(self, customizer):
        with (
            patch(f"{MODULE}.LINKEDIN_RECOMMENDED_JOBS_MODE", False),
            patch(f"{MODULE}.LINKEDIN_TOP_APPLICANT_JOBS_MODE", False),
            patch(f"{MODULE}.async_pause"),
            patch.object(customizer, "_set_basic_search_terms", new_callable=AsyncMock),
            patch.object(customizer, "_commit_basic_search", new_callable=AsyncMock),
            patch.object(
                customizer, "_open_all_filters", new_callable=AsyncMock, return_value=False
            ),
            patch.object(
                customizer, "_set_date_posted_filter", new_callable=AsyncMock
            ) as mock_date,
        ):
            await customizer.set_search_params()

        mock_date.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_on_page_navigation_error(self, customizer, mock_page):
        mock_page.goto.side_effect = Exception("network error")
        with (
            patch(f"{MODULE}.LINKEDIN_RECOMMENDED_JOBS_MODE", False),
            patch(f"{MODULE}.LINKEDIN_TOP_APPLICANT_JOBS_MODE", False),
            patch(f"{MODULE}.async_pause"),
        ):
            with pytest.raises(Exception, match="network error"):
                await customizer.set_search_params()
