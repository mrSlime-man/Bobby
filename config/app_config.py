from pathlib import Path

"""
This file contains application settings
"""

"""
Job site to use. Possible values: 'linkedin', 'indeed'
"""
JOB_SITE = "linkedin"

"""Maximum number of applications"""
MAX_APPLIES_NUM = 5

"""
If this mode is activated - the browser will be launched in headless mode
"""
HEADLESS_MODE = False

"""
If this mode is activated, saves screenshots and page HTML to data/debug/ on every selector failure.
Also enables Playwright tracing (saved to data/debug/trace.zip on exit, viewable at trace.playwright.dev).
No-op when False (default).
"""
DEBUG_MODE = False

"""
If this mode is activated - apply to all vacancies indiscriminately,
otherwise ask LLM to select only those vacancies that suit you
by interests or by tech stack
"""
MONKEY_MODE = False

"""
In this mode app doesn't apply to the jobs, only creates resumes, cover letters and gathers skill statistics
- resumes are created in data/resume
- cover letters are created in data/output/cover_letters
- skill statistics are gathered in data/output/skill_stat.yaml
"""
TEST_MODE = True

"""
In this mode app doesn't apply to the jobs or create resumes and cover letters, only gathers information for interesting jobs and
their skill statistics and saves them to the files data/output/interesting_jobs.yaml and data/output/skill_stat.yaml."""
COLLECT_INFO_MODE = False

"""
For Indeed only
If this setting is False, Indeed Resume will be used (no file upload).
If True, app will try to upload a resume file.
"""
UPLOAD_RESUME = True

"""
In this mode app applies only the jobs with Easy Apply
If this mode is deactivated, app will apply to the jobs with Easy Apply and try to apply to the jobs with 3rd party applications
WARNING: applying to the jobs with 3rd party applications is not guaranteed to be successful, but is guaranteed to consume at least 10-100x more tokens
"""
EASY_APPLY_ONLY_MODE = True

"""
If enabled for LinkedIn, ignores positions in search_config.yaml and processes
LinkedIn's recommended jobs list instead of a keyword search.
"""
LINKEDIN_RECOMMENDED_JOBS_MODE = False

"""
If enabled for LinkedIn, ignores positions in search_config.yaml and processes
LinkedIn's Top applicant picks collection instead of a keyword search.
"""
LINKEDIN_TOP_APPLICANT_JOBS_MODE = False

"""
If this mode is activated, app will check if the last search was less than a day ago.
This is useful if you want bot to automatically restart the search every 24 hours when LinkedIn resets the search limits.
"""
RESTART_EVERY_DAY = False
"""
Path to a ready-made resume document to use for all applications.
If empty string - a new resume is generated for each vacancy.
If set - the file at this path is used as-is for every application.
Example: data/resumes/resume.pdf or data/resumes/resume.docx
"""
READY_MADE_RESUME_PATH = str(Path.home() / '.local/share/bobby/resumes/resume.pdf')
"""
Optional path to a photo file for LinkedIn Easy Apply image upload fields.
If empty string - the bot will try to reuse your visible LinkedIn profile photo.
"""
READY_MADE_PHOTO_PATH = ''
"""
Resume style to use for generated resumes.
If set - skips the interactive style selection prompt.
If None - prompts user to select a style interactively.
Possible values:
    - "FAANGPath"
    - "Cloyola Grey"
    - "Modern Blue"
    - "Modern Grey"
    - "Default"
    - "Clean Blue"
"""
RESUME_STYLE = "FAANGPath"

"""
If LLM evaluated the 'interest' level of the job not below this threshold - the job is considered interesting for application.
Otherwise not.
"""
JOB_IS_INTERESTING_THRESH = 50

"""Minimum time spent on one job application"""
MINIMUM_WAIT_TIME_SEC = 10

"""
If this mode is activated, app will try to decrease RPM to avoid rate limit errors
"""
FREE_TIER = True

