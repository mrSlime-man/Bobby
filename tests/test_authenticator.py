"""Test suite for src/job_manager/authenticator.py"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.job_manager.indeed.authenticator_indeed import IndeedAuthenticator
from src.job_manager.linkedin.authenticator_linkedin import LinkedInAuthenticator


class TestLinkedInAuthenticatorInit:
    """Tests for LinkedInAuthenticator initialization"""

    def test_init_without_page(self):
        """Test initialization without page"""
        auth = LinkedInAuthenticator()

        assert auth.page is None
        assert auth.email is None
        assert auth.password is None
        assert auth.session_file == Path("data/linkedin_session.json")

    def test_init_with_page(self):
        """Test initialization with page"""
        mock_page = MagicMock()
        auth = LinkedInAuthenticator(page=mock_page)

        assert auth.page == mock_page
        assert auth.email is None
        assert auth.password is None

    def test_set_parameters(self):
        """Test setting email and password parameters"""
        auth = LinkedInAuthenticator()
        test_email = "test@example.com"
        test_password = "secure_password"

        auth.set_parameters(test_email, test_password)

        assert auth.email == test_email
        assert auth.password == test_password


class TestLinkedInAuthenticatorLogin:
    """Tests for LinkedInAuthenticator login methods"""

    @pytest.mark.asyncio
    async def test_is_logged_in_success(self):
        """Test is_logged_in when user is already logged in"""
        # Create mock page
        mock_page = AsyncMock()
        mock_page.url = "https://www.linkedin.com/feed/"

        # Mock find_element_safely to return a feed element
        with patch(
            "src.job_manager.linkedin.authenticator_linkedin.find_element_safely",
            new_callable=AsyncMock,
        ) as mock_find:
            mock_find.return_value = MagicMock()  # Feed element found

            auth = LinkedInAuthenticator(page=mock_page)
            result = await auth.is_logged_in()

            assert result is True
            mock_page.goto.assert_called_once_with(
                "https://www.linkedin.com/feed/", wait_until="domcontentloaded"
            )

    @pytest.mark.asyncio
    async def test_is_logged_in_redirected_to_login(self):
        """Test is_logged_in when redirected to login page"""
        mock_page = AsyncMock()
        mock_page.url = "https://www.linkedin.com/login"

        auth = LinkedInAuthenticator(page=mock_page)
        result = await auth.is_logged_in()

        assert result is False

    @pytest.mark.asyncio
    async def test_is_logged_in_no_feed_content(self):
        """Test is_logged_in when authenticated shell is detected"""
        mock_page = AsyncMock()
        mock_page.url = "https://www.linkedin.com/feed/"

        with patch(
            "src.job_manager.linkedin.authenticator_linkedin.find_element_safely",
            new_callable=AsyncMock,
        ) as mock_find:
            mock_find.side_effect = [MagicMock()]  # Authenticated nav found immediately

            auth = LinkedInAuthenticator(page=mock_page)
            result = await auth.is_logged_in()

            assert result is True

    @pytest.mark.asyncio
    async def test_is_logged_in_unknown_page_without_markers(self):
        """Test is_logged_in when no authenticated markers are found"""
        mock_page = AsyncMock()
        mock_page.url = "https://www.linkedin.com/authwall"

        with patch(
            "src.job_manager.linkedin.authenticator_linkedin.find_element_safely",
            new_callable=AsyncMock,
        ) as mock_find:
            mock_find.return_value = None

            auth = LinkedInAuthenticator(page=mock_page)
            result = await auth.is_logged_in()

            assert result is False

    @pytest.mark.asyncio
    async def test_is_logged_in_exception(self):
        """Test is_logged_in when an exception occurs"""
        mock_page = AsyncMock()
        mock_page.goto.side_effect = Exception("Network error")

        auth = LinkedInAuthenticator(page=mock_page)
        result = await auth.is_logged_in()

        assert result is False

    @pytest.mark.asyncio
    async def test_handle_login_success(self):
        """Test handle_login successful flow"""
        mock_page = AsyncMock()

        auth = LinkedInAuthenticator(page=mock_page)
        auth.email = "test@example.com"
        auth.password = "password123"

        with patch.object(auth, "enter_credentials", new_callable=AsyncMock) as mock_enter:
            mock_enter.return_value = True

            result = await auth.handle_login()

            assert result is True
            mock_page.goto.assert_called_once_with("https://www.linkedin.com/login")
            mock_enter.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_login_failure(self):
        """Test handle_login when navigation fails"""
        mock_page = AsyncMock()
        mock_page.goto.side_effect = Exception("Navigation failed")

        auth = LinkedInAuthenticator(page=mock_page)
        result = await auth.handle_login()

        assert result is False

    @pytest.mark.asyncio
    async def test_enter_credentials_success(self):
        """Test enter_credentials successful flow"""
        mock_page = AsyncMock()
        mock_locator = AsyncMock()
        mock_locator.count.return_value = 0  # No saved profile
        # Make locator() a non-async method that returns the mock_locator
        mock_page.locator = MagicMock(return_value=mock_locator)
        # get_by_role is synchronous in Playwright; override AsyncMock default
        mock_page.get_by_role = MagicMock()
        mock_page.get_by_role.return_value.click = AsyncMock()
        login_field = AsyncMock()
        login_field.count.return_value = 1
        mock_page.locator = MagicMock(return_value=login_field)

        auth = LinkedInAuthenticator(page=mock_page)
        auth.email = "test@example.com"
        auth.password = "password123"

        with (
            patch.object(
                auth, "_is_authenticated_page", new_callable=AsyncMock, return_value=False
            ),
            patch(
                "src.job_manager.linkedin.authenticator_linkedin.safe_fill", new_callable=AsyncMock
            ) as mock_fill,
            patch(
                "src.job_manager.linkedin.authenticator_linkedin.async_pause",
                new_callable=AsyncMock,
            ),
            patch.object(
                auth, "try_continue_with_saved_account", new_callable=AsyncMock, return_value=False
            ),
            patch.object(auth, "check_login_success", new_callable=AsyncMock) as mock_check,
        ):
            mock_fill.return_value = True
            mock_check.return_value = True

            result = await auth.enter_credentials()

            assert result is True
            # Should fill both email and password
            assert mock_fill.call_count == 2
            mock_check.assert_called_once()

    @pytest.mark.asyncio
    async def test_enter_credentials_with_saved_profile(self):
        """Test enter_credentials when profile is already saved"""
        mock_page = AsyncMock()
        mock_locator = AsyncMock()
        mock_locator.count.return_value = 1  # Saved profile found
        # Make locator() a non-async method that returns the mock_locator
        mock_page.locator = MagicMock(return_value=mock_locator)

        auth = LinkedInAuthenticator(page=mock_page)
        auth.email = "test@example.com"
        auth.password = "password123"

        with (
            patch.object(
                auth, "_is_authenticated_page", new_callable=AsyncMock, return_value=False
            ),
            patch(
                "src.job_manager.linkedin.authenticator_linkedin.safe_fill", new_callable=AsyncMock
            ) as mock_fill,
            patch.object(
                auth, "try_continue_with_saved_account", new_callable=AsyncMock, return_value=True
            ),
            patch.object(auth, "check_login_success", new_callable=AsyncMock, return_value=True),
        ):
            result = await auth.enter_credentials()

            assert result is True
            # No form fill when saved account is used
            assert mock_fill.call_count == 0

    @pytest.mark.asyncio
    async def test_enter_credentials_email_fill_failure(self):
        """Test enter_credentials when email fill fails"""
        mock_page = AsyncMock()
        mock_locator = AsyncMock()
        mock_locator.count.return_value = 0  # No saved profile
        # Make locator() a non-async method that returns the mock_locator
        mock_page.locator = MagicMock(return_value=mock_locator)

        auth = LinkedInAuthenticator(page=mock_page)
        auth.email = "test@example.com"
        auth.password = "password123"

        with (
            patch(
                "src.job_manager.linkedin.authenticator_linkedin.safe_fill", new_callable=AsyncMock
            ) as mock_fill,
            patch.object(
                auth, "try_continue_with_saved_account", new_callable=AsyncMock, return_value=False
            ),
        ):
            mock_fill.return_value = False  # Email fill fails

            result = await auth.enter_credentials()

            assert result is False

    @pytest.mark.asyncio
    async def test_enter_credentials_password_fill_failure(self):
        """Test enter_credentials when password fill fails"""
        mock_page = AsyncMock()
        mock_locator = AsyncMock()
        mock_locator.count.return_value = 0  # No saved profile
        # Make locator() a non-async method that returns the mock_locator
        mock_page.locator = MagicMock(return_value=mock_locator)

        auth = LinkedInAuthenticator(page=mock_page)
        auth.email = "test@example.com"
        auth.password = "password123"

        with (
            patch(
                "src.job_manager.linkedin.authenticator_linkedin.safe_fill", new_callable=AsyncMock
            ) as mock_fill,
            patch.object(
                auth, "try_continue_with_saved_account", new_callable=AsyncMock, return_value=False
            ),
        ):
            # Email succeeds, password fails
            mock_fill.side_effect = [True, False]

            result = await auth.enter_credentials()

            assert result is False

    @pytest.mark.asyncio
    async def test_enter_credentials_no_login_button(self):
        """Test enter_credentials when login button cannot be found"""
        mock_page = AsyncMock()
        mock_locator = AsyncMock()
        mock_locator.count.return_value = 0
        # Make locator() a non-async method that returns the mock_locator
        mock_page.locator = MagicMock(return_value=mock_locator)
        # get_by_role is synchronous in Playwright; make click raise to simulate missing button
        mock_page.get_by_role = MagicMock()
        mock_page.get_by_role.return_value.click = AsyncMock(
            side_effect=Exception("button not found")
        )

        auth = LinkedInAuthenticator(page=mock_page)
        auth.email = "test@example.com"
        auth.password = "password123"

        with (
            patch(
                "src.job_manager.linkedin.authenticator_linkedin.safe_fill", new_callable=AsyncMock
            ) as mock_fill,
            patch.object(
                auth, "try_continue_with_saved_account", new_callable=AsyncMock, return_value=False
            ),
        ):
            mock_fill.return_value = True

            result = await auth.enter_credentials()

            assert result is False

    @pytest.mark.asyncio
    async def test_enter_credentials_returns_success_when_already_authenticated(self):
        """Test enter_credentials short-circuits when session is already authenticated"""
        mock_page = AsyncMock()

        auth = LinkedInAuthenticator(page=mock_page)

        with (
            patch.object(auth, "_is_authenticated_page", new_callable=AsyncMock) as mock_auth_page,
            patch(
                "src.job_manager.linkedin.authenticator_linkedin.safe_fill",
                new_callable=AsyncMock,
            ) as mock_fill,
        ):
            mock_auth_page.return_value = True

            result = await auth.enter_credentials()

            assert result is True
            mock_fill.assert_not_called()

    @pytest.mark.asyncio
    async def test_enter_credentials_exception(self):
        """Test enter_credentials when an exception occurs"""
        mock_page = AsyncMock()
        mock_page.locator.side_effect = Exception("Locator error")

        auth = LinkedInAuthenticator(page=mock_page)
        result = await auth.enter_credentials()

        assert result is False


class TestCheckLoginSuccess:
    """Tests for check_login_success method"""

    @pytest.mark.asyncio
    async def test_check_login_success_feed_url(self):
        """Test check_login_success when redirected to feed"""
        mock_page = AsyncMock()
        mock_page.url = "https://www.linkedin.com/feed/"

        auth = LinkedInAuthenticator(page=mock_page)
        result = await auth.check_login_success()

        assert result is True

    @pytest.mark.asyncio
    async def test_check_login_success_home_url(self):
        """Test check_login_success when at homepage"""
        mock_page = AsyncMock()
        mock_page.url = "https://www.linkedin.com/"

        auth = LinkedInAuthenticator(page=mock_page)
        result = await auth.check_login_success()

        assert result is True

    @pytest.mark.asyncio
    async def test_check_login_success_profile_url(self):
        """Test check_login_success when at profile page"""
        mock_page = AsyncMock()
        mock_page.url = "https://www.linkedin.com/in/testuser/"

        auth = LinkedInAuthenticator(page=mock_page)
        result = await auth.check_login_success()

        assert result is True

    @pytest.mark.asyncio
    async def test_check_login_success_messaging_url(self):
        """Test check_login_success when at messaging page"""
        mock_page = AsyncMock()
        mock_page.url = "https://www.linkedin.com/messaging/"

        auth = LinkedInAuthenticator(page=mock_page)
        result = await auth.check_login_success()

        assert result is True

    @pytest.mark.asyncio
    async def test_check_login_success_with_login_error(self):
        """Test check_login_success when login error is shown"""
        mock_page = AsyncMock()
        mock_page.url = "https://www.linkedin.com/login"

        mock_locator = AsyncMock()
        mock_locator.count.return_value = 1
        mock_locator.first.text_content.return_value = "Invalid credentials"
        # Make locator() a non-async method that returns the mock_locator
        mock_page.locator = MagicMock(return_value=mock_locator)

        auth = LinkedInAuthenticator(page=mock_page)
        result = await auth.check_login_success()

        assert result is False

    @pytest.mark.asyncio
    async def test_check_login_success_with_captcha(self):
        """Test check_login_success when CAPTCHA is detected"""
        mock_page = AsyncMock()

        # Simulate staying on login page with CAPTCHA
        urls = ["https://www.linkedin.com/login"] * 5  # Stay on login for several checks
        mock_page.url = urls[0]

        # Create different mock locators for error and captcha selectors
        def locator_side_effect(selector):
            mock_loc = AsyncMock()
            if "captcha" in selector or "challenge" in selector:
                mock_loc.count.return_value = 1  # CAPTCHA found
            else:
                mock_loc.count.return_value = 0  # No error
            return mock_loc

        mock_page.locator.side_effect = locator_side_effect

        auth = LinkedInAuthenticator(page=mock_page)

        # This should timeout but not immediately fail
        with patch("src.job_manager.linkedin.authenticator_linkedin.async_pause"):
            result = await auth.check_login_success()

        assert result is False  # Eventually timeout

    @pytest.mark.asyncio
    async def test_check_login_success_with_checkpoint(self):
        """Test check_login_success when security checkpoint is encountered"""
        mock_page = AsyncMock()

        # Simulate checkpoint URL then success
        urls = ["https://www.linkedin.com/checkpoint/challenge", "https://www.linkedin.com/feed/"]
        url_index = [0]

        def get_url():
            idx = url_index[0]
            if idx < len(urls):
                url = urls[idx]
                url_index[0] += 1
                return url
            return urls[-1]

        type(mock_page).url = property(lambda self: get_url())

        auth = LinkedInAuthenticator(page=mock_page)

        with patch("src.job_manager.linkedin.authenticator_linkedin.async_pause"):
            result = await auth.check_login_success()

        assert result is True

    @pytest.mark.asyncio
    async def test_check_login_success_timeout(self):
        """Test check_login_success when it times out on login page"""
        mock_page = AsyncMock()
        mock_page.url = "https://www.linkedin.com/login"  # Always stay on login

        mock_locator = AsyncMock()
        mock_locator.count.return_value = 0  # No errors or challenges
        # Make locator() a non-async method that returns the mock_locator
        mock_page.locator = MagicMock(return_value=mock_locator)

        auth = LinkedInAuthenticator(page=mock_page)

        with patch("src.job_manager.linkedin.authenticator_linkedin.async_pause"):
            result = await auth.check_login_success()

        assert result is False

    @pytest.mark.asyncio
    async def test_check_login_success_exception(self):
        """Test check_login_success when an exception occurs"""
        mock_page = AsyncMock()
        mock_page.url  # This will raise AttributeError
        type(mock_page).url = property(lambda self: (_ for _ in ()).throw(Exception("URL error")))

        auth = LinkedInAuthenticator(page=mock_page)
        result = await auth.check_login_success()

        assert result is False


class TestTryContinueWithSavedAccount:
    """Tests for try_continue_with_saved_account method"""

    def _make_locator(self, count=0, url_after_click="https://www.linkedin.com/feed/"):
        mock_locator = AsyncMock()
        mock_locator.count.return_value = count
        mock_locator.first = mock_locator
        return mock_locator

    @pytest.mark.asyncio
    async def test_no_saved_accounts_found(self):
        """Returns False when no saved account buttons are present"""
        mock_page = AsyncMock()
        mock_locator = self._make_locator(count=0)
        mock_page.locator = MagicMock(return_value=mock_locator)
        mock_page.locator.return_value.first = mock_locator

        auth = LinkedInAuthenticator(page=mock_page)
        auth.email = "user@example.com"

        result = await auth.try_continue_with_saved_account()

        assert result is False

    @pytest.mark.asyncio
    async def test_saved_account_click_leads_to_feed(self):
        """Returns True when clicking saved account navigates away from login"""
        mock_page = AsyncMock()
        mock_locator = AsyncMock()
        mock_locator.count.return_value = 1
        mock_locator.first = mock_locator
        mock_page.locator = MagicMock(return_value=mock_locator)

        mock_page.url = "https://www.linkedin.com/feed/"

        auth = LinkedInAuthenticator(page=mock_page)
        auth.email = "user@example.com"

        with patch(
            "src.job_manager.linkedin.authenticator_linkedin.async_pause", new_callable=AsyncMock
        ):
            result = await auth.try_continue_with_saved_account()

        assert result is True

    @pytest.mark.asyncio
    async def test_saved_account_click_leads_to_password_prompt(self):
        """Returns False when clicking saved account leads to a password prompt"""
        mock_page = AsyncMock()

        domain_locator = AsyncMock()
        domain_locator.count.return_value = 1
        domain_locator.first = domain_locator

        password_locator = AsyncMock()
        password_locator.count.return_value = 1

        def locator_factory(selector):
            if "#password" in selector:
                return password_locator
            return domain_locator

        mock_page.locator = MagicMock(side_effect=locator_factory)
        mock_page.url = "https://www.linkedin.com/login"

        auth = LinkedInAuthenticator(page=mock_page)
        auth.email = "user@example.com"

        with patch(
            "src.job_manager.linkedin.authenticator_linkedin.async_pause", new_callable=AsyncMock
        ):
            result = await auth.try_continue_with_saved_account()

        assert result is False

    @pytest.mark.asyncio
    async def test_saved_account_click_raises_tries_next_selector(self):
        """Continues to next selector when click raises an exception"""
        mock_page = AsyncMock()

        failing_locator = AsyncMock()
        failing_locator.count.return_value = 1
        failing_locator.first = failing_locator
        failing_locator.click = AsyncMock(side_effect=Exception("click failed"))

        mock_page.locator = MagicMock(return_value=failing_locator)
        mock_page.url = "https://www.linkedin.com/login"

        auth = LinkedInAuthenticator(page=mock_page)
        auth.email = "user@example.com"

        with patch(
            "src.job_manager.linkedin.authenticator_linkedin.async_pause", new_callable=AsyncMock
        ):
            result = await auth.try_continue_with_saved_account()

        assert result is False

    @pytest.mark.asyncio
    async def test_no_email_set_skips_domain_selector(self):
        """When email is None, domain-based selector is skipped"""
        mock_page = AsyncMock()
        mock_locator = AsyncMock()
        mock_locator.count.return_value = 0
        mock_locator.first = mock_locator
        mock_page.locator = MagicMock(return_value=mock_locator)

        auth = LinkedInAuthenticator(page=mock_page)
        auth.email = None

        result = await auth.try_continue_with_saved_account()

        assert result is False
        # Ensure no domain-specific selector (has-text('<domain>')) was used
        for call in mock_page.locator.call_args_list:
            assert "example.com" not in call.args[0]


class TestAuthenticatorStart:
    """Tests for the main start method"""

    @pytest.mark.asyncio
    async def test_start_already_logged_in(self):
        """Test start when user is already logged in"""
        mock_page = AsyncMock()

        auth = LinkedInAuthenticator(page=mock_page)
        auth.email = "test@example.com"
        auth.password = "password123"

        with patch.object(auth, "is_logged_in", new_callable=AsyncMock) as mock_is_logged:
            mock_is_logged.return_value = True

            result = await auth.start()

            assert result is True
            mock_is_logged.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_needs_login(self):
        """Test start when login is required"""
        mock_page = AsyncMock()

        auth = LinkedInAuthenticator(page=mock_page)
        auth.email = "test@example.com"
        auth.password = "password123"

        with (
            patch.object(auth, "is_logged_in", new_callable=AsyncMock) as mock_is_logged,
            patch.object(auth, "handle_login", new_callable=AsyncMock) as mock_login,
        ):
            mock_is_logged.return_value = False
            mock_login.return_value = True

            result = await auth.start()

            assert result is True
            mock_is_logged.assert_called_once()
            mock_login.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_login_fails(self):
        """Test start when login fails"""
        mock_page = AsyncMock()

        auth = LinkedInAuthenticator(page=mock_page)
        auth.email = "test@example.com"
        auth.password = "password123"

        with (
            patch.object(auth, "is_logged_in", new_callable=AsyncMock) as mock_is_logged,
            patch.object(auth, "handle_login", new_callable=AsyncMock) as mock_login,
        ):
            mock_is_logged.return_value = False
            mock_login.return_value = False

            result = await auth.start()

            assert result is False
            mock_login.assert_called_once()


class TestAuthenticatorIntegration:
    """Integration tests for LinkedInAuthenticator"""

    @pytest.mark.asyncio
    async def test_full_login_flow_success(self):
        """Test complete login flow from start to finish"""
        mock_page = AsyncMock()
        mock_page.url = "https://www.linkedin.com/feed/"

        mock_locator = AsyncMock()
        mock_locator.count.return_value = 0
        # Make locator() a non-async method that returns the mock_locator
        mock_page.locator = MagicMock(return_value=mock_locator)
        # get_by_role is synchronous in Playwright
        mock_page.get_by_role = MagicMock()
        mock_page.get_by_role.return_value.click = AsyncMock()

        auth = LinkedInAuthenticator(page=mock_page)
        auth.set_parameters("test@example.com", "password123")

        with (
            patch(
                "src.job_manager.linkedin.authenticator_linkedin.find_element_safely",
                new_callable=AsyncMock,
            ) as mock_find,
            patch(
                "src.job_manager.linkedin.authenticator_linkedin.safe_fill", new_callable=AsyncMock
            ) as mock_fill,
            patch(
                "src.job_manager.linkedin.authenticator_linkedin.async_pause",
                new_callable=AsyncMock,
            ),
            patch.object(
                auth, "try_continue_with_saved_account", new_callable=AsyncMock, return_value=False
            ),
            patch.object(auth, "check_login_success", new_callable=AsyncMock, return_value=True),
        ):
            # First is_logged_in check returns False (not logged in)
            # After login, feed element is found
            mock_find.side_effect = [None, MagicMock()]
            mock_fill.return_value = True

            result = await auth.start()

            assert result is True

    @pytest.mark.asyncio
    async def test_full_login_flow_with_saved_session(self):
        """Test login flow when valid session already exists"""
        mock_page = AsyncMock()
        mock_page.url = "https://www.linkedin.com/feed/"

        auth = LinkedInAuthenticator(page=mock_page)
        auth.set_parameters("test@example.com", "password123")

        with (
            patch(
                "src.job_manager.linkedin.authenticator_linkedin.find_element_safely",
                new_callable=AsyncMock,
            ) as mock_find,
            patch(
                "src.job_manager.linkedin.authenticator_linkedin.safe_fill", new_callable=AsyncMock
            ) as mock_fill,
        ):
            mock_find.return_value = MagicMock()  # Feed element found immediately

            result = await auth.start()

            assert result is True
            # Should not attempt to fill credentials
            mock_fill.assert_not_called()


class TestIndeedAuthenticatorInit:
    def test_init_without_page(self):
        auth = IndeedAuthenticator()

        assert auth.page is None
        assert auth.email is None
        assert auth.password is None

    def test_init_with_page(self):
        mock_page = MagicMock()
        auth = IndeedAuthenticator(page=mock_page)

        assert auth.page == mock_page

    def test_set_parameters(self):
        auth = IndeedAuthenticator()
        auth.set_parameters("user@example.com", "pass123")

        assert auth.email == "user@example.com"
        assert auth.password == "pass123"


class TestIndeedIsLoggedIn:
    @pytest.mark.asyncio
    async def test_logged_in_account_menu_found(self):
        mock_page = AsyncMock()
        mock_page.url = "https://www.indeed.com/"

        with patch(
            "src.job_manager.indeed.authenticator_indeed.find_element_safely",
            new_callable=AsyncMock,
            return_value=MagicMock(),
        ):
            auth = IndeedAuthenticator(page=mock_page)
            result = await auth.is_logged_in()

        assert result is True
        mock_page.goto.assert_called_once_with(
            IndeedAuthenticator.INDEED_HOME_URL, wait_until="domcontentloaded"
        )

    @pytest.mark.asyncio
    async def test_redirected_to_login(self):
        mock_page = AsyncMock()
        mock_page.url = "https://secure.indeed.com/account/login"

        auth = IndeedAuthenticator(page=mock_page)
        result = await auth.is_logged_in()

        assert result is False

    @pytest.mark.asyncio
    async def test_redirected_to_signin(self):
        mock_page = AsyncMock()
        mock_page.url = "https://www.indeed.com/signin"

        auth = IndeedAuthenticator(page=mock_page)
        result = await auth.is_logged_in()

        assert result is False

    @pytest.mark.asyncio
    async def test_account_menu_not_found(self):
        mock_page = AsyncMock()
        mock_page.url = "https://www.indeed.com/"

        with patch(
            "src.job_manager.indeed.authenticator_indeed.find_element_safely",
            new_callable=AsyncMock,
            return_value=None,
        ):
            auth = IndeedAuthenticator(page=mock_page)
            result = await auth.is_logged_in()

        assert result is False

    @pytest.mark.asyncio
    async def test_exception_returns_false(self):
        mock_page = AsyncMock()
        mock_page.goto.side_effect = Exception("Network error")

        auth = IndeedAuthenticator(page=mock_page)
        result = await auth.is_logged_in()

        assert result is False


class TestIndeedHandleLogin:
    @pytest.mark.asyncio
    async def test_handle_login_success(self):
        mock_page = AsyncMock()
        auth = IndeedAuthenticator(page=mock_page)
        auth.email = "user@example.com"

        with (
            patch.object(auth, "enter_credentials", new_callable=AsyncMock, return_value=True),
            patch(
                "src.job_manager.indeed.authenticator_indeed.async_pause", new_callable=AsyncMock
            ),
        ):
            result = await auth.handle_login()

        assert result is True
        mock_page.goto.assert_called_once_with(IndeedAuthenticator.INDEED_LOGIN_URL)

    @pytest.mark.asyncio
    async def test_handle_login_navigation_fails(self):
        mock_page = AsyncMock()
        mock_page.goto.side_effect = Exception("Navigation failed")

        auth = IndeedAuthenticator(page=mock_page)
        result = await auth.handle_login()

        assert result is False


class TestIndeedEnterCredentials:
    @pytest.mark.asyncio
    async def test_enter_credentials_success(self):
        mock_page = AsyncMock()
        auth = IndeedAuthenticator(page=mock_page)
        auth.email = "user@example.com"

        with (
            patch(
                "src.job_manager.indeed.authenticator_indeed.safe_fill",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "src.job_manager.indeed.authenticator_indeed.safe_click",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("builtins.input", return_value=""),
            patch.object(auth, "check_login_success", new_callable=AsyncMock, return_value=True),
        ):
            result = await auth.enter_credentials()

        assert result is True

    @pytest.mark.asyncio
    async def test_enter_credentials_email_fill_fails(self):
        mock_page = AsyncMock()
        auth = IndeedAuthenticator(page=mock_page)
        auth.email = "user@example.com"

        with patch(
            "src.job_manager.indeed.authenticator_indeed.safe_fill",
            new_callable=AsyncMock,
            return_value=False,
        ):
            result = await auth.enter_credentials()

        assert result is False

    @pytest.mark.asyncio
    async def test_enter_credentials_exception(self):
        mock_page = AsyncMock()
        auth = IndeedAuthenticator(page=mock_page)
        auth.email = "user@example.com"

        with patch(
            "src.job_manager.indeed.authenticator_indeed.safe_fill",
            new_callable=AsyncMock,
            side_effect=Exception("fill error"),
        ):
            result = await auth.enter_credentials()

        assert result is False


class TestIndeedCheckLoginSuccess:
    @pytest.mark.asyncio
    async def test_success_on_jobs_url(self):
        mock_page = AsyncMock()
        mock_page.url = "https://www.indeed.com/jobs?q=python"

        with patch(
            "src.job_manager.indeed.authenticator_indeed.find_element_safely",
            new_callable=AsyncMock,
            return_value=None,
        ):
            auth = IndeedAuthenticator(page=mock_page)
            result = await auth.check_login_success()

        assert result is True

    @pytest.mark.asyncio
    async def test_success_on_myjobs_url(self):
        mock_page = AsyncMock()
        mock_page.url = "https://www.indeed.com/myjobs"

        with patch(
            "src.job_manager.indeed.authenticator_indeed.find_element_safely",
            new_callable=AsyncMock,
            return_value=None,
        ):
            auth = IndeedAuthenticator(page=mock_page)
            result = await auth.check_login_success()

        assert result is True

    @pytest.mark.asyncio
    async def test_success_blocked_when_signin_button_still_present(self):
        """Reaching a success URL but sign-in button still visible means not logged in yet."""
        mock_page = AsyncMock()
        mock_page.url = "https://www.indeed.com/jobs?q=python"

        with (
            patch(
                "src.job_manager.indeed.authenticator_indeed.find_element_safely",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch(
                "src.job_manager.indeed.authenticator_indeed.async_pause", new_callable=AsyncMock
            ),
        ):
            auth = IndeedAuthenticator(page=mock_page)
            result = await auth.check_login_success()

        assert result is False

    @pytest.mark.asyncio
    async def test_login_error_message_detected(self):
        mock_page = AsyncMock()
        mock_page.url = "https://www.indeed.com/login"

        mock_error_locator = AsyncMock()
        mock_error_locator.count.return_value = 1
        mock_error_locator.first.text_content.return_value = "Invalid email or password"
        mock_page.locator = MagicMock(return_value=mock_error_locator)

        with patch(
            "src.job_manager.indeed.authenticator_indeed.async_pause", new_callable=AsyncMock
        ):
            auth = IndeedAuthenticator(page=mock_page)
            result = await auth.check_login_success()

        assert result is False

    @pytest.mark.asyncio
    async def test_challenge_url_waits(self):
        mock_page = AsyncMock()

        urls = ["https://www.indeed.com/challenge/captcha", "https://www.indeed.com/jobs?q=dev"]
        url_index = [0]

        def get_url():
            idx = url_index[0]
            url = urls[min(idx, len(urls) - 1)]
            url_index[0] += 1
            return url

        type(mock_page).url = property(lambda self: get_url())

        with (
            patch(
                "src.job_manager.indeed.authenticator_indeed.find_element_safely",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "src.job_manager.indeed.authenticator_indeed.async_pause", new_callable=AsyncMock
            ),
        ):
            auth = IndeedAuthenticator(page=mock_page)
            result = await auth.check_login_success()

        assert result is True

    @pytest.mark.asyncio
    async def test_timeout_on_login_page(self):
        mock_page = AsyncMock()
        mock_page.url = "https://www.indeed.com/login"

        mock_locator = AsyncMock()
        mock_locator.count.return_value = 0
        mock_page.locator = MagicMock(return_value=mock_locator)

        with patch(
            "src.job_manager.indeed.authenticator_indeed.async_pause", new_callable=AsyncMock
        ):
            auth = IndeedAuthenticator(page=mock_page)
            result = await auth.check_login_success()

        assert result is False

    @pytest.mark.asyncio
    async def test_exception_returns_false(self):
        mock_page = AsyncMock()
        type(mock_page).url = property(lambda self: (_ for _ in ()).throw(Exception("URL error")))

        auth = IndeedAuthenticator(page=mock_page)
        result = await auth.check_login_success()

        assert result is False


class TestIndeedAuthenticatorStart:
    @pytest.mark.asyncio
    async def test_start_already_logged_in(self):
        mock_page = AsyncMock()
        auth = IndeedAuthenticator(page=mock_page)
        auth.set_parameters("user@example.com")

        with patch.object(auth, "is_logged_in", new_callable=AsyncMock, return_value=True):
            result = await auth.start()

        assert result is True

    @pytest.mark.asyncio
    async def test_start_needs_login_succeeds(self):
        mock_page = AsyncMock()
        auth = IndeedAuthenticator(page=mock_page)
        auth.set_parameters("user@example.com")

        with (
            patch.object(auth, "is_logged_in", new_callable=AsyncMock, return_value=False),
            patch.object(auth, "handle_login", new_callable=AsyncMock, return_value=True),
        ):
            result = await auth.start()

        assert result is True

    @pytest.mark.asyncio
    async def test_start_login_fails(self):
        mock_page = AsyncMock()
        auth = IndeedAuthenticator(page=mock_page)
        auth.set_parameters("user@example.com")

        with (
            patch.object(auth, "is_logged_in", new_callable=AsyncMock, return_value=False),
            patch.object(auth, "handle_login", new_callable=AsyncMock, return_value=False),
        ):
            result = await auth.start()

        assert result is False
