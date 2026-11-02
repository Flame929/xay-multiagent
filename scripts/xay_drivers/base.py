"""xay 执行者驱动的共同数据结构。

这里只放协议层的数据结构和很小的配置映射，具体的进程协议在各驱动文件中实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional


@dataclass
class Capabilities:
    """看板需要展示的能力声明。字段名与 arch-v4 §3.2/§5.2 一致。"""

    steer: str = "none"
    interrupt: str = "kill"
    resume: bool = False
    approve: bool = False
    file_events: bool = False
    usage_granularity: str = "turn"
    readonly_sandbox: bool = False
    structured_output: bool = False
    cli_version: str = ""
    schema_sha256: str = ""
    schema_valid: bool = True
    degraded: bool = False
    degraded_reason: str = ""

    def as_dict(self) -> dict:
        return {
            "steer": self.steer,
            "interrupt": self.interrupt,
            "resume": self.resume,
            "approve": self.approve,
            "file_events": self.file_events,
            "usage_granularity": self.usage_granularity,
            "readonly_sandbox": self.readonly_sandbox,
            "structured_output": self.structured_output,
        }


@dataclass
class ExecHandle:
    """一个驱动实例的句柄。

    ``driver`` 是实际执行该句柄的驱动；app-server 版本不兼容时它可以指向
    codex-exec，从而让上层不用再写一套分支。
    """

    driver: Any
    process: Any = None
    native_session_id: Optional[str] = None
    native_turn_id: Optional[str] = None
    cwd: Optional[str] = None
    card: dict = field(default_factory=dict)
    safety: Any = None
    metadata: dict = field(default_factory=dict)
    closed: bool = False


class Driver:
    """所有执行者驱动遵循的最小接口。"""

    name = "driver"
    capabilities = Capabilities()

    def probe(self) -> Capabilities:
        return self.capabilities

    def start(self, cwd, card, safety) -> ExecHandle:
        raise NotImplementedError

    def resume(self, native_session_id, cwd, safety) -> ExecHandle:
        raise NotImplementedError

    def prompt(self, handle, text, output_schema=None):
        raise NotImplementedError

    def steer(self, handle, text, expected_turn) -> bool:
        return False

    def interrupt(self, handle, grace_s=5) -> str:
        return "kill"

    def close(self, handle, discard=False):
        handle.closed = True

    def events(self) -> Iterator[dict]:
        return iter(())


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def safety_value(safety: Any, key: str, default: Any = None) -> Any:
    """同时支持契约中的 dict 和调用方常用的简单对象。"""

    if isinstance(safety, str):
        return safety if key in ("safety_tier", "tier") else default
    return _value(safety, key, default)


def resolve_sandbox(card: Any, safety: Any) -> str:
    mode = _value(card, "mode", "write")
    tier = safety_value(safety, "safety_tier", safety_value(safety, "tier", "standard"))
    if tier == "open":
        return "danger-full-access"
    if mode in ("readonly", "read-only", "ro"):
        return "read-only"
    return "workspace-write"


def resolve_provider(card: Any, safety: Any) -> str:
    source = _value(card, "source", "auto")
    tier = safety_value(safety, "safety_tier", safety_value(safety, "tier", "standard"))
    confidential = bool(
        safety_value(safety, "confidential", False)
        or _value(card, "confidential", False)
    )
    if confidential or source == "sub" or tier == "strict":
        return "openai"
    if source == "relay":
        return "custom"
    return "custom" if tier == "standard" else "openai"


def turn_sandbox_policy(card: Any, safety: Any) -> dict:
    """把任务卡的 net 开关转换成 turn/start 的协议字段。"""

    sandbox = resolve_sandbox(card, safety)
    network = bool(_value(card, "net", False))
    if sandbox == "danger-full-access":
        return {"type": "dangerFullAccess"}
    if sandbox == "read-only":
        return {"type": "readOnly", "networkAccess": network}
    return {"type": "workspaceWrite", "networkAccess": network}


def config_args(card: Any, safety: Any) -> list[str]:
    """生成必须显式出现在进程命令行里的 Codex 配置覆盖。"""

    sandbox = resolve_sandbox(card, safety)
    provider = resolve_provider(card, safety)
    effort = _value(card, "effort", "medium") or "medium"
    args = [
        "-c", f'model_provider="{provider}"',
        "-c", 'approval_policy="never"',
        "-c", f'sandbox_mode="{sandbox}"',
        "-c", f'model_reasoning_effort="{effort}"',
    ]
    model = _value(card, "model")
    if isinstance(model, str) and model:
        args.extend(["-c", f'model="{model}"'])
    # 这些名称来自本机配置和 arch-v4 §5.3。全部显式关闭，避免继承用户配置。
    # 0.165 仍会先解析用户旧配置；lingxing 的旧 sse type 已改名，先把
    # 类型改成当前名称，再关闭它，否则 app-server 会在启动前直接退出。
    args.extend(["-c", 'mcp_servers.lingxing.type="streamable_http"'])
    for server in ("lingxing", "computer-use", "node_repl"):
        args.extend(["-c", f"mcp_servers.{server}.enabled=false"])
    # 用户配置里通常没有 xay 这一节。0.165 对“新建但不完整”的 MCP 节会
    # 重新校验旧配置，因此给它一个完整的、关闭的 stdio 声明，再保留显式
    # enabled=false，确保 ps 和配置审计都能看到关闭开关。
    args.extend([
        "-c", 'mcp_servers.xay={enabled=false,type="stdio",command="/usr/bin/true"}',
        "-c", "mcp_servers.xay.enabled=false",
    ])
    return args

