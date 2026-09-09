from unittest.mock import MagicMock

from langchain_core.messages import AIMessage

from src.llm.benchmark_provider import benchmark_model, extract_usage, load_prompt


def test_load_prompt_prefers_inline_text(tmp_path):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("from file", encoding="utf-8")

    assert load_prompt("inline", str(prompt_file)) == "inline"


def test_extract_usage_reads_usage_metadata():
    response = AIMessage(
        content="ok",
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )

    assert extract_usage(response) == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }


def test_benchmark_model_returns_summary(monkeypatch):
    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content="benchmark ok",
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )
    perf_values = iter([1.0, 2.0, 3.0, 4.5])
    monkeypatch.setattr("src.llm.benchmark_provider.time.perf_counter", lambda: next(perf_values))

    result = benchmark_model(model, prompt=MagicMock(), repeats=2)

    assert result["runs"] == 2
    assert result["min_seconds"] == 1.0
    assert result["max_seconds"] == 1.5
    assert result["avg_seconds"] == 1.25
    assert result["total_tokens"] == 15
