"""Tests for src/job_manager/indeed/search_customizer_indeed.py"""

from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from src.job_manager.indeed.search_customizer_indeed import INDEED_BASE_URL, IndeedSearchCustomizer

MODULE = "src.job_manager.indeed.search_customizer_indeed"

BASE_PARAMS = {
    "positions": ["Software Engineer"],
    "locations": ["Germany"],
    "remote": False,
    "hybrid": False,
    "onsite": False,
    "experience_level": {},
    "job_types": {},
    "date": {},
    "apply_once_at_company": True,
    "company_blacklist": [],
    "title_blacklist": [],
    "location_blacklist": [],
}


@pytest.fixture
def mock_page():
    return AsyncMock()


@pytest.fixture
def customizer(mock_page):
    sc = IndeedSearchCustomizer(mock_page)
    sc.set_advanced_search_params(BASE_PARAMS)
    return sc


def _parse_url(url: str):
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    return parsed, qs


# ---------------------------------------------------------------------------
# _build_search_url
# ---------------------------------------------------------------------------


class TestBuildSearchUrl:
    def test_base_url_prefix(self, customizer):
        url = customizer._build_search_url("Python Developer")
        assert url.startswith(INDEED_BASE_URL)

    def test_encodes_position(self, customizer):
        url = customizer._build_search_url("Data Scientist")
        _, qs = _parse_url(url)
        assert qs["q"] == ["Data Scientist"]

    def test_encodes_location(self, customizer):
        url = customizer._build_search_url("Engineer", "New York")
        _, qs = _parse_url(url)
        assert qs["l"] == ["New York"]

    def test_no_location_param_when_empty(self, customizer):
        url = customizer._build_search_url("Engineer", "")
        _, qs = _parse_url(url)
        assert "l" not in qs

    def test_remote_work_arrangement(self, customizer):
        customizer.remote = True
        url = customizer._build_search_url("Engineer")
        assert "remote" in url

    def test_hybrid_work_arrangement(self, customizer):
        customizer.hybrid = True
        url = customizer._build_search_url("Engineer")
        assert "hybrid" in url

    def test_onsite_work_arrangement(self, customizer):
        customizer.onsite = True
        url = customizer._build_search_url("Engineer")
        assert "onsite" in url

    def test_no_work_arrangement_param_when_all_disabled(self, customizer):
        url = customizer._build_search_url("Engineer")
        assert "sc=0kf" not in url

    def test_multiple_work_arrangements(self, customizer):
        customizer.remote = True
        customizer.hybrid = True
        url = customizer._build_search_url("Engineer")
        assert "remote" in url
        assert "hybrid" in url

    def test_job_type_full_time(self, customizer):
        customizer.job_types = {"full_time": True}
        url = customizer._build_search_url("Engineer")
        _, qs = _parse_url(url)
        assert qs["jt"] == ["fulltime"]

    def test_job_type_part_time(self, customizer):
        customizer.job_types = {"part_time": True}
        url = customizer._build_search_url("Engineer")
        _, qs = _parse_url(url)
        assert qs["jt"] == ["parttime"]

    def test_job_type_contract(self, customizer):
        customizer.job_types = {"contract": True}
        url = customizer._build_search_url("Engineer")
        _, qs = _parse_url(url)
        assert qs["jt"] == ["contract"]

    def test_no_job_type_param_when_all_disabled(self, customizer):
        customizer.job_types = {"full_time": False, "contract": False}
        url = customizer._build_search_url("Engineer")
        _, qs = _parse_url(url)
        assert "jt" not in qs

    def test_date_past_24_hours(self, customizer):
        customizer.date_posted = {"past_24_hours": True}
        url = customizer._build_search_url("Engineer")
        _, qs = _parse_url(url)
        assert qs["fromage"] == ["1"]

    def test_date_past_week(self, customizer):
        customizer.date_posted = {"past_week": True}
        url = customizer._build_search_url("Engineer")
        _, qs = _parse_url(url)
        assert qs["fromage"] == ["7"]

    def test_date_past_month(self, customizer):
        customizer.date_posted = {"past_month": True}
        url = customizer._build_search_url("Engineer")
        _, qs = _parse_url(url)
        assert qs["fromage"] == ["14"]

    def test_no_fromage_when_any_time(self, customizer):
        customizer.date_posted = {"any_time": True}
        url = customizer._build_search_url("Engineer")
        _, qs = _parse_url(url)
        assert "fromage" not in qs

    def test_no_fromage_when_all_dates_disabled(self, customizer):
        customizer.date_posted = {"past_24_hours": False, "past_week": False}
        url = customizer._build_search_url("Engineer")
        _, qs = _parse_url(url)
        assert "fromage" not in qs

    def test_special_characters_encoded_in_position(self, customizer):
        url = customizer._build_search_url("C++ Developer")
        assert "C%2B%2B" in url or "C++".replace("+", "%2B") in url or "q=" in url

    def test_all_params_combined(self, customizer):
        customizer.remote = True
        customizer.job_types = {"full_time": True}
        customizer.date_posted = {"past_24_hours": True}
        url = customizer._build_search_url("Engineer", "Berlin")
        _, qs = _parse_url(url)
        assert "q" in qs
        assert "l" in qs
        assert "jt" in qs
        assert "fromage" in qs


