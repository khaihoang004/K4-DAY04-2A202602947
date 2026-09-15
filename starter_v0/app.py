from __future__ import annotations

import glob
import json
from pathlib import Path
from typing import Any
import chainlit as cl
from chainlit.input_widget import Select

from env_loader import load_lab_env
from providers import make_provider
from tools import load_tool_declarations, to_openai_tools
from versioning import artifact_version_dict, build_artifact_version
from chat import run_model_tool_loop, now_iso, safe_slug, write_transcript

ROOT = Path(__file__).parent
ARTIFACTS_DIR = ROOT / "artifacts"
DATA_DIR = ROOT / "data"

load_lab_env(ROOT)

def json_text(value: Any, max_chars: int | None = None) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return text[:max_chars] + "\n...<truncated>" if max_chars and len(text) > max_chars else text


def make_transcript_id(version: str, provider: str) -> str:
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    return "_".join([safe_slug(version), safe_slug(provider), timestamp])


def list_eval_files() -> list[Path]:
    if not DATA_DIR.exists():
        return []
    return sorted(Path(p) for p in glob.glob(str(DATA_DIR / "eval_*.json")))


def normalize_case(raw: dict[str, Any], fallback_id: str) -> dict[str, Any]:
    case_id = str(raw.get("id") or raw.get("case_id") or raw.get("name") or fallback_id)

    # Multi-turn cases: {"turns": [{"role": "user", "content": "..."}, ...]}
    # Single-turn cases: {"query": "..."} (or input/prompt/message/...)
    raw_turns = raw.get("turns")
    if isinstance(raw_turns, list) and raw_turns:
        turns = [t.get("content", "") for t in raw_turns if isinstance(t, dict) and t.get("role") == "user"]
    else:
        single = (
            raw.get("query") or raw.get("input") or raw.get("user_input")
            or raw.get("message") or raw.get("prompt") or raw.get("user_message") or ""
        )
        turns = [str(single)]

    expect_block = raw.get("expect")
    expected_tool_calls: list[dict[str, Any]] | None = None
    expected_text = None
    if isinstance(expect_block, dict) and "tool_calls" in expect_block:
        expected_tool_calls = expect_block.get("tool_calls") or []
    elif isinstance(raw.get("expected"), str) or isinstance(raw.get("expected_output"), str):
        expected_text = raw.get("expected") or raw.get("expected_output")

    return {
        "id": case_id,
        "turns": turns,
        "user_text": turns[0] if turns else "",  # preview / backward-compat
        "expected_tool_calls": expected_tool_calls,
        "expected_text": expected_text,
        "phase": raw.get("phase"),
        "suite": raw.get("suite"),
        "failure_type": raw.get("failure_type"),
        "metadata": raw.get("metadata") or {},
        "raw": raw,
    }


