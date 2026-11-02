"""管家用到的目录和文件位置，全部从这里取，方便测试时整体换成临时目录。"""
import os
from pathlib import Path


def v2_root() -> Path:
    """V2 目录（本文件在 V2/xay/office/ 下）。"""
    return Path(__file__).resolve().parents[2]


def web_root() -> Path:
    return v2_root() / "web"


def fixtures_dir() -> Path:
    return v2_root() / "fixtures"


def v2_home() -> Path:
    """新账本所在目录：$XAY2_HOME，默认 ~/.xay/v2。"""
    env = os.environ.get("XAY2_HOME")
    return Path(env).expanduser() if env else Path.home() / ".xay" / "v2"


def old_home() -> Path:
    """旧系统目录：$XAY_HOME，默认 ~/.xay。桥只读它。"""
    env = os.environ.get("XAY_HOME")
    return Path(env).expanduser() if env else Path.home() / ".xay"


def live_root() -> Path:
    return v2_home() / "live"


def logs_dir() -> Path:
    return v2_home() / "logs"


def log_file() -> Path:
    return logs_dir() / "office.log"


def pid_file() -> Path:
    return v2_home() / "office.pid"


def port_file() -> Path:
    return v2_home() / "office.port"