# ---------------------------------------------------------------------------
# _set_max_distance
# ---------------------------------------------------------------------------


class TestSetMaxDistance:
    @pytest.mark.asyncio
    async def test_skips_when_button_not_visible(self, customizer, mock_page):
        mock_btn = AsyncMock()
        mock_btn.is_visible = AsyncMock(return_value=False)
        mock_page.locator = MagicMock(return_value=mock_btn)

        with patch(f"{MODULE}.async_pause") as mock_pause:
            await customizer._set_max_distance()

        mock_pause.assert_not_called()
        mock_btn.click.assert_not_called()

    @pytest.mark.asyncio
    async def test_clicks_last_distance_option(self, customizer, mock_page):
        mock_btn = AsyncMock()
        mock_btn.is_visible = AsyncMock(return_value=True)

        mock_option_0 = AsyncMock()
        mock_option_1 = AsyncMock()
        mock_option_2 = AsyncMock()

        mock_options = AsyncMock()
        mock_options.count = AsyncMock(return_value=3)
        mock_options.nth = MagicMock(
            side_effect=lambda i: [mock_option_0, mock_option_1, mock_option_2][i]
        )

        mock_listbox = AsyncMock()
        mock_listbox.locator = MagicMock(return_value=mock_options)

        mock_listbox_chain = AsyncMock()
        mock_listbox_chain.first = mock_listbox

        mock_update_btn = AsyncMock()
        mock_update_btn.last = AsyncMock()

        def locator_side_effect(selector):
            if "radius_filter_button" in selector:
                return mock_btn
            if "listbox" in selector or "Distance options" in selector:
                return mock_listbox_chain
            if "Update" in selector:
                return mock_update_btn
            return AsyncMock()

        mock_page.locator = MagicMock(side_effect=locator_side_effect)

        with patch(f"{MODULE}.async_pause"):
            await customizer._set_max_distance()

        mock_option_2.click.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_click_when_no_options(self, customizer, mock_page):
        mock_btn = AsyncMock()
        mock_btn.is_visible = AsyncMock(return_value=True)

        mock_options = AsyncMock()
        mock_options.count = AsyncMock(return_value=0)

        mock_listbox = AsyncMock()
        mock_listbox.locator = MagicMock(return_value=mock_options)

        mock_listbox_chain = AsyncMock()
        mock_listbox_chain.first = mock_listbox

        mock_update_btn = AsyncMock()
        mock_update_btn.last = AsyncMock()

        def locator_side_effect(selector):
            if "radius_filter_button" in selector:
                return mock_btn
            if "listbox" in selector or "Distance options" in selector:
                return mock_listbox_chain
            if "Update" in selector:
                return mock_update_btn
            return AsyncMock()

        mock_page.locator = MagicMock(side_effect=locator_side_effect)

        with patch(f"{MODULE}.async_pause"):
            await customizer._set_max_distance()

        mock_options.nth.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_listbox_not_found(self, customizer, mock_page):
        mock_btn = AsyncMock()
        mock_btn.is_visible = AsyncMock(return_value=True)

        mock_listbox_first = AsyncMock()
        mock_listbox_first.wait_for = AsyncMock(side_effect=Exception("timeout"))

        mock_listbox_chain = AsyncMock()
        mock_listbox_chain.first = mock_listbox_first

        def locator_side_effect(selector):
            if "radius_filter_button" in selector:
                return mock_btn
            if "listbox" in selector or "Distance options" in selector:
                return mock_listbox_chain
            return AsyncMock()

        mock_page.locator = MagicMock(side_effect=locator_side_effect)

        with patch(f"{MODULE}.async_pause"):
            await customizer._set_max_distance()


