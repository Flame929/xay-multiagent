"""管家后台进程：start / stop / status（由 `xay2 office ...` 调用，也可 `python3 -m xay.office.daemon ...`）。

- 后台启动 = 起一个脱离终端的子进程跑 `start --foreground`；pid 写在 $XAY2_HOME/office.pid，端口写在 office.port，
  日志在 $XAY2_HOME/logs/office.log。
- 重复 start 只会提示"已经在运行"；stop 前会核对进程确实是管家，避免误杀同号的别的进程。
- status 退出码：运行中 0，没在运行 3。
- 看板端口（M5）：同一套页面另外也在 7878 提供（原来旧看板的端口，书签和通知链接不用改）。不写 --port 启动时默认开，
  写了 --port 就默认不开（测试和临时管家不会去占 7878）；--board-port N 指定，0 = 不开；也可以设 XAY2_BOARD_PORT；
  退回旧看板时建一个空文件 $XAY2_HOME/board.off，以后不写 --board-port 就都不开（一直有效，不用每次带环境变量）。
  7878 被占着（多半是旧看板还开着）时不影响管家，每 30 秒再试一次，让出来就自动接上；状态写在 office.board-port。
"""
import argparse
import errno
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time

from . import paths

DEFAULT_PORT = 7879
BOARD_PORT = 7878                 # M5：看板（原旧看板的端口）
BOARD_RETRY_SECONDS = 30.0
START_WAIT_SECONDS = 15.0
STOP_WAIT_SECONDS = 8.0
LOG_ROTATE_BYTES = 5 << 20
USAGE = "用法：xay2 office start [--port 7879] [--board-port 7878|0] [--foreground] | stop | status"
_OUR_MARKERS = ("xay.office", "xay.cli", "xay2")   # 工位进程是 xay.station，不会被当成管家


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        print(USAGE)
        raise SystemExit(2)


# ---------- 进程信息 ----------
def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _is_ours(pid):
    """核对这个进程的命令行确实是管家（pid 可能被别的程序复用）。"""
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return True                  # 查不了就按存活处理，不擅自清理
    return any(marker in out for marker in _OUR_MARKERS) if out.strip() else False


