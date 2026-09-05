"""
Generic multi-provider API key support.

Add a key for another provider (Claude/Anthropic, OpenAI, NVIDIA) from the
dashboard. When the primary Gemini call fails (quota exhausted, rate
limited, etc.), Mark automatically retries the same prompt against each
saved extra key until one succeeds - so one provider running out of quota
doesn't stop the assistant from responding. This module never touches the
Gemini Live voice connection; it only backs the text-based tools that call
it explicitly.
"""
import json
import re
import sys
from pathlib import Path

import requests


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

# Recognized key prefixes, checked in order. First match wins.
_PROVIDER_PATTERNS = [
    ("anthropic", re.compile(r"^sk-ant-")),
    ("nvidia", re.compile(r"^nvapi-")),
    ("openai", re.compile(r"^sk-proj-|^sk-[A-Za-z0-9]{20,}$")),
]


def detect_provider(key: str) -> str:
    key = (key or "").strip()
    for name, pattern in _PROVIDER_PATTERNS:
        if pattern.match(key):
            return name
    return "unknown"


def _load_config() -> dict:
    if not API_CONFIG_PATH.exists():
        return {}
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_config(cfg: dict) -> None:
    API_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(API_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def add_api_key(raw_key: str) -> dict:
    """Detects the provider from the key's prefix and saves it as a fallback
    key. Returns {"ok": bool, "provider": str|None, "message": str}."""
    raw_key = (raw_key or "").strip()
    if not raw_key:
        return {"ok": False, "provider": None, "message": "Empty key."}

    provider = detect_provider(raw_key)
    if provider == "unknown":
        return {
            "ok": False,
            "provider": None,
            "message": "Could not recognize this key's provider from its prefix.",
        }

    cfg = _load_config()
    extra = cfg.get("extra_api_keys", [])
    extra = [k for k in extra if k.get("provider") != provider]
    extra.append({"provider": provider, "key": raw_key})
    cfg["extra_api_keys"] = extra
    _save_config(cfg)
    return {"ok": True, "provider": provider, "message": "Saved as a %s fallback key." % provider}


def list_api_keys() -> list:
    """Saved fallback keys with the key itself masked, for display."""
    cfg = _load_config()
    out = []
    for entry in cfg.get("extra_api_keys", []):
        k = entry.get("key", "")
        masked = (k[:6] + "..." + k[-4:]) if len(k) > 12 else "***"
        out.append({"provider": entry.get("provider"), "key_masked": masked})
    return out


def remove_api_key(provider: str) -> bool:
    cfg = _load_config()
    extra = cfg.get("extra_api_keys", [])
    new_extra = [k for k in extra if k.get("provider") != provider]
    changed = len(new_extra) != len(extra)
    if changed:
        cfg["extra_api_keys"] = new_extra
        _save_config(cfg)
    return changed


class _TextResult:
    """Stand-in for the google-genai response object so existing code that
    reads `.text` off the result does not need to change."""
    def __init__(self, text: str):
        self.text = text


def _call_anthropic(key: str, prompt: str) -> str:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-3-5-sonnet-latest",
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "".join(parts)


def _call_openai_compatible(base_url: str, key: str, model: str, prompt: str) -> str:
    resp = requests.post(
        base_url + "/chat/completions",
        headers={
            "Authorization": "Bearer " + key,
            "content-type": "application/json",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _call_openai(key: str, prompt: str) -> str:
    return _call_openai_compatible("https://api.openai.com/v1", key, "gpt-4o-mini", prompt)


def _call_nvidia(key: str, prompt: str) -> str:
    return _call_openai_compatible(
        "https://integrate.api.nvidia.com/v1", key, "meta/llama-3.1-8b-instruct", prompt
    )


_CALLERS = {
    "anthropic": _call_anthropic,
    "openai": _call_openai,
    "nvidia": _call_nvidia,
}


def _contents_to_prompt(contents) -> str:
    if isinstance(contents, str):
        return contents
    try:
        return "\\n".join(str(c) for c in contents)
    except Exception:
        return str(contents)


def generate_with_fallback(contents, primary_error: Exception):
    """Call after the primary Gemini call has already failed. Tries each
    saved fallback key in turn and returns the first successful response as
    a `.text`-bearing object. Re-raises the original Gemini error if every
    fallback also fails, so the caller's existing error handling still
    applies unchanged."""
    cfg = _load_config()
    extra = cfg.get("extra_api_keys", [])
    if not extra:
        raise primary_error

    prompt = _contents_to_prompt(contents)
    last_err = primary_error
    for entry in extra:
        provider = entry.get("provider")
        key = entry.get("key")
        caller = _CALLERS.get(provider)
        if not caller or not key:
            continue
        try:
            text = caller(key, prompt)
            return _TextResult(text)
        except Exception as e:
            last_err = e
            continue

    raise last_err
