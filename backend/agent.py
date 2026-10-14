from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .activity import ActivitySpan
from .config import (
    AgentConfig,
    ContextConfig,
    ModelConfig,
    REASONING_POLICIES,
    ResolvedModel,
    resolve_model,
)
from .paper_downloader import PaperDownloadError, download_paper
from .paper_parser import parse_pdf
from .paper_visuals import list_paper_visuals, paper_path, render_paper_visuals
from .skill_loader import (
    load_paper_downloader_skill,
    load_paper_reviewer_skill,
    load_paper_search_skill,
    load_skill_catalog,
    load_paper_visual_extractor_skill,
)


WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web when the answer depends on current, externally verifiable, "
            "or uncertain information. Cite useful result URLs in the final answer."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}

DOWNLOAD_PAPER_TOOL = {
    "type": "function",
    "function": {
        "name": "download_paper",
        "description": (
            "Download and parse an identified open-access academic PDF from a supported "
            "repository. Use an arXiv ID or a direct repository PDF URL; search first when "
            "the user supplied only a title or DOI."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "arXiv ID or supported HTTPS repository PDF URL",
                }
            },
            "required": ["source"],
        },
    },
}

LIST_PAPER_VISUALS_TOOL = {
    "type": "function",
    "function": {
        "name": "list_paper_visuals",
        "description": (
            "List captioned figures and tables with caption-based category, confidence, "
            "importance score, and recommendations. Use before choosing screenshots."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

RENDER_PAPER_VISUALS_TOOL = {
    "type": "function",
    "function": {
        "name": "render_paper_visuals",
        "description": "Render up to four selected figure or table regions and display them in the conversation.",
        "parameters": {
            "type": "object",
            "properties": {
                "visual_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 4,
                }
            },
            "required": ["visual_ids"],
        },
    },
}

MAX_PAPER_CONTEXT_CHARS = 250_000
ALLOWED_SKILLS = {"paper-search", "paper-downloader", "paper-reviewer", "paper-visual-extractor"}
ALLOWED_SKILL_MODES = {"auto", "summary", "full-review", "focused-check", "story-lecture"}
MAX_TOOL_ROUNDS = 8
ACTIVATE_SKILL_TOOL = {
    "type": "function",
    "function": {
        "name": "activate_skill",
        "description": (
            "Activate exactly one installed Skill when it materially helps the current "
            "request. Do not activate a Skill for ordinary questions. After activation, "
            "its complete instructions and permitted tools will be loaded by the Agent."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill": {"type": "string", "enum": sorted(ALLOWED_SKILLS)},
                "reason": {
                    "type": "string",
                    "description": "Brief reason this Skill is the best fit.",
                },
            },
            "required": ["skill"],
        },
    },
}
UI_ACTION_TOOL = {
    "type": "function",
    "function": {
        "name": "ui_action",
        "description": (
            "Emit one structured UI element for the user to interact with. type=options "
            "presents up to five clickable choices; type=confirm asks the user to confirm or "
            "cancel an action; type=input asks the user to type a short answer. Return exactly "
            "one action per call. For options, the UI already appends an \"Other: please input\" "
            "option, so do not add your own generic other option. prompt / prompt_template / "
            "prompt_on_confirm may use the {input} placeholder to merge the user's composer text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["options", "confirm", "input"]},
                "options": {
                    "type": "array",
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "description": "Short visible label."},
                            "prompt": {
                                "type": "string",
                                "description": "Exact instruction sent when clicked.",
                            },
                            "skill": {"type": "string", "enum": sorted(ALLOWED_SKILLS)},
                            "skill_mode": {"type": "string", "enum": sorted(ALLOWED_SKILL_MODES)},
                        },
                        "required": ["label", "prompt"],
                    },
                },
                "title": {"type": "string", "description": "confirm: optional heading."},
                "message": {"type": "string", "description": "confirm: the question shown to the user."},
                "confirm_label": {"type": "string", "description": "confirm: confirm button label."},
                "cancel_label": {"type": "string", "description": "confirm: cancel button label."},
                "prompt_on_confirm": {"type": "string", "description": "confirm: prompt sent when confirmed."},
                "label": {"type": "string", "description": "input: the question shown above the field."},
                "placeholder": {"type": "string", "description": "input: placeholder text in the field."},
                "submit_label": {"type": "string", "description": "input: submit button label."},
                "prompt_template": {"type": "string", "description": "input: template for the sent prompt; use {input} for the typed text."},
                "skill": {"type": "string", "enum": sorted(ALLOWED_SKILLS)},
                "skill_mode": {"type": "string", "enum": sorted(ALLOWED_SKILL_MODES)},
            },
            "required": ["type"],
        },
    },
}
RESPONSE_FORMATTING_GUIDANCE = (
    "Use compact, readable Markdown. Group related sentences into coherent paragraphs; "
    "do not put every sentence on a separate line. Use headings and lists only when they "
    "materially clarify structure. Preserve necessary line breaks in code and equations."
)


def _clip_context_text(value: Any, limit: int) -> tuple[str, bool]:
    text = str(value or "")
    if len(text) <= limit:
        return text, False
    marker = "\n\n[…此消息已按上下文预算截断…]\n\n"
    available = max(0, limit - len(marker))
    head = round(available * 0.72)
    return text[:head] + marker + text[-(available - head):], True


