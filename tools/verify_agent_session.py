#!/usr/bin/env python3
"""Compare native agent sessions with Langfuse Observations API v2 totals."""
from __future__ import annotations

import argparse
import glob
import json
import pathlib
import sqlite3
import subprocess
import sys
from datetime import datetime

from langfuse_v2 import LangfuseV2, default_window, group_traces

HOME = pathlib.Path.home()
EXPECTED_NAMES = {
    "pi": "Pi - Turn {n}",
    "omp": "OMP - Turn {n}",
    "codex": "Codex - Turn {n}",
    "devin": "Devin - Turn {n}",
    "claude": "Claude Code - Turn {n}",
    "droid": "Factory Droid - Turn {n}",
    "amp": "Amp - Turn {n}",
}


def usage_total(usage: dict) -> int:
    if not isinstance(usage, dict):
        return 0
    for key in ("totalTokens", "total_tokens", "total"):
        if isinstance(usage.get(key), (int, float)):
            return int(usage[key])
    return sum(int(usage.get(key) or 0) for key in (
        "input", "output", "cacheRead", "cacheWrite",
        "input_tokens", "output_tokens",
        "cache_read_tokens", "cache_creation_tokens",
        "cache_read_input_tokens", "cache_creation_input_tokens",
        "inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens",
    ))


def parse_pi_like(session_id: str, root: pathlib.Path) -> list[dict]:
    matches = sorted(root.rglob(f"*{session_id}*.jsonl"))
    if not matches:
        raise SystemExit(f"native session not found under {root}")
    path = matches[-1]
    turns: list[dict] = []
    current = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "message":
            continue
        message = entry.get("message") or {}
        role = message.get("role")
        content = message.get("content")
        is_user = role == "user"
        if is_user and isinstance(content, list):
            is_user = any(block.get("type") == "text" for block in content if isinstance(block, dict))
            is_user = is_user and not any(block.get("type") == "tool_result" for block in content if isinstance(block, dict))
        if is_user:
            current = {"turn": len(turns) + 1, "total": 0, "assistants": 0}
            turns.append(current)
        elif role == "assistant" and current is not None:
            current["total"] += usage_total(message.get("usage") or {})
            current["assistants"] += 1
    return [{"turn": t["turn"], "total": t["total"], "path": str(path), "assistants": t["assistants"]} for t in turns]


def parse_codex(session_id: str) -> list[dict]:
    matches = sorted((HOME / ".codex/sessions").rglob(f"*{session_id}*.jsonl"))
    if not matches:
        raise SystemExit(f"Codex rollout not found for {session_id}")
    path = matches[-1]
    turns: dict[str, dict] = {}
    order: list[str] = []
    active_turn = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = entry.get("payload") or {}
        if entry.get("type") == "turn_context" or payload.get("type") in ("task_started", "turn_context"):
            turn_id = payload.get("turn_id") or payload.get("root_turn_id")
            if turn_id:
                active_turn = turn_id
                if turn_id not in turns:
                    turns[turn_id] = {"turn": len(order) + 1, "total": 0, "assistants": 0}
                    order.append(turn_id)
        if payload.get("type") == "token_count" and active_turn:
            last = (payload.get("info") or {}).get("last_token_usage") or {}
            total = last.get("total_tokens")
            if total is None:
                total = int(last.get("input_tokens") or 0) + int(last.get("output_tokens") or 0)
            turns[active_turn]["total"] += int(total or 0)
            turns[active_turn]["assistants"] += 1
    return [{"turn": turns[turn_id]["turn"], "total": turns[turn_id]["total"],
             "path": str(path), "assistants": turns[turn_id]["assistants"]} for turn_id in order]


def parse_devin(session_id: str) -> list[dict]:
    db = HOME / ".local/share/devin/cli/sessions.db"
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    session = connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if session is None:
        raise SystemExit(f"Devin session not found: {session_id}")
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
            messages.append(json.loads(row["chat_message"]))
        except (TypeError, json.JSONDecodeError):
            continue
    turns = []
    current = None
    for message in messages:
        role = message.get("role")
        if role == "user" and (message.get("metadata") or {}).get("is_user_input") is True:
            current = {"turn": len(turns) + 1, "total": 0, "assistants": 0}
            turns.append(current)
        elif role == "assistant" and current is not None:
            metrics = (message.get("metadata") or {}).get("metrics") or {}
            current["total"] += sum(int(metrics.get(k) or 0) for k in (
                "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
            ))
            current["assistants"] += 1
    return [{"turn": t["turn"], "total": t["total"], "path": str(db), "assistants": t["assistants"]} for t in turns]


def parse_claude(session_id: str) -> list[dict]:
    matches = sorted((HOME / ".claude").rglob(f"*{session_id}*.jsonl"))
    if not matches:
        raise SystemExit(f"Claude transcript not found for {session_id}")
    path = matches[-1]
    turns: list[dict] = []
    current = None
    seen_assistant_ids: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = entry.get("type")
        if role == "user":
            content = (entry.get("message") or {}).get("content")
            if isinstance(content, list) and any(
                isinstance(item, dict) and item.get("type") == "tool_result"
                for item in content
            ):
                continue
            current = {"turn": len(turns) + 1, "total": 0, "assistants": 0}
            turns.append(current)
            continue
        if role != "assistant" or current is None:
            continue
        message = entry.get("message") or {}
        message_id = message.get("id")
        if message_id:
            if message_id in seen_assistant_ids:
                continue
            seen_assistant_ids.add(message_id)
        current["total"] += usage_total(message.get("usage") or {})
        current["assistants"] += 1
    return [{"turn": t["turn"], "total": t["total"], "path": str(path), "assistants": t["assistants"]} for t in turns]


