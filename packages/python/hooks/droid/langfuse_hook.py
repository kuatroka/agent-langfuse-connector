#!/usr/bin/env python3
"""Factory Droid Stop hook modelled on Langfuse's official Claude Code hook."""

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

Langfuse = None
propagate_attributes = None
otel_trace_api = None


CONFIG_PATH = Path.home() / ".factory" / "langfuse.json"
LOCK_PATH = Path.home() / ".factory" / "langfuse-state.lock"
STATE_PATH = Path.home() / ".factory" / "langfuse-state.json"
LOG_PATH = Path.home() / ".factory" / "langfuse-hook.log"
MAX_CHARS = 20_000


def log(message):
    try:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now(timezone.utc).isoformat()} {message}\n")
    except Exception:
        pass

def load_sdk():
    global Langfuse, propagate_attributes, otel_trace_api
    if Langfuse is not None:
        return True
    try:
        from langfuse import Langfuse as langfuse_client, propagate_attributes as attributes
        from opentelemetry import trace as otel_api
    except Exception as exc:
        log(f"SDK import failed: {type(exc).__name__}")
        return False
    Langfuse = langfuse_client
    propagate_attributes = attributes
    otel_trace_api = otel_api
    return True


def read_config():
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return config if isinstance(config, dict) else {}
    except Exception:
        return {}
@contextmanager
def state_lock():
    handle = None
    try:
        handle = LOCK_PATH.open("a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except Exception as exc:
        if handle is not None:
            handle.close()
        log(f"state lock unavailable: {type(exc).__name__}")
        yield False
        return
    try:
        yield True
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        handle.close()

def write_state(state):
    try:
        mode = stat.S_IMODE(STATE_PATH.stat().st_mode)
    except OSError:
        mode = 0o600
    fd, temp_name = tempfile.mkstemp(
        prefix=f"{STATE_PATH.name}.",
        suffix=".tmp",
        dir=STATE_PATH.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, STATE_PATH)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)



