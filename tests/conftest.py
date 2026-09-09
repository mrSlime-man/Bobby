from unittest.mock import AsyncMock, patch

import pytest

from telegram import Bot


@pytest.fixture(autouse=True)
def disable_telegram_network():
    """Prevent real Telegram network calls during tests by mocking Bot's network methods."""
    with patch.object(Bot, "send_message", new_callable=AsyncMock):
        with patch.object(Bot, "send_photo", new_callable=AsyncMock):
            yield


@pytest.fixture(autouse=True)
def enable_external_workflow_unit_paths(monkeypatch):
    """Keep offline unit tests independent from safe production defaults."""
    import src.llm.apply_agent as apply_agent
    import src.job_manager.job_manager as base_manager
    import src.job_manager.linkedin.job_manager_linkedin as linkedin_manager
    import src.llm.llm_manager as llm_manager

    monkeypatch.setattr(apply_agent, "EXTERNAL_ATS_ENABLED", True)
    monkeypatch.setattr(apply_agent, "EXTERNAL_ATS_AUTO_ACCOUNT_CREATION", True)
    monkeypatch.setattr(base_manager, "TEST_MODE", False)
    monkeypatch.setattr(linkedin_manager, "TEST_MODE", False)
    monkeypatch.setattr(linkedin_manager, "EASY_APPLY_ONLY_MODE", False)
    monkeypatch.setattr(linkedin_manager, "COLLECT_INFO_MODE", False)
    monkeypatch.setattr(llm_manager, "LLM_MODEL_TYPE", "gemini")
    monkeypatch.setattr(llm_manager, "EASY_APPLY_MODEL", "gemini-3.1-flash-lite")
