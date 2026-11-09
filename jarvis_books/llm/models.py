"""Typed profiles, requests, responses and runtime configuration for the adaptive engine."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class ModelProfile:
    provider: str
    model_id: str
    version: Optional[str] = None
    display_name: Optional[str] = None
    family: Optional[str] = None
    tier: Optional[str] = None
    generation: Optional[float] = None
    preview: bool = False
    methods: list[str] = field(default_factory=list)
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    supports_streaming: Optional[bool] = None
    supports_tools: Optional[bool] = None
    supports_json_mode: Optional[bool] = None
    supports_thinking: Optional[bool] = None
    supports_system_messages: Optional[bool] = None
    supports_audio_out: Optional[bool] = None
    supports_live: Optional[bool] = None
    supports_embedding: Optional[bool] = None
    available: Optional[bool] = None
    metadata_source: str = "unknown"
    confidence: float = 0.0
    limitations: list[str] = field(default_factory=list)

    @property
    def kind(self) -> str:
        if self.supports_live:
            return "live"
        if self.supports_embedding:
            return "embedding"
        if self.supports_audio_out:
            return "tts"
        return "text"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ModelProfile":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class HardwareProfile:
    total_ram_bytes: Optional[int]
    available_ram_bytes: Optional[int]
    cpu_count: Optional[int]
    gpu_available: bool
    gpu_name: Optional[str]
    disk_free_bytes: Optional[int]
    audio_inputs: Optional[list[str]]
    audio_outputs: Optional[list[str]]
    network_latency_ms: Optional[float]
    detection_warnings: list[str] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)
    measured_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CapabilityResult:
    test_id: str
    model_id: str
    version: Optional[str]
    success: bool
    latency_s: float
    detail: str = ""
    timestamp: float = 0.0
    confidence: float = 0.0


@dataclass
class PromptSection:
    """A labeled block of prompt context. Lower priority sections are trimmed first; required ones never."""
    name: str
    text: str
    priority: int = 50
    required: bool = False
    trim_from: str = "end"  # which side to cut when shrinking: "end" keeps the start, "start" keeps the end

    def render(self) -> str:
        return f"## {self.name}\n{self.text.strip()}" if self.text.strip() else ""


@dataclass
class LLMRequest:
    role: str
    instruction: str
    system: str = ""
    sections: list[PromptSection] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)  # Gemini "contents" turns, oldest first
    json_schema: Optional[dict] = None
    tools: list[dict] = field(default_factory=list)  # Gemini function declarations
    max_output_tokens: Optional[int] = None
    temperature: Optional[float] = None
    purpose: str = ""
    allow_fallback: bool = True


@dataclass
class LLMResponse:
    text: str
    model: str
    data: Any = None
    function_calls: list[dict] = field(default_factory=list)
    content: Optional[dict] = None  # raw model turn, for multi-turn tool use
    finish_reason: Optional[str] = None
    prompt_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    latency_s: float = 0.0
    trimmed: list[str] = field(default_factory=list)
    attempts: int = 1


@dataclass(frozen=True)
class ModelBudget:
    model_id: str
    input_budget_tokens: int
    output_budget_tokens: int
    context_verified: bool
    rpm: int
    rpd: Optional[int]
    tpm: Optional[int]


@dataclass(frozen=True)
class RuntimeConfig:
    version: int
    routes: dict[str, tuple[str, ...]]  # role -> models in preference order
    budgets: dict[str, ModelBudget]
    memory_budget_tokens: int
    history_turns: int
    words_per_call: int
    scene_words: int
    concurrency: int
    tts_workers: int
    embed_batch: int
    max_retries: int
    adaptive: bool
    reason: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["routes"] = {k: list(v) for k, v in self.routes.items()}
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "RuntimeConfig":
        data = dict(data)
        data["routes"] = {k: tuple(v) for k, v in data["routes"].items()}
        data["budgets"] = {k: ModelBudget(**v) for k, v in data["budgets"].items()}
        return cls(**data)
