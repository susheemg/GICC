"""LLM provider registry and credential storage.

Six providers, one interface. Keys are encrypted at rest with a key derived
from SECRET_KEY, and never returned to the browser after they are saved - the
UI shows a prefix and a fingerprint so an operator can tell which key is
installed without being able to read it back.

Honest about the limit: encryption at rest protects against someone reading the
database file or a backup. It does not protect against someone who has both the
database and SECRET_KEY, because the application must be able to decrypt to
make a call. A real deployment puts these in a KMS or a secrets manager and
stores only a reference here.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..config import SECRET_KEY
from ..models import AiProviderConfig, now
from . import audit

# --------------------------------------------------------------------------
# Provider registry
# --------------------------------------------------------------------------
# Each entry describes how to authenticate, what a chat request looks like, and
# how to pull the text back out. Adding a provider is one dict, not a new code
# path through the application.
PROVIDERS: dict[str, dict] = {
    "anthropic": {
        "label": "Anthropic",
        "default_base_url": "https://api.anthropic.com",
        "path": "/v1/messages",
        "default_model": "claude-sonnet-4-6",
        "models": ["claude-opus-4-5", "claude-sonnet-4-6", "claude-haiku-4-5"],
        "auth": "x-api-key",
        "key_hint": "sk-ant-…",
        "docs": "https://docs.claude.com",
        "notes": "System prompt is a top-level field, not a message.",
    },
    "openai": {
        "label": "OpenAI",
        "default_base_url": "https://api.openai.com",
        "path": "/v1/chat/completions",
        "default_model": "gpt-4o",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1"],
        "auth": "bearer",
        "key_hint": "sk-…",
        "docs": "https://platform.openai.com/docs",
        "notes": "System prompt is the first message.",
    },
    "azure_openai": {
        "label": "Azure OpenAI",
        "default_base_url": "https://YOUR-RESOURCE.openai.azure.com",
        "path": "/openai/deployments/{model}/chat/completions?api-version=2024-10-21",
        "default_model": "",
        "models": [],
        "auth": "api-key",
        "key_hint": "32-character resource key",
        "docs": "https://learn.microsoft.com/azure/ai-services/openai/",
        "notes": "Model is your deployment name, and the base URL is your resource endpoint.",
    },
    "google": {
        "label": "Google Gemini",
        "default_base_url": "https://generativelanguage.googleapis.com",
        "path": "/v1beta/models/{model}:generateContent",
        "default_model": "gemini-2.0-flash",
        "models": ["gemini-2.0-flash", "gemini-1.5-pro"],
        "auth": "query",
        "key_hint": "AIza…",
        "docs": "https://ai.google.dev/docs",
        "notes": "Key travels as a query parameter, so avoid logging full URLs.",
    },
    "mistral": {
        "label": "Mistral",
        "default_base_url": "https://api.mistral.ai",
        "path": "/v1/chat/completions",
        "default_model": "mistral-large-latest",
        "models": ["mistral-large-latest", "mistral-small-latest"],
        "auth": "bearer",
        "key_hint": "…",
        "docs": "https://docs.mistral.ai",
        "notes": "OpenAI-compatible request shape.",
    },
    "openai_compatible": {
        "label": "OpenAI-compatible endpoint",
        "default_base_url": "http://localhost:11434",
        "path": "/v1/chat/completions",
        "default_model": "llama3.1",
        "models": [],
        "auth": "bearer",
        "key_hint": "often blank for a local server",
        "docs": "",
        "notes": ("Ollama, vLLM, LM Studio, Together, Groq, OpenRouter and anything else "
                  "serving the OpenAI chat schema. A self-hosted endpoint keeps document "
                  "text inside your own boundary, which removes the subprocessor question "
                  "entirely."),
    },
}


def provider_meta(key: str) -> dict:
    return PROVIDERS.get(key, {"label": key, "default_base_url": "", "path": "",
                               "default_model": "", "models": [], "auth": "bearer",
                               "key_hint": "", "docs": "", "notes": ""})


# --------------------------------------------------------------------------
# Encryption at rest
# --------------------------------------------------------------------------
def _cipher() -> Fernet:
    """Derives a Fernet key from SECRET_KEY. Rotating SECRET_KEY makes every
    stored credential unreadable - which is stated on the admin page, because
    silently losing keys on a redeploy would be worse."""
    digest = hashlib.sha256(f"gicc-provider-keys:{SECRET_KEY}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(secret: str) -> str:
    return _cipher().encrypt(secret.encode()).decode()


def decrypt(blob: str) -> str | None:
    try:
        return _cipher().decrypt(blob.encode()).decode()
    except (InvalidToken, ValueError, AttributeError):
        return None


def fingerprint(secret: str) -> str:
    """Lets an operator confirm which key is installed without reading it."""
    return hashlib.sha256(secret.encode()).hexdigest()[:12]


def mask(secret: str) -> str:
    if len(secret) <= 10:
        return "…" * 4
    return f"{secret[:6]}…{secret[-4:]}"


# --------------------------------------------------------------------------
# Configuration access
# --------------------------------------------------------------------------
def active(db: Session) -> AiProviderConfig | None:
    """The enabled provider with the lowest priority number wins. One active
    provider at a time: silent failover between providers would make an
    inference log entry ambiguous about which model produced a proposal."""
    return db.scalar(select(AiProviderConfig).where(
        AiProviderConfig.enabled.is_(True))
        .order_by(AiProviderConfig.priority, AiProviderConfig.created_at).limit(1))


def all_configs(db: Session) -> list[AiProviderConfig]:
    return list(db.scalars(select(AiProviderConfig)
                           .order_by(AiProviderConfig.priority,
                                     AiProviderConfig.created_at)).all())


def credentials(config: AiProviderConfig) -> tuple[str | None, str]:
    """Returns (api_key, error). A key that will not decrypt is reported rather
    than silently treated as absent."""
    if not config.api_key_encrypted:
        return "", ""
    key = decrypt(config.api_key_encrypted)
    if key is None:
        return None, ("The stored key could not be decrypted. SECRET_KEY has changed "
                      "since it was saved — re-enter the key.")
    return key, ""


# --------------------------------------------------------------------------
# Request construction, per provider
# --------------------------------------------------------------------------
def build_request(config: AiProviderConfig, system: str, prompt: str,
                  max_tokens: int = 1500) -> tuple[str, dict, dict] | tuple[None, None, None]:
    """Returns (url, headers, body) shaped for this provider."""
    meta = provider_meta(config.provider)
    key, error = credentials(config)
    if key is None:
        return None, None, None

    base = (config.base_url or meta["default_base_url"]).rstrip("/")
    path = meta["path"].replace("{model}", config.model or meta["default_model"])
    url = f"{base}{path}"
    headers = {"content-type": "application/json"}

    if meta["auth"] == "x-api-key":
        headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
        body = {"model": config.model, "max_tokens": max_tokens, "temperature": 0,
                "system": system,
                "messages": [{"role": "user", "content": prompt}]}
    elif meta["auth"] == "api-key":
        headers["api-key"] = key
        body = {"max_tokens": max_tokens, "temperature": 0,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": prompt}]}
    elif meta["auth"] == "query":
        url = f"{url}?key={key}"
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens}}
    else:  # bearer
        if key:
            headers["authorization"] = f"Bearer {key}"
        body = {"model": config.model, "max_tokens": max_tokens,
                "max_completion_tokens": max_tokens, "temperature": 0,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": prompt}]}
    return url, headers, body


def extract_text(config: AiProviderConfig, payload: dict) -> str:
    """Pulls the assistant text out of whichever envelope the provider used."""
    meta = provider_meta(config.provider)
    try:
        if meta["auth"] == "x-api-key":
            return "".join(block.get("text", "") for block in payload.get("content", [])
                           if block.get("type") == "text")
        if meta["auth"] == "query":
            candidates = payload.get("candidates", [])
            if not candidates:
                return ""
            return "".join(part.get("text", "")
                           for part in candidates[0].get("content", {}).get("parts", []))
        choices = payload.get("choices", [])
        return choices[0].get("message", {}).get("content", "") if choices else ""
    except (KeyError, IndexError, AttributeError, TypeError):
        return ""


def token_usage(config: AiProviderConfig, payload: dict) -> tuple[int, int]:
    usage = payload.get("usage") or payload.get("usageMetadata") or {}
    return (int(usage.get("input_tokens") or usage.get("prompt_tokens")
                or usage.get("promptTokenCount") or 0),
            int(usage.get("output_tokens") or usage.get("completion_tokens")
                or usage.get("candidatesTokenCount") or 0))


# --------------------------------------------------------------------------
# Connection test
# --------------------------------------------------------------------------
def test_connection(db: Session, config: AiProviderConfig, actor=None) -> dict:
    """Makes a real minimal call. A configuration that has never been proved
    against the live endpoint is a guess, and finding out during a document
    upload is the wrong time."""
    started = time.time()
    url, headers, body = build_request(
        config, "Reply with the single word OK and nothing else.",
        "Connection test.", max_tokens=16)

    if url is None:
        result = {"ok": False, "detail": "The stored key could not be decrypted. "
                                         "SECRET_KEY has changed — re-enter it."}
    else:
        try:
            import httpx
            with httpx.Client(timeout=20.0) as client:
                response = client.post(url, headers=headers, json=body)
            if response.status_code == 200:
                text = extract_text(config, response.json()).strip()
                result = {"ok": True,
                          "detail": f"Responded in {int((time.time() - started) * 1000)} ms"
                                    + (f": “{text[:40]}”" if text else
                                       " but returned no text — check the model name.")}
            elif response.status_code in (401, 403):
                result = {"ok": False, "detail": f"Rejected the key ({response.status_code}). "
                                                 "Check the key and that it has access to "
                                                 "this model."}
            elif response.status_code == 404:
                result = {"ok": False, "detail": "Endpoint not found (404). Check the base "
                                                 "URL and the model or deployment name."}
            elif response.status_code == 429:
                result = {"ok": False, "detail": "Rate limited (429). The credentials work; "
                                                 "the account is throttled."}
            else:
                detail = (response.text or "")[:180]
                result = {"ok": False,
                          "detail": f"Returned {response.status_code}. {detail}"}
        except Exception as exc:
            result = {"ok": False,
                      "detail": f"Could not reach the endpoint: {type(exc).__name__}"}

    config.last_tested_at = now()
    config.last_test_ok = result["ok"]
    config.last_test_detail = result["detail"][:400]
    config.latency_ms = int((time.time() - started) * 1000)
    db.flush()

    audit.record(db, tenant_id="platform", action="ai_provider.tested",
                 actor_kind="platform_admin", actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "operator"),
                 object_type="ai_provider_config", object_id=config.id,
                 payload={"provider": config.provider, "model": config.model,
                          "ok": result["ok"], "detail": result["detail"][:200]})
    return result


def status_summary(db: Session) -> dict:
    config = active(db)
    if config is None:
        return {"enabled": False, "provider": "none", "model": None,
                "fallback": "deterministic heuristics",
                "reason": "No provider is enabled"}
    key, error = credentials(config)
    if key is None:
        return {"enabled": False, "provider": config.provider, "model": config.model,
                "fallback": "deterministic heuristics", "reason": error}
    meta = provider_meta(config.provider)
    if meta["auth"] != "bearer" and not key:
        return {"enabled": False, "provider": config.provider, "model": config.model,
                "fallback": "deterministic heuristics",
                "reason": "No API key stored for this provider"}
    return {"enabled": True, "provider": config.provider, "label": meta["label"],
            "model": config.model, "fallback": "deterministic heuristics",
            "reason": None, "config_id": config.id}