def _bounded_conversation_messages(
    messages: Any, policy: ContextConfig
) -> tuple[List[Dict[str, str]], Dict[str, Any]]:
    if not isinstance(messages, list):
        raise HTTPException(status_code=422, detail="messages must be an array")

    systems: List[Dict[str, str]] = []
    turns: List[List[Dict[str, str]]] = []
    for raw in messages:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail="Each message must be an object")
        role = raw.get("role")
        content = raw.get("content")
        if role not in {"system", "user", "assistant"} or not isinstance(content, str):
            raise HTTPException(status_code=422, detail="Conversation messages must contain text roles")
        message = {"role": role, "content": content}
        if role == "system":
            systems.append(message)
        elif role == "user":
            turns.append([message])
        elif turns:
            turns[-1].append(message)

    truncated_messages = 0
    bounded_systems: List[Dict[str, str]] = []
    system_limit = min(policy.max_message_chars, 8_000)
    for message in systems[:2]:
        content, truncated = _clip_context_text(message["content"], system_limit)
        truncated_messages += int(truncated)
        bounded_systems.append({"role": "system", "content": content})

    bounded_turns: List[List[Dict[str, str]]] = []
    recent_start = max(0, len(turns) - policy.recent_turns)
    for index, turn in enumerate(turns):
        recent = index >= recent_start
        user_limit = min(policy.max_message_chars // 3, 6_000)
        assistant_limit = policy.max_message_chars if recent else max(2_000, policy.max_message_chars // 3)
        bounded: List[Dict[str, str]] = []
        for message in turn:
            limit = user_limit if message["role"] == "user" else assistant_limit
            content, truncated = _clip_context_text(message["content"], limit)
            truncated_messages += int(truncated)
            bounded.append({"role": message["role"], "content": content})
        bounded_turns.append(bounded)

    system_chars = sum(len(message["content"]) for message in bounded_systems)
    all_turn_chars = sum(
        len(message["content"]) for turn in bounded_turns for message in turn
    )
    omitted_indices: List[int] = []
    if system_chars + all_turn_chars <= policy.max_chars:
        selected_indices = list(range(len(bounded_turns)))
        outline = ""
    else:
        reserve = min(policy.outline_chars, max(500, policy.max_chars // 12))
        remaining = max(0, policy.max_chars - system_chars - reserve)
        selected_indices = []
        for index in range(len(bounded_turns) - 1, -1, -1):
            cost = sum(len(message["content"]) for message in bounded_turns[index])
            if cost <= remaining or not selected_indices:
                selected_indices.append(index)
                remaining = max(0, remaining - cost)
            else:
                omitted_indices.append(index)
        selected_indices.sort()
        omitted_indices.sort()
        outline_lines = [
            "Earlier ancestor turns were omitted by the context budget. This is a topic index, not a summary of their answers:"
        ]
        for index in omitted_indices:
            question = next(
                (message["content"] for message in bounded_turns[index] if message["role"] == "user"),
                "",
            )
            compact = " ".join(question.split())
            outline_lines.append(f"- Turn {index + 1}: {compact[:220]}")
        outline, outline_truncated = _clip_context_text("\n".join(outline_lines), reserve)
        truncated_messages += int(outline_truncated)

    prepared: List[Dict[str, str]] = list(bounded_systems)
    if outline:
        prepared.append({"role": "system", "content": outline})
    for index in selected_indices:
        prepared.extend(bounded_turns[index])
    estimated_chars = sum(len(message["content"]) for message in prepared)
    stats = {
        "strategy": "ancestor-path-budget-v1",
        "total_turns": len(turns),
        "included_turns": len(selected_indices),
        "omitted_turns": len(omitted_indices),
        "truncated_messages": truncated_messages,
        "estimated_chars": estimated_chars,
        "budget_chars": policy.max_chars,
    }
    return prepared, stats


def _reported_context_stats(payload: Dict[str, Any], server_stats: Dict[str, Any]) -> Dict[str, Any]:
    """Use client tree counts only for observability; server limits remain authoritative."""
    reported = payload.get("context_meta")
    if not isinstance(reported, dict) or reported.get("strategy") != "ancestor-path-budget-v1":
        return server_stats
    keys = ("total_turns", "included_turns", "omitted_turns", "truncated_messages")
    if any(not isinstance(reported.get(key), int) or reported[key] < 0 for key in keys):
        return server_stats
    if reported["included_turns"] + reported["omitted_turns"] != reported["total_turns"]:
        return server_stats
    return {
        **server_stats,
        "total_turns": reported["total_turns"],
        "included_turns": reported["included_turns"],
        "omitted_turns": reported["omitted_turns"],
        "truncated_messages": max(
            server_stats["truncated_messages"], reported["truncated_messages"]
        ),
    }


def _timeout(model: ModelConfig, read_seconds: Optional[float] = None) -> httpx.Timeout:
    return httpx.Timeout(
        connect=model.connect_timeout_seconds,
        read=read_seconds or model.read_timeout_seconds,
        write=model.connect_timeout_seconds,
        pool=model.connect_timeout_seconds,
    )


def _sse_error(code: str, message: str, retryable: bool = False) -> bytes:
    packet = {
        "error": {"code": code, "message": message, "retryable": retryable}
    }
    data = json.dumps(packet, ensure_ascii=False)
    return f"data: {data}\n\ndata: [DONE]\n\n".encode("utf-8")


def _sse_packet(payload: Dict[str, Any]) -> bytes:
    data = json.dumps(payload, ensure_ascii=False)
    return f"data: {data}\n\n".encode("utf-8")


def _sse_activity(
    span: ActivitySpan,
    state: str,
    message: str = "",
    detail: Optional[Dict[str, Any]] = None,
) -> bytes:
    return _sse_packet({"agent_activity": span.event(state, message, detail)})


def _resolved_review_mode(messages: List[Dict[str, Any]], skill_mode: Any = "auto") -> str:
    if skill_mode not in ALLOWED_SKILL_MODES:
        raise HTTPException(status_code=422, detail=f"Unsupported skill mode: {skill_mode}")
    if skill_mode and skill_mode != "auto":
        return str(skill_mode)
    user_text = "\n".join(
        str(message.get("content", ""))
        for message in messages
        if isinstance(message, dict) and message.get("role") == "user"
    ).lower()
    if any(
        marker in user_text
        for marker in ("full review", "peer review", "完整审稿", "全面审稿", "评审报告")
    ):
        return "full-review"
    if any(marker in user_text for marker in ("讲故事", "讲课", "教授", "story lecture")):
        return "story-lecture"
    if any(marker in user_text for marker in ("检查", "核查", "focused check")):
        return "focused-check"
    return "summary"


def _paper_reviewer_skill_text(
    messages: List[Dict[str, Any]], skill_mode: Any = "auto"
) -> str:
    skill = load_paper_reviewer_skill()
    mode = _resolved_review_mode(messages, skill_mode)
    user_text = "\n".join(
        str(message.get("content", ""))
        for message in messages
        if isinstance(message, dict) and message.get("role") == "user"
    ).lower()
    structured = any(
        marker in user_text
        for marker in ("json", "structured output", "结构化输出", "输出结构")
    )
    parts = [skill["instructions"], f"<selected_mode>{mode}</selected_mode>"]
    if mode == "full-review":
        parts.append(skill["rubric"])
    if mode == "story-lecture":
        parts.append(skill["story_lecture"])
    if any(
        marker in user_text
        for marker in (
            "figure",
            "table",
            "visual",
            "架构图",
            "机制图",
            "实验图",
            "结果图",
            "图表",
            "截图",
        )
    ):
        visual_skill = load_paper_visual_extractor_skill()
        parts.append(
            '<composed_skill name="paper-visual-extractor">\n'
            f"{visual_skill['instructions']}\n\n{visual_skill['classification']}\n"
            "</composed_skill>"
        )
    if structured:
        parts.append(skill["output_schema"])
    return "\n\n".join(parts)


def _paper_reviewer_context(
    paper_id: Any, messages: List[Dict[str, Any]], skill_mode: Any = "auto"
) -> str:
    path = paper_path(paper_id)
    parsed = parse_pdf(path)
    if parsed["character_count"] > MAX_PAPER_CONTEXT_CHARS:
        raise HTTPException(
            status_code=413,
            detail=(
                "The paper is too large for the current full-context reviewer. "
                "Use a paper-search tool implementation before reviewing it."
            ),
        )
    pages = "\n\n".join(
        f"[Page {page['page']}]\n{page['text']}" for page in parsed["pages"]
    )
    return (
        "<active_skill name=\"paper-reviewer\">\n"
        f"{_paper_reviewer_skill_text(messages, skill_mode)}\n"
        "</active_skill>\n\n"
        "<manuscript>\n"
        f"Title: {parsed['title']}\n"
        f"Pages: {parsed['page_count']}\n\n"
        f"{pages}\n"
        "</manuscript>"
    )


def _paper_visual_context(paper_id: Any) -> str:
    paper_path(paper_id)
    skill = load_paper_visual_extractor_skill()
    return (
        '<active_skill name="paper-visual-extractor">\n'
        f"{skill['instructions']}\n\n{skill['classification']}\n"
        "</active_skill>"
    )


def _paper_downloader_context() -> str:
    skill = load_paper_downloader_skill()
    return (
        '<active_skill name="paper-downloader">\n'
        f"{skill['instructions']}\n"
        "</active_skill>"
    )


def _paper_search_context() -> str:
    skill = load_paper_search_skill()
    return (
        '<active_skill name="paper-search">\n'
        f"{skill['instructions']}\n"
        "</active_skill>"
    )


def _skill_context(
    skill_name: str,
    paper_id: Any,
    messages: List[Dict[str, Any]],
    skill_mode: Any = "auto",
) -> str:
    """Load one Skill after explicit selection or an activate_skill tool call."""
    if skill_name not in ALLOWED_SKILLS:
        raise HTTPException(status_code=422, detail=f"Unsupported skill: {skill_name}")
    if skill_name == "paper-reviewer":
        if paper_id:
            return _paper_reviewer_context(paper_id, messages, skill_mode)
        return (
            '<active_skill name="paper-reviewer">\n'
            f"{_paper_reviewer_skill_text(messages, skill_mode)}\n"
            "</active_skill>\n\n"
            f"{_paper_downloader_context()}\n\n"
            "<paper_state>No local PDF is available. Identify and download the "
            "requested open paper before reviewing it. If the request does not "
            "identify a paper, ask the user for a title, DOI, arXiv ID, URL, or "
            "upload.</paper_state>"
        )
    if skill_name == "paper-visual-extractor":
        if paper_id:
            return _paper_visual_context(paper_id)
        skill = load_paper_visual_extractor_skill()
        return (
            '<active_skill name="paper-visual-extractor">\n'
            f"{skill['instructions']}\n\n{skill['classification']}\n"
            "</active_skill>\n\n"
            f"{_paper_downloader_context()}\n\n"
            "<paper_state>No local PDF is available. Download the identified open "
            "paper first, or ask the user to identify or upload it.</paper_state>"
        )
    if skill_name == "paper-search":
        return _paper_search_context()
    return _paper_downloader_context()


def _available_skills_context() -> str:
    lines = [
        "<available_skills>",
        "These are the Agent's locally installed Skills. Use their descriptions to identify "
        "relevant capabilities. A Skill's complete instructions and tools are loaded only "
        "when an <active_skill> block is also present; do not pretend an inactive Skill or "
        "its tools have been executed.",
    ]
    for skill in load_skill_catalog():
        lines.append(
            f'<skill name="{skill["name"]}">{skill["description"]}</skill>'
        )
    lines.append("</available_skills>")
    return "\n".join(lines)


def _agent_messages(
    messages: Any,
    config: AgentConfig,
    skill_name: Any = None,
    paper_id: Any = None,
    skill_mode: Any = "auto",
) -> List[Dict[str, Any]]:
    bounded_messages, _ = _bounded_conversation_messages(messages, config.context)
    instructions = (
        config.instructions
        + "\n\n"
        + RESPONSE_FORMATTING_GUIDANCE
        + "\n\n"
        + _available_skills_context()
    )
    if skill_name == "auto":
        instructions += (
            "\n\n<skill_routing mode=\"auto\">Inspect the request and available Skills. "
            "If one Skill materially helps, call activate_skill before claiming to use it. "
            "Use paper-search to find papers matching a description and present candidate "
            "choices. Use paper-downloader only to obtain and verify a local PDF for an "
            "already identified paper. Use paper-reviewer for review or explanation. Use "
            "paper-visual-extractor to display figures or tables. Answer ordinary questions "
            "without activating a Skill.</skill_routing>"
        )
    elif skill_name:
        instructions += "\n\n" + _skill_context(
            str(skill_name), paper_id, messages, skill_mode
        )
    return [{"role": "system", "content": instructions}, *bounded_messages]


def _apply_reasoning_control(
    request: Dict[str, Any], control: Any, selected: ResolvedModel
) -> None:
    for key in (
        "thinking",
        "reasoning",
        "reasoning_effort",
        "enable_thinking",
        "thinking_budget",
        "preserve_thinking",
        "output_config",
        "reasoning_control",
    ):
        request.pop(key, None)
    if control is None:
        return
    if not isinstance(control, dict):
        raise HTTPException(status_code=422, detail="reasoning_control must be an object")

    family = selected.profile.family
    policy = REASONING_POLICIES.get(family)
    if policy is None:
        if control:
            raise HTTPException(
                status_code=422,
                detail=f"Model {selected.profile.id} has no supported reasoning controls",
            )
        return

    enabled = control.get("enabled", policy["default_enabled"])
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=422, detail="reasoning_control.enabled must be boolean")
    if policy["always_enabled"] and not enabled:
        raise HTTPException(
            status_code=422,
            detail=f"{selected.profile.label} always runs in thinking mode",
        )

    effort = str(control.get("effort", policy["default_effort"])).strip()
    efforts = policy["efforts"]
    if enabled and efforts and effort not in efforts:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported reasoning effort for {selected.profile.label}: {effort}",
        )

    if family == "qwen":
        request["enable_thinking"] = enabled
        if enabled:
            request["reasoning_effort"] = effort
    elif family == "kimi":
        request["reasoning_effort"] = effort
    elif family == "glm":
        request["thinking"] = {"type": "enabled" if enabled else "disabled"}
    elif family == "deepseek":
        request["thinking"] = {"type": "enabled" if enabled else "disabled"}
        if enabled:
            request["reasoning_effort"] = effort


def _upstream_request(
    payload: Dict[str, Any],
    config: AgentConfig,
    include_tools: bool = False,
    selected: Optional[ResolvedModel] = None,
) -> Dict[str, Any]:
    selected = selected or resolve_model(config, payload.get("model_id"))
    request = dict(payload)
    request["model"] = selected.connection.name
    request["messages"] = _agent_messages(
        payload.get("messages"),
        config,
        payload.get("skill"),
        payload.get("paper_id"),
        payload.get("skill_mode", "auto"),
    )
    request.pop("skill", None)
    request.pop("paper_id", None)
    request.pop("skill_mode", None)
    request.pop("context_meta", None)
    request.pop("model_id", None)
    request.pop("tools", None)
    request.pop("tool_choice", None)
    _apply_reasoning_control(request, payload.get("reasoning_control"), selected)
    tools: List[Dict[str, Any]] = []
    if include_tools and config.tavily.enabled and config.tavily.api_key:
        tools.append(WEB_SEARCH_TOOL)
    if include_tools and payload.get("skill") == "auto":
        tools.append(ACTIVATE_SKILL_TOOL)
    if include_tools and (
        payload.get("skill") == "paper-downloader"
        or (
            payload.get("skill") in {"paper-reviewer", "paper-visual-extractor"}
            and not payload.get("paper_id")
        )
    ):
        tools.append(DOWNLOAD_PAPER_TOOL)
    if include_tools and payload.get("skill") in {
        "paper-reviewer",
        "paper-visual-extractor",
    } and payload.get("paper_id"):
        tools.extend([LIST_PAPER_VISUALS_TOOL, RENDER_PAPER_VISUALS_TOOL])
    if include_tools and payload.get("skill"):
        tools.append(UI_ACTION_TOOL)
    if tools:
        request["tools"] = tools
        request["tool_choice"] = "auto"
    return request


async def _web_search(query: str, config: AgentConfig) -> str:
    if not query.strip():
        return json.dumps({"error": "Search query is empty"})
    try:
        async with httpx.AsyncClient(
            timeout=_timeout(config.model, read_seconds=20),
            trust_env=config.model.use_system_proxy,
        ) as client:
            response = await client.post(
                "https://api.tavily.com/search",
                headers={"Authorization": f"Bearer {config.tavily.api_key}"},
                json={
                    "query": query,
                    "max_results": config.tavily.max_results,
                    "search_depth": "basic",
                    "include_answer": True,
                },
            )
        response.raise_for_status()
        data = response.json()
        result = {
            "answer": data.get("answer", ""),
            "results": [
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "content": item.get("content", ""),
                }
                for item in data.get("results", [])
            ],
        }
        return json.dumps(result, ensure_ascii=False)
    except (httpx.HTTPError, ValueError) as exc:
        return json.dumps({"error": f"Tavily search failed: {exc}"}, ensure_ascii=False)


def _tool_activity(
    tool_name: str,
    arguments: Dict[str, Any],
    parent_id: str,
    round_number: int,
) -> ActivitySpan:
    query = str(arguments.get("query", ""))[:300]
    source = str(arguments.get("source", ""))[:300]
    visual_ids = [str(item) for item in (arguments.get("visual_ids") or [])[:4]]
    definitions = {
        "activate_skill": (
            "skill.activate",
            "激活 Skill",
            {
                "skill": str(arguments.get("skill", ""))[:100],
                "reason": str(arguments.get("reason", ""))[:300],
            },
        ),
        "web_search": ("web.search", "联网搜索", {"query": query}),
        "download_paper": ("paper.download", "下载论文 PDF", {"source": source}),
        "list_paper_visuals": ("paper.visuals.scan", "扫描论文图表", {}),
        "render_paper_visuals": (
            "paper.visuals.render",
            "截取论文图表",
            {"visual_ids": visual_ids},
        ),
        "ui_action": (
            "ui.action",
            "呈现交互",
            {"ui_type": str(arguments.get("type", ""))[:40]},
        ),
    }
    action, title, detail = definitions.get(
        tool_name, ("tool.unknown", f"未知工具：{tool_name or '?'}", {})
    )
    return ActivitySpan(
        kind="tool",
        action=action,
        title=title,
        parent_id=parent_id,
        detail={"round": round_number, **detail},
    )


def _normalize_ui_options(raw: Any) -> List[Dict[str, str]]:
    if not isinstance(raw, list):
        return []
    options: List[Dict[str, str]] = []
    for item in raw[:5]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip()
        prompt = str(item.get("prompt", "")).strip()
        if not label or not prompt:
            continue
        option: Dict[str, str] = {"label": label[:80], "prompt": prompt[:4000]}
        skill = item.get("skill")
        if isinstance(skill, str) and skill in ALLOWED_SKILLS:
            option["skill"] = skill
        skill_mode = item.get("skill_mode")
        if isinstance(skill_mode, str) and skill_mode in ALLOWED_SKILL_MODES:
            option["skill_mode"] = skill_mode
        options.append(option)
    return options


def _build_ui_action(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize a ui_action call into a UI intent payload."""

    def _skill_fields(src: Dict[str, Any]) -> Dict[str, str]:
        fields: Dict[str, str] = {}
        skill = src.get("skill")
        if isinstance(skill, str) and skill in ALLOWED_SKILLS:
            fields["skill"] = skill
        skill_mode = src.get("skill_mode")
        if isinstance(skill_mode, str) and skill_mode in ALLOWED_SKILL_MODES:
            fields["skill_mode"] = skill_mode
        return fields

    def _fail(message: str) -> Dict[str, Any]:
        return {
            "content": json.dumps({"error": message}, ensure_ascii=False),
            "state": "failed",
            "message": message,
            "detail": {},
            "artifacts": [],
        }

    ui_type = str(arguments.get("type", "")).strip()
    if ui_type == "options":
        options = _normalize_ui_options(arguments.get("options"))
        if not options:
            return _fail("没有可呈现的选项")
        return {
            "content": json.dumps({"emitted": "options"}, ensure_ascii=False),
            "state": "completed",
            "message": f"已呈现 {len(options)} 个选项",
            "detail": {"option_count": len(options)},
            "artifacts": [],
            "ui": {"type": "options", "options": options},
        }
    if ui_type == "confirm":
        message_text = str(arguments.get("message", "")).strip()
        if not message_text:
            return _fail("确认内容不能为空")
        ui = {
            "type": "confirm",
            "title": str(arguments.get("title", "")).strip()[:200],
            "message": message_text[:1000],
            "confirm_label": str(arguments.get("confirm_label", "")).strip()[:40] or "确认",
            "cancel_label": str(arguments.get("cancel_label", "")).strip()[:40] or "取消",
            "prompt_on_confirm": str(arguments.get("prompt_on_confirm", "")).strip()[:4000] or "确认",
        }
        ui.update(_skill_fields(arguments))
        return {
            "content": json.dumps({"emitted": "confirm"}, ensure_ascii=False),
            "state": "completed",
            "message": "已请求用户确认",
            "detail": {},
            "artifacts": [],
            "ui": ui,
        }
    if ui_type == "input":
        label = str(arguments.get("label", "")).strip()
        if not label:
            return _fail("输入提示不能为空")
        ui = {
            "type": "input",
            "label": label[:200],
            "placeholder": str(arguments.get("placeholder", "")).strip()[:200],
            "submit_label": str(arguments.get("submit_label", "")).strip()[:40] or "发送",
        }
        template = arguments.get("prompt_template")
        if isinstance(template, str) and template.strip():
            ui["prompt_template"] = template.strip()[:4000]
        ui.update(_skill_fields(arguments))
        return {
            "content": json.dumps({"emitted": "input"}, ensure_ascii=False),
            "state": "completed",
            "message": "已请求用户输入",
            "detail": {},
            "artifacts": [],
            "ui": ui,
        }
    return _fail(f"未知交互类型：{ui_type or '?'}")


async def _execute_agent_tool(
    tool_name: str,
    arguments: Dict[str, Any],
    payload: Dict[str, Any],
    config: AgentConfig,
) -> Dict[str, Any]:
    if tool_name == "activate_skill":
        skill_name = str(arguments.get("skill", "")).strip()
        reason = str(arguments.get("reason", "")).strip()
        if skill_name not in ALLOWED_SKILLS:
            return {
                "content": json.dumps(
                    {"error": f"Unsupported skill: {skill_name}"}, ensure_ascii=False
                ),
                "state": "failed",
                "message": f"无法激活未知 Skill：{skill_name or '?'}",
                "detail": {"skill": skill_name},
                "artifacts": [],
                "activated_skill": None,
            }
        return {
            "content": json.dumps(
                {"activated_skill": skill_name, "reason": reason}, ensure_ascii=False
            ),
            "state": "completed",
            "message": f"已激活 Skill：{skill_name}",
            "detail": {"skill": skill_name, "reason": reason[:300]},
            "artifacts": [],
            "activated_skill": skill_name,
        }
    if tool_name == "web_search":
        query = str(arguments.get("query", ""))
        content = await _web_search(query, config)
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = {"error": "Search returned invalid JSON"}
        failed = bool(data.get("error"))
        return {
            "content": content,
            "state": "failed" if failed else "completed",
            "message": f"联网搜索失败：{query}" if failed else f"已完成联网搜索：{query}",
            "detail": {"query": query[:300], "result_count": len(data.get("results") or [])},
            "artifacts": [],
        }
    if tool_name == "download_paper":
        source = str(arguments.get("source", ""))
        try:
            downloaded = await download_paper(
                source, use_system_proxy=config.model.use_system_proxy
            )
        except PaperDownloadError as exc:
            return {
                "content": json.dumps({"error": str(exc)}, ensure_ascii=False),
                "state": "failed",
                "message": f"论文下载失败：{exc}",
                "detail": {"source": source[:300]},
                "artifacts": [],
                "paper": None,
            }
        parsed = downloaded.pop("parsed")
        paper = {
            key: downloaded[key]
            for key in (
                "paper_id",
                "filename",
                "title",
                "page_count",
                "character_count",
                "source_url",
            )
        }
        if parsed["character_count"] > MAX_PAPER_CONTEXT_CHARS:
            manuscript: Dict[str, Any] = {
                "error": (
                    "The paper was downloaded, but it is too large for the current "
                    "full-context reviewer."
                )
            }
        else:
            manuscript = {
                "title": parsed["title"],
                "pages": parsed["pages"],
            }
        return {
            "content": json.dumps(
                {"paper": paper, "manuscript": manuscript}, ensure_ascii=False
            ),
            "state": "completed",
            "message": f"已下载并解析论文：{paper['title']}",
            "detail": {
                "paper_id": paper["paper_id"],
                "page_count": paper["page_count"],
                "character_count": paper["character_count"],
            },
            "artifacts": [],
            "paper": paper,
        }
    if tool_name == "list_paper_visuals":
        visuals = list_paper_visuals(payload.get("paper_id"))
        recommended = [visual for visual in visuals if visual.get("recommended")]
        return {
            "content": json.dumps(
                {
                    "visuals": visuals,
                    "recommended": recommended,
                    "classification_basis": "caption-and-layout heuristics; pixels not inspected",
                },
                ensure_ascii=False,
            ),
            "state": "completed",
            "message": f"已识别 {len(visuals)} 个图表候选，推荐 {len(recommended)} 个",
            "detail": {
                "candidate_count": len(visuals),
                "recommended_count": len(recommended),
            },
            "artifacts": [],
        }
    if tool_name == "render_paper_visuals":
        visuals = render_paper_visuals(payload.get("paper_id"), arguments.get("visual_ids"))
        return {
            "content": json.dumps(
                {
                    "rendered": visuals,
                    "instruction": "The screenshots are already visible in the conversation. Refer to them by label and page.",
                },
                ensure_ascii=False,
            ),
            "state": "completed",
            "message": f"已截取 {len(visuals)} 个论文图表",
            "detail": {"rendered_count": len(visuals)},
            "artifacts": visuals,
        }
    if tool_name == "ui_action":
        return _build_ui_action(arguments)
    return {
        "content": json.dumps({"error": f"Unknown tool: {tool_name}"}),
        "state": "failed",
        "message": f"无法执行未知工具：{tool_name or '?'}",
        "detail": {},
        "artifacts": [],
    }


def _tool_calls_from_delta(
    tool_map: Dict[int, Dict[str, Any]], delta: Dict[str, Any]
) -> None:
    for item in delta.get("tool_calls") or []:
        index = int(item.get("index", 0))
        call = tool_map.setdefault(
            index,
            {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            },
        )
        if item.get("id"):
            call["id"] = item["id"]
        function = item.get("function") or {}
        if function.get("name"):
            call["function"]["name"] = function["name"]
        if function.get("arguments"):
            call["function"]["arguments"] += function["arguments"]


async def run_agent(payload: Dict[str, Any], config: AgentConfig) -> Response:
    try:
        selected = resolve_model(config, payload.get("model_id"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    model = selected.connection
    if not model.base_url or not model.name or not model.api_key:
        raise HTTPException(
            status_code=503,
            detail="Agent is not configured. Set model.base_url and model.api_key in config.local.toml.",
        )

    url = f"{model.base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {model.api_key}",
        "Content-Type": "application/json",
    }
    request = _upstream_request(payload, config, include_tools=False, selected=selected)

    if not request.get("stream", False):
        try:
            async with httpx.AsyncClient(
                timeout=_timeout(model),
                trust_env=model.use_system_proxy,
            ) as client:
                upstream = await client.post(url, headers=headers, json=request)
        except httpx.TimeoutException:
            return JSONResponse(
                status_code=504,
                content={"detail": "Model API timed out before returning a response."},
            )
        except httpx.HTTPError as exc:
            return JSONResponse(
                status_code=502,
                content={"detail": f"Model API connection failed: {exc}"},
            )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    async def stream() -> AsyncIterator[bytes]:
        _, server_context_stats = _bounded_conversation_messages(
            payload.get("messages"), config.context
        )
        context_stats = _reported_context_stats(payload, server_context_stats)
        run_span = ActivitySpan(
            kind="run",
            action="agent.run",
            title="Agent 执行",
            detail={
                "model_id": selected.profile.id,
                "model": selected.profile.label,
                "upstream_model": model.name,
                "skill": payload.get("skill") or None,
                "skill_mode": payload.get("skill_mode") or None,
                **context_stats,
            },
        )
        active_model_span: Optional[ActivitySpan] = None
        context_message = (
            f"Agent 已接收任务；上下文保留 {context_stats['included_turns']}/"
            f"{context_stats['total_turns']} 轮"
        )
        yield _sse_activity(run_span, "started", context_message)
        try:
            async with httpx.AsyncClient(
                timeout=_timeout(model),
                trust_env=model.use_system_proxy,
            ) as client:
                tool_request = _upstream_request(
                    payload, config, include_tools=True, selected=selected
                )
                for _round in range(MAX_TOOL_ROUNDS):
                    round_number = _round + 1
                    model_span = ActivitySpan(
                        kind="model",
                        action="model.generate",
                        title=f"调用模型 · 第 {round_number} 轮",
                        parent_id=run_span.id,
                        detail={
                            "round": round_number,
                            "model_id": selected.profile.id,
                            "model": selected.profile.label,
                        },
                    )
                    active_model_span = model_span
                    yield _sse_activity(
                        model_span,
                        "started",
                        f"正在请求模型（第 {round_number} 轮）",
                    )
                    tool_map: Dict[int, Dict[str, Any]] = {}
                    content_parts: List[str] = []
                    reasoning_parts: List[str] = []
                    upstream_done = False
                    async with client.stream(
                        "POST", url, headers=headers, json=tool_request
                    ) as upstream:
                        if upstream.status_code >= 400:
                            detail = (await upstream.aread()).decode("utf-8", errors="replace")
                            yield _sse_activity(
                                model_span,
                                "failed",
                                f"模型返回 HTTP {upstream.status_code}",
                                {"http_status": upstream.status_code},
                            )
                            active_model_span = None
                            yield _sse_activity(run_span, "failed", "Agent 因模型请求失败而停止")
                            yield _sse_error(
                                "MODEL_HTTP_ERROR",
                                f"Model API returned HTTP {upstream.status_code}: {detail[:1000]}",
                            )
                            return
                        async for line in upstream.aiter_lines():
                            stripped = line.strip()
                            if not stripped.startswith("data:"):
                                continue
                            data = stripped[5:].strip()
                            if data == "[DONE]":
                                upstream_done = True
                                continue
                            if not data:
                                continue
                            yield f"{stripped}\n\n".encode("utf-8")
                            try:
                                packet = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            delta = (packet.get("choices") or [{}])[0].get("delta") or {}
                            if delta.get("content"):
                                content_parts.append(delta["content"])
                            if delta.get("reasoning_content"):
                                reasoning_parts.append(delta["reasoning_content"])
                            _tool_calls_from_delta(tool_map, delta)

                    tool_calls = [tool_map[key] for key in sorted(tool_map)]
                    if not tool_calls:
                        if not upstream_done:
                            yield _sse_activity(model_span, "failed", "模型响应流意外中断")
                            active_model_span = None
                            yield _sse_activity(run_span, "failed", "Agent 因响应流中断而停止")
                            yield _sse_error(
                                "STREAM_INTERRUPTED",
                                "Model stream ended without a completion marker. Please retry.",
                                retryable=True,
                            )
                            return
                        if not content_parts:
                            yield _sse_activity(model_span, "failed", "模型没有生成可见回答")
                            active_model_span = None
                            yield _sse_activity(run_span, "failed", "Agent 没有获得可显示的回答")
                            yield _sse_error(
                                "EMPTY_RESPONSE",
                                "Model completed without producing a visible answer.",
                                retryable=True,
                            )
                            return
                        yield _sse_activity(
                            model_span,
                            "completed",
                            f"模型已完成回答（第 {round_number} 轮）",
                            {"answer_chars": len("".join(content_parts))},
                        )
                        active_model_span = None
                        yield _sse_activity(run_span, "completed", "Agent 已完成任务")
                        yield b"data: [DONE]\n\n"
                        return

                    yield _sse_activity(
                        model_span,
                        "completed",
                        f"模型请求执行 {len(tool_calls)} 个工具",
                        {
                            "tool_count": len(tool_calls),
                            "tool_names": [call["function"].get("name", "") for call in tool_calls],
                        },
                    )
                    active_model_span = None

                    for call in tool_calls:
                        if not call["id"]:
                            call["id"] = f"call_{uuid.uuid4().hex}"
                    assistant_message: Dict[str, Any] = {
                        "role": "assistant",
                        "content": "".join(content_parts) or None,
                        "tool_calls": tool_calls,
                    }
                    if reasoning_parts:
                        assistant_message["reasoning_content"] = "".join(reasoning_parts)
                    tool_request["messages"].append(assistant_message)

                    for call in tool_calls:
                        function = call["function"]
                        try:
                            arguments = json.loads(function.get("arguments") or "{}")
                        except json.JSONDecodeError:
                            arguments = {}
                        tool_name = str(function.get("name") or "")
                        tool_span = _tool_activity(
                            tool_name, arguments, model_span.id, round_number
                        )
                        yield _sse_activity(tool_span, "started", f"正在{tool_span.title}")
                        try:
                            outcome = await _execute_agent_tool(
                                tool_name, arguments, payload, config
                            )
                            result = str(outcome["content"])
                            ui_payload = outcome.get("ui")
                            if ui_payload:
                                yield _sse_packet({"agent_ui": ui_payload})
                            activated_skill = outcome.get("activated_skill")
                            if activated_skill:
                                payload["skill"] = activated_skill
                                skill_context = _skill_context(
                                    activated_skill,
                                    payload.get("paper_id"),
                                    payload.get("messages") or [],
                                    payload.get("skill_mode", "auto"),
                                )
                                tool_request["messages"][0]["content"] += (
                                    "\n\n" + skill_context
                                )
                                current_tools = [
                                    item
                                    for item in (tool_request.get("tools") or [])
                                    if item.get("function", {}).get("name")
                                    not in {
                                        "activate_skill",
                                        "download_paper",
                                        "list_paper_visuals",
                                        "render_paper_visuals",
                                    }
                                ]
                                if activated_skill == "paper-downloader":
                                    current_tools.append(DOWNLOAD_PAPER_TOOL)
                                elif activated_skill == "paper-search":
                                    pass  # web_search 与 ui_action 已在 current_tools 中
                                elif payload.get("paper_id"):
                                    current_tools.extend(
                                        [LIST_PAPER_VISUALS_TOOL, RENDER_PAPER_VISUALS_TOOL]
                                    )
                                else:
                                    current_tools.append(DOWNLOAD_PAPER_TOOL)
                                tool_request["tools"] = current_tools
                            downloaded_paper = outcome.get("paper")
                            if downloaded_paper:
                                payload["paper_id"] = downloaded_paper["paper_id"]
                                yield _sse_packet({"agent_paper": downloaded_paper})
                                current_tools = [
                                    item
                                    for item in (tool_request.get("tools") or [])
                                    if item.get("function", {}).get("name")
                                    not in {
                                        "download_paper",
                                        "list_paper_visuals",
                                        "render_paper_visuals",
                                    }
                                ]
                                if payload.get("skill") in {
                                    "paper-reviewer",
                                    "paper-visual-extractor",
                                }:
                                    current_tools.extend(
                                        [LIST_PAPER_VISUALS_TOOL, RENDER_PAPER_VISUALS_TOOL]
                                    )
                                tool_request["tools"] = current_tools
                            for visual in outcome["artifacts"]:
                                yield _sse_packet({"agent_artifact": visual})
                                artifact_span = ActivitySpan(
                                    kind="artifact",
                                    action="artifact.image",
                                    title=f"生成截图 · {visual.get('label', visual.get('id', '论文图表'))}",
                                    parent_id=tool_span.id,
                                    detail={
                                        "visual_id": visual.get("id"),
                                        "page": visual.get("page"),
                                        "url": visual.get("url"),
                                    },
                                )
                                yield _sse_activity(
                                    artifact_span,
                                    "started",
                                    f"正在生成 {visual.get('label', '论文图表')} 截图",
                                )
                                yield _sse_activity(
                                    artifact_span,
                                    "completed",
                                    f"已将 {visual.get('label', '论文图表')} 截图加入对话",
                                )
                            yield _sse_activity(
                                tool_span,
                                str(outcome["state"]),
                                str(outcome["message"]),
                                outcome["detail"],
                            )
                        except Exception as exc:
                            yield _sse_activity(
                                tool_span,
                                "failed",
                                f"{tool_span.title}失败：{exc}",
                            )
                            raise
                        tool_request["messages"].append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": result,
                            }
                        )

                yield _sse_activity(run_span, "failed", "Agent 达到工具调用轮次上限")
                yield _sse_error(
                    "TOOL_ROUND_LIMIT",
                    f"Agent stopped after reaching the {MAX_TOOL_ROUNDS}-round tool limit.",
                )
        except httpx.TimeoutException:
            if active_model_span is not None:
                yield _sse_activity(active_model_span, "failed", "模型请求超时")
            yield _sse_activity(run_span, "failed", "Agent 因模型请求超时而停止")
            yield _sse_error(
                "MODEL_TIMEOUT",
                "No data arrived from the model within the configured timeout.",
                retryable=True,
            )
        except httpx.HTTPError as exc:
            if active_model_span is not None:
                yield _sse_activity(active_model_span, "failed", "模型连接失败")
            yield _sse_activity(run_span, "failed", "Agent 因模型连接失败而停止")
            yield _sse_error(
                "MODEL_CONNECTION_ERROR",
                f"Model API connection failed: {exc}",
                retryable=True,
            )
        except Exception as exc:
            if active_model_span is not None:
                yield _sse_activity(active_model_span, "failed", "模型调用失败")
            yield _sse_activity(run_span, "failed", f"Agent 执行失败：{exc}")
            yield _sse_error("AGENT_INTERNAL_ERROR", f"Agent failed: {exc}")

    return StreamingResponse(stream(), media_type="text/event-stream")
