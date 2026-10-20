#!/usr/bin/env python3
"""Export the exact completed Devin CLI turn selected by its Stop hook."""

from __future__ import annotations

import fcntl
import json
import sqlite3
import sys
import traceback
from pathlib import Path
from typing import Any

from agent_langfuse_common import emit_turn, model_fields, parse_time

DATA_DIR = Path.home() / ".local" / "share" / "agent-langfuse"
STATE_PATH = DATA_DIR / "devin-state.json"
LOCK_PATH = DATA_DIR / "devin-state.lock"
LOG_PATH = DATA_DIR / "devin.log"
DB_PATH = Path.home() / ".local" / "share" / "devin" / "cli" / "sessions.db"


def log(message: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(message.rstrip() + "\n")


def read_state() -> dict[str, str]:
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(value: dict[str, str]) -> None:
    STATE_PATH.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    payload = json.load(sys.stdin)
    session_id = str(payload.get("session_id") or "")
    prompt_id = str(payload.get("prompt_id") or "")
    if not session_id:
        raise RuntimeError("Devin hook input omitted session_id")

    connection = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    session = connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if session is None:
        raise RuntimeError(f"Devin session not found: {session_id}")
    rows = connection.execute(
        """
        WITH RECURSIVE chain(node_id, parent_node_id, chat_message, depth) AS (
          SELECT node_id, parent_node_id, chat_message, 0
          FROM message_nodes WHERE session_id = ? AND node_id = ?
          UNION ALL
          SELECT parent.node_id, parent.parent_node_id, parent.chat_message, chain.depth + 1
          FROM message_nodes parent JOIN chain ON parent.node_id = chain.parent_node_id
          WHERE parent.session_id = ?
        )
        SELECT node_id, chat_message FROM chain ORDER BY depth DESC
        """,
        (session_id, session["main_chain_id"], session_id),
    ).fetchall()
    messages = []
    for row in rows:
        try:
            message = json.loads(row["chat_message"])
        except (TypeError, json.JSONDecodeError):
            continue
        messages.append(message)
    user_indexes = [
        index for index, message in enumerate(messages)
        if message.get("role") == "user" and message.get("metadata", {}).get("is_user_input") is True
    ]
    if not user_indexes:
        raise RuntimeError("No user-authored turn found on Devin's main chain")
    start_index = user_indexes[-1]
    user = messages[start_index]
    assistants = [message for message in messages[start_index + 1:] if message.get("role") == "assistant"]
    if not assistants:
        raise RuntimeError("No assistant response found after Devin's latest user prompt")
    assistant = assistants[-1]
    assistant_metadata = assistant.get("metadata", {}) if isinstance(assistant.get("metadata"), dict) else {}
    response_dimensions = assistant_metadata.get("response_dimensions", [])
    display_model = None
    for dimension in response_dimensions if isinstance(response_dimensions, list) else []:
        try:
            if dimension.get("uid") == "model":
                display_model = dimension["kind"]["Metric"]["value"]
                break
        except (KeyError, TypeError):
            continue
    raw_model = assistant_metadata.get("generation_model") or session["model"]
    model, model_base, effort, effort_source = model_fields(str(raw_model), str(display_model or ""))
    # Sum metrics across every assistant message in this turn (a turn may span
    # several model calls). Missing/None metric values count as 0.
    def _metric(message: dict[str, Any], key: str) -> int:
        metadata = message.get("metadata", {})
        if not isinstance(metadata, dict):
            return 0
        metrics = metadata.get("metrics", {})
        if not isinstance(metrics, dict):
            return 0
        value = metrics.get(key)
        try:
            return int(value) if value is not None else 0
        except (TypeError, ValueError):
            return 0

    input_tokens = sum(_metric(message, "input_tokens") for message in assistants)
    output_tokens = sum(_metric(message, "output_tokens") for message in assistants)
    cache_read = sum(_metric(message, "cache_read_tokens") for message in assistants)
    cache_creation = sum(_metric(message, "cache_creation_tokens") for message in assistants)
    usage = {
        "input": input_tokens,
        "output": output_tokens,
        "total": input_tokens + output_tokens + cache_read + cache_creation,
        **({"cache_read_input_tokens": cache_read} if cache_read else {}),
        **({"cache_creation_input_tokens": cache_creation} if cache_creation else {}),
    }
    turn_id = prompt_id or str(user.get("message_id") or user.get("uuid") or start_index)
    turn_number = len(user_indexes)
    trace_id = emit_turn(
        harness="devin",
        session_id=session_id,
        turn_id=turn_id,
        prompt=str(user.get("content") or ""),
        output=str(assistant.get("content") or ""),
        model=model,
        model_base=model_base,
        model_source="devin-message",
        reasoning_effort=effort,
        reasoning_effort_source=f"devin-{effort_source}",
        usage=usage,
        start_time=parse_time(assistant_metadata.get("started_generation_at")),
        end_time=parse_time(assistant_metadata.get("created_at")),
        extra_metadata={
            "native_model": str(raw_model),
            "display_model": str(display_model or ""),
            "agent_mode": str(session["agent_mode"]),
            "prompt_id": turn_id,
        },
        turn_number=turn_number,
    )
    state_key = f"{session_id}:{turn_id}"
    state = read_state()
    state[state_key] = trace_id
    write_state(state)
    log(f"exported {state_key} trace={trace_id}")


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            incoming = sys.stdin.read()
            if not incoming.strip():
                raise RuntimeError("empty Devin hook input")
            sys.stdin = __import__("io").StringIO(incoming)
            check = json.loads(incoming)
            key = f"{check.get('session_id', '')}:{check.get('prompt_id', '')}"
            if key not in read_state():
                main()
        except Exception as error:
            log(f"ERROR {type(error).__name__}: {error}\n{traceback.format_exc()}")
    print("{}")
