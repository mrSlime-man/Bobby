from typing import List, Optional

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator


class ExperienceLevel(BaseModel):
    internship: bool = False
    entry: bool = False
    associate: bool = False
    mid_senior_level: bool = False
    director: bool = False
    executive: bool = False


class JobTypes(BaseModel):
    full_time: bool = False
    contract: bool = False
    part_time: bool = False
    temporary: bool = False
    volunteer: bool = False
    internship: bool = False
    other: bool = False


class DatePosted(BaseModel):
    all_time: bool = False
    month: bool = False
    week: bool = False
    day_24_hours: bool = Field(
        default=False,
        validation_alias=AliasChoices("day_24_hours", "24_hours"),
    )

    @model_validator(mode="after")
    def validate_only_one_true(self):
        true_count = sum(
            1
            for flag in (
                self.all_time,
                self.month,
                self.week,
                self.day_24_hours,
            )
            if bool(flag)
        )
        if true_count > 1:
            raise ValueError("Only one of DatePosted fields can be True at a time")
        return self


class SearchQueryFamily(BaseModel):
    """One bounded, semantically related LinkedIn keyword group."""

    name: str
    positions: List[str]

    @field_validator("positions")
    @classmethod
    def require_at_least_one_position(cls, value):
        if not any(str(position).strip() for position in value):
            raise ValueError("Search query family must contain at least one position")
        return value


class SearchConfig(BaseModel):
    # Search criteria
    positions: List[str]

    # Work arrangement options
    remote: Optional[bool] = False
    hybrid: Optional[bool] = False
    onsite: Optional[bool] = False

    # Experience level settings
    experience_level: Optional[ExperienceLevel] = ExperienceLevel()

    # Job type settings
    job_types: Optional[JobTypes] = JobTypes()

    # Date posted settings
    date: Optional[DatePosted] = DatePosted()

    # Location settings
    locations: Optional[List[str]] = []

    # A profile uses one family and one location variant per cycle.  The
    # complete configured position list remains a fallback for older configs.
    query_families: List[SearchQueryFamily] = []
    location_variants: List[str] = []
    search_profile: Optional[str] = None

    # Application settings
    apply_once_at_company: Optional[bool] = True

    # Blacklists
    company_blacklist: Optional[List[str]] = []
    title_blacklist: Optional[List[str]] = []
    location_blacklist: Optional[List[str]] = []

    @field_validator(
        "locations",
        "company_blacklist",
        "title_blacklist",
        "location_blacklist",
        mode="before",
    )
    @classmethod
    def none_list_fields_default_to_empty(cls, value):
        if value is None:
            return []
        return value


class ConnectionSearcherConfig(BaseModel):
    main_search_words: List[str] = [
        "Open Networker",
        "LION",
        "NO IDK",
    ]
    additional_search_words: List[str] = []


class LinkedInMessagesConfig(BaseModel):
    dry_run: bool = True
    execute_archives: bool = False
    execute_replies: bool = False
    max_conversations_to_scan: int = 25
    unread_only: bool = False
    auto_star_job_offers: bool = True
    auto_label_job_offers: bool = True
    skip_drafting_for_personal_messages: bool = True
    reply_tone: str = "a thoughtful senior engineering leader"
    reply_max_characters: int = 600
    reply_short_paragraphs: bool = True
    reply_avoid_em_dash: bool = True
    old_message_threshold_days: int = 60
    old_message_apology_enabled: bool = True
    old_message_apology_reason: str = "you've been busy with multiple projects"
    old_job_message_follow_up_enabled: bool = True
    old_job_message_follow_up_text: str = "ask if the opportunity is still available"

    @field_validator("max_conversations_to_scan", "reply_max_characters", "old_message_threshold_days")
    @classmethod
    def validate_positive_integers(cls, value, info):
        if value < 1:
            raise ValueError(f"{info.field_name} must be at least 1")
        return value


class Secrets(BaseModel):
    linkedin_email: Optional[str] = None
    linkedin_password: Optional[str] = None
    indeed_email: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_proxy: Optional[str] = None
    llm_api_url: Optional[str] = None
    tg_token: Optional[str] = None
    tg_api_id: Optional[str] = None
    tg_api_hash: Optional[str] = None

    @field_validator("tg_api_id", mode="before")
    @classmethod
    def validate_tg_api_id(cls, v):
        if isinstance(v, int):
            return str(v)
        elif isinstance(v, str):
            return v
        else:
            raise ValueError("tg_api_id must be a string or an integer")
