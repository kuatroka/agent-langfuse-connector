#!/usr/bin/env python3
"""
Sends Claude Code traces to Langfuse after each response.
"""
 
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
 
# Check if Langfuse is available
try:
    from langfuse import Langfuse, propagate_attributes
except ImportError:
    print("Error: langfuse package not installed. Run: pip install langfuse", file=sys.stderr)
    sys.exit(0)
 
# Configuration
LOG_FILE = Path.home() / ".claude" / "state" / "langfuse_hook.log"
STATE_FILE = Path.home() / ".claude" / "state" / "langfuse_state.json"
DEBUG = os.environ.get("CC_LANGFUSE_DEBUG", "").lower() == "true"
 
 
def log(level: str, message: str) -> None:
    """Log a message to the log file."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"{timestamp} [{level}] {message}\n")
 
 
def debug(message: str) -> None:
    """Log a debug message (only if DEBUG is enabled)."""
    if DEBUG:
        log("DEBUG", message)
 
 
def load_state() -> dict:
    """Load the state file containing session tracking info."""
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, IOError):
        return {}
 
 
def save_state(state: dict) -> None:
    """Save the state file."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))
 
 
def get_content(msg: dict) -> Any:
    """Extract content from a message."""
    if isinstance(msg, dict):
        if "message" in msg:
            return msg["message"].get("content")
        return msg.get("content")
    return None
 
 
def is_tool_result(msg: dict) -> bool:
    """Check if a message contains tool results."""
    content = get_content(msg)
    if isinstance(content, list):
        return any(
            isinstance(item, dict) and item.get("type") == "tool_result"
            for item in content
        )
    return False
 
 
def get_tool_calls(msg: dict) -> list:
    """Extract tool use blocks from a message."""
    content = get_content(msg)
    if isinstance(content, list):
        return [
            item for item in content
            if isinstance(item, dict) and item.get("type") == "tool_use"
        ]
    return []
 
 
def get_text_content(msg: dict) -> str:
    """Extract text content from a message."""
    content = get_content(msg)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(item.get("text", ""))
            elif isinstance(item, str):
                text_parts.append(item)
        return "\n".join(text_parts)
    return ""
 
 
def merge_assistant_parts(parts: list) -> dict:
    """Merge multiple assistant message parts into one."""
    if not parts:
        return {}
    
    merged_content = []
    for part in parts:
        content = get_content(part)
        if isinstance(content, list):
            merged_content.extend(content)
        elif content:
            merged_content.append({"type": "text", "text": str(content)})
    
    # Use the structure from the first part
    result = parts[0].copy()
    if "message" in result:
        result["message"] = result["message"].copy()
        result["message"]["content"] = merged_content
    else:
        result["content"] = merged_content
    
    return result


def assistant_model(msg: dict) -> str:
    """Read the model recorded by Claude Code for an assistant message."""
    message = msg.get("message", {}) if isinstance(msg, dict) else {}
    model = message.get("model") if isinstance(message, dict) else None
    return str(model) if model else "claude"


def assistant_effort(msg: dict) -> str | None:
    """Read Claude Code's applied effort from the transcript envelope."""
    if not isinstance(msg, dict):
        return None
    effort = msg.get("effort")
    if not effort and isinstance(msg.get("message"), dict):
        effort = msg["message"].get("effort")
    return str(effort) if effort else None