def load_eval_cases(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    dataset_meta: dict[str, Any] = {}
    if isinstance(data, dict):
        dataset_meta = {
            "dataset_id": data.get("dataset_id"),
            "dataset_role": data.get("dataset_role"),
            "description": data.get("description"),
            "allowed_failure_types": data.get("allowed_failure_types"),
        }
        cases_raw = data.get("cases") or data.get("test_cases") or data.get("data") or []
    else:
        cases_raw = data
    if not isinstance(cases_raw, list):
        return [], dataset_meta
    cases = [normalize_case(item, fallback_id=f"case_{i+1}") for i, item in enumerate(cases_raw) if isinstance(item, dict)]
    return cases, dataset_meta


async def build_session() -> dict[str, Any]:
    system_prompt_path, tools_path = ARTIFACTS_DIR / "system_prompt.md", ARTIFACTS_DIR / "tools.yaml"
    provider_name, model, version = "gemini", None, "v0"

    system_prompt = system_prompt_path.read_text(encoding="utf-8")
    tool_declarations = load_tool_declarations(tools_path)
    openai_tools = to_openai_tools(tool_declarations)

    provider = make_provider(provider_name)
    selected_model = model or getattr(provider, "default_model", None)
    artifact_version = build_artifact_version(version, system_prompt_path, tools_path)

    transcript_id = make_transcript_id(version, provider_name)
    transcript_path = ROOT / "transcripts" / f"{transcript_id}.transcript.json"

    transcript: dict[str, Any] = {
        "transcript_id": transcript_id, **artifact_version_dict(artifact_version),
        "provider": provider_name, "model": selected_model,
        "system_prompt": str(system_prompt_path), "tools": str(tools_path),
        "history_window": 5, "max_tool_rounds": 4,
        "created_at": now_iso(), "updated_at": now_iso(), "turns": [],
    }

    return {
        "system_prompt": system_prompt, "tool_declarations": tool_declarations,
        "openai_tools": openai_tools, "provider": provider, "model": selected_model,
        "version": version, "history_window": 5, "max_tool_rounds": 4,
        "history": [], "transcript": transcript, "transcript_path": transcript_path,
        "mode": "chat", "current_eval_file": None, "current_eval_cases": [],
    }


@cl.on_chat_start
async def on_chat_start():
    session_data = await build_session()
    for key, val in session_data.items():
        cl.user_session.set(key, val)

    await cl.ChatSettings(
        [
            Select(
                id="mode",
                label="Chế độ",
                values=["Chat", "Test"],
                initial_index=0,
            )
        ]
    ).send()

    tool_declarations = session_data["tool_declarations"]
    artifact_version = build_artifact_version(
        session_data["version"], ARTIFACTS_DIR / "system_prompt.md", ARTIFACTS_DIR / "tools.yaml"
    )

    await cl.Message(
        content=(
            f"# 🤖 IT Helpdesk Agent\n\nAgent đã sẵn sàng.\n\n"
            f"- **Provider:** `{session_data['provider'].__class__.__name__}`\n"
            f"- **Model:** `{session_data['model']}`\n"
            f"- **Artifact:** `{artifact_version.artifact_version}`\n"
            f"- **Tools:** `{len(tool_declarations)}`\n\n"
            "Dùng biểu tượng ⚙️ (Settings) để chuyển giữa **Chat mode** và **Test mode**.\n\n"
            "- **Chat mode**: hội thoại tự do với agent.\n"
            "- **Test mode**: chọn sẵn test case trong `data/eval_*.json` để chạy, không cần gõ tay.\n\n"
            "Ở cả hai mode, mỗi lượt trả lời sẽ có một khối **🔍 Debug trace** (có thể mở/thu gọn) chứa toàn bộ "
            "tool calls, arguments và tool results — tách riêng khỏi phần hội thoại chính."
        )
    ).send()


@cl.on_settings_update
async def on_settings_update(settings: dict[str, Any]):
    mode = "test" if settings.get("mode") == "Test" else "chat"
    cl.user_session.set("mode", mode)

    if mode == "chat":
        await cl.Message(content="💬 Đã chuyển sang **Chat mode**. Gõ câu hỏi bình thường để hội thoại với agent.").send()
        return

    files = list_eval_files()
    if not files:
        await cl.Message(
            content=f"⚠️ Không tìm thấy file eval nào trong `{DATA_DIR}` (định dạng `eval_*.json`)."
        ).send()
        return

    actions = [
        cl.Action(name="select_eval_file", payload={"path": str(f)}, label=f.name)
        for f in files
    ]
    await cl.Message(
        content="🧪 Đã chuyển sang **Test mode**. Chọn một file test case bên dưới:",
        actions=actions,
    ).send()

@cl.action_callback("select_eval_file")
async def on_select_eval_file(action: cl.Action):
    path = Path(action.payload["path"])
    cases, dataset_meta = load_eval_cases(path)
    cl.user_session.set("current_eval_file", str(path))
    cl.user_session.set("current_eval_cases", cases)

    if not cases:
        await cl.Message(content=f"⚠️ File `{path.name}` không có test case hợp lệ.").send()
        return

    actions = [
        cl.Action(
            name="run_test_case",
            payload={"path": str(path), "index": i},
            label=f"{i+1}. {c['id']}",
        )
        for i, c in enumerate(cases)
    ]
    actions.append(cl.Action(name="run_all_cases", payload={"path": str(path)}, label="▶️ Run tất cả"))

    preview = "\n".join(
        f"- **{c['id']}** ({c.get('failure_type') or '—'})"
        + (f" 🔁 multi-turn ×{len(c['turns'])}" if len(c["turns"]) > 1 else "")
        + f": {c['turns'][0][:80]}"
        for c in cases[:10]
    )
    more = f"\n- ... và {len(cases) - 10} case khác" if len(cases) > 10 else ""
    desc = f"\n_{dataset_meta['description']}_\n" if dataset_meta.get("description") else ""

    await cl.Message(
        content=(
            f"📄 **{path.name}**{desc}— {len(cases)} test case(s):\n\n{preview}{more}\n\n"
            "Chọn một case để chạy, hoặc chạy tất cả:"
        ),
        actions=actions,
    ).send()

async def render_debug_trace(rounds: list[dict[str, Any]], parent_name: str = "🔍 Debug trace"):
    """All tool-calling evidence lives inside one collapsible step, kept
    separate from the user-facing conversation bubbles."""
    if not rounds:
        return
    async with cl.Step(name=parent_name, type="run") as parent:
        parent.output = f"{len(rounds)} round(s) of tool-calling."
        for round_record in rounds:
            assistant_text = round_record.get("assistant_text", "")
            tool_calls = round_record.get("tool_calls", [])
            tool_results = round_record.get("tool_results", [])

            async with cl.Step(name=f"Round {round_record['round']}", type="tool", parent_id=parent.id) as step:
                parts = []
                if assistant_text:
                    parts.append(f"**Model reasoning / partial response:**\n\n{assistant_text}")
                if tool_calls:
                    parts.append(f"**🔧 Tool calls:**\n```json\n{json_text(tool_calls)}\n```")
                if tool_results:
                    parts.append(f"**📦 Tool results:**\n```json\n{json_text(tool_results, max_chars=24000)}\n```")
                step.output = "\n\n".join(parts) if parts else "(no output)"


async def render_final_answer(result: dict[str, Any], author: str = "Agent"):
    status = result.get("status")
    status_map = {
        "answered": "✅ Answered",
        "waiting_for_user": "⏸️ Waiting for user",
        "max_tool_rounds": "⚠️ Max tool rounds reached",
    }
    status_text = status_map.get(status, status or "unknown")
    rounds = result.get("rounds", [])
    total_tools = sum(len(r.get("tool_calls", [])) for r in rounds)

    await cl.Message(
        author=author,
        content=result.get("assistant_text") or "(empty response)",
    ).send()
    await cl.Message(
        content=f"_{status_text} · {len(rounds)} round(s) · {total_tools} tool call(s)_"
    ).send()


async def _run_and_render(user_text: str, history: list[dict[str, Any]], *, label: str | None = None) -> dict[str, Any]:
    """Runs one user message through the model/tool loop against the given
    `history` list (mutated in place with this turn's user/assistant
    messages), renders it (debug trace + final answer), and logs it into
    the transcript. `history` is caller-owned: pass the session's shared
    history for Chat mode, or a fresh local list per test case for Test
    mode so cases never leak context into one another."""
    session = cl.user_session
    sys_prompt, openai_tools = session.get("system_prompt"), session.get("openai_tools")
    provider, model = session.get("provider"), session.get("model")
    hw, mtr = session.get("history_window", 5), session.get("max_tool_rounds", 4)
    transcript, transcript_path = session.get("transcript"), session.get("transcript_path")

    await cl.Message(author="You", content=f"{label + chr(10) if label else ''}{user_text}").send()

    messages = [{"role": "system", "content": sys_prompt}] + history[-hw * 2:] + [{"role": "user", "content": user_text}]

    turn_record: dict[str, Any] = {
        "turn_index": len(transcript["turns"]) + 1, "started_at": now_iso(),
        "user": user_text, "label": label, "status": "started", "assistant_text": None,
        "rounds": [], "tool_events": [],
    }

    processing = cl.Message(content="⏳ **Agent đang xử lý...**")
    await processing.send()

    try:
        result = run_model_tool_loop(provider=provider, messages=messages, tools=openai_tools, model=model, max_tool_rounds=mtr)
        await processing.remove()

        await render_debug_trace(result.get("rounds", []))
        await render_final_answer(result)

        assistant_text = result.get("assistant_text", "")
        history.extend([{"role": "user", "content": user_text}, {"role": "assistant", "content": assistant_text}])
        turn_record.update(result)
        return result

    except Exception as exc:
        await processing.remove()
        error_message = f"{type(exc).__name__}: {str(exc)}"
        turn_record.update({"status": "provider_error", "error": error_message})
        await cl.Message(author="Agent", content=f"❌ **Agent Error**\n\n```text\n{error_message}\n```").send()
        return {"status": "provider_error", "assistant_text": None, "rounds": []}

    finally:
        turn_record["ended_at"] = now_iso()
        transcript["turns"].append(turn_record)
        write_transcript(transcript_path, transcript)


async def run_turn(user_text: str) -> dict[str, Any]:
    """Chat-mode entry point: reads/writes the session's shared history."""
    session = cl.user_session
    history = session.get("history", [])
    result = await _run_and_render(user_text, history)
    session.set("history", history)
    return result


async def run_case(case: dict[str, Any]) -> dict[str, Any]:
    """Runs every turn of a test case (1 turn for single-turn cases, N for
    multi-turn cases) against a fresh, isolated local history — so this
    case's conversation never mixes with Chat mode or with other cases.
    Returns the tool-call rounds aggregated across all turns plus the
    final assistant_text, for verdict checking."""
    local_history: list[dict[str, Any]] = []
    all_rounds: list[dict[str, Any]] = []
    last_result: dict[str, Any] = {}
    turns = case["turns"]

    for i, turn_text in enumerate(turns):
        suffix = f" — turn {i + 1}/{len(turns)}" if len(turns) > 1 else ""
        label = f"🧪 **Test case `{case['id']}`**{suffix}"
        result = await _run_and_render(turn_text, local_history, label=label)
        all_rounds.extend(result.get("rounds", []))
        last_result = result

    return {"rounds": all_rounds, "assistant_text": last_result.get("assistant_text")}

def extract_actual_tool_calls(rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flattens {name, args} out of every round's tool_calls. Shape of a raw
    tool_call entry depends on chat.py / the provider adapter — this tries
    the common variants (OpenAI-style function.name/arguments, or flat
    name/args, or name/arguments-as-json-string)."""
    actual: list[dict[str, Any]] = []
    for r in rounds:
        for tc in r.get("tool_calls", []):
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            name = tc.get("name") or fn.get("name") or tc.get("tool_name")
            args = tc.get("args")
            if args is None:
                raw_args = tc.get("arguments") if "arguments" in tc else fn.get("arguments")
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        args = raw_args
                elif isinstance(raw_args, dict):
                    args = raw_args
            actual.append({"name": name, "args": args or {}})
    return actual


def args_contains(expected_args: dict[str, Any], actual_args: dict[str, Any]) -> bool:
    """True if every key/value in expected_args is present in actual_args.
    Extra keys in actual_args (that expect.args doesn't mention) are ignored."""
    if not isinstance(expected_args, dict):
        return expected_args == actual_args
    if not isinstance(actual_args, dict):
        return False
    return all(actual_args.get(k) == v for k, v in expected_args.items())


def check_expected_tool_calls(case_result: dict[str, Any], case: dict[str, Any]) -> bool | None:
    expected = case.get("expected_tool_calls")
    if expected is None:
        # fall back to plain-text expected if that's what this file uses
        expected_text = case.get("expected_text")
        if expected_text is None:
            return None
        assistant_text = case_result.get("assistant_text")
        return assistant_text is not None and expected_text.strip().lower() in assistant_text.strip().lower()

    actual = extract_actual_tool_calls(case_result.get("rounds", []))
    for exp in expected:
        found = any(
            act.get("name") == exp.get("name") and args_contains(exp.get("args", {}), act.get("args", {}))
            for act in actual
        )
        if not found:
            return False
    return True


async def show_verdict(case_result: dict[str, Any], case: dict[str, Any]) -> None:
    verdict = check_expected_tool_calls(case_result, case)
    if verdict is None:
        return
    if case.get("expected_tool_calls") is not None:
        actual = extract_actual_tool_calls(case_result.get("rounds", []))
        badge = "✅ Đã gọi đủ các tool_calls kỳ vọng" if verdict else "❌ THIẾU (hoặc sai args/tool) so với expect.tool_calls"
        detail = (
            f"**Expected (chỉ cần xuất hiện, không cần đúng thứ tự/số lượng):**\n```json\n{json_text(case['expected_tool_calls'])}\n```\n"
            f"**Actual (gộp tất cả turn):**\n```json\n{json_text(actual)}\n```"
        )
    else:
        badge = "✅ Khớp expected" if verdict else "❌ Không khớp expected"
        detail = f"**Expected:**\n```text\n{case['expected_text']}\n```"

    failure_hint = f"\n_failure_type kỳ vọng nếu sai: `{case['failure_type']}`_" if not verdict and case.get("failure_type") else ""
    await cl.Message(content=f"{badge}{failure_hint}\n\n{detail}").send()


@cl.action_callback("run_test_case")
async def on_run_test_case(action: cl.Action):
    path = Path(action.payload["path"])
    index = action.payload["index"]
    cases = cl.user_session.get("current_eval_cases", [])
    if not (cases and str(cl.user_session.get("current_eval_file")) == str(path) and index < len(cases)):
        cases, _ = load_eval_cases(path)
    case = cases[index]

    case_result = await run_case(case)
    await show_verdict(case_result, case)


@cl.action_callback("run_all_cases")
async def on_run_all_cases(action: cl.Action):
    path = Path(action.payload["path"])
    cases, _ = load_eval_cases(path)
    if not cases:
        await cl.Message(content=f"⚠️ File `{path.name}` không có test case hợp lệ.").send()
        return

    passed, failed, no_expected = 0, 0, 0
    for case in cases:
        case_result = await run_case(case)
        verdict = check_expected_tool_calls(case_result, case)
        await show_verdict(case_result, case)
        if verdict is None:
            no_expected += 1
        elif verdict:
            passed += 1
        else:
            failed += 1

    await cl.Message(
        content=(
            f"## 📊 Kết quả chạy `{path.name}`\n\n"
            f"- Tổng số case: **{len(cases)}**\n"
            f"- ✅ Passed: **{passed}**\n"
            f"- ❌ Failed: **{failed}**\n"
            f"- ➖ Không có expected để so sánh: **{no_expected}**"
        )
    ).send()


@cl.on_message
async def on_message(message: cl.Message):
    user_text = message.content.strip()
    if not user_text:
        return

    mode = cl.user_session.get("mode", "chat")
    if mode == "test":
        await cl.Message(
            content=(
                "ℹ️ Bạn đang ở **Test mode**. Hãy chọn test case bằng nút bên trên, "
                "hoặc mở ⚙️ Settings và chuyển lại **Chat mode** để hội thoại tự do."
            )
        ).send()
        return

    await run_turn(user_text)


@cl.on_chat_resume
async def on_chat_resume(thread):
    pass