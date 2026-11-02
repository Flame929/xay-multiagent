import hashlib, os, secrets, subprocess, time
from pathlib import Path

def ws_for(path):
    p = Path(path).expanduser().resolve()
    try:
        root = subprocess.check_output(["git", "-C", str(p), "rev-parse", "--show-toplevel"], text=True, stderr=subprocess.DEVNULL).strip()
        p = Path(root).resolve()
    except Exception:
        pass
    return "ws_" + hashlib.sha1(str(p).encode()).hexdigest()[:8]

def new_conv():
    return "cv_" + time.strftime("%Y%m%d-%H%M%S") + "_" + secrets.token_hex(2)

def new_id(prefix):
    return prefix + secrets.token_hex(4)
