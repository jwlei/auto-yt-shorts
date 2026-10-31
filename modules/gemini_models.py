import os

# Tried in order; each has its own free-tier daily quota (20/day for the Flash
# models, 500/day for 3.5 Flash Lite), so a used-up model just hands over to the
# next. Override with GEMINI_MODEL (one model or a comma-separated chain).
DEFAULT_MODELS = ("gemini-3.8-flash", "gemini-3.5-flash", "gemini-3-flash-preview",
                  "gemini-3.5-flash-lite")

# Small yes/no decisions (is this topic a repeat?) don't need the big models or
# their 20/day quota. Override with GEMINI_FAST_MODEL.
DEFAULT_FAST_MODELS = ("gemini-3.5-flash-lite", "gemini-3.5-flash")

# Tried in order. Both share the voice library, so a fallback keeps the same voice.
# Override with GEMINI_TTS_MODEL (one model or a comma-separated chain).
DEFAULT_TTS_MODELS = ("gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts")

# Tried in order, for thumbnails. Override with GEMINI_IMAGE_MODEL (one model or a
# comma-separated chain).
DEFAULT_IMAGE_MODELS = ("gemini-3.1-flash-image", "gemini-2.5-flash-image")


def _get_models(env_var, defaults):
    # An unset CI variable arrives as an empty string, not as None.
    configured = os.getenv(env_var)
    if configured:
        return [m.strip() for m in configured.split(",") if m.strip()]
    return list(defaults)


def get_text_models():
    return _get_models("GEMINI_MODEL", DEFAULT_MODELS)


def get_fast_models():
    return _get_models("GEMINI_FAST_MODEL", DEFAULT_FAST_MODELS)


def get_tts_models():
    return _get_models("GEMINI_TTS_MODEL", DEFAULT_TTS_MODELS)


def get_image_models():
    return _get_models("GEMINI_IMAGE_MODEL", DEFAULT_IMAGE_MODELS)
