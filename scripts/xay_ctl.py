#!/usr/bin/env python3
"""xay-agent 控制 socket 的小型客户端。

协议是 NDJSON：发一行 JSON，请求一行 JSON 响应。这个文件只依赖 Python
标准库，方便负责人 CLI、看板服务和测试共同使用。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
from pathlib import Path
from typing import Any, Dict, Optional


class CtlError(RuntimeError):
    """控制请求无法送达或服务端返回了错误。"""


def socket_path(run: str | os.PathLike[str], xay_home: str | os.PathLike[str] | None = None) -> str:
    """把 run id、run 目录或 socket 路径变成控制 socket 的路径。"""

    value = os.fspath(run)
    if value.endswith(".sock"):
        return value
    home = Path(xay_home or os.environ.get("XAY_HOME", str(Path.home() / ".xay")))
    return str(home / "ctl" / (Path(value).name + ".sock"))


class CtlClient:
    """一个请求一个连接的控制客户端。

    连接关闭后，托管进程仍然独立运行；因此看板进程重启不会影响任务。
    """

    def __init__(self, run: str | os.PathLike[str], *, xay_home: str | os.PathLike[str] | None = None,
                 timeout: float = 10.0, sock: str | os.PathLike[str] | None = None):
        self.sock = str(sock or socket_path(run, xay_home))
        self.timeout = float(timeout)

    def request(self, op: str, **fields: Any) -> Dict[str, Any]:
        payload = {"op": op}
        payload.update({key: value for key, value in fields.items() if value is not None})
        raw = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(self.timeout)
                conn.connect(self.sock)
                conn.sendall(raw)
                response = self._readline(conn)
        except OSError as exc:
            raise CtlError(f"无法连接控制 socket：{self.sock}: {exc}") from exc
        try:
            result = json.loads(response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CtlError("控制 socket 返回了无效 JSON") from exc
        if not isinstance(result, dict):
            raise CtlError("控制 socket 返回的不是 JSON 对象")
        return result

    @staticmethod
    def _readline(conn: socket.socket) -> bytes:
        chunks = []
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        data = b"".join(chunks)
        return data.split(b"\n", 1)[0]

    def steer(self, text: str, expected_turn: str | None = None, *, by: str | None = None) -> Dict[str, Any]:
        return self.request("steer", text=text, expected_turn=expected_turn, by=by)

    def interrupt(self, text: str | None = None, *, by: str | None = None) -> Dict[str, Any]:
        return self.request("interrupt", text=text, by=by)

    def pause(self, *, by: str | None = None) -> Dict[str, Any]:
        return self.request("pause", by=by)

    def resume(self, text: str | None = None, *, by: str | None = None) -> Dict[str, Any]:
        return self.request("resume", text=text, by=by)

    def stop(self, *, by: str | None = None) -> Dict[str, Any]:
        return self.request("stop", by=by)

    def reassign(self, to_agent: str, text: str | None = None, *, by: str | None = None) -> Dict[str, Any]:
        return self.request("reassign", to_agent=to_agent, text=text, by=by)

    def decide(self, decision_id: str, option: str, version: int, text: str | None = None,
               *, by: str | None = None, confirm: bool | None = None) -> Dict[str, Any]:
        return self.request("decide", id=decision_id, option=option, version=version,
                            text=text, by=by, confirm=confirm)

    def status(self, *, by: str | None = None) -> Dict[str, Any]:
        return self.request("status", by=by)


# 看板和早期代码可能使用这两个名字，保留同一实现而不增加另一套协议。
ControlClient = CtlClient
Client = CtlClient
XayCtl = CtlClient


def request(run: str | os.PathLike[str], op: str, *, xay_home: str | os.PathLike[str] | None = None,
            timeout: float = 10.0, **fields: Any) -> Dict[str, Any]:
    return CtlClient(run, xay_home=xay_home, timeout=timeout).request(op, **fields)


send = request


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="向 xay-agent 发送一个控制命令")
    parser.add_argument("run", help="run id、run 目录或 .sock 路径")
    parser.add_argument("op", choices=("steer", "interrupt", "pause", "resume", "stop", "reassign", "decide", "status"))
    parser.add_argument("--text")
    parser.add_argument("--expected-turn")
    parser.add_argument("--to-agent")
    parser.add_argument("--id")
    parser.add_argument("--option")
    parser.add_argument("--version", type=int)
    parser.add_argument("--by")
    parser.add_argument("--xay-home")
    args = parser.parse_args(argv)
    fields = {
        "text": args.text,
        "expected_turn": args.expected_turn,
        "to_agent": args.to_agent,
        "id": args.id,
        "option": args.option,
        "version": args.version,
        "by": args.by,
    }
    try:
        result = request(args.run, args.op, xay_home=args.xay_home, **fields)
    except CtlError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok", False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