def _read_int(path):
    try:
        return int(path.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _board_file():
    return paths.v2_home() / "office.board-port"


def _forget():
    for path in (paths.pid_file(), paths.port_file(), _board_file()):
        try:
            path.unlink()
        except OSError:
            pass


def running_info():
    """管家在运行就返回 (pid, port, 已运行秒数)，否则返回 None（顺手清掉过期的 pid 文件）。"""
    pid = _read_int(paths.pid_file())
    if pid is None:
        return None
    if not (_pid_alive(pid) and _is_ours(pid)):
        _forget()
        return None
    port = _read_int(paths.port_file()) or DEFAULT_PORT
    try:
        uptime = max(0.0, time.time() - paths.pid_file().stat().st_mtime)
    except OSError:
        uptime = 0.0
    return pid, port, uptime


def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _uptime_text(seconds):
    if seconds < 60:
        return "已运行不到 1 分钟"
    if seconds < 3600:
        return "已运行 %d 分钟" % (seconds // 60)
    if seconds < 86400:
        return "已运行 %d 小时 %d 分钟" % (seconds // 3600, seconds % 3600 // 60)
    return "已运行 %d 天 %d 小时" % (seconds // 86400, seconds % 86400 // 3600)


def _url(port):
    return "http://127.0.0.1:%d/org" % port


def _board_state():
    """看板端口现在怎样：(端口, "ok" | "wait" | "fail") 或 None（没开）。"""
    try:
        parts = _board_file().read_text(encoding="utf-8").split()
        return int(parts[0]), (parts[1] if len(parts) > 1 else "ok")
    except (OSError, ValueError, IndexError):
        return None


def _board_text(port):
    """status / start 里说看板在哪。"""
    state = _board_state()
    main = "http://127.0.0.1:%d/" % port
    if not state:
        return "看板 %s" % main
    bport, st = state
    if st == "ok":
        return "看板 http://127.0.0.1:%d/（%s 也能打开）" % (bport, main)
    if st == "wait":
        return "看板先用 %s（%d 端口被别的程序占着，多半是旧看板；让出来后管家自动接上）" % (main, bport)
    return "看板 %s（%d 端口没能打开，见日志）" % (main, bport)


def resolve_board_port(port_given, board_port_arg, port, env=None):
    """要不要另外在看板端口上提供页面：--board-port 优先，其次 XAY2_BOARD_PORT，再其次 board.off（不开）；
    都没写时，只有没写 --port 才默认开 7878。"""
    env = os.environ if env is None else env
    if board_port_arg is not None:
        value = board_port_arg
    elif not (env.get("XAY2_BOARD_PORT") or "").strip() and (paths.v2_home() / "board.off").exists():
        value = 0
    elif (env.get("XAY2_BOARD_PORT") or "").strip():
        try:
            value = int(env["XAY2_BOARD_PORT"].strip())
        except ValueError:
            value = 0
    else:
        value = 0 if port_given else BOARD_PORT
    if not 0 < value <= 65535 or value == port:
        return 0
    return value


# ---------- 子命令 ----------
def cmd_status():
    info = running_info()
    if not info:
        print("管家没在运行（启动：xay2 office start）")
        return 3
    pid, port, uptime = info
    print("管家运行中：进程 %d，网址 %s，%s，%s" % (pid, _url(port), _board_text(port), _uptime_text(uptime)))
    return 0


def cmd_stop():
    info = running_info()
    if not info:
        print("管家没在运行")
        return 0
    pid = info[0]
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError as exc:
        print("没能停掉管家（进程 %d）：%s" % (pid, exc))
        return 1
    deadline = time.time() + STOP_WAIT_SECONDS
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.1)
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        time.sleep(0.3)
    if _pid_alive(pid):
        print("管家（进程 %d）没有响应停止请求，请手动结束这个进程" % pid)
        return 1
    _forget()
    print("管家已停止")
    return 0


def cmd_start(port, foreground, board_port=0):
    info = running_info()
    if info:
        print("管家已经在运行：进程 %d，网址 %s，%s" % (info[0], _url(info[1]), _board_text(info[1])))
        return 0
    if foreground:
        return serve(port, board_port)
    return _spawn(port, board_port)


def _spawn(port, board_port=0):
    paths.logs_dir().mkdir(parents=True, exist_ok=True)
    log_path = paths.log_file()
    _rotate_log(log_path)
    env = dict(os.environ)
    root = str(paths.v2_root())
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "xay.office.daemon", "start", "--foreground", "--port", str(port),
             "--board-port", str(board_port)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=root, env=env,
            start_new_session=True, close_fds=True)
    deadline = time.time() + START_WAIT_SECONDS
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        if _port_open(port) and _read_int(paths.pid_file()) == proc.pid:
            if board_port:
                until = time.time() + 2.0
                while time.time() < until and not _board_state():
                    time.sleep(0.1)
            print("管家已启动：进程 %d，网址 %s，%s" % (proc.pid, _url(port), _board_text(port)))
            return 0
        time.sleep(0.1)
    if proc.poll() is None:
        print("管家启动超时，请看日志：%s" % log_path)
    else:
        print("管家没能启动，原因请看日志：%s%s" % (log_path, _log_hint(log_path)))
    return 1


def _log_hint(log_path):
    try:
        lines = [x for x in log_path.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()]
    except OSError:
        return ""
    return "\n最后一行：" + lines[-1][:200] if lines else ""


def _rotate_log(log_path):
    try:
        if log_path.stat().st_size > LOG_ROTATE_BYTES:
            os.replace(log_path, log_path.with_name(log_path.name + ".1"))
    except OSError:
        pass


class BoardPortThread(threading.Thread):
    """M5：在看板端口（默认 7878）上再开一份同样的页面。端口被占着就每 30 秒再试，不影响管家本身。"""

    def __init__(self, port, retry=BOARD_RETRY_SECONDS, factory=None):
        super().__init__(name="xay-office-board", daemon=True)
        self.port, self.retry = port, retry
        self.stop_event = threading.Event()
        self.server = None
        self._factory = factory

    def _mark(self, state):
        try:
            _board_file().write_text("%d %s\n" % (self.port, state), encoding="utf-8")
        except OSError:
            pass

    def run(self):
        log = logging.getLogger("xay.office")
        if self._factory is None:
            from .web import OfficeServer
            self._factory = OfficeServer
        told = False
        while not self.stop_event.is_set():
            try:
                srv = self._factory(self.port)
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE:
                    log.warning("看板端口 %d 没能打开：%s", self.port, exc)
                    self._mark("fail")
                    return
                if not told:
                    log.info("看板端口 %d 被别的程序占着（多半是旧看板），每 %d 秒再试", self.port, self.retry)
                    told = True
                self._mark("wait")
                self.stop_event.wait(self.retry)
                continue
            if self.stop_event.is_set():
                srv.server_close()
                return
            self.server = srv
            self._mark("ok")
            log.info("看板也开在 http://127.0.0.1:%d/", self.port)
            try:
                srv.serve_forever(poll_interval=0.25)
            except Exception as exc:  # noqa: BLE001
                log.warning("看板端口 %d 出错停了：%s", self.port, exc)
            return

    def stop(self, timeout=3.0):
        self.stop_event.set()
        if self.server is not None:
            try:
                self.server.stop()
            except Exception:  # noqa: BLE001
                pass
        if self.is_alive() and self is not threading.current_thread():
            self.join(timeout)


def serve(port, board_port=0):
    """前台运行管家：网页服务 + 排程（起工位、心跳看门狗）+ 额度刷新 + 看板端口，直到收到 SIGTERM / Ctrl+C。"""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, force=True,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("xay.office")
    from .web import OfficeServer
    paths.v2_home().mkdir(parents=True, exist_ok=True)
    try:
        server = OfficeServer(port)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print("端口 %d 已被别的程序占用，换一个再试：xay2 office start --port %d" % (port, port + 1))
        else:
            print("管家没能启动：%s" % exc)
        return 1
    try:
        _board_file().unlink()           # 上次留下的看板端口状态不算数
    except OSError:
        pass
    board = BoardPortThread(board_port) if board_port else None
    scheduler = None
    if os.environ.get("XAY2_NO_SCHEDULER") != "1":   # 排程 + 起工位 + 心跳看门狗（M2）；设 1 可以关掉
        from .scheduler import SchedulerThread
        scheduler = SchedulerThread()
    quota_thread = None
    if os.environ.get("XAY2_NO_QUOTA") != "1":       # 四条线路的真实额度：后台定时查（xay/quota_live.py）；设 1 可以关掉
        from ..quota_live import QuotaThread
        quota_thread = QuotaThread()
    me = os.getpid()
    paths.pid_file().write_text("%d\n" % me, encoding="utf-8")
    paths.port_file().write_text("%d\n" % server.port, encoding="utf-8")

    def request_stop(signum, _frame):
        log.info("收到停止信号 %s，正在退出", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    if board is not None:
        board.start()
    if scheduler is not None:
        scheduler.start()
    if quota_thread is not None:
        quota_thread.start()
    log.info("管家已启动：%s（进程 %d）", _url(server.port), me)
    print("管家已启动：%s（按 Ctrl+C 停止）" % _url(server.port), flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        if board is not None:
            board.stop()
        if scheduler is not None:
            scheduler.stop()     # 只停排程；已经起的工位是独立进程，照常干活
        if quota_thread is not None:
            quota_thread.stop()
        server.stop()
        if _read_int(paths.pid_file()) == me:
            _forget()
        log.info("管家已退出")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _Parser(prog="xay2 office", add_help=False)
    sub = parser.add_subparsers(dest="cmd")
    start = sub.add_parser("start", add_help=False)
    start.add_argument("--port", type=int, default=None)
    start.add_argument("--board-port", type=int, default=None)
    start.add_argument("--foreground", action="store_true")
    sub.add_parser("stop", add_help=False)
    sub.add_parser("status", add_help=False)
    args = parser.parse_args(argv)
    if args.cmd == "start":
        port_given = args.port is not None
        port = DEFAULT_PORT if args.port is None else args.port
        if not 0 <= port <= 65535:
            print("端口号不对：%d" % port)
            return 2
        if args.board_port is not None and not 0 <= args.board_port <= 65535:
            print("端口号不对：%d" % args.board_port)
            return 2
        return cmd_start(port, args.foreground, resolve_board_port(port_given, args.board_port, port))
    if args.cmd == "stop":
        return cmd_stop()
    if args.cmd == "status":
        return cmd_status()
    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())
