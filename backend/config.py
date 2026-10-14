from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.local.toml"


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    name: str
    api_key: str
    connect_timeout_seconds: float
    read_timeout_seconds: float
    use_system_proxy: bool


@dataclass(frozen=True)
class ModelProfile:
    id: str
    label: str
    vendor: str
    family: str
    name: str
    base_url: str = ""
    api_key: str = ""
    documentation_status: str = "verified"
    note: str = ""


@dataclass(frozen=True)
class ResolvedModel:
    profile: ModelProfile
    connection: ModelConfig


@dataclass(frozen=True)
class TavilyConfig:
    enabled: bool
    api_key: str
    max_results: int


@dataclass(frozen=True)
class ContextConfig:
    max_chars: int = 60_000
    recent_turns: int = 4
    max_message_chars: int = 12_000
    outline_chars: int = 4_000


DEFAULT_UI_THEME: Dict[str, str] = {
    "background": "#FAF2E0",
    "panel": "#FFFCF5",
    "text": "#17324A",
    "border": "#D8CDB8",
    "accent": "#0E61AC",
}
HEX_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")


@dataclass(frozen=True)
class AgentConfig:
    name: str
    instructions: str
    model: ModelConfig
    tavily: TavilyConfig
    default_model_id: str = ""
    models: Mapping[str, ModelProfile] = field(default_factory=dict)
    ui_theme: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_UI_THEME))
    context: ContextConfig = field(default_factory=ContextConfig)


BUILTIN_MODELS: Tuple[ModelProfile, ...] = (
    ModelProfile("qwen3-8-max", "Qwen3.8-Max", "阿里巴巴（千问）", "qwen", "Qwen3.8-Max"),
    ModelProfile("qwen3-8-flash", "Qwen3.8-Flash", "阿里巴巴（千问）", "qwen", "Qwen3.8-Flash"),
    ModelProfile("kimi-k3", "Kimi K3", "月之暗面（Kimi）", "kimi", "Kimi-K3"),
    ModelProfile(
        "glm-5-3",
        "GLM-5.3",
        "智谱 AI",
        "glm",
        "GLM-5.3",
        documentation_status="unverified_model_id",
        note="智谱官网尚未检索到 GLM-5.3 模型页；当前名称按所用聚合网关的模型别名处理。",
    ),
    ModelProfile(
        "deepseek-v4-pro-0813",
        "DeepSeek-V4-Pro-0813",
        "DeepSeek",
        "deepseek",
        "DeepSeek-V4-Pro-0813",
    ),
    ModelProfile(
        "deepseek-v4-flash",
        "DeepSeek-V4-Flash",
        "DeepSeek",
        "deepseek",
        "DeepSeek-V4-Flash",
    ),
)


REASONING_POLICIES: Dict[str, Dict[str, Any]] = {
    "qwen": {
        "can_toggle": True,
        "always_enabled": False,
        "default_enabled": True,
        "efforts": ["low", "medium", "xhigh"],
        "default_effort": "xhigh",
        "hint": "支持关闭思考；开启后可选 low / medium / xhigh。",
    },
    "kimi": {
        "can_toggle": False,
        "always_enabled": True,
        "default_enabled": True,
        "efforts": ["low", "high", "max"],
        "default_effort": "max",
        "hint": "Kimi K3 始终思考，只能调整 low / high / max。",
    },
    "glm": {
        "can_toggle": True,
        "always_enabled": False,
        "default_enabled": True,
        "efforts": [],
        "default_effort": "",
        "hint": "按 GLM 系列协议切换思考；GLM-5.3 的强度档位尚无官网依据。",
    },
    "deepseek": {
        "can_toggle": True,
        "always_enabled": False,
        "default_enabled": True,
        "efforts": ["low", "high", "max"],
        "default_effort": "high",
        "hint": "支持关闭思考；开启后可选 low / high / max。",
    },
}


def _table(data: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"[{key}] must be a TOML table")
    return value


