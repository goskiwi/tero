"""One configuration for model access, permissions, and execution budgets."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_env(directory: Path):
    """Load local configuration without evaluating shell expressions."""
    values = {}
    for name in (".env", ".env.local"):
        path = directory / name
        if not path.is_file() or path.is_symlink():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.removeprefix("export ").partition("=")
            if not separator or not key.strip().isidentifier():
                raise ValueError(f"Invalid configuration line in {name}")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    for key, value in values.items():
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Config:
    api_key: str = field(default="", repr=False)
    base_url: str = "https://www.rightapi.ai/codex/v1"
    model: str = "gpt-5.6-luna"
    mode: str = "code"
    max_turns: int = 32
    max_parallel_tools: int = 4
    runtime_seconds: float = 600
    request_seconds: float = 300
    tool_seconds: float = 120
    context_tokens: int = 272000
    output_tokens: int = 32000
    compaction_trigger_tokens: int = 0
    recent_tokens: int = 12000
    summary_tokens: int = 4096
    summary_generation_tokens: int = 32768
    memory_tokens: int = 2000
    repo_map_tokens: int = 1200
    verify_command: str = ""
    allowed_write_paths: tuple[str, ...] | None = None
    memory_enabled: bool = True
    repo_map_enabled: bool = True

    def __post_init__(self):
        if not 1 <= self.max_parallel_tools <= 4:
            raise ValueError("max_parallel_tools must be between 1 and 4")
        if self.mode not in {"ask", "code", "auto"}:
            raise ValueError("mode must be ask, code, or auto")
        for name in (
            "max_turns",
            "runtime_seconds",
            "request_seconds",
            "tool_seconds",
            "context_tokens",
            "output_tokens",
            "recent_tokens",
            "summary_tokens",
            "summary_generation_tokens",
            "memory_tokens",
            "repo_map_tokens",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not 0 <= self.compaction_trigger_tokens < self.context_tokens:
            raise ValueError(
                "Compaction trigger must be zero (disabled) or below the context window"
            )
        if self.output_tokens >= self.context_tokens:
            raise ValueError("Output reserve must be smaller than the context window")
        object.__setattr__(self, "verify_command", self.verify_command.strip())

    @classmethod
    def from_env(cls, **options):
        for field_name, env_name in (
            ("context_tokens", "TERO_CONTEXT_TOKENS"),
            ("output_tokens", "TERO_OUTPUT_TOKENS"),
        ):
            if options.get(field_name) is None:
                options[field_name] = int(os.environ.get(env_name, getattr(cls, field_name)))
        return cls(
            api_key=os.environ.get("TERO_OPENAI_API_KEY", ""),
            base_url=os.environ.get("TERO_OPENAI_API_BASE", cls.base_url),
            model=os.environ.get("TERO_OPENAI_MODEL", cls.model),
            **options,
        )
