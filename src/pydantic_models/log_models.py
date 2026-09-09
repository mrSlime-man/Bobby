from typing import Dict

from pydantic import BaseModel, Field


class LLMCall(BaseModel):
    """
    LLM call model for storing LLM call information.
    """

    model_name: str = Field(default="", description="Model name")
    timestamp: str = Field(default="", description="Timestamp")
    total_tokens: int = Field(default=0, description="Total tokens")
    input_tokens: int = Field(default=0, description="Input tokens")
    output_tokens: int = Field(default=0, description="Output tokens")
    response_time_seconds: float = Field(default=0.0, description="LLM response time in seconds")
    total_cost: float = Field(default=0.0, description="Total cost")
    job_url: str = Field(default="", description="LinkedIn job URL associated with the call")
    job_title: str = Field(default="", description="Job title associated with the call")
    company_name: str = Field(default="", description="Company name associated with the call")
    prompts: Dict[str, str] = Field(default=None, description="Prompts")
    parsed_reply: str = Field(default="", description="Parsed reply")
