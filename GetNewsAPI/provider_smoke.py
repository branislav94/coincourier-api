import os

from openai import OpenAI

from config import (
    GROK_API_KEY,
    GROK_BASE_URL,
    GROK_IMAGE_ASPECT_RATIO,
    GROK_IMAGE_MODEL,
    GROK_IMAGE_RESOLUTION,
    GROK_REASONING_EFFORT,
    GROK_TEXT_MODEL,
    IMAGE_FALLBACK_PROVIDER,
    IMAGE_SOURCE_MODE,
    IMAGE_SOURCE_PRIORITY,
    OPENAI_IMAGE_FALLBACK,
    PRIMARY_IMAGE_PROVIDER,
    PRIMARY_LLM_PROVIDER,
    STOCK_IMAGE_REUSE_CHECK_WP_HISTORY,
    STOCK_IMAGE_REUSE_WINDOW_DAYS,
    STOCK_IMAGE_USAGE_PATH,
    USE_SOURCE_IMAGES,
    get_grok_reasoning_effort,
)


def _env_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _provider_label(provider: str) -> str:
    provider = (provider or "").strip().lower()
    if provider == "grok":
        return "grok-image"
    if provider == "openai":
        return "openai"
    return provider or "unknown"


def _expected_image_flow() -> str:
    mode = (IMAGE_SOURCE_MODE or "hybrid").strip().lower()
    priority = (IMAGE_SOURCE_PRIORITY or "stock_first").strip().lower()
    primary = _provider_label(PRIMARY_IMAGE_PROVIDER)
    fallback = _provider_label(IMAGE_FALLBACK_PROVIDER)
    openai_fallback = OPENAI_IMAGE_FALLBACK and fallback == "openai"

    if mode == "openai" or priority == "openai_only":
        return "openai"
    if priority == "grok_only":
        flow = ["grok-image"]
        if openai_fallback:
            flow.append("openai")
        return " -> ".join(flow)

    generated = [primary]
    if openai_fallback and primary != "openai":
        generated.append("openai")

    if priority == "source_first" and USE_SOURCE_IMAGES:
        return " -> ".join(["source", "pexels", "pixabay", *generated])
    if priority == "stock_first" and USE_SOURCE_IMAGES:
        return " -> ".join(["pexels", "pixabay", primary, "source", *generated[1:]])
    return " -> ".join(["pexels", "pixabay", *generated])


def _run_grok_text_smoke() -> None:
    if not _env_enabled("RUN_GROK_TEXT_SMOKE"):
        print("grok-text-smoke=skipped set RUN_GROK_TEXT_SMOKE=true to run a tiny live text call")
        return

    if not GROK_API_KEY:
        print("grok-text-smoke=skipped GROK_API_KEY missing")
        return

    client = OpenAI(api_key=GROK_API_KEY, base_url=GROK_BASE_URL, timeout=45)
    payload = {
        "model": GROK_TEXT_MODEL,
        "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
        "max_completion_tokens": 32,
    }
    payload["reasoning_effort"] = get_grok_reasoning_effort("rewrite")

    response = client.chat.completions.create(**payload)
    content = (response.choices[0].message.content or "").strip()
    print(f"grok-text-smoke=ok chars={len(content)}")


def _run_grok_image_smoke() -> None:
    if not _env_enabled("RUN_GROK_IMAGE_SMOKE"):
        print("grok-image-smoke=skipped set RUN_GROK_IMAGE_SMOKE=true to run a live image call")
        return

    if not GROK_API_KEY:
        print("grok-image-smoke=skipped GROK_API_KEY missing")
        return

    client = OpenAI(api_key=GROK_API_KEY, base_url=GROK_BASE_URL, timeout=120)
    payload = {
        "model": GROK_IMAGE_MODEL,
        "prompt": "Abstract blockchain market chart, no text, no logos, 16:9 editorial hero image.",
        "n": 1,
        "extra_body": {
            "aspect_ratio": GROK_IMAGE_ASPECT_RATIO,
            "resolution": GROK_IMAGE_RESOLUTION,
        },
    }
    response = client.images.generate(**payload)
    first = response.data[0]
    has_image = bool(getattr(first, "b64_json", None) or getattr(first, "url", None))
    print(f"grok-image-smoke=ok has_image={_bool_text(has_image)}")


def main() -> None:
    print(f"primary-llm={PRIMARY_LLM_PROVIDER}")
    print(f"grok-text-model={GROK_TEXT_MODEL}")
    print(f"grok-reasoning-effort={GROK_REASONING_EFFORT or 'unset'}")
    print(f"grok-reasoning-effort-default={get_grok_reasoning_effort()}")
    print(f"grok-reasoning-effort-rewrite={get_grok_reasoning_effort('rewrite')}")
    print(f"grok-reasoning-effort-expansion={get_grok_reasoning_effort('expansion')}")
    print(f"grok-reasoning-effort-repair={get_grok_reasoning_effort('repair')}")
    print(f"grok-reasoning-effort-scoring={get_grok_reasoning_effort('scoring')}")
    print(f"primary-image-provider={PRIMARY_IMAGE_PROVIDER}")
    print(f"image-source-mode={IMAGE_SOURCE_MODE}")
    print(f"image-source-priority={IMAGE_SOURCE_PRIORITY}")
    print(f"use-source-images={_bool_text(USE_SOURCE_IMAGES)}")
    print(f"image-fallback-provider={IMAGE_FALLBACK_PROVIDER}")
    print(f"openai-image-fallback={_bool_text(OPENAI_IMAGE_FALLBACK)}")
    print(f"expected-image-flow={_expected_image_flow()}")
    print(f"stock-image-reuse-window-days={STOCK_IMAGE_REUSE_WINDOW_DAYS}")
    print(f"stock-image-usage-path={STOCK_IMAGE_USAGE_PATH}")
    print(f"stock-image-reuse-check-wp-history={_bool_text(STOCK_IMAGE_REUSE_CHECK_WP_HISTORY)}")

    _run_grok_text_smoke()
    _run_grok_image_smoke()


if __name__ == "__main__":
    main()
