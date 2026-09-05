"""
Adds generic multi-provider API key support:
1. core/multi_provider.py       - new file, provider detection + fallback calls
2. plugins/api_keys.py          - new file, lets you add a key by talking to JARVIS
3. actions/code_helper.py       - patched to use fallback on Gemini failure
4. actions/dev_agent.py         - patched to use fallback on Gemini failure

Safe to run again - it skips anything already applied.
"""
import sys
from pathlib import Path

MULTI_PROVIDER_SRC = r'''"""
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
'''
PLUGIN_SRC = r'''"""
Lets you add API keys for other providers (Claude/Anthropic, OpenAI,
NVIDIA) just by telling JARVIS, e.g. "Add this API key: sk-ant-...".
JARVIS detects the provider from the key's own prefix and saves it as a
fallback - so if the main Gemini key ever hits its quota on a text-based
tool (code helper, dev agent), JARVIS automatically retries with one of
these instead of just failing.

No other file needs to change - JARVIS discovers this automatically at
startup, same as any other plugin.
"""
import sys
from pathlib import Path

# Make sure the app's own "core" package is importable from here, same way
# actions/*.py already do it.
_BASE_DIR = Path(__file__).resolve().parent.parent
if str(_BASE_DIR) not in sys.path:
    sys.path.insert(0, str(_BASE_DIR))

from core import multi_provider  # noqa: E402

PLUGIN = {
    "name": "add_api_key",
    "description": (
        "Saves an API key for another AI provider (Claude/Anthropic, OpenAI, "
        "or NVIDIA) as a fallback for JARVIS's text-based tools (code helper, "
        "dev agent). Call this whenever the user gives you a raw API key and "
        "asks to add, save, link, or register it - phrases like 'add this "
        "API key', 'save my Claude key', 'link my OpenAI key', 'add api key "
        "sk-ant-...'. Do NOT use this for the main Gemini key (that one goes "
        "in setup, not here) - only for extra/backup provider keys."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "api_key": {
                "type": "STRING",
                "description": "The raw API key the user gave you, exactly as they typed it.",
            },
        },
        "required": ["api_key"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    api_key = (parameters.get("api_key") or "").strip()
    if not api_key:
        return "Sir, I didn't catch an actual key to save - please give me the key itself."

    try:
        result = multi_provider.add_api_key(api_key)
    except Exception as e:
        return f"Sir, I couldn't save that key: {e}"

    if not result.get("ok"):
        return (
            "Sir, I couldn't tell which provider that key belongs to from its "
            "prefix, so I didn't save it. Currently I recognize Anthropic "
            "(sk-ant-...), OpenAI (sk-...), and NVIDIA (nvapi-...) keys."
        )

    provider = result.get("provider")
    msg = f"Saved sir - your {provider} key is now a fallback for my text tools."
    if player:
        try:
            player.write_log(f"JARVIS: {msg}")
        except Exception:
            pass
    return msg
'''

# 1. core/multi_provider.py
p1 = Path("core/multi_provider.py")
if p1.exists():
    print("core/multi_provider.py already exists - leaving it as is.")
else:
    p1.write_text(MULTI_PROVIDER_SRC, encoding="utf-8")
    print("Created core/multi_provider.py")

# 2. plugins/api_keys.py
p2 = Path("plugins/api_keys.py")
if p2.exists():
    print("plugins/api_keys.py already exists - leaving it as is.")
else:
    p2.write_text(PLUGIN_SRC, encoding="utf-8")
    print("Created plugins/api_keys.py")

# 3. actions/code_helper.py
p3 = Path("actions/code_helper.py")
c3 = p3.read_text(encoding="utf-8")
if "multi_provider" in c3:
    print("actions/code_helper.py already patched.")
else:
    old = '''    class _W:
        def generate_content(self, contents):
            return _c.models.generate_content(model=model, contents=contents)

    return _W()'''
    new = '''    class _W:
        def generate_content(self, contents):
            try:
                return _c.models.generate_content(model=model, contents=contents)
            except Exception as e:
                from core import multi_provider
                return multi_provider.generate_with_fallback(contents, e)

    return _W()'''
    if c3.count(old) != 1:
        print("ERROR: anchor not found/not unique in code_helper.py - skipped.")
    else:
        p3.write_text(c3.replace(old, new, 1), encoding="utf-8")
        print("Patched actions/code_helper.py")

# 4. actions/dev_agent.py
p4 = Path("actions/dev_agent.py")
c4 = p4.read_text(encoding="utf-8")
if "multi_provider" in c4:
    print("actions/dev_agent.py already patched.")
else:
    old = '''    class _W:
        def generate_content(self, contents):
            return _c.models.generate_content(model=model_name, contents=contents)

    return _W()'''
    new = '''    class _W:
        def generate_content(self, contents):
            try:
                return _c.models.generate_content(model=model_name, contents=contents)
            except Exception as e:
                from core import multi_provider
                return multi_provider.generate_with_fallback(contents, e)

    return _W()'''
    if c4.count(old) != 1:
        print("ERROR: anchor not found/not unique in dev_agent.py - skipped.")
    else:
        p4.write_text(c4.replace(old, new, 1), encoding="utf-8")
        print("Patched actions/dev_agent.py")

print("Done. Restart JARVIS: python main.py")