def _positive_float(value: Any, key: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise ValueError(f"{key} must be greater than zero")
    return parsed


def _ui_theme(data: Dict[str, Any]) -> Dict[str, str]:
    ui = _table(data, "ui")
    configured = _table(ui, "theme")
    theme = dict(DEFAULT_UI_THEME)
    for key in theme:
        value = str(configured.get(key, theme[key])).strip()
        if HEX_COLOR_RE.fullmatch(value):
            theme[key] = value.upper()
    return theme


def _model_profiles(data: Dict[str, Any]) -> Dict[str, ModelProfile]:
    configured = _table(data, "models")
    profiles: Dict[str, ModelProfile] = {}
    for builtin in BUILTIN_MODELS:
        override = configured.get(builtin.id, {})
        if not isinstance(override, dict):
            raise ValueError(f"[models.{builtin.id}] must be a TOML table")
        if override.get("enabled", True) is False:
            continue
        profiles[builtin.id] = replace(
            builtin,
            label=str(override.get("label", builtin.label)).strip() or builtin.label,
            name=str(override.get("name", builtin.name)).strip() or builtin.name,
            base_url=str(override.get("base_url", builtin.base_url)).rstrip("/"),
            api_key=str(override.get("api_key", builtin.api_key)).strip(),
        )
    if not profiles:
        raise ValueError("At least one model profile must be enabled")
    return profiles


def _default_model_id(model: Dict[str, Any], profiles: Mapping[str, ModelProfile]) -> str:
    explicit = str(model.get("default", "")).strip()
    if explicit:
        if explicit not in profiles:
            raise ValueError(f"model.default references an unknown model: {explicit}")
        return explicit
    legacy_name = str(model.get("name", "")).strip().casefold()
    if legacy_name:
        for profile in profiles.values():
            if profile.name.casefold() == legacy_name or profile.label.casefold() == legacy_name:
                return profile.id
    return next(iter(profiles))


def _context_config(data: Dict[str, Any]) -> ContextConfig:
    configured = _table(data, "context")
    max_chars = max(12_000, min(int(configured.get("max_chars", 60_000)), 200_000))
    recent_turns = max(1, min(int(configured.get("recent_turns", 4)), 12))
    max_message_chars = max(
        2_000,
        min(int(configured.get("max_message_chars", 12_000)), 50_000, max_chars // 2),
    )
    outline_chars = max(500, min(int(configured.get("outline_chars", 4_000)), 8_000))
    return ContextConfig(
        max_chars=max_chars,
        recent_turns=recent_turns,
        max_message_chars=max_message_chars,
        outline_chars=outline_chars,
    )


def load_config(path: Path = CONFIG_PATH) -> AgentConfig:
    if not path.exists():
        raise RuntimeError(
            "Missing config.local.toml. Copy config.example.toml and add your model settings."
        )
    with path.open("rb") as handle:
        data = tomllib.load(handle)

    agent = _table(data, "agent")
    model = _table(data, "model")
    tools = _table(data, "tools")
    tavily = _table(tools, "tavily")
    profiles = _model_profiles(data)
    default_model_id = _default_model_id(model, profiles)
    default_profile = profiles[default_model_id]
    return AgentConfig(
        name=str(agent.get("name", "InferTree Agent")).strip() or "InferTree Agent",
        instructions=str(agent.get("instructions", "You are a helpful assistant.")).strip(),
        model=ModelConfig(
            base_url=str(model.get("base_url", "")).rstrip("/"),
            name=default_profile.name,
            api_key=str(model.get("api_key", "")).strip(),
            connect_timeout_seconds=_positive_float(
                model.get("connect_timeout_seconds", 10), "model.connect_timeout_seconds"
            ),
            read_timeout_seconds=_positive_float(
                model.get("read_timeout_seconds", 45), "model.read_timeout_seconds"
            ),
            use_system_proxy=bool(model.get("use_system_proxy", False)),
        ),
        tavily=TavilyConfig(
            enabled=bool(tavily.get("enabled", False)),
            api_key=str(tavily.get("api_key", "")).strip(),
            max_results=max(1, min(int(tavily.get("max_results", 5)), 10)),
        ),
        default_model_id=default_model_id,
        models=profiles,
        ui_theme=_ui_theme(data),
        context=_context_config(data),
    )


def resolve_model(config: AgentConfig, requested_id: Any = None) -> ResolvedModel:
    profiles = config.models or {"legacy": ModelProfile(
        "legacy", config.model.name or "Configured model", "Custom", "legacy", config.model.name
    )}
    model_id = str(requested_id or config.default_model_id or next(iter(profiles))).strip()
    profile = profiles.get(model_id)
    if profile is None:
        raise ValueError(f"Unsupported model_id: {model_id}")
    connection = replace(
        config.model,
        name=profile.name,
        base_url=profile.base_url or config.model.base_url,
        api_key=profile.api_key or config.model.api_key,
    )
    return ResolvedModel(profile=profile, connection=connection)


def model_catalog(config: AgentConfig) -> Dict[str, Any]:
    profiles = config.models or {
        "legacy": ModelProfile("legacy", config.model.name, "Custom", "legacy", config.model.name)
    }
    default_id = config.default_model_id or next(iter(profiles))
    items: List[Dict[str, Any]] = []
    for profile in profiles.values():
        policy = REASONING_POLICIES.get(profile.family, {
            "can_toggle": False,
            "always_enabled": False,
            "default_enabled": False,
            "efforts": [],
            "default_effort": "",
            "hint": "该自定义模型没有预设思考控制。",
        })
        items.append({
            "id": profile.id,
            "label": profile.label,
            "vendor": profile.vendor,
            "family": profile.family,
            "reasoning": dict(policy),
            "documentation_status": profile.documentation_status,
            "note": profile.note,
        })
    return {"default_model_id": default_id, "models": items}


def ui_theme(config: AgentConfig) -> Dict[str, str]:
    """Return the browser-safe theme palette only."""
    return dict(config.ui_theme)


def context_policy(config: AgentConfig) -> Dict[str, int]:
    """Return only non-sensitive conversation context limits for the GUI."""
    return {
        "max_chars": config.context.max_chars,
        "recent_turns": config.context.recent_turns,
        "max_message_chars": config.context.max_message_chars,
        "outline_chars": config.context.outline_chars,
    }


def config_status() -> Dict[str, Any]:
    try:
        config = load_config()
    except (OSError, RuntimeError, ValueError) as exc:
        return {"configured": False, "error": str(exc)}
    default_model = resolve_model(config)
    missing = [
        key
        for key, value in {
            "model.base_url": default_model.connection.base_url,
            "model.api_key": default_model.connection.api_key,
        }.items()
        if not value
    ]
    return {
        "configured": not missing,
        "agent_name": config.name,
        "model": default_model.profile.label,
        "default_model_id": config.default_model_id,
        "model_count": len(config.models) or 1,
        "tavily_enabled": config.tavily.enabled and bool(config.tavily.api_key),
        "missing": missing,
    }
