from abc import ABC, abstractmethod
from typing import Union

from playwright.sync_api import Page

from config.logger_config import logger


class BaseAuthenticator(ABC):
    """Abstract base class for platform authenticators"""

    def __init__(self, page: Union[Page, any] = None):
        self.page = page
        self.email = None
        self.password = None

    def set_parameters(self, email: str, password: str = None) -> None:
        self.email = email
        self.password = password

    async def start(self) -> bool:
        """Main method for starting authentication"""
        logger.info("Checking if user is already logged in...")

        if await self.is_logged_in():
            logger.info("User already logged in using saved session")
            return True

        logger.info("Saved session is invalid, performing new login")
        result = await self.handle_login()
        if result:
            logger.info("Login successful")
        return result

    @abstractmethod
    async def is_logged_in(self) -> bool:
        """Check if user is logged into the platform"""

    @abstractmethod
    async def handle_login(self) -> bool:
        """Navigate to login page and authenticate"""

    @abstractmethod
    async def enter_credentials(self) -> bool:
        """Enter credentials on the login form"""

    @abstractmethod
    async def check_login_success(self) -> bool:
        """Verify successful login"""
