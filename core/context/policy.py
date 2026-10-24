"""Provider-independent context policy shared with application settings."""

CONTEXT_WINDOWS = (128_000, 256_000, 512_000, 1_000_000)
DEFAULT_CONTEXT_WINDOW = 256_000
# Native capacities; a hosted deployment can impose a smaller ceiling.
MODEL_CONTEXT_WINDOWS = {
    "MiniMax-M2.7": 204_800,
    "MiniMax-M3": 1_000_000,
    "deepseek-flash": 1_048_576,
    "Qwen/Qwen3.8-27B": 262_144,
}


# Qwen explicitly supports a deployed extension; native capacity remains the default.
MODEL_MAX_CONTEXT_WINDOWS = {**MODEL_CONTEXT_WINDOWS, "Qwen/Qwen3.8-27B": 1_000_000}


def model_capacity(model: str, overrides=None) -> int:
    configured = (overrides or {}).get(model, MODEL_CONTEXT_WINDOWS.get(model, 128_000))
    return min(configured, MODEL_MAX_CONTEXT_WINDOWS.get(model, configured))


def effective_window(selected: int, model: str, overrides=None) -> int:
    return min(selected, model_capacity(model, overrides))


def validate_windows(overrides) -> None:
    if not isinstance(overrides, dict) or any(
        not isinstance(k, str) or not k or type(v) is not int or v < 4096
        for k, v in overrides.items()
    ):
        raise ValueError("model_context_windows requires model IDs and positive token capacities")
