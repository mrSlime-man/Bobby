import os
import sys
from pathlib import Path

from loguru import logger

from config.constants import LOG_DIR
from config.app_config import MINIMUM_LOG_LEVEL
from src.telegram.telegram_error_handler import AsyncTelegramSink
from src.utils.log_privacy import load_sensitive_log_values, protect_log_record

logger.remove()

minimum_log_level = MINIMUM_LOG_LEVEL
_sensitive_log_values = load_sensitive_log_values(Path(__file__).resolve().parents[1])
logger.configure(patcher=lambda record: protect_log_record(record, _sensitive_log_values))

Path(LOG_DIR).mkdir(mode=0o700, parents=True, exist_ok=True)
for _log_name in ("app.log", "error.log", "internal_logger.log"):
    _log_path = Path(LOG_DIR) / _log_name
    _log_path.touch(mode=0o600, exist_ok=True)
    _log_path.chmod(0o600)

# Terminal output without tracebacks
logger.add(sys.stdout, level=minimum_log_level, backtrace=False, diagnose=False)

logger.add(
    AsyncTelegramSink(
        max_retries=4,
        cooldown=600,  # in case of the same error, we wait 10 minutes
    ),
    level="ERROR",
    format="{message}",
    backtrace=False,
    diagnose=False,
)

# Configuration of logging to a file
logger.add(
    os.path.join(LOG_DIR, "app.log"),
    rotation="10 MB",  # Rotate when file reaches 500 MB
    retention="30 days",  # Keep logs for 10 days
    compression="zip",  # Compress rotated logs
    level=minimum_log_level,
    backtrace=False,
    diagnose=False,
)

# Configuration of logging errors to a file
logger.add(
    os.path.join(LOG_DIR, "error.log"),
    rotation="5 MB",  # Rotate when file reaches 100 MB
    retention="30 days",  # Keep error logs longer
    compression="zip",
    level="ERROR",
    backtrace=False,
    diagnose=False,
)
