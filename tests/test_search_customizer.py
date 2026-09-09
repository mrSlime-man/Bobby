"""Tests for src/job_manager/search_customizer.py"""

from unittest.mock import MagicMock

import pytest

from src.job_manager.search_customizer import BaseSearchCustomizer


class ConcreteSearchCustomizer(BaseSearchCustomizer):
    """Minimal concrete implementation for testing BaseSearchCustomizer methods."""

    async def set_search_params(self):
        pass


@pytest.fixture
def customizer():
    return ConcreteSearchCustomizer(page=MagicMock())


class TestSearchCustomizerInit:
    def test_default_attributes(self, customizer):
        assert customizer.positions == []
        assert customizer.locations == []
        assert customizer.remote is False
        assert customizer.onsite is False
        assert customizer.hybrid is False
        assert customizer.experience_level == {}
        assert customizer.job_types == {}
        assert customizer.date_posted == {}
        assert customizer.apply_once_at_company is True
        assert customizer.company_blacklist == []
        assert customizer.title_blacklist == []
        assert customizer.location_blacklist == []


class TestSetAdvancedSearchParams:
    def test_sets_all_params(self, customizer):
        params = {
            "positions": ["Software Engineer", "Backend Developer"],
            "locations": ["New York", "Remote"],
            "remote": True,
            "onsite": False,
            "hybrid": True,
            "experience_level": {"mid": True, "senior": True},
            "job_types": {"full_time": True},
            "date": {"month": True},
            "apply_once_at_company": False,
            "company_blacklist": ["BadCorp"],
            "title_blacklist": ["intern"],
            "location_blacklist": ["Antarctica"],
        }

        customizer.set_advanced_search_params(params)

        assert customizer.positions == ["Software Engineer", "Backend Developer"]
        assert customizer.locations == ["New York", "Remote"]
        assert customizer.remote is True
        assert customizer.hybrid is True
        assert customizer.onsite is False
        assert customizer.apply_once_at_company is False
        assert customizer.company_blacklist == ["BadCorp"]
        assert customizer.title_blacklist == ["intern"]
        assert customizer.location_blacklist == ["Antarctica"]

    def test_defaults_for_optional_params(self, customizer):
        customizer.set_advanced_search_params({"positions": ["Engineer"]})

        assert customizer.remote is False
        assert customizer.onsite is False
        assert customizer.hybrid is False
        assert customizer.apply_once_at_company is True
        assert customizer.company_blacklist == []

    def test_none_list_params_default_to_empty_lists(self, customizer):
        customizer.set_advanced_search_params(
            {
                "positions": ["Engineer"],
                "locations": None,
                "company_blacklist": None,
                "title_blacklist": None,
                "location_blacklist": None,
            }
        )

        assert customizer.locations == []
        assert customizer.company_blacklist == []
        assert customizer.title_blacklist == []
        assert customizer.location_blacklist == []


class TestIsJobBlacklisted:
    def test_title_blacklisted(self, customizer):
        customizer.title_blacklist = ["intern", "junior"]
        assert customizer.is_job_blacklisted("Junior Developer", "GoodCorp", "New York") is True

    def test_title_blacklist_case_insensitive(self, customizer):
        customizer.title_blacklist = ["INTERN"]
        assert customizer.is_job_blacklisted("Software Intern", "GoodCorp", "New York") is True

    def test_company_blacklisted(self, customizer):
        customizer.company_blacklist = ["BadCorp"]
        assert customizer.is_job_blacklisted("Engineer", "BadCorp Inc", "New York") is True

    def test_company_blacklist_case_insensitive(self, customizer):
        customizer.company_blacklist = ["badcorp"]
        assert customizer.is_job_blacklisted("Engineer", "BADCORP", "New York") is True

    def test_location_blacklisted(self, customizer):
        customizer.location_blacklist = ["Antarctica"]
        assert customizer.is_job_blacklisted("Engineer", "GoodCorp", "Antarctica Base") is True

    def test_location_blacklist_case_insensitive(self, customizer):
        customizer.location_blacklist = ["ANTARCTICA"]
        assert customizer.is_job_blacklisted("Engineer", "GoodCorp", "antarctica") is True

    def test_not_blacklisted(self, customizer):
        customizer.title_blacklist = ["intern"]
        customizer.company_blacklist = ["BadCorp"]
        customizer.location_blacklist = ["Antarctica"]
        assert customizer.is_job_blacklisted("Senior Engineer", "GoodCorp", "New York") is False

    def test_empty_blacklists(self, customizer):
        assert customizer.is_job_blacklisted("Any Title", "Any Corp", "Any Location") is False

    def test_partial_title_match(self, customizer):
        customizer.title_blacklist = ["sales"]
        assert customizer.is_job_blacklisted("Sales Manager", "Corp", "NY") is True
        assert customizer.is_job_blacklisted("Software Engineer", "Corp", "NY") is False
