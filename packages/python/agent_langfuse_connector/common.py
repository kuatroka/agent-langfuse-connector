#!/usr/bin/env python3
"""Small stdlib-only OTLP exporter shared by local coding-agent hooks."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

KEYCHAIN_SERVICE = "amp-langfuse"
MAX_CHARS = 20_000


def clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_CHARS:
        return value[:MAX_CHARS] + f"… [truncated; original length {len(value)}]"
    return value


def keychain_value(account: str) -> str | None:
    result = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def credentials() -> tuple[str, str, str]:
    public_key = keychain_value("public-key")
    secret_key = keychain_value("secret-key")
    base_url = keychain_value("base-url") or "https://cloud.langfuse.com"
    if not public_key or not secret_key:
        raise RuntimeError("Langfuse credentials are missing from the macOS Keychain")
    return public_key, secret_key, base_url.rstrip("/")


def attr(key: str, value: str) -> dict[str, Any]:
    return {"key": key, "value": {"stringValue": value}}


def json_attr(key: str, value: Any) -> dict[str, Any]:
    return attr(key, json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def array_attr(key: str, values: list[str]) -> dict[str, Any]:
    return {"key": key, "value": {"arrayValue": {"values": [{"stringValue": value} for value in values]}}}


def stable_hex(seed: str, length: int) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:length]


def ns(value: datetime | None = None) -> str:
    moment = value or datetime.now(timezone.utc)
    return str(int(moment.timestamp() * 1_000_000_000))


def parse_time(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def model_fields(raw_model: str | None, display_model: str | None = None) -> tuple[str, str, str, str]:
    import re

    raw = (raw_model or "unknown").strip()
    effort = "unknown"
    effort_source = "not-exposed"
    suffix = re.match(r"^(.*?)[(-](none|minimal|low|medium|high|xhigh|max|ultra)\)?(?:-priority)?$", raw, re.I)
    if suffix:
        base = suffix.group(1).rstrip("-(")
        effort = suffix.group(2).lower()
        effort_source = "model-id"
    else:
        base = raw
        display = (display_model or "").lower()
        found = re.search(r"\b(none|minimal|low|medium|high|xhigh|max|ultra)(?:\s+thinking)?\b", display)
        if found:
            effort = found.group(1).lower()
            effort_source = "display-metadata"
    if " " in base and base.lower().startswith(("gemini ", "gpt ", "claude ")):
        base = re.sub(r"[^a-z0-9.]+", "-", base.lower()).strip("-")
        if base.startswith("claude-"):
            base = re.sub(r"(?<=\d)\.(?=\d)", "-", base)
    base = re.sub(r"^gpt-(\d+)-(\d+)(?=-|$)", r"gpt-\1.\2", base, flags=re.I)
    model = f"{base}({effort})" if effort != "unknown" else base
    return model, base, effort, effort_source


def emit_turn(
    *,
    harness: str,
    session_id: str,
    turn_id: str,
    prompt: str,
    output: str,
    model: str,
    model_base: str,
    model_source: str,
    reasoning_effort: str,
    reasoning_effort_source: str,
    usage: dict[str, int] | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    extra_metadata: dict[str, Any] | None = None,
    turn_number: int | None = None,
) -> str:
    trace_id = stable_hex(f"{harness}:{session_id}:{turn_id}", 32)
    root_id = stable_hex(f"{trace_id}:root", 16)
    generation_id = stable_hex(f"{trace_id}:generation", 16)
    display_name = harness[:1].upper() + harness[1:]
    trace_name = f"{display_name} - Turn {turn_number}" if turn_number is not None else f"{harness} Turn"
    tags = [f"harness:{harness}", f"model:{model}", f"reasoning-effort:{reasoning_effort}"]
    metadata = {
        "harness": harness,
        "model": model,
        "model_base": model_base,
        "model_source": model_source,
        "reasoning_effort": reasoning_effort,
        "reasoning_effort_source": reasoning_effort_source,
        **(extra_metadata or {}),
    }
    if turn_number is not None:
        metadata["turn_number"] = turn_number
    start = ns(start_time)
    end = ns(end_time)
    trace_context = [
        attr("langfuse.trace.name", trace_name),
        attr("langfuse.session.id", session_id),
        array_attr("langfuse.trace.tags", tags),
        *[json_attr(f"langfuse.trace.metadata.{key}", value) for key, value in metadata.items()],
    ]
    root = {
        "traceId": trace_id,
        "spanId": root_id,
        "name": trace_name,
        "kind": 1,
        "startTimeUnixNano": start,
        "endTimeUnixNano": end,
        "attributes": [
            *trace_context,
            attr("langfuse.observation.type", "agent"),
            json_attr("langfuse.observation.input", {"role": "user", "content": clip(prompt)}),
            json_attr("langfuse.observation.output", {"role": "assistant", "content": clip(output)}),
        ],
        "status": {"code": 1},
    }
    generation_attrs = [
        *trace_context,
        attr("langfuse.observation.type", "generation"),
        attr("langfuse.observation.model.name", model_base),
        json_attr("langfuse.observation.input", {"role": "user", "content": clip(prompt)}),
        json_attr("langfuse.observation.output", {"role": "assistant", "content": clip(output)}),
        attr("langfuse.observation.metadata.reasoning_effort", reasoning_effort),
        attr("langfuse.observation.metadata.reasoning_effort_source", reasoning_effort_source),
    ]
    if usage:
        generation_attrs.append(json_attr("langfuse.observation.usage_details", usage))
    generation = {
        "traceId": trace_id,
        "spanId": generation_id,
        "parentSpanId": root_id,
        "name": f"{harness} Generation",
        "kind": 1,
        "startTimeUnixNano": start,
        "endTimeUnixNano": end,
        "attributes": generation_attrs,
        "status": {"code": 1},
    }
    body = {
        "resourceSpans": [{
            "resource": {"attributes": [attr("service.name", f"{harness}-langfuse-hook")]},
            "scopeSpans": [{"scope": {"name": "agent-langfuse-hooks", "version": "1.0.0"}, "spans": [root, generation]}],
        }]
    }
    try:
        import subprocess
        subprocess.run([str(Path.home() / ".local/bin/agent-eval"), "ingest"],
            input=json.dumps({"id": trace_id, "session_id": session_id, "metadata": metadata, "prompt": prompt, "response": output, "occurred_at": start_time.isoformat() if start_time else None}), text=True, capture_output=True, timeout=3, check=True)
    except Exception as exc:
        print(f"AgentEval local capture failed: {type(exc).__name__}", file=__import__("sys").stderr)
    public_key, secret_key, base_url = credentials()
    authorization = base64.b64encode(f"{public_key}:{secret_key}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        f"{base_url}/api/public/otel/v1/traces",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Basic {authorization}",
            "Content-Type": "application/json",
            "x-langfuse-ingestion-version": "4",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"Langfuse returned HTTP {response.status}")
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Langfuse returned HTTP {error.code}") from error
    return trace_id
