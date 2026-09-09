import argparse
import os
import statistics
import time
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv
from langchain_core.prompt_values import ChatPromptValue
from langchain_core.prompts import ChatPromptTemplate

from src.llm.llm_manager import ClaudeModel, GeminiModel, OllamaModel, OpenAIModel, OpenRouterModel


DEFAULT_PROMPT = "Say 'benchmark ok' and then summarize this request in one short sentence."


def build_model(
    model_type: str,
    model_name: str,
    api_key: str | None = None,
    llm_proxy: str | None = None,
    llm_api_url: str | None = None,
):
    model_type = model_type.lower()
    if model_type == "openrouter":
        return OpenRouterModel(api_key=api_key or "", llm_model=model_name, llm_proxy=llm_proxy)
    if model_type == "openai":
        return OpenAIModel(api_key=api_key or "", llm_model=model_name, llm_proxy=llm_proxy)
    if model_type == "gemini":
        return GeminiModel(api_key=api_key or "", llm_model=model_name, llm_proxy=llm_proxy)
    if model_type == "claude":
        return ClaudeModel(api_key=api_key or "", llm_model=model_name)
    if model_type == "ollama":
        return OllamaModel(llm_model=model_name, llm_api_url=llm_api_url or "")
    raise ValueError(f"Unsupported model type: {model_type}")


def load_prompt(prompt_text: str | None, prompt_file: str | None) -> str:
    if prompt_text:
        return prompt_text
    if prompt_file:
        return Path(prompt_file).read_text(encoding="utf-8")
    return DEFAULT_PROMPT


def build_prompt(prompt_text: str) -> ChatPromptValue:
    return ChatPromptTemplate.from_messages([("human", prompt_text)]).format_prompt()


def extract_usage(response: Any) -> Dict[str, int]:
    usage = getattr(response, "usage_metadata", None) or {}
    return {
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "total_tokens": int(usage.get("total_tokens", 0) or 0),
    }


def benchmark_model(model: Any, prompt: ChatPromptValue, repeats: int) -> Dict[str, Any]:
    durations = []
    last_response = None

    for _ in range(repeats):
        started_at = time.perf_counter()
        last_response = model.invoke(prompt)
        durations.append(time.perf_counter() - started_at)

    usage = extract_usage(last_response)
    content = getattr(last_response, "content", "")
    return {
        "runs": repeats,
        "min_seconds": round(min(durations), 3),
        "max_seconds": round(max(durations), 3),
        "avg_seconds": round(statistics.mean(durations), 3),
        "median_seconds": round(statistics.median(durations), 3),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "total_tokens": usage["total_tokens"],
        "response_preview": str(content)[:300],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark LLM provider latency with a real prompt.")
    parser.add_argument("--model-type", required=True, help="openrouter, openai, gemini, claude, ollama")
    parser.add_argument("--model", required=True, help="Provider model name")
    parser.add_argument("--prompt-file", help="Path to a prompt text file")
    parser.add_argument("--prompt-text", help="Prompt text to send directly")
    parser.add_argument("--repeats", type=int, default=3, help="Number of benchmark runs")
    parser.add_argument("--proxy", default=None, help="Optional LLM proxy URL")
    parser.add_argument("--api-url", default=None, help="Optional Ollama/API base URL")
    parser.add_argument("--api-key", default=None, help="Optional API key override")
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()
    prompt_text = load_prompt(args.prompt_text, args.prompt_file)
    prompt = build_prompt(prompt_text)
    api_key = args.api_key or os.getenv("llm_api_key")
    llm_proxy = args.proxy or os.getenv("llm_proxy")
    llm_api_url = args.api_url or os.getenv("llm_api_url")

    model = build_model(
        model_type=args.model_type,
        model_name=args.model,
        api_key=api_key,
        llm_proxy=llm_proxy,
        llm_api_url=llm_api_url,
    )
    result = benchmark_model(model, prompt, repeats=args.repeats)

    print(f"model_type: {args.model_type}")
    print(f"model: {args.model}")
    print(f"prompt_chars: {len(prompt_text)}")
    print(f"runs: {result['runs']}")
    print(f"min_seconds: {result['min_seconds']}")
    print(f"median_seconds: {result['median_seconds']}")
    print(f"avg_seconds: {result['avg_seconds']}")
    print(f"max_seconds: {result['max_seconds']}")
    print(f"input_tokens: {result['input_tokens']}")
    print(f"output_tokens: {result['output_tokens']}")
    print(f"total_tokens: {result['total_tokens']}")
    print(f"response_preview: {result['response_preview']}")


if __name__ == "__main__":
    main()
