from dataclasses import dataclass, field

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 10721
_DEFAULT_LOG_LEVEL = "INFO"
_DEFAULT_LOG_FILE = "~/.mc/logs/core.log"
_DEFAULT_LOG_FORMAT = "text"
_DEFAULT_CONFIG_PATH = "~/.mc/config.toml"
_DEFAULT_MAX_STEPS = 20
_DEFAULT_MODEL = "claude-sonnet-4-6"
_DEFAULT_TRACE_FILE = "~/.mc/logs/trace.jsonl"
_DEFAULT_SANDBOX_MODE = "workspace"

# 沙箱支持的模式：off（不隔离）/ workspace（写限定工作区）/ strict（在 workspace 基础上禁网）
SANDBOX_MODES: frozenset[str] = frozenset({"off", "workspace", "strict"})


@dataclass
class LoggingConfig:
    level: str = _DEFAULT_LOG_LEVEL
    file: str = _DEFAULT_LOG_FILE
    format: str = _DEFAULT_LOG_FORMAT  # "text" | "json"


@dataclass
class AgentConfig:
    max_steps: int = _DEFAULT_MAX_STEPS


@dataclass
class LlmConfig:
    default_model: str = _DEFAULT_MODEL
    router: str = "static"  # "static" | "rule_based" (S4) | "cost_budget" (S6)


@dataclass
class TraceConfig:
    enabled: bool = False
    file: str = _DEFAULT_TRACE_FILE
    include_llm_payload: bool = False

@dataclass
class PermissionConfig:
    timeout_s: float = 60.0  # 审批超时秒数；0 表示不超时

@dataclass
class CompactionConfig:
    auto_threshold: float = 0.0    # context_pct 触发自动压缩的阈值（0 表示禁用，推荐用手动 /compact）
    tool_result_limit: int = 8_000  # tool_result 截断触发字符数
    tool_result_keep: int = 4_000   # 截断后保留的前缀字符数

@dataclass
class SandboxConfig:
    mode: str = _DEFAULT_SANDBOX_MODE  # off | workspace | strict
    allow_write: list[str] = field(default_factory=list)  # workspace 外额外可写路径
    allow_read: list[str] = field(default_factory=list)   # 家目录外额外可读路径

@dataclass
class McpServerConfig:
    name: str
    transport: str = "stdio"       # "stdio" | "tcp"
    command: str = ""              # stdio 专用：可执行文件路径
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    host: str = "localhost"        # tcp 专用
    port: int = 3000               # tcp 专用

@dataclass
class McpConfig:
    servers: list[McpServerConfig] = field(default_factory=list)

@dataclass
class MiniClaudeConfig:
    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    trace: TraceConfig = field(default_factory=TraceConfig)
    permission: PermissionConfig = field(default_factory=PermissionConfig)
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    mcp: McpConfig = field(default_factory=McpConfig)


