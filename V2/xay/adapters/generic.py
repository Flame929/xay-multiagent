"""通用命令行员工适配器：任何有非交互模式的命令行 AI，写一份声明就能当员工（协议 §4）。

声明放 $XAY2_HOME/agents.json，代码内置 opencode / devin / zcode 三份预设。
支持 text 和 jsonl 两种输出格式，占位符 {prompt}、{prompt_file}、{model}、{session}，
支持会话续接、只读模式附加参数，出错按退出码和关键词自动归类。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

from .. import providers
from .base import (
    Adapter, AdapterBusy, AdapterConfigError, AdapterError, Capabilities,
    classify_error, close_pipe, compose_prompt, redact, stop_process,
)

LOG = logging.getLogger("xay.adapters.generic")


def _get_nested(data: Any, path: Optional[str], default: Any = None) -> Any:
    """按路径从字典中取值，支持点号嵌套路径（如 usage.total_tokens）。"""
    if not path or not isinstance(data, dict):
        return default
    if path in data:
        return data[path]
    parts = path.split(".")
    curr = data
    for p in parts:
        if isinstance(curr, dict) and p in curr:
            curr = curr[p]
        else:
            return default
    return curr


class GenericAdapter(Adapter):
    """通用命令行员工适配器。"""

    def __init__(self, agent: str, spec: Optional[dict] = None, command: Optional[Sequence[str]] = None) -> None:
        super().__init__()
        self.agent = agent
        loaded_spec = spec or providers.get_agent_spec(agent)
        if not loaded_spec:
            raise ValueError("找不到通用员工声明：%r" % agent)
        providers.validate_agent_spec(self.agent, loaded_spec)
        self.spec = dict(loaded_spec)
        self._command = list(command) if command is not None else None

        has_resume = bool(self.spec.get("resume_args"))
        output_mode = str(self.spec.get("output") or "text").lower()
        self.capabilities = Capabilities(
            steer="none",
            interrupt="kill",
            resume=has_resume,
            native_compact=False,
            usage="step" if output_mode == "jsonl" else "turn",
            thinking=False,
            plan=False,
            persistent_process=False,
        )

        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._turn_active = False
        self._killing = False
        self._session_emitted = False
        self._temp_files: List[str] = []
        self._lock = threading.RLock()
        self._window = int(self.spec.get("context_window") or 200_000)

    def open(self, cwd: str, native_id: Optional[str] = None, card: Optional[dict] = None) -> None:
        """开始或续接原生对话，固定工作目录。"""
        with self._lock:
            self.cwd = os.path.abspath(cwd)
            self.native_id = native_id
            self.card = dict(card or {})
            self._opened = True
            self._closed = False
            self.set_state("idle")

    def _substitute_arg(self, template: str, replacements: Dict[str, str]) -> str:
        res = template
        for key, val in replacements.items():
            res = res.replace("{" + key + "}", val)
        return res

    def send(self, text: str, extra_context: Any = None) -> None:
        """开始一轮对话。立即返回，通过 events() 获取进展。"""
        with self._lock:
            if not self._opened or self._closed:
                raise AdapterError("适配器未打开或已关闭")
            if self._turn_active:
                raise AdapterBusy("通用员工 %s 正在忙上一轮" % self.agent)
            self._turn_active = True
            self._killing = False

        self.set_state("working")
        self.emit("turn_started", native_turn=1)

        prompt = compose_prompt(text, extra_context)
        readonly = bool(self.card.get("readonly"))
        readonly_args = self.spec.get("readonly_args") or []
        if readonly and not readonly_args:
            with self._lock:
                self._turn_active = False
            self.set_state("idle")
            raise AdapterConfigError("通用员工 %r 声明中没有 readonly_args，不接只读活" % self.agent)

        is_resume = bool(self.native_id and self.capabilities.resume)
        if self._command is not None:
            raw_args = list(self._command)
        else:
            if is_resume and self.spec.get("resume_args"):
                raw_args = list(self.spec.get("resume_args"))
            else:
                raw_args = list(self.spec.get("args") or [])
            if readonly and readonly_args:
                raw_args.extend(readonly_args)

        # 检查是否需要临时提示词文件
        prompt_file_path = ""
        needs_prompt_file = any("{prompt_file}" in str(a) for a in raw_args)
        if needs_prompt_file:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, prefix="xay-prompt-") as tf:
                tf.write(prompt)
                prompt_file_path = tf.name
                self._temp_files.append(prompt_file_path)

        model = ""
        card_model = self.card.get("model")
        if card_model:
            try:
                model = str(providers.validate_model(self.agent, card_model) or "")
            except providers.UnknownAgent:
                model = str(card_model)
        if not model:
            try:
                model = str(providers.default_model(self.agent) or "")
            except providers.UnknownAgent:
                model = str(self.spec.get("model") or "")
        session = str(self.native_id or "")

        replacements = {
            "prompt": prompt,
            "prompt_file": prompt_file_path,
            "model": model,
            "session": session,
        }

        has_prompt_placeholder = False
        final_args = []
        for a in raw_args:
            sub = self._substitute_arg(str(a), replacements)
            if "{prompt}" in str(a) or "{prompt_file}" in str(a):
                has_prompt_placeholder = True
            final_args.append(sub)

        # 如果模板中没有任何提示词占位符，且没传 prompt_file，则将 prompt 作为末尾参数追加
        if not has_prompt_placeholder and not needs_prompt_file:
            final_args.append(prompt)

        # 确定二进制命令
        if self._command is not None:
            cmd = final_args
        else:
            exe = str(self.spec.get("exe") or self.agent)
            override = os.environ.get("XAY_%s_BIN" % self.agent.upper().replace("-", "_"))
            if override:
                exe = override
            cmd = [exe] + final_args

        # 环境变量构建
        child_env = dict(os.environ)
        for target_k, src_k in (self.spec.get("env") or {}).items():
            if src_k.startswith("$XAY2_HOME/"):
                # 员工自己的目录（如 Claude 员工的独立配置目录），不读本机已登录的状态
                d = providers.v2_home() / src_k[len("$XAY2_HOME/"):]
                d.mkdir(parents=True, exist_ok=True)
                child_env[target_k] = str(d)
            else:
                child_env[target_k] = os.environ.get(src_k, "")

        LOG.info("[%s] 启动通用员工命令行: %r", self.agent, cmd)
        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=self.cwd,
                env=child_env,
                start_new_session=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            with self._lock:
                self._turn_active = False
            self.set_state("idle")
            self._cleanup_temp_files()
            raise AdapterError("启动命令失败: %s" % exc)

        self._reader = threading.Thread(
            target=self._run_loop,
            args=(self._proc,),
            daemon=True,
            name="xay-generic-%s" % self.agent,
        )
        self._reader.start()

    def _run_loop(self, proc: subprocess.Popen) -> None:
        buf: List[str] = []
        stderr_lines: List[str] = []
        output_mode = str(self.spec.get("output") or "text").lower()
        session_field = self.spec.get("session_field") or "session_id"
        text_field = self.spec.get("text_field") or "text"
        usage_field = self.spec.get("usage_field") or "usage.total_tokens"

        def read_stderr():
            try:
                for line in iter(proc.stderr.readline, b""):
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                    stderr_lines.append(text)
            except (OSError, ValueError):
                pass

        err_thread = threading.Thread(target=read_stderr, daemon=True)
        err_thread.start()

        try:
            for raw_line in iter(proc.stdout.readline, b""):
                if not raw_line:
                    break
                line_text = raw_line.decode("utf-8", errors="replace")
                if output_mode == "jsonl":
                    clean = line_text.strip()
                    if not clean:
                        continue
                    try:
                        data = json.loads(clean)
                    except json.JSONDecodeError:
                        # 坏行当作纯文本
                        buf.append(clean)
                        self.emit("text", text=clean)
                        continue
                    if isinstance(data, dict):
                        sid = _get_nested(data, session_field)
                        if sid and not self._session_emitted:
                            self.native_id = str(sid)
                            self._session_emitted = True
                            self.emit("session", native_id=str(sid))

                        piece = _get_nested(data, text_field)
                        if piece is not None:
                            p_str = str(piece)
                            buf.append(p_str)
                            self.emit("text", text=p_str)

                        usage_val = _get_nested(data, usage_field)
                        if usage_val is not None:
                            try:
                                tokens = int(usage_val)
                                self.emit_usage(used=tokens, window=self._window, estimated=True, source="jsonl")
                            except (TypeError, ValueError):
                                pass
                else:
                    buf.append(line_text)
                    self.emit("text", text=line_text)
                    if not self._session_emitted:
                        sid = self.native_id or ("session-%s" % int(time.time() * 1000))
                        self.native_id = sid
                        self._session_emitted = True
                        self.emit("session", native_id=sid)
        except (OSError, ValueError):
            pass

        ret = proc.wait()
        close_pipe(proc.stdout)
        close_pipe(proc.stderr)
        err_thread.join(timeout=2.0)
        self._cleanup_temp_files()

        final_text = "".join(buf)
        err_text = "\n".join(stderr_lines).strip()

        with self._lock:
            was_killing = self._killing
            self._turn_active = False

        if was_killing:
            self.emit("turn_ended", status="interrupted", final_text=final_text)
        elif ret == 0:
            self.emit("turn_ended", status="completed", final_text=final_text)
        else:
            combined_err = (err_text + "\n" + final_text).strip()
            err_kind = classify_error(combined_err, code=ret)
            self.emit("turn_ended", status="failed", final_text=final_text, error_kind=err_kind, error=combined_err[:500])

        self.set_state("idle")

    def _cleanup_temp_files(self) -> None:
        for p in self._temp_files:
            try:
                if os.path.exists(p):
                    os.unlink(p)
            except OSError:
                pass
        self._temp_files.clear()

    def interrupt(self) -> str:
        """打断当前这一轮。"""
        with self._lock:
            self._killing = True
            proc = self._proc
        if proc is not None and proc.poll() is None:
            stop_process(proc, grace=2.0)
        return "kill"

    def close(self) -> None:
        """关闭适配器并终止进程。"""
        with self._lock:
            self._closed = True
        self.interrupt()
        self._cleanup_temp_files()
        self.set_state("offline")

    def alive(self) -> bool:
        """适配器是否处于打开且未关闭状态。"""
        with self._lock:
            return self._opened and not self._closed