"""
Free tier mode wait time in seconds
"""
FREE_TIER_RPM_LIMIT = 15

"""
If this mode is activated, bot process output will be printed to the dashboard console in addition to the log file.
"""
DASHBOARD_OUTPUT_APP_LOGS = False

"""
Logging level
Possible values:
    - "DEBUG"
    - "INFO"
    - "WARNING"
    - "ERROR"
    - "CRITICAL"
"""
MINIMUM_LOG_LEVEL = 'INFO'

"""
LLM type
Possible values:
    - "openai"
    - "gigachat"
    - "claude"
    - "ollama"
    - "gemini"
    - "huggingface"
    - "openrouter"
    - "nvidia_nim"
    - "groq"
    - "cerebras"
    - "openai_compatible"
"""
LLM_MODEL_TYPE = 'openai'
# LLM_MODEL_TYPE = "openai"

# LLM models
EASY_APPLY_MODEL = 'gpt-4o-mini'
# EASY_APPLY_MODEL = "google/gemini-3-flash-preview"
# EASY_APPLY_MODEL = "gpt-5-mini"
APPLY_AGENT_MODEL = 'gpt-4o-mini'
# APPLY_AGENT_MODEL = "google/gemini-3-flash-preview"
# APPLY_AGENT_MODEL = "gpt-5-mini"

"""
Easy Apply model temperature
the higher it is, the more creative the model, but hallucinations may occur
the lower it is, the more strictly the model follows the prompt and invents less
"""
TEMPERATURE = 0.4
# External/browser-use reliability
APPLY_AGENT_MAX_RETRIES = 3
APPLY_AGENT_RETRY_DELAY_SEC = 8

# Keep this on a stable Gemini model available to your API account.
APPLY_AGENT_FALLBACK_MODEL = "gemini-3.6-flash"

"""
Ordered external provider candidates.  OpenAI is a real Browser Use provider
and is included as the secondary slot, but it is skipped unless a separate
``openai_api_key``/``OPENAI_API_KEY`` credential is configured.  The primary
Gemini credential is never reused for another provider.
"""
LLM_PROVIDER_ORDER = ("gemini", "openai")

"""Default model shown for the real secondary provider."""
LLM_SECONDARY_PROVIDER = "openai"
LLM_SECONDARY_MODEL = "gpt-4o-mini"

"""Allow a bounded provider switch after a provider-level failure."""
LLM_FALLBACK_ENABLED = True

"""Seconds before a temporarily unavailable provider may be retried."""
LLM_PROVIDER_COOLDOWN_SEC = 120

"""Maximum provider attempts for one external application worker."""
LLM_PROVIDER_MAX_RETRIES = 2

"""External ATS operator controls.  Easy Apply does not read these values."""
EXTERNAL_ATS_ENABLED = False
EXTERNAL_ATS_MAX_RECOVERY_ATTEMPTS = 2
EXTERNAL_ATS_PAGE_TIMEOUT_SEC = 120
EXTERNAL_ATS_NAVIGATION_TIMEOUT_SEC = 45
EXTERNAL_ATS_AUTO_ACCOUNT_CREATION = False
EXTERNAL_ATS_RESUME_UPLOAD_ENABLED = False

"""Submission verification is a safety invariant and is intentionally read-only."""
EXTERNAL_ATS_SUBMISSION_VERIFICATION_ENABLED = True

GMAIL_APPLICATION_INTEGRATION = False

GMAIL_CREDENTIALS_PATH = str(Path.home() / '.config/bobby/gmail_credentials.json')

GMAIL_TOKEN_PATH = str(Path.home() / '.config/bobby/gmail_token.json')

GMAIL_VERIFICATION_TIMEOUT_SEC = 180

GMAIL_VERIFICATION_POLL_SEC = 5

GMAIL_RECEIPT_TIMEOUT_SEC = 90
