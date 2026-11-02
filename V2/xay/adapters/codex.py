"""Codex 适配器：驱动 ``codex app-server --listen stdio://``（按行传输、省略 jsonrpc 字段的 JSON-RPC）。

目前一个适配器一个进程（以后 arch-v10 §3.1 要做"每条线路一个共享进程"，接口不变）。

移植自 scripts/xay_drivers/codex_appserver.py，并修好已知问题：
- 续接（thread/resume）沿用原来的线路 / 模型 / 强度。旧驱动续接时传了空卡片，悄悄切到了中转站。
  现在 open() 无论新建还是续接，都用同一个卡片算同一组 -c 参数和 thread 参数。
- 不再传 ``mcp_servers.<x>.type``（Codex 会忽略），只留 ``.enabled=false``。
- 不再有 app-server -> exec 的降级（旧代码读错了队列）；app-server 起不来就直接报错。

协议细节（schema 来自本机 `codex app-server generate-json-schema`，行为来自 V2/docs/probes.md）：
- 压缩（thread/compact/start）是单独的一轮（contextCompaction 条目）。这一轮没有 turn/completed 之前
  发 turn/start 会被拒绝，所以 send() 会先等压缩结束。
- 记忆条 = thread/tokenUsage/updated 里 ``last.totalTokens`` ÷ ``modelContextWindow``（不能用 total，它是累计值）。
- "Selected model is at capacity"（codexErrorInfo=serverOverloaded）归为 error kind = capacity。
"""

from __future__ import annotations

import collections
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

from .. import providers
from .base import (
    Adapter, AdapterBusy, AdapterError, Capabilities, classify_error, clip, clip_tail,
    close_pipe, context_entries, strip_shell, stop_process, MAX_TOOL_TEXT,
)


def disabled_mcp_servers() -> tuple[str, ...]:
    """读取用户本机 Codex 配置中的所有 MCP 服务，加上默认要关掉的插件。
    员工不该使用负责人的私有插件。
    """
    defaults = ["computer-use", "node_repl"]
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        cfg_path = Path(codex_home) / "config.toml"
    else:
        cfg_path = Path.home() / ".codex" / "config.toml"
    servers: list[str] = []
    if cfg_path.is_file():
        try:
            import tomllib
            with cfg_path.open("rb") as f:
                data = tomllib.load(f)
            mcp = data.get("mcp_servers")
            if isinstance(mcp, dict):
                servers = list(mcp.keys())
        except Exception:
            servers = []
    seen = set()
    res = []
    for s in servers + defaults:
        if s not in seen:
            seen.add(s)
            res.append(s)
    return tuple(res)


DISABLED_MCP_SERVERS = disabled_mcp_servers
DEFAULT_REASONING_SUMMARY = "detailed"  # 待真机验证：默认配置的思考摘要是空的（probes.md）

_PLAN_STATUS = {"pending": "pending", "inProgress": "in_progress", "completed": "completed"}
_TURN_STATUS = {"completed": "completed", "failed": "failed", "interrupted": "interrupted"}


def _join_text(value: Any, depth: int = 0) -> str:
    """从 str / {text} / [..] 里取出纯文字。"""
    if depth > 5 or value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "content", "summary"):
            if key in value:
                return _join_text(value[key], depth + 1)
        return ""
    if isinstance(value, (list, tuple)):
        return "\n".join(part for part in (_join_text(v, depth + 1) for v in value) if part)
    return ""


def _error_parts(err: Any) -> tuple:
    """codex 的 error 对象 -> (message, code)。code 可能是字符串，也可能是 {名字: {...}}。"""
    if not isinstance(err, dict):
        return (str(err) if err else ""), None
    info = err.get("codexErrorInfo")
    code = None
    if isinstance(info, str):
        code = info
    elif isinstance(info, dict) and info:
        code = next(iter(info))
    return str(err.get("message") or ""), code


