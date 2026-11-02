#!/usr/bin/env python3
"""可脚本化回放的假 codex app-server（只依赖 Python 标准库）。

用途：给 tests/test_agent_integration.py 提供真实的 app-server 线协议，
让 xay-agent / codex-app-server 驱动在完全离线、可重复的条件下跑集成测试。

协议依据（本机 codex-cli 0.165.0 的 generate-json-schema 与实测）：
  - JSON-RPC 2.0，省略 jsonrpc 字段，stdin/stdout 换行分隔 JSON；
  - 客户端请求：initialize、thread/start、thread/resume、turn/start、
    turn/steer、turn/interrupt 等；
  - 服务端通知：thread/started、thread/status/changed、turn/started、
    turn/completed、item/started、item/completed、
    item/agentMessage/delta、thread/tokenUsage/updated 等。

启动方式（驱动通常这样拉起 codex）：
    fake_appserver.py app-server --listen stdio:// -c model_provider="openai" ...

也支持驱动的版本/能力探测：
    fake_appserver.py --version
    fake_appserver.py app-server generate-json-schema --out DIR

配置（环境变量）：
    XAY_FAKE_SCENARIO   JSON 场景文件路径；不传则用默认脚本
    XAY_FAKE_TRACE      NDJSON 收发记录路径；测试据此做时序断言
    XAY_FAKE_LATENCY    每个请求响应的固定延迟（秒），默认 0
    XAY_FAKE_TURN_SECONDS  默认回合等待时长（秒），默认 0.25

场景 JSON：
    {
      "latency": 0.02,
      "steer_delay": 0.05,
      "turns": [
        {"final_text": "FAKE-DONE",
         "steps": [
           {"type": "reasoning", "text": "thinking", "delay": 0.05},
           {"type": "command", "command": "sleep 30", "output": "running",
            "delay": 1.0},
           {"type": "message", "text": "FINAL", "final": true}
         ]}
      ],
      "crash": {"on": "turn_start", "turn_index": 0,
                "delay": 0.2, "exit_code": 9}
    }

step.type 支持：
    wait / reasoning / command / message / raw / crash / complete
其中 raw 直接回放 {"method": ..., "params": ...} 通知；crash 会 os._exit，
用于模拟 app-server 进程突然消失（不写任何收尾文件）。
"""

import json
import os
import sys
import threading
import time
import uuid

_MISSING = object()

CLIENT_METHODS = [
    "initialize",
    "initialized",
    "thread/start",
    "thread/resume",
    "thread/read",
    "thread/list",
    "thread/loaded/list",
    "thread/status/changed",
    "turn/start",
    "turn/steer",
    "turn/interrupt",
    "model/list",
    "account/rateLimits/read",
]

SERVER_METHODS = [
    "thread/started",
    "thread/status/changed",
    "turn/started",
    "turn/completed",
    "item/started",
    "item/completed",
    "item/agentMessage/delta",
    "item/commandExecution/outputDelta",
    "thread/tokenUsage/updated",
    "error",
    "warning",
]


def _uid(prefix):
    return "%s_%s" % (prefix, uuid.uuid4().hex[:12])


class RpcError(Exception):
    def __init__(self, message, code=-32600):
        Exception.__init__(self, message)
        self.code = code


class Trace:
    """线程安全的 NDJSON 记录器。崩溃前也保证已写内容落盘。"""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        if path:
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)

    def write(self, direction, payload, turn_id=None):
        if not self.path:
            return
        row = {
            "ts": time.time(),
            "pid": os.getpid(),
            "dir": direction,
            "payload": payload,
        }
        if turn_id:
            row["turn_id"] = turn_id
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass


class TurnState(object):
    def __init__(self, thread_id, turn_id, input_text, spec):
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.input_text = input_text
        self.spec = spec or {}
        self.cancelled = threading.Event()
        self.finished = False
        self.status = "inProgress"
        self.final_text = ""
        self.started_at = int(time.time())
        self.items = []
        self.lock = threading.Lock()

    def is_finished(self):
        with self.lock:
            return self.finished