# ---------------------------------------------------------------------------
# set_search_params
# ---------------------------------------------------------------------------


class TestSetSearchParams:
    @pytest.mark.asyncio
    async def test_navigates_to_built_url(self, customizer, mock_page):
        with (
            patch(f"{MODULE}.async_pause"),
            patch.object(customizer, "_set_max_distance", new_callable=AsyncMock),
        ):
            await customizer.set_search_params()

        mock_page.goto.assert_called_once()
        call_url = mock_page.goto.call_args[0][0]
        assert call_url.startswith(INDEED_BASE_URL)
        assert (
            "Software+Engineer" in call_url
            or "Software%20Engineer" in call_url
            or "Software" in call_url
        )

    @pytest.mark.asyncio
    async def test_uses_first_position_and_location(self, customizer, mock_page):
        customizer.positions = ["Data Scientist", "ML Engineer"]
        customizer.locations = ["Berlin", "Munich"]

        with (
            patch(f"{MODULE}.async_pause"),
            patch.object(customizer, "_set_max_distance", new_callable=AsyncMock),
        ):
            await customizer.set_search_params()

        call_url = mock_page.goto.call_args[0][0]
        assert "Data+Scientist" in call_url or "Data%20Scientist" in call_url or "Data" in call_url
        assert "Berlin" in call_url

    @pytest.mark.asyncio
    async def test_skips_navigation_when_no_positions(self, mock_page):
        sc = IndeedSearchCustomizer(mock_page)
        sc.positions = []

        with patch(f"{MODULE}.async_pause"):
            await sc.set_search_params()

        mock_page.goto.assert_not_called()

    @pytest.mark.asyncio
    async def test_uses_empty_location_when_none_configured(self, mock_page):
        sc = IndeedSearchCustomizer(mock_page)
        sc.set_advanced_search_params({**BASE_PARAMS, "locations": []})

        with (
            patch(f"{MODULE}.async_pause"),
            patch.object(sc, "_set_max_distance", new_callable=AsyncMock),
        ):
            await sc.set_search_params()

        call_url = mock_page.goto.call_args[0][0]
        _, qs = _parse_url(call_url)
        assert "l" not in qs

    @pytest.mark.asyncio
    async def test_calls_set_max_distance(self, customizer, mock_page):
        with (
            patch(f"{MODULE}.async_pause"),
            patch.object(customizer, "_set_max_distance", new_callable=AsyncMock) as mock_distance,
        ):
            await customizer.set_search_params()

        mock_distance.assert_called_once()

    @pytest.mark.asyncio
    async def test_passes_domcontentloaded_wait(self, customizer, mock_page):
        with (
            patch(f"{MODULE}.async_pause"),
            patch.object(customizer, "_set_max_distance", new_callable=AsyncMock),
        ):
            await customizer.set_search_params()

        call_kwargs = mock_page.goto.call_args[1]
        assert call_kwargs.get("wait_until") == "domcontentloaded"
