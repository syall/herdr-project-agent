"""Synthetic session lines for the pa-costs tests (t-009). Usage counters and ids only; every content field holds
the canary, which must never reach a file under costs/ or billing/, stdout, stderr or an exception text.

Claude lines follow the transcript layout the collector reads (type, timestamp, sessionId, cwd, message.id,
message.model, message.usage); Codex lines follow the rollout layout (session_meta, turn_context, event_msg
token_count with cumulative total_token_usage). Models are neutral: model-a, model-b.
"""
import json
import os

CANARY = "CANARY-7f3a"


def _line(obj):
    return json.dumps(obj, separators=(",", ":")) + "\n"


def claude_user(ts, sid, cwd, text=True, meta=False, tool_result=False, sidechain=False, uuid=None):
    """A user line. text=True: a prompt (a string content); False: a tool result block (not a turn start)."""
    content = "{} prompt text".format(CANARY) if text else [{"type": "tool_result", "tool_use_id": CANARY, "content": CANARY}]
    obj = {"parentUuid": None, "isSidechain": sidechain, "userType": "external", "cwd": cwd, "sessionId": sid,
           "version": "9.9.9", "gitBranch": CANARY, "type": "user", "message": {"role": "user", "content": content},
           "uuid": uuid or "u-" + ts, "timestamp": ts}
    if meta:
        obj["isMeta"] = True
    if tool_result:
        obj["toolUseResult"] = {"stdout": CANARY}
    return _line(obj)


def claude_assistant(ts, sid, cwd, msg_id, model="model-a", inp=2, out=10, cw5m=0, cw1h=0, cr=0, request_id=None,
                     uuid=None, flat_cache=False, block="text"):
    """One line of one API call. A real call is written as several such lines (one per content block)."""
    usage = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
             "cache_creation_input_tokens": cw5m + cw1h, "service_tier": "standard"}
    if not flat_cache:
        usage["cache_creation"] = {"ephemeral_5m_input_tokens": cw5m, "ephemeral_1h_input_tokens": cw1h}
    message = {"model": model, "role": "assistant", "type": "message",
               "content": [{"type": block, "text": CANARY, "thinking": CANARY}], "usage": usage}
    if msg_id is not None:
        message["id"] = msg_id
    obj = {"parentUuid": CANARY, "isSidechain": False, "userType": "external", "cwd": cwd, "sessionId": sid,
           "version": "9.9.9", "gitBranch": CANARY, "message": message, "type": "assistant",
           "uuid": uuid or "a-{}-{}".format(msg_id, out), "timestamp": ts}
    if request_id is not None:
        obj["requestId"] = request_id
    return _line(obj)


def claude_call(ts, sid, cwd, msg_id, lines=3, out=30, **kw):
    """The lines one API call writes: `lines` lines, output growing to `out` on the last one."""
    step = max(1, out // lines)
    return [claude_assistant(ts, sid, cwd, msg_id, out=(out if i == lines - 1 else step * (i + 1)), **kw) for i in range(lines)]


def codex_meta(ts, sid, cwd):
    return _line({"timestamp": ts, "type": "session_meta",
                  "payload": {"id": sid, "timestamp": ts, "cwd": cwd, "originator": CANARY, "cli_version": "0.0.1",
                              "instructions": CANARY, "model_provider": "provider-x"}})


def codex_turn(ts, model="model-b", cwd="/x"):
    return _line({"timestamp": ts, "type": "turn_context",
                  "payload": {"model": model, "cwd": cwd, "approval_policy": CANARY, "user_instructions": CANARY}})


def codex_user(ts):
    return _line({"timestamp": ts, "type": "response_item", "payload": {"type": "message", "role": "user",
                                                                       "content": [{"type": "input_text", "text": CANARY}]}})


def codex_tokens(ts, inp, cached=0, cw=0, out=0, reasoning=0):
    """A token_count event with CUMULATIVE totals (input includes cached and cache-write tokens, as Codex counts)."""
    total = {"input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": cw, "output_tokens": out,
             "reasoning_output_tokens": reasoning, "total_tokens": inp + out}
    return _line({"timestamp": ts, "type": "event_msg",
                  "payload": {"type": "token_count", "info": {"total_token_usage": total, "last_token_usage": total,
                                                              "model_context_window": 1000},
                              "rate_limits": {"note": CANARY}}})


def append(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write("".join(lines) if isinstance(lines, (list, tuple)) else lines)


def claude_path(claude_home, cwd, sid):
    import re
    return os.path.join(claude_home, "projects", re.sub(r"[^A-Za-z0-9]", "-", cwd), sid + ".jsonl")


def codex_path(codex_home, day, sid):
    """day: YYYY-MM-DD."""
    y, m, d = day.split("-")
    return os.path.join(codex_home, "sessions", y, m, d, "rollout-{}T00-00-00-{}.jsonl".format(day, sid))