def parse_droid(session_id: str) -> list[dict]:
    matches = list((HOME / ".factory" / "sessions").glob(f"*/{session_id}.settings.json"))
    if not matches:
        raise SystemExit(f"Droid session settings not found for {session_id}")
    settings = json.loads(matches[0].read_text(encoding="utf-8"))
    usage = settings.get("tokenUsage") or {}
    total = sum(int(usage.get(key) or 0) for key in (
        "inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens",
    ))
    return [{"turn": 1, "total": total, "path": str(matches[0]), "assistants": 0}]

def parse_amp(session_id: str) -> list[dict]:
    amp = HOME / ".amp" / "bin" / "amp"
    result = subprocess.run(
        [str(amp), "threads", "export", session_id],
        text=True,
        capture_output=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise SystemExit(f"amp threads export failed: {result.stderr[-500:]}")
    thread = json.loads(result.stdout)
    turns: list[dict] = []
    current = None
    for message in thread.get("messages") or []:
        if message.get("role") == "user":
            blocks = message.get("content") or []
            has_prompt_text = any(
                isinstance(block, dict) and isinstance(block.get("text"), str)
                for block in blocks
            )
            has_tool_result = any(
                isinstance(block, dict)
                and block.get("type") in ("tool_result", "toolResult", "run")
                for block in blocks
            )
            if has_prompt_text and not has_tool_result:
                current = {"turn": len(turns) + 1, "total": 0, "assistants": 0}
                turns.append(current)
            continue
        if message.get("role") != "assistant" or current is None:
            continue
        current["total"] += usage_total(message.get("usage") or {})
        current["assistants"] += 1
    return [
        {"turn": turn["turn"], "total": turn["total"], "path": session_id,
         "assistants": turn["assistants"]}
        for turn in turns
    ]

def parse_native(harness: str, session_id: str) -> list[dict]:
    if harness in ("pi", "omp"):
        root = HOME / (".pi/agent/sessions" if harness == "pi" else ".omp/agent/sessions")
        return parse_pi_like(session_id, root)
    if harness == "codex":
        return parse_codex(session_id)
    if harness == "devin":
        return parse_devin(session_id)
    if harness == "claude":
        return parse_claude(session_id)
    if harness == "droid":
        return parse_droid(session_id)
    if harness == "amp":
        return parse_amp(session_id)
    raise SystemExit(f"native parser not implemented for {harness}")


def parse_time(value: str | None):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", required=True, choices=sorted(EXPECTED_NAMES))
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--from", dest="from_time", default=None)
    parser.add_argument("--to", dest="to_time", default=None)
    parser.add_argument("--skip-native", action="store_true")
    args = parser.parse_args()

    from_time, to_time = default_window()
    if args.from_time:
        from_time = parse_time(args.from_time)
    if args.to_time:
        to_time = parse_time(args.to_time)
    if from_time is None or to_time is None:
        raise SystemExit("invalid time window")

    native = [] if args.skip_native else parse_native(args.harness, args.session_id)
    client = LangfuseV2()
    rows = client.observations(session_id=args.session_id, from_time=from_time, to_time=to_time)
    traces = group_traces(rows)
    print(f"native turns: {[(t['turn'], t['total']) for t in native]}")
    print(f"Langfuse v2 traces: {len(traces)}")
    by_turn = {}
    for trace in traces:
        turn = trace["turn_number"]
        by_turn[turn] = trace
        print(f"  {trace['name']!r} turn_number={turn!r} "
              f"generation_total={trace['generation_total']} all_total={trace['all_total']} "
              f"tools={trace['tools']} tags={trace['tags']}")
    if args.skip_native:
        print(f"Langfuse trace total={sum(trace['generation_total'] for trace in traces)}")
        print("native comparison skipped")
        return
    if len(traces) != len(native):
        print(f"TRACE COUNT MISMATCH: native {len(native)} vs Langfuse {len(traces)}")
    for item in native:
        expected = EXPECTED_NAMES[args.harness].format(n=item["turn"])
        trace = by_turn.get(item["turn"])
        if not trace:
            print(f"turn {item['turn']}: MISSING in Langfuse (native {item['total']})")
            continue
        print(f"turn {item['turn']}: name={trace['name']!r} expected={expected!r} "
              f"name_ok={trace['name'] == expected} token_ok={trace['generation_total'] == item['total']} "
              f"native={item['total']} langfuse={trace['generation_total']} tools={trace['tools']}")
    native_sum = sum(item["total"] for item in native)
    lf_sum = sum(trace["generation_total"] for trace in by_turn.values())
    print(f"native sum={native_sum} langfuse sum={lf_sum} MATCH={native_sum == lf_sum}")


if __name__ == "__main__":
    main()
