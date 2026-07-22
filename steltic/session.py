"""Single-user, in-memory LLM-credential store.

Steltic runs locally for one user: no login, no accounts. The LLM base-url + API key entered in
Settings are held ONLY in process memory -- never written to disk, gone when the app stops.
Bind to 127.0.0.1 (the default); put a reverse proxy with real auth in front before ever
exposing the port beyond your machine.
"""
import time

_CREDS: dict | None = None   # {"base_url":..., "api_key":..., "model":..., ...}  (memory only)


def set_creds(base_url: str, api_key: str, model: str, reasoning: str = "high",
              max_tokens: int = 32000, provider: str = ""):
    global _CREDS
    _CREDS = {"base_url": base_url.rstrip("/"), "api_key": api_key, "model": model,
              "reasoning": reasoning, "max_tokens": int(max_tokens), "provider": provider.strip(),
              "ts": int(time.time())}


def get_creds() -> dict | None:
    return _CREDS


def clear_creds():
    global _CREDS
    _CREDS = None
