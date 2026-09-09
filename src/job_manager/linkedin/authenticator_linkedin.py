from pathlib import Path
from typing import Union

from playwright.sync_api import Page

from config.logger_config import logger
from src.job_manager.authenticator import BaseAuthenticator
from src.utils.browser_utils import debug_capture, find_element_safely, safe_fill
from src.utils.utils import async_pause


class LinkedInAuthenticator(BaseAuthenticator):
    """Class for LinkedIn login and session management"""

    AUTHENTICATED_URL_PATTERNS = (
        "/feed/",
        "/jobs/",
        "/in/",
        "/mynetwork/",
        "/notifications/",
        "/messaging/",
    )

    UNAUTHENTICATED_URL_PATTERNS = (
        "/login",
        "/uas/login",
        "/signup",
        "/authwall",
        "/checkpoint/challenge",
        "/challenge",
    )

    def __init__(self, page: Union[Page, any] = None):
        super().__init__(page)
        self.session_file = Path("data/linkedin_session.json")
        logger.info("LinkedIn authenticator initialized")

    def set_parameters(self, email: str, password: str) -> None:
        logger.info("Setting LinkedIn Authenticator parameters")
        super().set_parameters(email, password)

    async def is_logged_in(self) -> bool:
        """Check if user is logged into LinkedIn (async)"""
        try:
            await self.page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
            logger.info("Checking if user is logged into LinkedIn...")

            if await self._is_authenticated_page():
                logger.info("LinkedIn session appears authenticated")
                return True

            logger.warning("Could not determine authorization status, assuming not logged in")
            return False

        except Exception as e:
            logger.error(f"Error checking authorization status: {e}")
            return False

    async def handle_login(self) -> bool:
        """Login to LinkedIn (async)"""
        logger.info("Navigating to LinkedIn login page...")

        try:
            await self.page.goto("https://www.linkedin.com/login")
            await async_pause(1, 2)
            return await self.enter_credentials()

        except Exception as e:
            logger.error(f"Error accessing LinkedIn login: {e}")
            return False

    async def enter_credentials(self) -> bool:
        """Enter user credentials (async)"""
        logger.info("Entering user credentials in LinkedIn...")

        try:
            if await self._is_authenticated_page():
                logger.info("LinkedIn already redirected to an authenticated page")
                return True

            if await self.try_continue_with_saved_account():
                logger.info("Continued via saved account chooser")
                return await self.check_login_success()

            # If saved account chooser is not shown, fall back to the classic login form.
            email_selectors = [
                "#username",
                "input[autocomplete='username webauthn']",
                "input[autocomplete='username']",
                "input[type='email']",
            ]
            filled_email = False

            logger.info(
                "Waiting up to 3 minutes for LinkedIn login page/session to become ready..."
            )

            # LinkedIn can take a while to restore the saved session,
            # show the account chooser, security page, or render the
            # classic username/password form. Do not immediately fail.
            for attempt in range(90):
                # The user/session may become authenticated while we wait.
                if await self._is_authenticated_page():
                    logger.info(
                        "LinkedIn became authenticated while waiting for login form"
                    )
                    return True

                # Look for the email field without triggering noisy
                # safe_fill failures on every retry.
                found_selector = None

                for sel in email_selectors:
                    try:
                        locator = self.page.locator(sel)
                        if await locator.count() > 0:
                            found_selector = sel
                            break
                    except Exception:
                        continue

                if found_selector:
                    logger.info(
                        f"LinkedIn email field appeared after "
                        f"{attempt * 2} seconds"
                    )

                    if await safe_fill(
                        self.page,
                        found_selector,
                        self.email,
                    ):
                        filled_email = True
                        break

                # Check again whether a remembered-account chooser appeared.
                try:
                    if await self.try_continue_with_saved_account():
                        logger.info(
                            "Saved LinkedIn account became available while waiting"
                        )
                        if await self.check_login_success():
                            return True
                except Exception:
                    pass

                if attempt > 0 and attempt % 15 == 0:
                    logger.info(
                        f"Still waiting for LinkedIn login/session "
                        f"({attempt * 2}s elapsed)"
                    )

                await async_pause(2, 2)

            if not filled_email:
                # One last authentication check before giving up.
                if await self._is_authenticated_page():
                    logger.info(
                        "LinkedIn authenticated at the end of login wait"
                    )
                    return True

                logger.error(
                    "LinkedIn login form did not become available "
                    "within 3 minutes"
                )
                await debug_capture(
                    self.page,
                    "email_field_not_found_after_wait",
                )
                return False

            logger.info("Email entered")

            # Wait for and fill password field
            password_selectors = [
                "#password",
                "input[autocomplete='current-password']",
            ]
            filled_password = False
            for sel in password_selectors:
                if await safe_fill(self.page, sel, self.password):
                    filled_password = True
                    break
            if not filled_password:
                logger.error("Failed to fill password field")
                await debug_capture(self.page, "password_field_not_found")
                return False
            logger.info("Password entered")

            await async_pause(1, 2)

            # Click login button with multiple selectors
            try:
                selector = "button:text-is('Sign in')"
                locator = self.page.get_by_role("button", name="Sign in", exact=True)
                await locator.click(timeout=5000)
                logger.info(f"Login button clicked using selector: {selector}")
            except Exception as e:
                logger.error(f"Could not find or click login button: {e}")
                return False

            # Wait for login to complete
            await async_pause(3, 5)

            # Check login success
            if await self.check_login_success():
                logger.info("LinkedIn login successful")
                return True
            else:
                logger.error("Login verification failed")
                return False

        except Exception as e:
            logger.error(f"Error during credential entry: {e}")
            return False

    async def try_continue_with_saved_account(self) -> bool:
        """Handle the remembered-account chooser shown instead of the classic login form."""
        domain = self.email.split("@", 1)[1] if self.email and "@" in self.email else ""
        account_selectors = []
        if domain:
            account_selectors.append(f"div[role='button'][tabindex='0']:has-text('{domain}')")
        account_selectors.extend(
            [
                "div[role='button'][tabindex='0']:has-text('@')",
                "div[role='button'][tabindex='0']:has(img)",
            ]
        )

        for selector in account_selectors:
            locator = self.page.locator(selector).first
            if await locator.count() == 0:
                continue

            logger.info(f"Saved account chooser detected via selector: {selector}")
            try:
                await locator.wait_for(state="visible", timeout=5000)
                await locator.click(timeout=5000)
            except Exception as e:
                logger.warning(f"Failed to click saved account chooser '{selector}': {e}")
                continue

            await async_pause(2, 3)

            # If LinkedIn accepted the remembered account, we will leave the login page.
            if "/login" not in self.page.url and "/uas/login" not in self.page.url:
                return True

            # Some flows click into an intermediate password prompt.
            password_locator = self.page.locator("#password")
            if await password_locator.count() > 0:
                logger.info("Saved account chooser led to password prompt")
                return False

        return False

    async def _is_authenticated_page(self) -> bool:
        """Detect authenticated LinkedIn pages using stable URLs and nav markers."""
        current_url = self.page.url.lower()

        if any(pattern in current_url for pattern in self.UNAUTHENTICATED_URL_PATTERNS):
            logger.warning(f"LinkedIn is on an unauthenticated URL: {self.page.url}")
            return False

        if current_url in {"https://www.linkedin.com/", "https://www.linkedin.com"} or any(
            pattern in current_url for pattern in self.AUTHENTICATED_URL_PATTERNS
        ):
            return True

        authenticated_selectors = [
            "nav[aria-label='Primary Navigation']",
            "a[href='https://www.linkedin.com/feed/']",
            "a[href='https://www.linkedin.com/jobs/']",
            "button[aria-label*='Notifications']",
            "button[aria-label*='Messaging']",
            "input[placeholder*='Search']",
        ]

        for selector in authenticated_selectors:
            if await find_element_safely(self.page, selector, timeout=2000):
                logger.info(f"Authenticated LinkedIn UI detected via selector: {selector}")
                return True

        try:
            page_title = (await self.page.title()).strip().lower()
        except Exception as e:
            logger.debug(f"Could not read LinkedIn page title while checking auth state: {e}")
            return False

        if "linkedin" in page_title and page_title != "sign in | linkedin":
            logger.info(f"Authenticated LinkedIn page inferred from title: {page_title}")
            return True

        return False

    async def check_login_success(self) -> bool:
        """Check login success with improved detection (async)"""
        try:
            # Give LinkedIn enough time to finish redirects,
            # restore the session, or allow manual verification.
            # Up to roughly 3 minutes.
            for attempt in range(120):
                current_url = self.page.url
                logger.debug(f"Login check attempt {attempt + 1}, current URL: {current_url}")

                # Success URL patterns
                success_patterns = [
                    "/feed/",
                    "/in/",
                    "/mynetwork/",
                    "/notifications/",
                    "/messaging/",
                ]

                # Check if we reached a success page
                if current_url == "https://www.linkedin.com/" or any(
                    pattern in current_url for pattern in success_patterns
                ):
                    logger.info("Login successful - reached authenticated page")
                    return True

                # If still on login page - check for errors or challenges
                if "/login" in current_url or "/uas/login" in current_url:
                    # Check for various error message selectors
                    error_selectors = [
                        ".form__label--error",
                        ".alert--error",
                        ".mercado-flash-error",
                        "[data-test='error-message']",
                        ".login-form__error-message",
                    ]

                    for error_selector in error_selectors:
                        error_locator = self.page.locator(error_selector)
                        if await error_locator.count() > 0:
                            error_text = await error_locator.first.text_content()
                            if error_text and error_text.strip():
                                logger.error(f"Login error detected: {error_text.strip()}")
                                return False

                    # Check for CAPTCHA or security challenge
                    challenge_selectors = [
                        ".captcha-form",
                        ".challenge-form",
                        ".security-challenge",
                        "[data-test='captcha-form']",
                    ]

                    for challenge_selector in challenge_selectors:
                        challenge_locator = self.page.locator(challenge_selector)
                        if await challenge_locator.count() > 0:
                            logger.warning(
                                "Security challenge or CAPTCHA detected - manual intervention may be required"
                            )
                            # Continue waiting for user to solve challenge
                            break

                # Check for redirect or checkpoint pages
                if "/checkpoint/challenge" in current_url or "/challenge" in current_url:
                    logger.warning(
                        "LinkedIn security checkpoint detected - waiting 60s for resolution"
                    )
                    await async_pause(60, 60)

                await async_pause(1, 2)

            # Final attempt - check if we can detect logged-in state
            logger.info("Login timeout reached")
            return False

        except Exception as e:
            logger.error(f"Error checking login success: {e}")
            return False