class FakeAppServer(object):
    def __init__(self, scenario, trace):
        self.scenario = scenario or {}
        self.trace = trace
        self.out_lock = threading.RLock()
        self.threads = {}
        self.active = {}
        self.turn_index = 0
        self.initialized = False
        self.default_latency = _env_float("XAY_FAKE_LATENCY", 0.0)
        self.latency = float(self.scenario.get("latency", self.default_latency) or 0)
        self.steer_delay = float(self.scenario.get("steer_delay", 0.05) or 0)
        self.default_turn_seconds = _env_float("XAY_FAKE_TURN_SECONDS", 0.25)
        self._post = None

    # -- 输出 ---------------------------------------------------------------

    def send(self, payload, turn_id=None):
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self.out_lock:
            self.trace.write("send", payload, turn_id)
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def notify(self, method, params, turn_id=None):
        self.send({"method": method, "params": params}, turn_id=turn_id)

    def respond(self, request_id, result, turn_id=None):
        self.send({"id": request_id, "result": result}, turn_id=turn_id)

    def respond_error(self, request_id, message, code=-32600):
        self.send({"id": request_id, "error": {"code": code, "message": message}})

    def pause(self, seconds):
        if seconds and seconds > 0:
            time.sleep(seconds)

    # -- 主循环 -------------------------------------------------------------

    def run(self):
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self.send({"id": None, "error": {"code": -32700,
                                                  "message": "parse error"}})
                continue
            self.handle(message)
        # stdin 关闭：正常退出，不杀任何写线程（它们已 daemon）。
        return 0

    def handle(self, message):
        self.trace.write("recv", message)
        method = message.get("method")
        if method is None:
            # 客户端对服务端请求的响应；本假服务不发服务端请求。
            return
        request_id = message.get("id", _MISSING)
        params = message.get("params") or {}
        if request_id is _MISSING:
            # initialized 等通知：只记录，不回包。
            if method in ("initialized", "notifications/initialized"):
                self.initialized = True
            return

        self._maybe_crash_method(method)
        self._post = None
        try:
            result = self.dispatch(method, params, request_id)
        except RpcError as exc:
            self._post = None
            self.respond_error(request_id, str(exc), exc.code)
            return
        except Exception as exc:  # 假服务自身 bug：返回错误而不是静默挂住
            self._post = None
            self.respond_error(request_id, "fake app-server: %s" % exc, -32603)
            return

        post = self._post
        self._post = None
        notify_before_response = (
            method == "turn/start"
            and self.scenario.get("turn_start_notifications_before_response")
            and self.turn_index > 1)
        if result is not _MISSING and not notify_before_response:
            self.pause(self.latency)
            self.respond(request_id, result)
        if post is not None:
            post()
        if result is not _MISSING and notify_before_response:
            self.pause(self.latency)
            self.respond(request_id, result)

    # -- 方法分发 -----------------------------------------------------------

    def dispatch(self, method, params, request_id):
        if method == "initialize":
            self.initialized = True
            return {
                "userAgent": "xay-fake-appserver/0.165.0",
                "codexHome": os.path.expanduser("~/.codex"),
                "platformFamily": "unix",
                "platformOs": "macos",
            }
        if method == "thread/start":
            return self.thread_start(params)
        if method == "thread/resume":
            return self.thread_resume(params)
        if method == "thread/read":
            thread = self.threads.get(params.get("threadId"))
            if thread is None:
                raise RpcError("thread not found: %s" % params.get("threadId"))
            return {"thread": thread}
        if method == "thread/list":
            return {"data": list(self.threads.values()), "nextCursor": None}
        if method == "thread/loaded/list":
            return {"data": list(self.threads.keys()), "nextCursor": None}
        if method == "turn/start":
            return self.turn_start(params)
        if method == "turn/steer":
            return self.turn_steer(params)
        if method == "turn/interrupt":
            return self.turn_interrupt(params)
        if method == "model/list":
            return {"data": [{"id": "fake-model", "model": "fake-model",
                              "displayName": "Fake Model"}], "nextCursor": None}
        if method == "account/rateLimits/read":
            return {"rateLimits": {}}
        if method in ("thread/archive", "thread/delete", "thread/unsubscribe",
                      "shutdown", "review/start"):
            return {}
        raise RpcError("method not found: %s" % method, -32601)

    # -- thread -------------------------------------------------------------

    def _sandbox_policy(self, value):
        if isinstance(value, dict):
            return value
        if value == "read-only":
            return {"type": "readOnly", "networkAccess": False}
        if value == "danger-full-access":
            return {"type": "dangerFullAccess"}
        return {"type": "workspaceWrite", "networkAccess": False,
                "writableRoots": [], "excludeTmpdirEnvVar": False,
                "excludeSlashTmp": False}

    def _thread_object(self, thread_id, cwd, params):
        now = int(time.time())
        thread = {
            "id": thread_id,
            "sessionId": thread_id,
            "preview": "",
            "modelProvider": params.get("modelProvider") or "openai",
            "model": params.get("model") or "gpt-5-codex",
            "cwd": cwd,
            "cliVersion": "0.165.0",
            "createdAt": now,
            "updatedAt": now,
            "ephemeral": bool(params.get("ephemeral")),
            "projectId": None,
            "source": "appServer",
            "status": {"type": "idle"},
            "turns": [],
            "historyMode": "legacy",
            "gitInfo": None,
            "name": None,
            "originator": "xay-fake",
            "path": None,
            "reasoningEffort": params.get("effort"),
        }
        return thread

    def _thread_response(self, thread, params):
        sandbox = thread.get("sandboxPolicy") or self._sandbox_policy(
            params.get("sandbox") or "workspace-write")
        return {
            "thread": thread,
            "model": thread["model"],
            "modelProvider": thread["modelProvider"],
            "cwd": thread["cwd"],
            "approvalPolicy": params.get("approvalPolicy") or "never",
            "approvalsReviewer": "user",
            "sandbox": sandbox,
            "reasoningEffort": thread.get("reasoningEffort"),
            "serviceTier": None,
            "instructionSources": [],
            "disabledPluginIds": [],
        }

    def thread_start(self, params):
        thread_id = _uid("thr")
        cwd = params.get("cwd") or os.getcwd()
        thread = self._thread_object(thread_id, cwd, params)
        thread["sandboxPolicy"] = self._sandbox_policy(
            params.get("sandbox") or "workspace-write")
        self.threads[thread_id] = thread

        def post():
            self.notify("thread/started", {"thread": thread})
            self._maybe_crash("thread_start", 0)

        self._post = post
        return self._thread_response(thread, params)

    def thread_resume(self, params):
        thread_id = params.get("threadId")
        if not thread_id:
            raise RpcError("threadId is required")
        cwd = params.get("cwd") or os.getcwd()
        thread = self.threads.get(thread_id)
        if thread is None:
            thread = self._thread_object(thread_id, cwd, params)
            self.threads[thread_id] = thread
        if params.get("cwd"):
            thread["cwd"] = params["cwd"]
        if params.get("model"):
            thread["model"] = params["model"]
        if params.get("modelProvider"):
            thread["modelProvider"] = params["modelProvider"]
        return self._thread_response(thread, params)

    # -- turn ---------------------------------------------------------------

    def _turn_spec(self, index):
        turns = self.scenario.get("turns") or []
        if turns:
            if index < len(turns):
                return turns[index]
            # 未列出的后续回合用默认短脚本，避免测试被上一个长回合拖住。
            return {}
        if "steps" in self.scenario or "final_text" in self.scenario:
            return self.scenario
        return {}

    def turn_start(self, params):
        thread_id = params.get("threadId")
        if not thread_id:
            raise RpcError("threadId is required")
        if thread_id not in self.threads:
            raise RpcError("thread not found: %s" % thread_id)
        active = self.active.get(thread_id)
        if active is not None and not active.is_finished():
            raise RpcError("a turn is already in progress for thread %s"
                           % thread_id)
        input_items = params.get("input") or []
        input_text = ""
        for item in input_items:
            if isinstance(item, dict) and item.get("type") == "text":
                input_text += item.get("text") or ""
        index = self.turn_index
        self.turn_index += 1
        spec = self._turn_spec(index)
        turn_id = _uid("turn")
        state = TurnState(thread_id, turn_id, input_text, spec)
        self.active[thread_id] = state
        self.threads[thread_id]["status"] = {"type": "active",
                                             "activeFlags": []}
        turn_obj = {
            "id": turn_id,
            "items": [],
            "status": "inProgress",
            "startedAt": state.started_at,
            "completedAt": None,
            "durationMs": None,
            "error": None,
            "rootTurnId": turn_id,
            "itemsView": "full",
        }

        def post():
            self.notify("turn/started", {"threadId": thread_id,
                                         "turn": turn_obj}, turn_id=turn_id)
            self.notify("thread/status/changed",
                        {"threadId": thread_id,
                         "status": {"type": "active", "activeFlags": []}})
            self._maybe_crash("turn_start", index)
            worker = threading.Thread(target=self._run_turn,
                                      args=(state,), daemon=True)
            worker.start()

        self._post = post
        return {"turn": turn_obj}

    def turn_steer(self, params):
        thread_id = params.get("threadId")
        expected = params.get("expectedTurnId")
        state = self.active.get(thread_id)
        if state is None or state.is_finished():
            raise RpcError("no active turn for thread %s" % thread_id)
        if expected != state.turn_id:
            raise RpcError("expectedTurnId %s does not match active turn %s"
                           % (expected, state.turn_id))
        text = ""
        for item in params.get("input") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                text += item.get("text") or ""
        client_id = params.get("clientUserMessageId")
        delay = float(state.spec.get("steer_delay", self.steer_delay) or 0)

        def post():
            worker = threading.Thread(target=self._steer_worker,
                                      args=(state, text, client_id, delay),
                                      daemon=True)
            worker.start()

        self._post = post
        return {"turnId": state.turn_id}

    def _steer_worker(self, state, text, client_id, delay):
        if self._wait(state, delay):
            return
        if state.is_finished():
            return
        self._user_message(state, text, client_id=client_id)

    def turn_interrupt(self, params):
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        state = self.active.get(thread_id)
        if state is None or state.is_finished():
            raise RpcError("no active turn for thread %s" % thread_id)
        if turn_id and turn_id != state.turn_id:
            raise RpcError("turnId %s does not match active turn %s"
                           % (turn_id, state.turn_id))

        def post():
            self.finish_turn(state, "interrupted")

        self._post = post
        return {}

    # -- 回合脚本 -----------------------------------------------------------

    def _wait(self, state, seconds):
        """可被 interrupt 打断的 sleep；返回 True 表示应停止脚本。"""
        deadline = time.time() + float(seconds or 0)
        while time.time() < deadline:
            if state.cancelled.is_set() or state.is_finished():
                return True
            time.sleep(min(0.01, max(0.0, deadline - time.time())))
        return state.cancelled.is_set() or state.is_finished()

    def _run_turn(self, state):
        try:
            self._user_message(state, state.input_text)
            self._token_usage(state)
            steps = state.spec.get("steps")
            if steps is None:
                steps = [{"type": "wait",
                          "delay": state.spec.get("delay",
                                                  self.default_turn_seconds)}]
            final_text = state.spec.get("final_text", "FAKE-DONE")
            last_text = ""
            completion_status = state.spec.get("status", "completed")
            completion_error = state.spec.get("error")
            for step in steps:
                if state.cancelled.is_set() or state.is_finished():
                    return
                if not isinstance(step, dict):
                    continue
                if self._wait(state, step.get("delay", 0)):
                    return
                kind = step.get("type")
                if kind == "crash":
                    self._crash(step.get("exit_code", 9))
                elif kind == "reasoning":
                    self._reasoning(state, step.get("text", "思考中"))
                elif kind == "command":
                    self._command(state, step.get("command", "true"),
                                  step.get("output", ""),
                                  step.get("exit_code", 0))
                elif kind == "message":
                    text = step.get("text", "")
                    self._agent_message(state, text)
                    last_text = text
                    if step.get("final"):
                        final_text = text
                elif kind == "raw":
                    self.notify(step.get("method"),
                                step.get("params") or {},
                                turn_id=state.turn_id)
                elif kind == "complete":
                    completion_status = step.get("status", completion_status)
                    self.finish_turn(state, completion_status, completion_error)
                    return
                # wait / 未知类型：继续
            if state.cancelled.is_set() or state.is_finished():
                return
            if final_text and final_text != last_text:
                self._agent_message(state, final_text)
            self.finish_turn(state, completion_status, completion_error)
        except Exception as exc:  # 不把假服务异常写进 stdout 污染协议
            sys.stderr.write("fake_appserver worker error: %s\n" % exc)
            sys.stderr.flush()
            self.finish_turn(state, "failed")

    def _maybe_crash(self, event, turn_index):
        crash = self.scenario.get("crash")
        if not crash:
            return
        on = crash.get("on", "turn_start")
        if on == "method":
            return
        if on == "turn_start" and event != "turn_start":
            return
        if on == "thread_start" and event != "thread_start":
            return
        if "turn_index" in crash and int(crash["turn_index"]) != turn_index:
            return
        delay = float(crash.get("delay", 0) or 0)
        code = int(crash.get("exit_code", 9) or 9)
        timer = threading.Timer(delay, self._crash, args=(code,))
        timer.daemon = True
        timer.start()

    def _maybe_crash_method(self, method):
        crash = self.scenario.get("crash")
        if not crash or crash.get("on") != "method":
            return
        if crash.get("method") != method:
            return
        delay = float(crash.get("delay", 0) or 0)
        code = int(crash.get("exit_code", 9) or 9)
        if delay <= 0:
            self._crash(code)
        timer = threading.Timer(delay, self._crash, args=(code,))
        timer.daemon = True
        timer.start()

    def _crash(self, code=9):
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(int(code))

    # -- 事件构造 -----------------------------------------------------------

    def _item_started(self, state, item):
        with state.lock:
            state.items.append(item)
        self.notify("item/started",
                    {"threadId": state.thread_id, "turnId": state.turn_id,
                     "item": item, "startedAtMs": int(time.time() * 1000)},
                    turn_id=state.turn_id)

    def _item_completed(self, state, item):
        self.notify("item/completed",
                    {"threadId": state.thread_id, "turnId": state.turn_id,
                     "item": item, "completedAtMs": int(time.time() * 1000)},
                    turn_id=state.turn_id)

    def _user_message(self, state, text, client_id=None):
        item = {"id": _uid("msg"), "type": "userMessage",
                "content": [{"type": "text", "text": text}],
                "clientId": client_id}
        self._item_started(state, item)
        self._item_completed(state, item)
        return item

    def _token_usage(self, state):
        def breakdown(count):
            return {"inputTokens": 10 * count, "cachedInputTokens": 0,
                    "outputTokens": 2 * count, "reasoningOutputTokens": 0,
                    "totalTokens": 12 * count, "cacheWriteInputTokens": 0}

        count = max(1, len(state.items))
        self.notify("thread/tokenUsage/updated",
                    {"threadId": state.thread_id, "turnId": state.turn_id,
                     "tokenUsage": {"last": breakdown(1),
                                    "total": breakdown(count),
                                    "modelContextWindow": 200000}},
                    turn_id=state.turn_id)

    def _agent_message(self, state, text):
        item = {"id": _uid("msg"), "type": "agentMessage", "text": text,
                "phase": None, "memoryCitation": None, "delivery": None,
                "questions": None}
        self._item_started(state, item)
        if text:
            # 像真实服务一样分片；驱动必须能靠 item/completed 拿到全文。
            chunk = max(1, len(text) // 3)
            for start in range(0, len(text), chunk):
                self.notify("item/agentMessage/delta",
                            {"threadId": state.thread_id,
                             "turnId": state.turn_id, "itemId": item["id"],
                             "delta": text[start:start + chunk]},
                            turn_id=state.turn_id)
        self._item_completed(state, item)
        state.final_text = text
        return item

    def _reasoning(self, state, text):
        item = {"id": _uid("rs"), "type": "reasoning",
                "summary": [text], "content": []}
        self._item_started(state, item)
        self._item_completed(state, item)
        return item

    def _command(self, state, command, output="", exit_code=0):
        cwd = self.threads.get(state.thread_id, {}).get("cwd") or os.getcwd()
        item = {"id": _uid("cmd"), "type": "commandExecution",
                "command": command,
                "commandActions": [{"type": "unknown", "command": command}],
                "cwd": cwd, "status": "inProgress", "aggregatedOutput": None,
                "exitCode": None, "durationMs": None, "processId": None,
                "source": "agent"}
        self._item_started(state, item)
        if output:
            self.notify("item/commandExecution/outputDelta",
                        {"threadId": state.thread_id,
                         "turnId": state.turn_id, "itemId": item["id"],
                         "delta": output + "\n"}, turn_id=state.turn_id)
        item = dict(item)
        item["status"] = "completed" if exit_code == 0 else "failed"
        item["aggregatedOutput"] = output
        item["exitCode"] = exit_code
        item["durationMs"] = 1
        self._item_completed(state, item)
        return item

    def finish_turn(self, state, status, error=None):
        with state.lock:
            if state.finished:
                return
            state.finished = True
            state.status = status
            state.cancelled.set()
        if self.active.get(state.thread_id) is state:
            del self.active[state.thread_id]
        thread = self.threads.get(state.thread_id)
        if thread is not None:
            thread["status"] = {"type": "idle"}
        turn_obj = {
            "id": state.turn_id,
            "items": list(state.items),
            "status": status,
            "startedAt": state.started_at,
            "completedAt": int(time.time()),
            "durationMs": max(0, int((time.time() - state.started_at) * 1000)),
            "error": error if error is not None else (
                {"message": "interrupted"} if status == "interrupted" else None),
            "rootTurnId": state.turn_id,
            "itemsView": "full",
        }
        self.notify("turn/completed",
                    {"threadId": state.thread_id, "turn": turn_obj},
                    turn_id=state.turn_id)
        self.notify("thread/status/changed",
                    {"threadId": state.thread_id,
                     "status": {"type": "idle"}})


# ---------------------------------------------------------------------------
# 版本 / schema / 启动参数
# ---------------------------------------------------------------------------

def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _arg_value(argv, name):
    for index, arg in enumerate(argv):
        if arg == name and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


def write_schema(out_dir):
    os.makedirs(out_dir, exist_ok=True)

    def one_of(methods):
        return {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "oneOf": [
                {"type": "object",
                 "properties": {"method": {"enum": [method]}},
                 "required": ["method"]}
                for method in methods
            ],
        }

    files = {
        "ClientRequest.json": one_of(CLIENT_METHODS),
        "ServerNotification.json": one_of(SERVER_METHODS),
        "ClientNotification.json": one_of(["initialized"]),
        "codex_app_server_protocol.schemas.json": one_of(
            CLIENT_METHODS + SERVER_METHODS),
        "codex_app_server_protocol.v2.schemas.json": {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "definitions": {},
            "methods": {"client": CLIENT_METHODS, "server": SERVER_METHODS},
        },
    }
    for name, content in files.items():
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
            json.dump(content, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(out_dir, "methods.json"), "w", encoding="utf-8") as fh:
        json.dump({"client": CLIENT_METHODS, "server": SERVER_METHODS},
                  fh, ensure_ascii=False, indent=1)
    sys.stdout.write("wrote fake app-server schema to %s\n" % out_dir)
    sys.stdout.flush()


def load_scenario(argv):
    path = _arg_value(argv, "--scenario") or os.environ.get("XAY_FAKE_SCENARIO")
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, ValueError) as exc:
        sys.stderr.write("fake app-server: cannot load scenario %s: %s\n"
                         % (path, exc))
        sys.stderr.flush()
        return {}


def main(argv):
    args = list(argv)
    if "--version" in args or "-V" in args or "version" in args:
        print("codex-cli 0.165.0")
        return 0
    if "--help" in args or "-h" in args:
        print("fake codex app-server (see tests/fake_appserver.py)")
        return 0
    if "generate-json-schema" in args:
        out = (_arg_value(args, "--out") or _arg_value(args, "-o")
               or os.getcwd())
        write_schema(out)
        return 0
    if "app-server" in args:
        args = args[args.index("app-server") + 1:]

    scenario = load_scenario(args)
    # 按 provider 选场景：额度降级测试需要 openai/custom 两条线路表现不同。
    if isinstance(scenario.get("by_provider"), dict):
        provider = None
        for index, arg in enumerate(args):
            if arg == "-c" and index + 1 < len(args):
                value = str(args[index + 1])
                if value.startswith("model_provider="):
                    provider = value.split("=", 1)[1].strip().strip('"').strip("'")
        scenario = scenario["by_provider"].get(provider, scenario.get("default", {}))
    trace_path = _arg_value(args, "--trace") or os.environ.get("XAY_FAKE_TRACE")
    trace = Trace(trace_path)
    server = FakeAppServer(scenario, trace)
    return server.run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