def assistant_usage(msg: dict) -> dict[str, int] | None:
    """Translate native Claude usage without estimating missing counters."""
    message = msg.get("message", {}) if isinstance(msg, dict) else {}
    usage = message.get("usage", {}) if isinstance(message, dict) else {}
    if not isinstance(usage, dict):
        return None
    fields = {
        "input": usage.get("input_tokens"),
        "output": usage.get("output_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
    }
    result = {key: value for key, value in fields.items() if isinstance(value, int) and value >= 0}
    if "input" in result and "output" in result:
        result["total"] = (
            result["input"]
            + result["output"]
            + result.get("cache_read_input_tokens", 0)
            + result.get("cache_creation_input_tokens", 0)
        )
    return result or None
 
 
def find_latest_transcript() -> tuple[str, Path] | None:
    """Find the most recently modified transcript file.
    
    Claude Code stores transcripts as *.jsonl files directly in the project directory.
    Main conversation files have UUID names, agent files have agent-*.jsonl names.
    The session ID is stored inside each JSON line.
    """
    projects_dir = Path.home() / ".claude" / "projects"
    
    if not projects_dir.exists():
        debug(f"Projects directory not found: {projects_dir}")
        return None
    
    latest_file = None
    latest_mtime = 0
    
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        
        # Look for all .jsonl files directly in the project directory
        for transcript_file in project_dir.glob("*.jsonl"):
            mtime = transcript_file.stat().st_mtime
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest_file = transcript_file
    
    if latest_file:
        # Extract session ID from the first line of the file
        try:
            first_line = latest_file.read_text().split("\n")[0]
            first_msg = json.loads(first_line)
            session_id = first_msg.get("sessionId", latest_file.stem)
            debug(f"Found transcript: {latest_file}, session: {session_id}")
            return (session_id, latest_file)
        except (json.JSONDecodeError, IOError, IndexError) as e:
            debug(f"Error reading transcript {latest_file}: {e}")
            return None
    
    debug("No transcript files found")
    return None
 
 
def create_trace(
    langfuse: Langfuse,
    session_id: str,
    turn_num: int,
    user_msg: dict,
    assistant_msgs: list,
    tool_results: list,
) -> None:
    """Create a Langfuse trace for a single turn using the new SDK API."""
    # Extract user text
    user_text = get_text_content(user_msg)
    
    # Extract final assistant text
    final_output = ""
    if assistant_msgs:
        final_output = get_text_content(assistant_msgs[-1])
    
    models = list(dict.fromkeys(assistant_model(message) for message in assistant_msgs))
    efforts = list(filter(None, dict.fromkeys(assistant_effort(message) for message in assistant_msgs)))
    model = models[-1] if models else "claude"
    effort = efforts[-1] if efforts else None
    
    # Collect all tool calls and results
    all_tool_calls = []
    for assistant_msg in assistant_msgs:
        tool_calls = get_tool_calls(assistant_msg)
        for tool_call in tool_calls:
            tool_name = tool_call.get("name", "unknown")
            tool_input = tool_call.get("input", {})
            tool_id = tool_call.get("id", "")
            
            # Find matching tool result
            tool_output = None
            for tr in tool_results:
                tr_content = get_content(tr)
                if isinstance(tr_content, list):
                    for item in tr_content:
                        if isinstance(item, dict) and item.get("tool_use_id") == tool_id:
                            tool_output = item.get("content")
                            break
            
            all_tool_calls.append({
                "name": tool_name,
                "input": tool_input,
                "output": tool_output,
                "id": tool_id,
            })
    
    trace_metadata = {
        "harness": "claude-code",
        "source": "claude-code",
        "turn_number": turn_num,
        "session_id": session_id,
        "model": f"{model}({effort})" if effort else model,
        "model_base": model,
        "model_source": "claude-transcript",
        "models_used": models,
        "reasoning_effort": effort,
        "reasoning_effort_source": "claude-transcript",
        "reasoning_efforts_used": efforts,
    }
    model_value = f"{model}({effort})" if effort else model
    tags = ["harness:claude-code", f"model:{model_value}"]
    if effort:
        tags.append(f"reasoning-effort:{effort}")

    # Propagation maps the native session ID onto Langfuse's real session field.
    with propagate_attributes(
        session_id=session_id,
        trace_name=f"Claude Code - Turn {turn_num}",
        tags=tags,
        metadata={
            "harness": "claude-code",
            "model": f"{model}({effort})" if effort else model,
            "model_base": model,
            "model_source": "claude-transcript",
            "reasoning_effort": effort or "unknown",
            "reasoning_effort_source": "claude-transcript",
        },
    ):
        with langfuse.start_as_current_observation(
            name=f"Claude Code - Turn {turn_num}",
            input={"role": "user", "content": user_text},
            metadata=trace_metadata,
        ) as trace_span:
            for index, assistant_msg in enumerate(assistant_msgs, 1):
                generation_model = assistant_model(assistant_msg)
                generation_effort = assistant_effort(assistant_msg)
                generation_text = get_text_content(assistant_msg)
                generation_calls = get_tool_calls(assistant_msg)
                generation_output = {"role": "assistant", "content": generation_text}
                if generation_calls:
                    generation_output["tool_calls"] = [
                        {
                            "id": call.get("id"),
                            "name": call.get("name", "unknown"),
                            "input": call.get("input", {}),
                        }
                        for call in generation_calls
                    ]
                with langfuse.start_as_current_observation(
                    name=f"Claude Generation {index}",
                    as_type="generation",
                    model=generation_model,
                    input={"role": "user", "content": user_text} if index == 1 else None,
                    output=generation_output,
                    usage_details=assistant_usage(assistant_msg),
                    metadata={
                        "reasoning_effort": generation_effort,
                        "reasoning_effort_source": "claude-transcript",
                        "assistant_index": index,
                        "tool_count": len(generation_calls),
                    },
                ):
                    pass

            for tool_call in all_tool_calls:
                with langfuse.start_as_current_observation(
                    name=f"Tool: {tool_call['name']}",
                    as_type="tool",
                    input=tool_call["input"],
                    metadata={
                        "tool_name": tool_call["name"],
                        "tool_id": tool_call["id"],
                    },
                ) as tool_span:
                    tool_span.update(output=tool_call["output"])
                debug(f"Created span for tool: {tool_call['name']}")

            trace_span.update(output={"role": "assistant", "content": final_output})
            try:
                import subprocess
                subprocess.run([str(Path.home() / ".local/bin/agent-eval"), "ingest"],
                    input=json.dumps({"id": trace_span.trace_id, "session_id": session_id, "metadata": trace_metadata, "prompt": user_text, "response": final_output}), text=True, capture_output=True, timeout=3, check=True)
            except Exception as exc:
                print(f"AgentEval local capture failed: {type(exc).__name__}", file=__import__("sys").stderr)
    
    debug(f"Created trace for turn {turn_num}")
 
 
def process_transcript(langfuse: Langfuse, session_id: str, transcript_file: Path, state: dict) -> int:
    """Process a transcript file and create traces for new turns."""
    # Get previous state for this session
    session_state = state.get(session_id, {})
    last_line = session_state.get("last_line", 0)
    turn_count = session_state.get("turn_count", 0)
    
    # Read transcript
    lines = transcript_file.read_text().strip().split("\n")
    total_lines = len(lines)
    
    if last_line >= total_lines:
        debug(f"No new lines to process (last: {last_line}, total: {total_lines})")
        return 0
    
    # Parse new messages
    new_messages = []
    for i in range(last_line, total_lines):
        try:
            msg = json.loads(lines[i])
            new_messages.append(msg)
        except json.JSONDecodeError:
            continue
    
    if not new_messages:
        return 0
    
    debug(f"Processing {len(new_messages)} new messages")
    
    # Group messages into turns (user -> assistant(s) -> tool_results)
    turns = 0
    current_user = None
    current_assistants = []
    current_assistant_parts = []
    current_msg_id = None
    current_tool_results = []
    
    for msg in new_messages:
        role = msg.get("type") or (msg.get("message", {}).get("role"))
        
        if role == "user":
            # Check if this is a tool result
            if is_tool_result(msg):
                current_tool_results.append(msg)
                continue
            
            # New user message - finalize previous turn
            if current_msg_id and current_assistant_parts:
                merged = merge_assistant_parts(current_assistant_parts)
                current_assistants.append(merged)
                current_assistant_parts = []
                current_msg_id = None
            
            if current_user and current_assistants:
                turns += 1
                turn_num = turn_count + turns
                create_trace(langfuse, session_id, turn_num, current_user, current_assistants, current_tool_results)
            
            # Start new turn
            current_user = msg
            current_assistants = []
            current_assistant_parts = []
            current_msg_id = None
            current_tool_results = []
            
        elif role == "assistant":
            msg_id = None
            if isinstance(msg, dict) and "message" in msg:
                msg_id = msg["message"].get("id")
            
            if not msg_id:
                # No message ID, treat as continuation
                current_assistant_parts.append(msg)
            elif msg_id == current_msg_id:
                # Same message ID, add to current parts
                current_assistant_parts.append(msg)
            else:
                # New message ID - finalize previous message
                if current_msg_id and current_assistant_parts:
                    merged = merge_assistant_parts(current_assistant_parts)
                    current_assistants.append(merged)
                
                # Start new assistant message
                current_msg_id = msg_id
                current_assistant_parts = [msg]
    
    # Process final turn
    if current_msg_id and current_assistant_parts:
        merged = merge_assistant_parts(current_assistant_parts)
        current_assistants.append(merged)
    
    if current_user and current_assistants:
        turns += 1
        turn_num = turn_count + turns
        create_trace(langfuse, session_id, turn_num, current_user, current_assistants, current_tool_results)
    
    # Update state
    state[session_id] = {
        "last_line": total_lines,
        "turn_count": turn_count + turns,
        "updated": datetime.now(timezone.utc).isoformat(),
    }
    save_state(state)
    
    return turns
 
 
def main():
    script_start = datetime.now()
    debug("Hook started")
    
    # Check if tracing is enabled
    if os.environ.get("TRACE_TO_LANGFUSE", "").lower() != "true":
        debug("Tracing disabled (TRACE_TO_LANGFUSE != true)")
        sys.exit(0)
    
    # Check for required environment variables
    public_key = os.environ.get("CC_LANGFUSE_PUBLIC_KEY") or os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("CC_LANGFUSE_SECRET_KEY") or os.environ.get("LANGFUSE_SECRET_KEY")
    host = os.environ.get("CC_LANGFUSE_HOST") or os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")
    
    if not public_key or not secret_key:
        log("ERROR", "Langfuse API keys not set (CC_LANGFUSE_PUBLIC_KEY / CC_LANGFUSE_SECRET_KEY)")
        sys.exit(0)
    
    # Initialize Langfuse client
    try:
        langfuse = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
        )
    except Exception as e:
        log("ERROR", f"Failed to initialize Langfuse client: {e}")
        sys.exit(0)
    
    # Load state
    state = load_state()
    
    # Stop hooks provide the exact session and transcript. Use those fields to
    # avoid attributing one concurrent Claude session to another.
    payload = {}
    try:
        if not sys.stdin.isatty():
            raw_payload = sys.stdin.read()
            if raw_payload.strip():
                decoded = json.loads(raw_payload)
                payload = decoded if isinstance(decoded, dict) else {}
    except (json.JSONDecodeError, OSError) as error:
        log("ERROR", f"Invalid hook input: {error}")
        sys.exit(0)

    transcript_value = payload.get("transcript_path")
    session_value = payload.get("session_id")
    if transcript_value:
        transcript_file = Path(str(transcript_value)).expanduser().resolve()
        if transcript_file.suffix != ".jsonl" or not transcript_file.is_file():
            log("ERROR", f"Hook transcript is not a readable JSONL file: {transcript_file}")
            sys.exit(0)
        result = (str(session_value or transcript_file.stem), transcript_file)
    else:
        result = find_latest_transcript()
    if not result:
        debug("No transcript file found")
        sys.exit(0)
    
    session_id, transcript_file = result
    
    if not transcript_file:
        debug("No transcript file found")
        sys.exit(0)
    
    debug(f"Processing session: {session_id}")
    
    # Process the transcript
    try:
        turns = process_transcript(langfuse, session_id, transcript_file, state)
        
        # Flush to ensure all data is sent
        langfuse.flush()
        
        # Log execution time
        duration = (datetime.now() - script_start).total_seconds()
        log("INFO", f"Processed {turns} turns in {duration:.1f}s")
        
        if duration > 180:
            log("WARN", f"Hook took {duration:.1f}s (>3min), consider optimizing")
            
    except Exception as e:
        log("ERROR", f"Failed to process transcript: {e}")
        import traceback
        debug(traceback.format_exc())
    finally:
        langfuse.shutdown()
    
    sys.exit(0)
 
 
if __name__ == "__main__":
    main()