def keychain(account):
    try:
        return subprocess.check_output(
            ["security", "find-generic-password", "-s", "amp-langfuse", "-a", account, "-w"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def truncate(value):
    if value is None:
        return None
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    if len(value) <= MAX_CHARS:
        return value
    return value[:MAX_CHARS] + "… [truncated]"


def timestamp(row):
    try:
        return datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
    except Exception:
        return None


def content_text(content):
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    )


def is_prompt(row):
    message = row.get("message")
    return (
        row.get("type") == "message"
        and isinstance(message, dict)
        and message.get("role") == "user"
        and "interactionMode" in message
    )


def turns(rows):
    """Assemble Factory's user -> assistant/tool-result message sequence into turns."""
    result, current, assistants, results = [], None, [], {}
    for row in rows:
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        if is_prompt(row):
            if current and assistants:
                result.append((current, assistants, results))
            current, assistants, results = row, [], {}
        elif current and message.get("role") == "assistant":
            assistants.append(row)
        elif current and message.get("role") == "user":
            for block in message.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id"):
                    results[str(block["tool_use_id"])] = (block.get("content"), timestamp(row))
    if current and assistants:
        result.append((current, assistants, results))
    return result


def model_details(assistant):
    message = assistant.get("message", {})
    model = str(message.get("modelId") or "unknown")
    if model.startswith("custom:"):
        try:
            settings = json.loads((Path.home() / ".factory" / "settings.json").read_text(encoding="utf-8"))
            for configured in settings.get("customModels", []):
                if configured.get("id") == model and isinstance(configured.get("model"), str):
                    model = configured["model"].removeprefix("cx/").removeprefix("cu/")
                    break
        except Exception:
            pass
    if "(" in model:
        model = model.split("(", 1)[0]
    effort = message.get("reasoningEffort")
    return model, str(effort) if effort else None


TOKEN_USAGE_KEYS = (
    "inputTokens",
    "outputTokens",
    "cacheCreationTokens",
    "cacheReadTokens",
    "thinkingTokens",
)

def session_settings_path(transcript_path):
    path = Path(transcript_path)
    return path.with_name(f"{path.stem}.settings.json")

def read_token_usage(transcript_path):
    """Read token usage Droid persists in the session settings file.

    Returns (usage, is_exec). TUI sessions keep cumulative counters; `droid exec`
    sessions are tagged "exec" and store the current turn's usage instead.
    """
    try:
        settings = json.loads(session_settings_path(transcript_path).read_text(encoding="utf-8"))
    except Exception:
        return None, False
    tags = settings.get("tags")
    is_exec = isinstance(tags, list) and any(
        isinstance(tag, dict) and tag.get("name") == "exec" for tag in tags
    )
    usage = settings.get("tokenUsage")
    if not isinstance(usage, dict):
        return None, is_exec
    parsed = {}
    for key in TOKEN_USAGE_KEYS:
        try:
            parsed[key] = int(usage.get(key) or 0)
        except (TypeError, ValueError):
            parsed[key] = 0
    return parsed, is_exec

def usage_delta(current, baseline):
    current = current or {}
    baseline = baseline or {}
    return {
        key: max(0, int(current.get(key) or 0) - int(baseline.get(key) or 0))
        for key in TOKEN_USAGE_KEYS
    }

def usage_details(delta):
    """Map Droid cumulative counters to Langfuse usage_details keys."""
    input_tokens = delta.get("inputTokens", 0)
    output_tokens = delta.get("outputTokens", 0)
    cache_read = delta.get("cacheReadTokens", 0)
    cache_creation = delta.get("cacheCreationTokens", 0)
    thinking = delta.get("thinkingTokens", 0)
    details = {
        "input": input_tokens,
        "output": output_tokens,
        "total": input_tokens + output_tokens + cache_read + cache_creation,
    }
    if cache_read:
        details["cache_read_input_tokens"] = cache_read
    if cache_creation:
        details["cache_creation_input_tokens"] = cache_creation
    if thinking:
        details["reasoning_tokens"] = thinking
    return details

def to_ns(value):
    return int(value.timestamp() * 1_000_000_000) if value else None


def start_backdated(langfuse, name, kind, start_time, parent=None, **kwargs):
    start_ns = to_ns(start_time)
    if parent is None:
        span = langfuse._otel_tracer.start_span(name=name, start_time=start_ns)
    else:
        with otel_trace_api.use_span(parent, end_on_exit=False):
            span = langfuse._otel_tracer.start_span(name=name, start_time=start_ns)
    return langfuse._create_observation_from_otel_span(otel_span=span, as_type=kind, **kwargs)


def emit_turn(langfuse, session_id, number, turn, transcript_path, turn_usage=None, usage_source="session-cumulative-delta"):
    user, assistants, tool_results = turn
    prompt = truncate(content_text(user["message"].get("content")))
    model, effort = model_details(assistants[-1])
    model_value = f"{model}({effort})" if effort else model
    tag_model = model.rsplit("/", 1)[-1]
    tag_model_value = f"{tag_model}({effort})" if effort else tag_model
    tags = ["harness:factory", f"model:{tag_model_value}"]
    if effort:
        tags.append(f"reasoning-effort:{effort}")
    user_time = timestamp(user)
    final = assistants[-1]
    final_time = timestamp(final) or user_time
    usage = usage_details(turn_usage) if turn_usage else None
    metadata = {
        "harness": "factory",
        "model": model_value,
        "model_base": model,
        "model_source": "factory-transcript",
        "reasoning_effort": effort,
        "reasoning_effort_source": "factory-transcript",
        "turn_number": number,
        "transcript_path": str(transcript_path),
        "token_usage_scope": "turn",
        "token_usage_source": usage_source,
        "token_usage_status": "complete" if usage else "unavailable",
    }
    if usage:
        metadata["token_usage"] = json.dumps(usage)
    with propagate_attributes(
        session_id=session_id,
        trace_name=f"Factory Droid - Turn {number}",
        tags=tags,
        metadata=metadata,
    ):
        trace = start_backdated(
            langfuse, f"Factory Droid - Turn {number}", "span", user_time,
            input={"role": "user", "content": prompt}, metadata=metadata,
        )
        previous_time = user_time
        final_text = ""
        for index, assistant in enumerate(assistants, 1):
            message = assistant["message"]
            assistant_time = timestamp(assistant) or previous_time
            model, effort = model_details(assistant)
            blocks = message.get("content", [])
            text = truncate(content_text(blocks))
            final_text = text or final_text
            calls = [block for block in blocks if isinstance(block, dict) and block.get("type") == "tool_use"]
            output = {"role": "assistant", "content": text}
            if calls:
                output["tool_calls"] = [
                    {"id": call.get("id"), "name": call.get("name"), "input": call.get("input")}
                    for call in calls
                ]
            # Langfuse v4 only counts usage on generation observations. Attach the
            # turn aggregate exactly once, to the final generation of the turn.
            is_final_generation = index == len(assistants)
            generation_metadata = {
                "reasoning_effort": effort,
                "reasoning_effort_source": "factory-transcript",
                "assistant_index": index,
            }
            if is_final_generation and usage:
                generation_metadata.update({
                    "token_usage_scope": "turn",
                    "token_usage_source": usage_source,
                    "token_usage_status": "complete",
                })
            generation = start_backdated(
                langfuse, f"Factory Generation {index}", "generation", previous_time,
                parent=trace._otel_span, model=model,
                input={"role": "user", "content": prompt} if index == 1 else None,
                output=output,
                metadata=generation_metadata,
                usage_details=usage if is_final_generation else None,
            )
            end_time = assistant_time
            for call in calls:
                result, result_time = tool_results.get(str(call.get("id")), (None, None))
                tool = start_backdated(
                    langfuse, f"Tool: {call.get('name') or 'unknown'}", "tool", assistant_time,
                    parent=generation._otel_span, input=call.get("input"),
                    metadata={"tool_id": call.get("id"), "tool_name": call.get("name")},
                )
                tool.update(output=truncate(result))
                tool.end(end_time=to_ns(result_time or assistant_time))
                if result_time and (not end_time or result_time > end_time):
                    end_time = result_time
            generation.end(end_time=to_ns(end_time))
            previous_time = end_time
        trace.update(output={"role": "assistant", "content": final_text})
        if usage:
            trace.update(metadata={"token_usage": json.dumps(usage)})
        trace.end(end_time=to_ns(final_time or previous_time))
        try:
            import subprocess
            subprocess.run([str(Path.home() / ".local/bin/agent-eval"), "ingest"],
                input=json.dumps({"id": trace.trace_id, "session_id": session_id, "metadata": metadata, "prompt": prompt, "response": final_text, "occurred_at": user_time.isoformat() if user_time else None}), text=True, capture_output=True, timeout=3, check=True)
        except Exception as exc:
            print(f"AgentEval local capture failed: {type(exc).__name__}", file=__import__("sys").stderr)


def export_main():
    global MAX_CHARS
    config = read_config()
    if not config.get("enabled"):
        return
    try:
        MAX_CHARS = int(config.get("max_chars", MAX_CHARS))
    except (TypeError, ValueError):
        pass
    try:
        payload = json.load(sys.stdin)
        session_id = payload["session_id"]
        transcript = Path(payload["transcript_path"]).resolve()
        rows = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines() if line.strip()]
    except Exception as exc:
        log(f"input failed: {type(exc).__name__}")
        return
    public_key, secret_key, host = keychain("public-key"), keychain("secret-key"), keychain("base-url")
    if not public_key or not secret_key or not host:
        log("Langfuse credentials unavailable from Keychain")
        return
    key = hashlib.sha256(f"{session_id}:{transcript}".encode()).hexdigest()
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    except Exception:
        state = {}
    offset = int(state.get(key, 0))
    new_rows = rows[offset:]
    ready = turns(new_rows)
    if not ready:
        state[key] = len(rows)
        write_state(state)
        return
    if not load_sdk():
        return
    try:
        langfuse = Langfuse(public_key=public_key, secret_key=secret_key, host=host)
        current, is_exec = read_token_usage(transcript)
        baseline_key = f"{key}:usage"
        if is_exec:
            # `droid exec` session settings store the current turn's usage
            # directly, not a cumulative counter.
            turn_usage = current
            usage_source = "exec-turn-settings"
        else:
            baseline = state.get(baseline_key)
            if current is not None:
                turn_usage = usage_delta(current, baseline)
                state[baseline_key] = current
            else:
                turn_usage = None
            usage_source = "session-cumulative-delta"
        if len(ready) > 1:
            log(f"{len(ready)} turns pending in one Stop event; attaching the turn usage to the last turn only, earlier turns exported with token_usage_status=unavailable")
        for index, turn in enumerate(ready, 1):
            # `droid exec` settings hold per-turn usage; TUI counters are
            # cumulative, so either way the value belongs to the most recent turn.
            per_turn = turn_usage if index == len(ready) else None
            emit_turn(langfuse, session_id, int(state.get(f"{key}:turn", 0)) + index, turn, transcript, turn_usage=per_turn, usage_source=usage_source)
        langfuse.flush()
        langfuse.shutdown()
        state[key] = len(rows)
        state[f"{key}:turn"] = int(state.get(f"{key}:turn", 0)) + len(ready)
        write_state(state)
    except Exception as exc:
        log(f"emit failed: {type(exc).__name__}: {exc}")

def launch_worker():
    try:
        payload = sys.stdin.buffer.read()
        if not payload:
            return
        worker = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        try:
            worker.stdin.write(payload)
            worker.stdin.close()
        except (BrokenPipeError, OSError):
            try:
                worker.kill()
            except OSError:
                pass
    except Exception as exc:
        log(f"worker launch failed: {type(exc).__name__}")

def main():
    if "--worker" not in sys.argv:
        launch_worker()
        return
    with state_lock() as locked:
        if locked:
            export_main()


if __name__ == "__main__":
    main()
