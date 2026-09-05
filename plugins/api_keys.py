"""
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