class CodexAdapter(Adapter):
    agent = "codex"

    def __init__(self, command=None, rpc_timeout: float = 30.0, compact_timeout: float = 180.0) -> None:
        super().__init__()
        self._command = command
        self.rpc_timeout = rpc_timeout
        self.compact_timeout = compact_timeout
        self.capabilities = Capabilities(
            steer="live", interrupt="soft", resume=True, native_compact=True,
            usage="live", thinking=True, plan=True, persistent_process=True)
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._stderr_tail: collections.deque = collections.deque(maxlen=40)
        self._write_lock = threading.Lock()
        self._pending: Dict[int, "queue.Queue[dict]"] = {}
        self._next_id = 1
        self._cfg: Dict[str, Any] = {}
        # 一轮的状态
        self._turn_active = False
        self._turn_id: Optional[str] = None
        self._killing = False
        self._reset_turn()
        # 压缩状态：None / "requested"（已发请求，等压缩这一轮开始）/ "running"
        self._compact_state: Optional[str] = None
        self._compact_turn_id: Optional[str] = None
        self._compact_done = threading.Event()
        self._compact_done.set()
        self._compact_ok = False
        self._compaction_open = False
        self._compaction_auto = False
        self._compaction_before: Optional[Dict[str, Any]] = None
        self._compaction_after: Optional[Dict[str, Any]] = None
        # 额度（account/rateLimits/updated 是稀疏更新：null 不清掉以前看到的值，要自己合并）
        self._rate_windows: Dict[str, Dict[str, Any]] = {}
        self._rate_reached: Optional[str] = None

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_config(card: dict) -> Dict[str, Any]:
        """从卡片算出这个员工用的线路 / 模型 / 强度 / 沙箱。新建和续接共用这一份。"""
        line = card.get("line")
        provider = providers.codex_provider_for_line(line)
        model = providers.validate_model("codex", card.get("model"))
        effort = providers.resolve_effort("codex", card)
        explicit = card.get("sandbox")
        if explicit:
            if explicit not in ("read-only", "workspace-write"):
                raise ValueError("codex 沙箱只允许 read-only / workspace-write，收到：%r" % explicit)
            sandbox = explicit
        elif str(card.get("mode") or "").lower() in ("readonly", "read-only", "ro"):
            sandbox = "read-only"
        else:
            sandbox = "workspace-write"
        summary = str(card.get("reasoning_summary") or DEFAULT_REASONING_SUMMARY)
        if summary not in ("auto", "concise", "detailed", "none"):
            raise ValueError("reasoning_summary 只允许 auto/concise/detailed/none，收到：%r" % summary)
        # Codex 自己的自动压缩点：卡片给 auto_compact_limit（整数 token，0 = 不传）优先，否则按提供者表的比例 × 窗口
        limit = card.get("auto_compact_limit")
        if limit is None:
            ratio = providers.get("codex").pin_auto_compact_at
            window = card.get("window") or providers.get("codex").window_fallback
            limit = int(ratio * int(window)) if ratio else 0
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("auto_compact_limit 要是不小于 0 的整数，收到：%r" % (limit,))
        return {"line": line, "provider": provider, "model": model, "effort": effort,
                "sandbox": sandbox, "net": bool(card.get("net", False)), "summary": summary,
                "auto_compact_limit": limit}

    def _config_args(self) -> List[str]:
        cfg = self._cfg
        args = [
            "-c", 'model_provider="%s"' % cfg["provider"],
            "-c", 'approval_policy="never"',
            "-c", 'sandbox_mode="%s"' % cfg["sandbox"],
            "-c", 'model_reasoning_effort="%s"' % cfg["effort"],
            # 默认配置下 reasoning 条目的摘要是空的；要显式打开（待真机验证 detailed 能出摘要）
            "-c", 'model_reasoning_summary="%s"' % cfg["summary"],
        ]
        if cfg["model"]:
            args += ["-c", 'model="%s"' % cfg["model"]]
        if cfg.get("auto_compact_limit"):
            # 键名在 0.165.0 的配置字段表里有；效果待真机验证（V2/docs/cli-compat.md §2）
            args += ["-c", "model_auto_compact_token_limit=%d" % cfg["auto_compact_limit"]]
        for server in disabled_mcp_servers():
            args += ["-c", "mcp_servers.%s.enabled=false" % server]
        return args

    def _sandbox_policy(self) -> dict:
        net = self._cfg["net"]
        if self._cfg["sandbox"] == "read-only":
            return {"type": "readOnly", "networkAccess": net}
        return {"type": "workspaceWrite", "networkAccess": net}

    # ------------------------------------------------------------------
    # 打开 / 关闭
    # ------------------------------------------------------------------

    def open(self, cwd: str, native_id: Optional[str] = None, card: Optional[dict] = None) -> None:
        with self._lock:
            if self._opened:
                raise AdapterError("codex 适配器已经打开")
            self._opened = True
        try:
            self.card = dict(card or {})
            self.cwd = str(cwd)
            self._cfg = self.resolve_config(self.card)
            command = self._command or providers.resolve_command("codex", self.card)
            if isinstance(command, str):
                command = [command]
            argv = list(command) + ["app-server", "--listen", "stdio://"] + self._config_args()
            env = dict(os.environ)
            env.update({str(k): str(v) for k, v in (self.card.get("env") or {}).items()})
            try:
                self._proc = subprocess.Popen(
                    argv, cwd=self.cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                    bufsize=1, start_new_session=True)
            except OSError as exc:
                raise AdapterError("codex 启动失败：%s" % exc)
            self._reader = threading.Thread(target=self._read_loop, args=(self._proc,), daemon=True)
            self._reader.start()
            threading.Thread(target=self._drain_stderr, args=(self._proc,), daemon=True).start()

            self._rpc("initialize", {"clientInfo": {"name": "xay", "version": "2"},
                                      "capabilities": {"experimentalApi": True}})
            self._notify("initialized", {})
            if native_id:
                params = {"threadId": native_id, "cwd": self.cwd, "sandbox": self._cfg["sandbox"],
                          "approvalPolicy": "never", "modelProvider": self._cfg["provider"],
                          "excludeTurns": True}  # excludeTurns：避免整段历史水合（真机日志里的弃用提示）
                model = self._cfg["model"]
                if not model and self.card.get("line_switch"):
                    # 换额度线路续接同一个 thread：原来那条线路的模型在新线路上可能没有，改用新线路的默认模型
                    model = self._default_model_from_server()
                if model:
                    params["model"] = model
                result = self._rpc("thread/resume", params, timeout=max(self.rpc_timeout, 60.0))
            else:
                model = self._cfg["model"] or self._default_model_from_server()
                params = {"cwd": self.cwd, "modelProvider": self._cfg["provider"],
                          "sandbox": self._cfg["sandbox"], "approvalPolicy": "never", "ephemeral": False}
                if model:
                    params["model"] = model
                result = self._rpc("thread/start", params)
            thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
            self.native_id = thread.get("id") or result.get("threadId") or native_id
            if not self.native_id:
                raise AdapterError("codex 没有返回 thread 编号")
            # 服务端报告的才是实际在用的；没报就沿用卡片的
            if thread.get("model"):
                self._cfg["model"] = thread["model"]
            if thread.get("reasoningEffort"):
                self._cfg["effort"] = thread["reasoningEffort"]
        except Exception:
            self._abort_open()
            raise
        self.emit("session", native_id=self.native_id, provider=self._cfg["provider"],
                  model=self._cfg["model"], effort=self._cfg["effort"], resumed=bool(native_id))
        self.set_state("idle")

    def _default_model_from_server(self) -> Optional[str]:
        """卡片没指定模型：问 app-server 当前线路的默认模型（旧驱动对订阅线路也是这么做的）。"""
        try:
            result = self._rpc("model/list", {})
        except AdapterError:
            return None
        for model in result.get("data") or result.get("models") or []:
            if isinstance(model, dict) and model.get("isDefault"):
                return model.get("model") or model.get("id")
        return None

    def _abort_open(self) -> None:
        self._killing = True  # 这是我们自己杀的，别当成意外退出
        self._kill_process()
        with self._lock:
            self._opened = False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if self.alive_process() and self.native_id:
            try:  # 礼貌地退订（待真机验证 unsubscribe 之后马上关进程没问题）
                self._rpc("thread/unsubscribe", {"threadId": self.native_id}, timeout=3.0)
            except AdapterError:
                pass
        self._killing = True
        self._kill_process(graceful=1.5)
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=3.0)
        self.set_state("offline")
        self._finish_events()

    def alive_process(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def alive(self) -> bool:
        return self._opened and not self._closed and self.alive_process()

    def _kill_process(self, graceful: float = 0.0) -> None:
        """关输入；graceful>0 时先等进程自己退出，不退再 SIGTERM -> SIGKILL（整个进程组）。"""
        proc = self._proc
        if proc is None:
            return
        close_pipe(proc.stdin)
        if proc.poll() is None and graceful > 0:
            try:
                proc.wait(timeout=graceful)
            except subprocess.TimeoutExpired:
                pass
        stop_process(proc, grace=3.0)

    # ------------------------------------------------------------------
    # 一轮
    # ------------------------------------------------------------------

    def _reset_turn(self) -> None:
        self._messages: List[tuple] = []             # [(item_id, phase, text)]
        self._msg_deltas: Dict[str, List[str]] = {}
        self._streamed_msgs: set = set()
        self._streamed_reasoning: set = set()
        self._tools_seen: set = set()
        self._turn_usage = {"in": 0, "out": 0, "cached": 0, "total": 0}
        self._turn_usage_seen = False
        self._last_total: Optional[int] = None
        self._last_error: Optional[tuple] = None

    def send(self, text: str, extra_context: Any = None) -> None:
        self._require_open()
        if self._compact_state is not None:
            # 压缩是单独的一轮，没结束就发 turn/start 会被拒绝（ActiveTurnNotSteerable）
            if not self._compact_done.wait(self.compact_timeout):
                raise AdapterError("压缩还没结束，暂时不能开新的一轮")
        with self._lock:
            if self._turn_active:
                raise AdapterBusy("Codex 正在做上一轮")
            self._turn_active = True
            self._turn_id = None
            self._reset_turn()
        params: Dict[str, Any] = {
            "threadId": self.native_id,
            "input": [{"type": "text", "text": str(text)}],
            "cwd": self.cwd,
            "approvalPolicy": "never",
            "sandboxPolicy": self._sandbox_policy(),
            "effort": self._cfg["effort"],      # 每轮都带上原来的强度 / 模型 / 思考摘要设置，
            "summary": self._cfg["summary"],    # 续接后也不会悄悄变
        }
        if self._cfg["model"]:
            params["model"] = self._cfg["model"]
        entries = context_entries(extra_context)
        if entries:
            # 额外背景走 additionalContext（schema 里有：{来源: {kind: untrusted|application, value}}）；
            # 待真机验证 Codex 真的把它放进模型输入。
            params["additionalContext"] = {name: {"kind": "application", "value": value}
                                            for name, value in entries}
        self.set_state("working")
        try:
            result = self._rpc("turn/start", params)
        except Exception:
            with self._lock:
                self._turn_active = False
            self.set_state("idle")
            raise
        turn = result.get("turn") if isinstance(result.get("turn"), dict) else {}
        with self._lock:
            if self._turn_active and self._turn_id is None:
                self._turn_id = turn.get("id") or result.get("turnId")

    def steer(self, text: str) -> bool:
        with self._lock:
            turn_id = self._turn_id if (self._turn_active and self._compact_state is None) else None
        if not turn_id or not self.alive():
            return False
        try:
            self._rpc("turn/steer", {"threadId": self.native_id, "expectedTurnId": turn_id,
                                      "input": [{"type": "text", "text": str(text)}]})
        except AdapterError:
            return False
        return True

    def interrupt(self) -> str:
        if not self.alive():
            return "kill"
        with self._lock:
            active = self._turn_active or self._compact_state is not None
            turn_id = self._compact_turn_id if self._compact_state == "running" else self._turn_id
        if not active:
            return "soft"  # 没有要打断的
        deadline = time.monotonic() + 2.0   # 刚发出去的一轮，turn 编号可能还没到
        while not turn_id and time.monotonic() < deadline:
            time.sleep(0.05)
            with self._lock:
                turn_id = self._compact_turn_id if self._compact_state == "running" else self._turn_id
        if turn_id:
            try:
                self._rpc("turn/interrupt", {"threadId": self.native_id, "turnId": turn_id}, timeout=5.0)
                return "soft"
            except AdapterError:
                pass
        self._killing = True
        self._kill_process()
        return "kill"

    def compact(self, wait: bool = True, timeout: Optional[float] = None) -> bool:
        """thread/compact/start。wait=True 时一直等压缩这一轮结束（成功返回 True）。"""
        self._require_open()
        with self._lock:
            if self._turn_active or self._compact_state is not None:
                raise AdapterBusy("正在做一轮或正在压缩，干活中途交给 Codex 自己的自动压缩")
            self._compact_state = "requested"
            self._compact_done.clear()
            self._compact_ok = False
            self._compaction_before = dict(self.last_usage) if self.last_usage else None
            self._compaction_after = None
            self._compaction_auto = False
        self.set_state("compacting")
        try:
            self._rpc("thread/compact/start", {"threadId": self.native_id})
        except AdapterError:
            with self._lock:
                self._compact_state = None
                self._compact_done.set()
            self.set_state("idle")
            return False
        if not wait:
            return True
        if not self._compact_done.wait(timeout if timeout is not None else self.compact_timeout):
            return False
        return self._compact_ok

    # ------------------------------------------------------------------
    # JSON-RPC
    # ------------------------------------------------------------------

    def _send_json(self, payload: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise AdapterError("codex app-server 进程已经退出")
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with self._write_lock:
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise AdapterError("写 codex app-server 失败：%s" % exc)

    def _notify(self, method: str, params: dict) -> None:
        self._send_json({"method": method, "params": params})

    def _rpc(self, method: str, params: dict, timeout: Optional[float] = None) -> dict:
        with self._lock:
            msg_id = self._next_id
            self._next_id += 1
            reply: "queue.Queue[dict]" = queue.Queue(maxsize=1)
            self._pending[msg_id] = reply
        try:
            self._send_json({"id": msg_id, "method": method, "params": params})
            try:
                message = reply.get(timeout=timeout if timeout is not None else self.rpc_timeout)
            except queue.Empty:
                raise AdapterError("codex %s 超时" % method)
        finally:
            self._pending.pop(msg_id, None)
        if "error" in message:
            err = message["error"]
            detail = err.get("message") if isinstance(err, dict) else err
            hint = (" | " + " | ".join(list(self._stderr_tail)[-2:])) if self._stderr_tail and method == "initialize" else ""
            raise AdapterError("codex %s 失败：%s%s" % (method, detail, hint))
        return message.get("result") or {}

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stderr:
                text = line.rstrip("\n")
                if text.strip():
                    self._stderr_tail.append(text[:300])
        except (OSError, ValueError):
            pass
        finally:
            close_pipe(proc.stderr)

    def _read_loop(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    try:
                        self._dispatch(message)
                    except Exception as exc:  # 解析 bug 不能让读取线程死掉
                        self.emit("error", kind="other", text="适配器解析出错：%s" % exc)
        except (OSError, ValueError):
            pass
        finally:
            try:
                self._on_exit(proc)
            finally:
                close_pipe(proc.stdout)
                close_pipe(proc.stdin)

    def _dispatch(self, message: dict) -> None:
        msg_id = message.get("id")
        method = message.get("method")
        if method is None and msg_id is not None:      # 对我们请求的回复
            waiter = self._pending.get(msg_id)
            if waiter is not None:
                waiter.put(message)
            return
        if method is not None and msg_id is not None:  # 服务端发来的请求（审批 / 提问）
            self._answer_server_request(msg_id, method, message.get("params") or {})
            return
        if method is not None:
            self._on_notification(method, message.get("params") or {})

    def _answer_server_request(self, msg_id: Any, method: str, params: dict) -> None:
        """approval_policy=never 时本来不该有这类请求；万一来了就拒绝，免得协议挂住。"""
        error = None
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            result = {"decision": "decline"}
        elif method in ("execCommandApproval", "applyPatchApproval"):
            result = {"decision": "denied"}
        elif method == "item/permissions/requestApproval":
            result = {"permissions": {}, "scope": "turn"}
        elif method == "item/tool/requestUserInput":
            result = {"answers": {}}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "decline"}
        else:
            result, error = None, {"code": -32601, "message": "xay 不支持 %s" % method}
        try:
            if error is not None:
                self._send_json({"id": msg_id, "error": error})
            else:
                self._send_json({"id": msg_id, "result": result})
        except AdapterError:
            pass
        what = params.get("command") or params.get("reason") or params.get("path") or method
        self.emit("error", kind="other", text="Codex 请求批准（%s），已自动拒绝：%s" % (method, clip(what, 200)))

    def _on_exit(self, proc: subprocess.Popen) -> None:
        for waiter in list(self._pending.values()):
            try:
                waiter.put_nowait({"error": {"message": "codex app-server 已退出"}})
            except queue.Full:
                pass
        if proc is not self._proc:
            return
        deliberate = self._killing or self._closed
        with self._lock:
            was_active = self._turn_active
            self._turn_active = False
            compacting = self._compact_state is not None
            self._compact_state = None
        if compacting:
            self._compact_ok = False
            self._compact_done.set()
        if was_active:
            tail = " | ".join(list(self._stderr_tail)[-2:])
            status = "interrupted" if deliberate else "failed"
            data = {"status": status, "final_text": self._final_text()}
            if not deliberate:
                data.update(error_kind="other", error="codex app-server 意外退出" + (("：" + tail) if tail else ""))
            self.emit("turn_ended", **data)
        if not deliberate:
            self.emit("error", kind="other", text="codex app-server 意外退出")
        self.set_state("offline")

    # ------------------------------------------------------------------
    # 通知 -> 统一事件
    # ------------------------------------------------------------------

    def _on_notification(self, method: str, p: dict) -> None:
        thread_id = p.get("threadId")
        if thread_id and self.native_id and thread_id != self.native_id:
            return
        if method == "turn/started":
            self._on_turn_started(p)
        elif method == "turn/completed":
            self._on_turn_completed(p)
        elif method == "item/started":
            self._on_item(p.get("item") or {}, started=True)
        elif method == "item/completed":
            self._on_item(p.get("item") or {}, started=False)
        elif method == "item/agentMessage/delta":
            delta = str(p.get("delta") or "")
            iid = str(p.get("itemId") or "")
            if delta:
                self._streamed_msgs.add(iid)
                self._msg_deltas.setdefault(iid, []).append(delta)
                self.emit("text", text=delta)
        elif method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            delta = str(p.get("delta") or "")
            if delta:
                self._streamed_reasoning.add(str(p.get("itemId") or ""))
                self.emit("thinking", text=delta)
        elif method == "item/reasoning/summaryPartAdded":
            if int(p.get("summaryIndex") or 0) > 0:     # 第二段摘要开始：和前一段隔开
                self.emit("thinking", text="\n\n")
        elif method == "thread/tokenUsage/updated":
            self._on_token_usage(p.get("tokenUsage") or {})
        elif method == "turn/plan/updated":
            steps = [{"text": str(s.get("step") or ""), "status": _PLAN_STATUS.get(s.get("status"), "pending")}
                     for s in (p.get("plan") or []) if isinstance(s, dict)]
            self.emit("plan", steps=steps)
        elif method == "error":
            self._on_error_notification(p)
        elif method == "account/rateLimits/updated":
            self._on_rate_limits(p.get("rateLimits"))
        # 其余通知（status/changed、moderation、warning 等）不需要

    # 只看 Codex 自己那份额度（limitId 是 "codex" 或没给）；别的 limitId（某个模型单独的额度）不管
    _RATE_LIMIT_IDS = (None, "", "codex")

    def _on_rate_limits(self, limits: Any) -> None:
        """account/rateLimits/updated → rate_limits 事件 {line, windows:[{key, mins, used_pct, resets_at}], reached?}。

        primary 一般是 5 小时窗、secondary 是 7 天窗（windowDurationMins 300 / 10080）。这是稀疏更新：
        某个窗口是 null 时保留以前看到的值。走中转站（API key）时两个窗口都是 null → windows 为空，照样发出去，
        工位据此知道这条线路看不到额度。"""
        if not isinstance(limits, dict) or limits.get("limitId") not in self._RATE_LIMIT_IDS:
            return
        for slot in ("primary", "secondary"):
            w = limits.get(slot)
            if not isinstance(w, dict):
                continue
            used = w.get("usedPercent")
            if not isinstance(used, (int, float)) or isinstance(used, bool):
                continue
            mins = w.get("windowDurationMins")
            mins = int(mins) if isinstance(mins, (int, float)) and not isinstance(mins, bool) and mins > 0 else \
                (300 if slot == "primary" else 10080)
            key = "5h" if mins == 300 else "week" if mins == 10080 else "%dm" % mins
            resets = w.get("resetsAt")
            item = {"key": key, "mins": mins, "used_pct": max(0.0, min(100.0, float(used)))}
            if isinstance(resets, (int, float)) and not isinstance(resets, bool) and resets > 0:
                item["resets_at"] = int(resets)
            self._rate_windows[slot] = item
        if limits.get("rateLimitReachedType"):
            self._rate_reached = str(limits["rateLimitReachedType"])
        elif "rateLimitReachedType" in limits and any(isinstance(limits.get(s), dict) for s in ("primary", "secondary")):
            self._rate_reached = None          # 带着窗口、而且明确写了 null：没撞上限
        windows = [self._rate_windows[s] for s in ("primary", "secondary") if s in self._rate_windows]
        data = {"windows": [dict(w) for w in windows], "line": self._cfg.get("line")}
        if self._rate_reached:
            data["reached"] = self._rate_reached
        self.emit("rate_limits", **data)

    def _on_turn_started(self, p: dict) -> None:
        turn = p.get("turn") if isinstance(p.get("turn"), dict) else {}
        turn_id = turn.get("id") or p.get("turnId")
        with self._lock:
            if self._compact_state == "requested":
                # 这是我们发起的压缩那一轮：不算一件活的一轮
                self._compact_state = "running"
                self._compact_turn_id = turn_id
                return
            if not self._turn_active:
                # Codex 自己开的一轮（比如排队的活自动开始）
                self._turn_active = True
                self._reset_turn()
            self._turn_id = turn_id or self._turn_id
        self.set_state("working")
        self.emit("turn_started", native_turn=turn_id)

    def _on_turn_completed(self, p: dict) -> None:
        turn = p.get("turn") if isinstance(p.get("turn"), dict) else {}
        turn_id = turn.get("id")
        status = _TURN_STATUS.get(turn.get("status"), "failed")
        message, code = _error_parts(turn.get("error"))
        with self._lock:
            is_compaction = (self._compact_state == "running" and turn_id == self._compact_turn_id) or \
                            (self._compact_state == "requested" and not self._turn_active)
        if is_compaction:
            if self._compaction_open:
                self._emit_compacted()
            if status != "completed":
                self.emit("error", kind=classify_error(message, code), text=message or "压缩失败")
            self.set_state("idle")
            with self._lock:
                self._compact_ok = status == "completed"
                self._compact_state = None
                self._compact_turn_id = None
                self._compact_done.set()  # 最后才放行：等压缩的 send() 不会被迟到的 idle 盖掉状态
            return
        if self._compaction_open:  # 自动压缩所在的这一轮结束了，压缩条目没收尾
            self._emit_compacted()
        data: Dict[str, Any] = {"status": status, "final_text": self._final_text(turn)}
        if self._turn_usage_seen:
            data["usage"] = dict(self._turn_usage)
        if status == "failed" or message:
            kind = classify_error(message, code)
            data.update(error_kind=kind, error=message or "这一轮失败了")
        # 先回到空闲再报"这一轮结束"：工位收到 turn_ended 马上发下一轮时，状态不会被迟到的 idle 盖掉
        with self._lock:
            self._turn_active = False
            self._turn_id = None
        self.set_state("idle")
        self.emit("turn_ended", **data)

    def _final_text(self, turn: Optional[dict] = None) -> str:
        finals = [text for _, phase, text in self._messages if phase == "final_answer" and text]
        if finals:
            return finals[-1]
        anything = [text for _, _, text in self._messages if text]
        if anything:
            return anything[-1]
        if turn:
            for item in reversed(turn.get("items") or []):
                if isinstance(item, dict) and item.get("type") == "agentMessage" and item.get("text"):
                    return str(item["text"])
        if self._msg_deltas:
            return "".join(list(self._msg_deltas.values())[-1])
        return ""

    def _on_error_notification(self, p: dict) -> None:
        message, code = _error_parts(p.get("error"))
        if p.get("willRetry"):
            return  # Codex 自己会重连的临时断流，不算出错
        kind = classify_error(message, code)
        self._last_error = (kind, message)
        self.emit("error", kind=kind, text=message or "Codex 出错")

    def _on_token_usage(self, usage: dict) -> None:
        last = usage.get("last") if isinstance(usage.get("last"), dict) else {}
        total = usage.get("total") if isinstance(usage.get("total"), dict) else {}
        # 这一轮的 token 用量 = 每次模型请求的 last 累加（同样的 total 重复出现只算一次）
        total_tokens = total.get("totalTokens")
        if last and total_tokens != self._last_total:
            self._last_total = total_tokens
            self._turn_usage_seen = True
            self._turn_usage["in"] += int(last.get("inputTokens") or 0)
            self._turn_usage["out"] += int(last.get("outputTokens") or 0)
            self._turn_usage["cached"] += int(last.get("cachedInputTokens") or 0)
            self._turn_usage["total"] += int(last.get("totalTokens") or 0)
        used = last.get("totalTokens")
        if isinstance(used, int) and not isinstance(used, bool):
            window, estimated = providers.context_window("codex", self._cfg.get("model"),
                                                          reported=usage.get("modelContextWindow"))
            # Codex 口径：totalTokens = inputTokens + outputTokens，inputTokens 已含 cachedInputTokens；
            # 新读的 fresh = in - cached（样本见 fixtures_adapters/codex_turn_ok.jsonl）。
            turn_total = self._turn_usage["total"] if self._turn_active else None
            turn_out = self._turn_usage["out"] if self._turn_active else None
            turn_fresh = max(0, self._turn_usage["in"] - self._turn_usage["cached"]) if self._turn_active else None
            self.emit_usage(used, window, estimated, "codex_last",
                            turn_total=turn_total, turn_out=turn_out, turn_fresh=turn_fresh)
            if self._compaction_open:
                self._compaction_after = dict(self.last_usage)

    # ---- 条目 ---------------------------------------------------------

    def _on_item(self, item: dict, started: bool) -> None:
        typ = item.get("type")
        iid = str(item.get("id") or "")
        status = str(item.get("status") or "")
        if typ == "agentMessage":
            if started:
                return
            text = str(item.get("text") or "")
            if text and iid not in self._streamed_msgs:
                self.emit("text", text=text)
            self._messages.append((iid, item.get("phase"), text))
        elif typ == "reasoning":
            if started or iid in self._streamed_reasoning:
                return
            text = _join_text(item.get("summary")) or _join_text(item.get("content"))
            if text:
                self.emit("thinking", text=text)
        elif typ == "commandExecution":
            if started or iid not in self._tools_seen:
                self._tools_seen.add(iid)
                self.emit("tool", kind="cmd", text=clip(strip_shell(item.get("command")), MAX_TOOL_TEXT))
            if not started:
                exit_code = item.get("exitCode")
                ok = status == "completed" and exit_code in (None, 0)
                self.emit("tool_output", text=clip_tail(item.get("aggregatedOutput")), ok=ok, exit=exit_code)
        elif typ == "fileChange":
            changes = [c for c in (item.get("changes") or []) if isinstance(c, dict)]
            names = ", ".join(os.path.basename(str(c.get("path") or "")) for c in changes if c.get("path"))
            if started or iid not in self._tools_seen:
                self._tools_seen.add(iid)
                self.emit("tool", kind="write", text=clip(names or "文件改动", MAX_TOOL_TEXT))
            if not started:
                kinds = []
                for c in changes:
                    k = c.get("kind")
                    kinds.append((k.get("type") if isinstance(k, dict) else str(k or "update")) + " "
                                 + os.path.basename(str(c.get("path") or "")))
                self.emit("tool_output", text=clip("; ".join(kinds)), ok=status == "completed")
        elif typ in ("mcpToolCall", "dynamicToolCall"):
            label = ".".join(part for part in (str(item.get("server") or item.get("namespace") or ""),
                                                str(item.get("tool") or "")) if part) or "mcp"
            if started or iid not in self._tools_seen:
                self._tools_seen.add(iid)
                self.emit("tool", kind="mcp" if typ == "mcpToolCall" else "other", text=clip(label))
            if not started:
                body = item.get("error") or item.get("result") or item.get("contentItems")
                text = body if isinstance(body, str) else (json.dumps(body, ensure_ascii=False) if body else "")
                self.emit("tool_output", text=clip_tail(text), ok=status == "completed")
        elif typ == "webSearch":
            if started or iid not in self._tools_seen:
                self._tools_seen.add(iid)
                self.emit("tool", kind="web", text=clip(str(item.get("query") or "网页搜索")))
        elif typ == "contextCompaction":
            if started:
                self._on_compaction_started()
            else:
                self._emit_compacted()

    # ---- 压缩 ---------------------------------------------------------

    def _on_compaction_started(self) -> None:
        if self._compaction_open:
            return
        self._compaction_open = True
        if self._compact_state is None:      # 不是我们发起的 -> Codex 自己的自动压缩
            self._compaction_auto = True
            self._compaction_before = dict(self.last_usage) if self.last_usage else None
            self._compaction_after = None
        self.emit("compaction_started", auto=self._compaction_auto)

    def _emit_compacted(self) -> None:
        if not self._compaction_open:
            return
        self._compaction_open = False
        before, after = self._compaction_before, self._compaction_after
        data: Dict[str, Any] = {"auto": self._compaction_auto, "method": "native_auto" if self._compaction_auto else "native"}
        if before:
            data.update(before=before["pct"], before_used=before["used"])
        if after:
            data.update(after=after["pct"], after_used=after["used"])
        self._compaction_auto = False
        self.emit("compacted", **data)
