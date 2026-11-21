"""OpenRouter client for disc mapping and the three capped disc decisions.

The recovery ladder, speeds, retries, filenames, and NAS copy stay in code.
Each call returns strict JSON. Anything outside that schema, including a
missing ``OPENROUTER_API_KEY``, is ``stop``. This module does not rip and
does not start a shell.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ripknife.identify import is_generic_disc_name
from ripknife.skills import load_skill

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
# Cheap model (OpenRouter catalog, 2026-11-20): strict JSON, and images when
# a volume label is a placeholder. Not a reasoning model. Every call defaults
# to this id. A per-call env var overrides it; RIPKNIFE_LLM_MODEL overrides
# whichever calls do not set their own.
DEFAULT_MODEL = "google/gemini-2.5-flash-lite"
ENV_API_KEY = "OPENROUTER_API_KEY"
ENV_MODEL = "RIPKNIFE_LLM_MODEL"
ENV_MAP_MODEL = "RIPKNIFE_LLM_MAP_MODEL"
ENV_FAILURE_MODEL = "RIPKNIFE_LLM_FAILURE_MODEL"
ENV_GAP_MODEL = "RIPKNIFE_LLM_GAP_MODEL"
ENV_VERIFY_MODEL = "RIPKNIFE_LLM_VERIFY_MODEL"
ENV_VISION_MODEL = "RIPKNIFE_LLM_VISION_MODEL"
STOP = "stop"
HTTP_TIMEOUT_SEC = 20
MAP_MAX_TOKENS = 2048
MAP_ACTIONS = ("write", "stop", "ambiguous")

_CALL_MODEL_ENV = {
    "map": ENV_MAP_MODEL,
    "failure": ENV_FAILURE_MODEL,
    "gap": ENV_GAP_MODEL,
    "verify": ENV_VERIFY_MODEL,
    "vision": ENV_VISION_MODEL,
}

FAILURE_ACTIONS = (
    "bad_sectors",
    "wrong_title",
    "empty_slot",
    "ambiguous_map",
    "tooling",
    STOP,
)
GAP_ACTIONS = (
    "another_disc",
    "another_drive",
    "accept_gap",
    STOP,
)
VERIFY_ACTIONS = (
    "keep",
    "discard",
    STOP,
)

_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
_PLACEHOLDER_COMPACT = frozenset({"UNKOWN"})
Opener = Callable[..., Any]


def label_needs_jacket(volume_label: str | None) -> bool:
    """True when the label is a placeholder and jacket art may be sent.

    ``UNKNOWN`` is already generic. ``UNKOWN`` is the one-letter typo MakeMKV
    stamped on a disc whose bdmt name was also ``UNKNOWN``.
    """
    if is_generic_disc_name(volume_label):
        return True
    compact = re.sub(r"[^A-Za-z0-9]", "", volume_label or "").upper()
    return compact in _PLACEHOLDER_COMPACT


def resolve_model(env: Mapping[str, str] | None = None, call: str | None = None) -> str:
    """Model id for one call.

    Per-call env wins, then ``RIPKNIFE_LLM_MODEL``, then the cheap default.
    """
    source = os.environ if env is None else env
    if call:
        specific = _CALL_MODEL_ENV.get(call)
        if specific:
            chosen = str(source.get(specific) or "").strip()
            if chosen:
                return chosen
    chosen = str(source.get(ENV_MODEL) or "").strip()
    return chosen or DEFAULT_MODEL


def classify_disc_failure(
    text: str,
    *,
    volume_label: str | None = None,
    jacket: bytes | None = None,
    jacket_media_type: str = "image/jpeg",
    env: Mapping[str, str] | None = None,
    opener: Opener | None = None,
) -> str:
    """Bad sectors, wrong title, empty slot, ambiguous map, or tooling.

    Jacket bytes are attached only when ``volume_label`` is a placeholder.
    """
    image = jacket if label_needs_jacket(volume_label) else None
    media = jacket_media_type if image else None
    label = volume_label if volume_label else "(none)"
    prompt = (
        load_skill("failure").rstrip()
        + f"\n\nAllowed: {', '.join(FAILURE_ACTIONS)}.\n"
        + f"Volume label: {label}\n"
        + f"Log:\n{text}"
    )
    return _decide(
        prompt,
        FAILURE_ACTIONS,
        call="vision" if image else "failure",
        image=image,
        image_media_type=media,
        env=env,
        opener=opener,
    )


def choose_gap_action(
    text: str,
    *,
    env: Mapping[str, str] | None = None,
    opener: Opener | None = None,
) -> str:
    """Another owned disc, a different-brand drive, or accept the gap."""
    prompt = (
        load_skill("gap").rstrip()
        + f"\n\nAllowed: {', '.join(GAP_ACTIONS)}.\n"
        + f"Situation:\n{text}"
    )
    return _decide(prompt, GAP_ACTIONS, call="gap", env=env, opener=opener)


def choose_verify_action(
    text: str,
    *,
    env: Mapping[str, str] | None = None,
    opener: Opener | None = None,
) -> str:
    """Keep a failed verify for review, or discard it. Never a NAS copy."""
    prompt = (
        load_skill("verify").rstrip()
        + f"\n\nAllowed: {', '.join(VERIFY_ACTIONS)}.\n"
        + f"Situation:\n{text}"
    )
    return _decide(prompt, VERIFY_ACTIONS, call="verify", env=env, opener=opener)


def _api_key(env: Mapping[str, str] | None) -> str:
    source = os.environ if env is None else env
    return str(source.get(ENV_API_KEY) or "").strip()


@dataclass(frozen=True)
class MapReply:
    """Parsed map response. ``failure`` is set when nothing should be written."""

    action: str
    manifest: dict[str, Any] | None = None
    failure: str | None = None


@dataclass(frozen=True)
class _HttpResult:
    payload: dict[str, Any] | None = None
    failure: str | None = None


def _schema(actions: tuple[str, ...]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "disc_action",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {"type": "string", "enum": list(actions)},
                },
                "required": ["action"],
            },
        },
    }


def _content(
    prompt: str,
    *,
    image: bytes | None,
    image_media_type: str | None,
) -> str | list[dict[str, Any]]:
    if not image or image_media_type not in _IMAGE_TYPES:
        return prompt
    encoded = base64.standard_b64encode(image).decode("ascii")
    return [
        {"type": "text", "text": prompt},
        {
            "type": "image_url",
            "image_url": {"url": f"data:{image_media_type};base64,{encoded}"},
        },
    ]


def _map_schema() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "disc_manifest",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {"type": "string", "enum": list(MAP_ACTIONS)},
                    "manifest": {"type": ["object", "null"]},
                },
                "required": ["action", "manifest"],
            },
        },
    }


def request_map(
    prompt: str,
    *,
    env: Mapping[str, str] | None = None,
    opener: Opener | None = None,
    timeout: float = HTTP_TIMEOUT_SEC,
) -> MapReply:
    """Ask for one manifest JSON object. Missing key does not call out."""
    key = _api_key(env)
    if not key:
        return MapReply(STOP, None, "auth")
    body = {
        "model": resolve_model(env, call="map"),
        "temperature": 0,
        "max_tokens": MAP_MAX_TOKENS,
        "reasoning": {"effort": "none"},
        "response_format": _map_schema(),
        "messages": [{"role": "user", "content": prompt}],
    }
    result = _post(body, api_key=key, opener=opener, timeout=timeout)
    if result.payload is None:
        return MapReply(STOP, None, result.failure or "transport")
    return _parse_map(_message_text(result.payload))


def _decide(
    prompt: str,
    actions: tuple[str, ...],
    *,
    call: str,
    image: bytes | None = None,
    image_media_type: str | None = None,
    env: Mapping[str, str] | None = None,
    opener: Opener | None = None,
) -> str:
    key = _api_key(env)
    if not key:
        return STOP
    body = {
        "model": resolve_model(env, call=call),
        "temperature": 0,
        "max_tokens": 128,
        "reasoning": {"effort": "none"},
        "response_format": _schema(actions),
        "messages": [
            {
                "role": "user",
                "content": _content(
                    prompt, image=image, image_media_type=image_media_type
                ),
            }
        ],
    }
    result = _post(body, api_key=key, opener=opener, timeout=HTTP_TIMEOUT_SEC)
    if result.payload is None:
        return STOP
    return _parse_action(_message_text(result.payload), frozenset(actions))


def _post(
    body: dict[str, Any],
    *,
    api_key: str,
    opener: Opener | None,
    timeout: float,
) -> _HttpResult:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        OPENROUTER_CHAT_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    open_fn = opener if opener is not None else urllib.request.urlopen
    try:
        with open_fn(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        failure = "auth" if exc.code in (401, 403) else "resource" if exc.code == 429 else "http"
        exc.close()
        return _HttpResult(None, failure)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return _HttpResult(None, "transport")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _HttpResult(None, "invalid")
    if not isinstance(parsed, dict):
        return _HttpResult(None, "invalid")
    return _HttpResult(parsed, None)


def _message_text(payload: dict[str, Any]) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return ""


def _parse_map(text: str) -> MapReply:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return MapReply(STOP, None, "invalid")
    if not isinstance(data, dict):
        return MapReply(STOP, None, "invalid")
    action = data.get("action")
    if not isinstance(action, str) or action not in MAP_ACTIONS:
        return MapReply(STOP, None, "action")
    if action != "write":
        return MapReply(action, None, None)
    manifest = data.get("manifest")
    if not isinstance(manifest, dict):
        return MapReply(STOP, None, "manifest")
    return MapReply("write", manifest, None)


def _parse_action(text: str, allowed: frozenset[str]) -> str:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return STOP
    if not isinstance(data, dict):
        return STOP
    action = data.get("action")
    if not isinstance(action, str) or action not in allowed:
        return STOP
    return action
