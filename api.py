#!/usr/bin/env python3
"""Snell Manager HTTP API — wraps snell-server instance management over REST."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, field_validator

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_DIR   = Path("/etc/snell/users")
BINARY_PATH  = Path("/usr/local/bin/snell-server")
SERVICE_FILE = Path("/etc/systemd/system/snell@.service")
TOKEN_FILE   = Path("/etc/snell/api_token")
PREFIX_FILE  = Path("/etc/snell/api_prefix")
BINARY_URL   = "https://dl.nssurge.com/snell/snell-server-v5.0.1-linux-amd64.zip"
PORT_START   = 8388

SYSTEMD_UNIT = """\
[Unit]
Description=Snell Proxy Server (%i)
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/snell-server -c /etc/snell/users/%i.conf
Restart=on-failure
RestartSec=3s
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
"""

# ── URL prefix (read once at startup) ────────────────────────────────────────

def _load_prefix() -> str:
    if PREFIX_FILE.exists():
        p = PREFIX_FILE.read_text().strip().strip("/")
        if p:
            return f"/{p}"
    return ""

PREFIX = _load_prefix()

# ── App + Router ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="Snell Manager API",
    description=(
        "Manage multiple Snell v5 proxy server instances.\n\n"
        "All endpoints require `Authorization: Bearer <token>`.\n\n"
        "Run `snell-manage keygen` on the server to generate prefix & token."
    ),
    version="1.0.0",
    docs_url=f"{PREFIX}/docs",
    redoc_url=f"{PREFIX}/redoc",
    openapi_url=f"{PREFIX}/openapi.json",
)

router = APIRouter(prefix=PREFIX)

# ── Auth ──────────────────────────────────────────────────────────────────────

_bearer = HTTPBearer()

def verify_token(creds: HTTPAuthorizationCredentials = Security(_bearer)) -> str:
    if not TOKEN_FILE.exists():
        raise HTTPException(503, detail="Token not initialised. Run: snell-manage keygen")
    if creds.credentials != TOKEN_FILE.read_text().strip():
        raise HTTPException(403, detail="Invalid token")
    return creds.credentials

# ── Internal helpers ──────────────────────────────────────────────────────────

def _run(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())
    return r.stdout.strip()

def _read_conf(conf: Path) -> dict:
    text = conf.read_text()
    port = re.search(r"listen\s*=\s*0\.0\.0\.0:(\d+)", text)
    psk  = re.search(r"psk\s*=\s*(.+)", text)
    return {
        "port": int(port.group(1)) if port else None,
        "psk":  psk.group(1).strip() if psk else None,
    }

def _service_status(username: str) -> str:
    r = subprocess.run(
        ["systemctl", "is-active", f"snell@{username}"],
        capture_output=True, text=True,
    )
    return "running" if r.returncode == 0 else "stopped"

def _next_port() -> int:
    used: set[int] = set()
    for conf in CONFIG_DIR.glob("*.conf"):
        d = _read_conf(conf)
        if d["port"]:
            used.add(d["port"])
    p = PORT_START
    while p in used:
        p += 1
    return p

def _server_ip() -> str:
    try:
        with urllib.request.urlopen("https://api4.ipify.org", timeout=5) as r:
            return r.read().decode().strip()
    except Exception:
        return "YOUR_SERVER_IP"

def _validate_username(username: str):
    if not re.match(r"^[a-zA-Z0-9_-]+$", username):
        raise HTTPException(400, detail="Username may only contain letters, numbers, _ and -")

# ── Schemas ───────────────────────────────────────────────────────────────────

class UserOut(BaseModel):
    username: str
    port:     int
    psk:      str
    status:   str

class AddUserIn(BaseModel):
    username: str
    port: Optional[int] = None

    @field_validator("username")
    @classmethod
    def username_valid(cls, v: str) -> str:
        if not re.match(r"^[a-zA-Z0-9_-]+$", v):
            raise ValueError("Username may only contain letters, numbers, _ and -")
        return v

    @field_validator("port")
    @classmethod
    def port_valid(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and not (1 <= v <= 65535):
            raise ValueError("Port must be 1–65535")
        return v

class SurgeOut(BaseModel):
    username: str
    line:     str

class MessageOut(BaseModel):
    message: str

# ── /install ──────────────────────────────────────────────────────────────────

@router.post(
    "/install",
    response_model=MessageOut,
    summary="Install snell-server binary",
    description=(
        "Downloads the snell-server binary from the official URL, "
        "installs it to `/usr/local/bin/snell-server`, and writes the "
        "systemd template unit `snell@.service`."
    ),
    tags=["Setup"],
)
def install(_token: str = Depends(verify_token)):
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "snell.zip")
        try:
            urllib.request.urlretrieve(BINARY_URL, zip_path)
        except Exception as e:
            raise HTTPException(502, detail=f"Download failed: {e}")

        with zipfile.ZipFile(zip_path) as z:
            names = [n for n in z.namelist() if "snell-server" in n and not n.endswith("/")]
            if not names:
                raise HTTPException(500, detail="snell-server binary not found in archive")
            z.extract(names[0], tmp)
            extracted = os.path.join(tmp, names[0])

        os.chmod(extracted, 0o755)
        shutil.move(extracted, str(BINARY_PATH))

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SERVICE_FILE.write_text(SYSTEMD_UNIT)
    _run(["systemctl", "daemon-reload"])
    return {"message": f"snell-server installed to {BINARY_PATH}"}

# ── /users ────────────────────────────────────────────────────────────────────

@router.get(
    "/users",
    response_model=List[UserOut],
    summary="List all users",
    description="Returns every configured Snell user with port, PSK, and systemd service status.",
    tags=["Users"],
)
def list_users(_token: str = Depends(verify_token)):
    result = []
    for conf in sorted(CONFIG_DIR.glob("*.conf")):
        username = conf.stem
        d = _read_conf(conf)
        result.append(UserOut(
            username=username,
            port=d["port"],
            psk=d["psk"],
            status=_service_status(username),
        ))
    return result


@router.post(
    "/users",
    response_model=UserOut,
    status_code=201,
    summary="Add a new user",
    description=(
        "Creates a config file for the user, enables and starts the "
        "`snell@<username>` systemd service. "
        "Port is auto-assigned (starting from 8388) when omitted."
    ),
    tags=["Users"],
)
def add_user(body: AddUserIn, _token: str = Depends(verify_token)):
    conf_path = CONFIG_DIR / f"{body.username}.conf"
    if conf_path.exists():
        raise HTTPException(409, detail=f"User '{body.username}' already exists")
    if not BINARY_PATH.exists():
        install(_token)

    port = body.port or _next_port()

    for conf in CONFIG_DIR.glob("*.conf"):
        if _read_conf(conf)["port"] == port:
            raise HTTPException(409, detail=f"Port {port} is already used by '{conf.stem}'")

    psk = secrets.token_urlsafe(18)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conf_path.write_text(
        f"[snell-server]\nlisten = 0.0.0.0:{port}\npsk = {psk}\nobfs = off\n"
    )

    try:
        _run(["systemctl", "enable", "--now", f"snell@{body.username}"])
    except RuntimeError as e:
        conf_path.unlink(missing_ok=True)
        raise HTTPException(500, detail=f"Failed to start service: {e}")

    return UserOut(username=body.username, port=port, psk=psk, status="running")


@router.get(
    "/users/{username}",
    response_model=UserOut,
    summary="Get a single user",
    tags=["Users"],
)
def get_user(username: str, _token: str = Depends(verify_token)):
    _validate_username(username)
    conf_path = CONFIG_DIR / f"{username}.conf"
    if not conf_path.exists():
        raise HTTPException(404, detail=f"User '{username}' not found")
    d = _read_conf(conf_path)
    return UserOut(username=username, port=d["port"], psk=d["psk"], status=_service_status(username))


@router.delete(
    "/users/{username}",
    status_code=204,
    summary="Delete a user",
    description="Stops and disables the systemd service, then removes the config file.",
    tags=["Users"],
)
def delete_user(username: str, _token: str = Depends(verify_token)):
    _validate_username(username)
    conf_path = CONFIG_DIR / f"{username}.conf"
    if not conf_path.exists():
        raise HTTPException(404, detail=f"User '{username}' not found")
    subprocess.run(["systemctl", "disable", "--now", f"snell@{username}"], capture_output=True)
    conf_path.unlink()


@router.post(
    "/users/{username}/{action}",
    response_model=MessageOut,
    summary="Start / stop / restart a user's instance",
    description="`action` must be one of `start`, `stop`, `restart`.",
    tags=["Users"],
)
def control_user(username: str, action: str, _token: str = Depends(verify_token)):
    _validate_username(username)
    if action not in ("start", "stop", "restart"):
        raise HTTPException(400, detail="action must be start, stop, or restart")
    if not (CONFIG_DIR / f"{username}.conf").exists():
        raise HTTPException(404, detail=f"User '{username}' not found")
    try:
        _run(["systemctl", action, f"snell@{username}"])
    except RuntimeError as e:
        raise HTTPException(500, detail=str(e))
    return {"message": f"{action}ed snell@{username}"}

# ── /surge ────────────────────────────────────────────────────────────────────

@router.get(
    "/surge",
    response_model=List[SurgeOut],
    summary="Surge proxy lines for all users",
    description="Returns ready-to-paste Surge `[Proxy]` lines for every user.",
    tags=["Surge"],
)
def surge_all(_token: str = Depends(verify_token)):
    ip = _server_ip()
    result = []
    for conf in sorted(CONFIG_DIR.glob("*.conf")):
        d = _read_conf(conf)
        u = conf.stem
        result.append(SurgeOut(
            username=u,
            line=f"{u} = snell, {ip}, {d['port']}, psk={d['psk']}, version=5",
        ))
    return result


@router.get(
    "/surge/{username}",
    response_model=SurgeOut,
    summary="Surge proxy line for one user",
    tags=["Surge"],
)
def surge_one(username: str, _token: str = Depends(verify_token)):
    _validate_username(username)
    conf_path = CONFIG_DIR / f"{username}.conf"
    if not conf_path.exists():
        raise HTTPException(404, detail=f"User '{username}' not found")
    d = _read_conf(conf_path)
    ip = _server_ip()
    return SurgeOut(
        username=username,
        line=f"{username} = snell, {ip}, {d['port']}, psk={d['psk']}, version=5",
    )

# ── /quota ────────────────────────────────────────────────────────────────────

QUOTA_DIR = Path("/etc/snell/quota")

def _qget(username: str, key: str, default: int = 0) -> int:
    f = QUOTA_DIR / username
    if not f.exists():
        return default
    for line in f.read_text().splitlines():
        if line.startswith(f"{key}="):
            try:
                return int(line.split("=", 1)[1])
            except ValueError:
                return default
    return default

def _qset(username: str, key: str, value: int):
    f = QUOTA_DIR / username
    if not f.exists():
        return
    lines = f.read_text().splitlines()
    new_lines = []
    updated = False
    for line in lines:
        if line.startswith(f"{key}="):
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f"{key}={value}")
    f.write_text("\n".join(new_lines) + "\n")

def _parse_size(s: str) -> int:
    s = s.upper().rstrip("B")
    units = {"T": 1099511627776, "G": 1073741824, "M": 1048576, "K": 1024}
    if s and s[-1] in units:
        num, unit = s[:-1], s[-1]
    else:
        num, unit = s, ""
    if not num.isdigit():
        raise HTTPException(400, detail=f"Invalid size '{s}'. Examples: 100GB, 50G, 1TB, 512MB")
    n = int(num)
    return n * units.get(unit, 1)

def _fmt_bytes(b: int) -> str:
    if b >= 1099511627776: return f"{b/1099511627776:.2f} TB"
    if b >= 1073741824:    return f"{b/1073741824:.2f} GB"
    if b >= 1048576:       return f"{b/1048576:.2f} MB"
    if b >= 1024:          return f"{b/1024:.2f} KB"
    return f"{b} B"

class QuotaOut(BaseModel):
    username:        str
    limit:           int
    limit_fmt:       str
    used:            int
    used_fmt:        str
    remain:          int
    remain_fmt:      str
    percent:         float
    blocked:         bool
    period:          str   # subscription plan: monthly / quarterly / yearly
    next_reset:      int   # unix timestamp of next monthly traffic reset
    next_reset_date: str   # YYYY-MM-DD or "—"

class SetQuotaIn(BaseModel):
    limit: str   # e.g. "100GB" — monthly traffic allowance
    plan: str    # monthly / quarterly / yearly — subscription duration

class RenewIn(BaseModel):
    plan: Optional[str] = None  # keep current plan if omitted

def _qget_str(username: str, key: str, default: str = "") -> str:
    f = QUOTA_DIR / username
    if not f.exists():
        return default
    for line in f.read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    return default

def _fmt_date(ts: int) -> str:
    if ts <= 0:
        return "—"
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

def _quota_info(username: str) -> QuotaOut:
    limit        = _qget(username, "limit")
    used         = _qget(username, "used")
    blocked      = bool(_qget(username, "blocked"))
    block_reason = _qget_str(username, "block_reason", "")
    plan         = _qget_str(username, "plan", "—")
    expire       = _qget(username, "expire", 0)
    next_reset   = _qget(username, "next_reset", 0)
    remain       = max(limit - used, 0)
    pct          = round(used / limit * 100, 1) if limit > 0 else 0.0
    return QuotaOut(
        username=username,
        limit=limit,       limit_fmt=_fmt_bytes(limit),
        used=used,         used_fmt=_fmt_bytes(used),
        remain=remain,     remain_fmt=_fmt_bytes(remain),
        percent=pct,
        blocked=blocked,
        period=plan,
        next_reset=next_reset,
        next_reset_date=_fmt_date(next_reset),
    )


@router.get(
    "/quota",
    response_model=List[QuotaOut],
    summary="List quota for all users",
    tags=["Quota"],
)
def quota_list(_token: str = Depends(verify_token)):
    result = []
    for f in sorted(QUOTA_DIR.glob("*")):
        if f.is_file():
            result.append(_quota_info(f.name))
    return result


@router.get(
    "/quota/{username}",
    response_model=QuotaOut,
    summary="Get quota for one user",
    tags=["Quota"],
)
def quota_get(username: str, _token: str = Depends(verify_token)):
    _validate_username(username)
    if not (QUOTA_DIR / username).exists():
        raise HTTPException(404, detail=f"No quota set for '{username}'")
    return _quota_info(username)


@router.post(
    "/quota/{username}",
    response_model=QuotaOut,
    status_code=201,
    summary="Set quota for a user",
    description="Creates or updates the traffic quota. Accepts sizes like `100GB`, `50G`, `1TB`, `512MB`.",
    tags=["Quota"],
)
def quota_set(username: str, body: SetQuotaIn, _token: str = Depends(verify_token)):
    _validate_username(username)
    if not (CONFIG_DIR / f"{username}.conf").exists():
        raise HTTPException(404, detail=f"User '{username}' not found")

    limit = _parse_size(body.limit)
    QUOTA_DIR.mkdir(parents=True, exist_ok=True)
    f = QUOTA_DIR / username
    if not f.exists():
        f.write_text(f"limit={limit}\nused=0\nblocked=0\nlast_in=0\nlast_out=0\n")
    else:
        _qset(username, "limit", limit)

    if body.plan not in ("monthly", "quarterly", "yearly"):
        raise HTTPException(400, detail="plan must be: monthly, quarterly, yearly")
    subprocess.run(
        ["/usr/local/bin/snell-manage", "quota", "set", username, body.limit, body.plan],
        capture_output=True,
    )

    return _quota_info(username)


@router.delete(
    "/quota/{username}",
    status_code=204,
    summary="Remove quota for a user",
    tags=["Quota"],
)
def quota_delete(username: str, _token: str = Depends(verify_token)):
    _validate_username(username)
    if not (QUOTA_DIR / username).exists():
        raise HTTPException(404, detail=f"No quota set for '{username}'")
    subprocess.run(
        ["/usr/local/bin/snell-manage", "quota", "rm", username],
        capture_output=True,
    )


@router.post(
    "/quota/{username}/reset",
    response_model=QuotaOut,
    summary="Reset usage counter and unblock user",
    tags=["Quota"],
)
def quota_reset(username: str, _token: str = Depends(verify_token)):
    _validate_username(username)
    if not (QUOTA_DIR / username).exists():
        raise HTTPException(404, detail=f"No quota set for '{username}'")
    try:
        _run(["/usr/local/bin/snell-manage", "quota", "reset", username])
    except RuntimeError as e:
        raise HTTPException(500, detail=str(e))
    return _quota_info(username)


@router.post(
    "/quota/{username}/renew",
    response_model=QuotaOut,
    summary="Renew subscription",
    description=(
        "Extends the account expiry by one plan period. "
        "Pass `plan` to switch plans; omit to keep the current one. "
        "If already expired, extends from now; otherwise extends from the current expiry."
    ),
    tags=["Quota"],
)
def quota_renew(username: str, body: RenewIn, _token: str = Depends(verify_token)):
    _validate_username(username)
    if not (QUOTA_DIR / username).exists():
        raise HTTPException(404, detail=f"No quota set for '{username}'")
    if body.plan and body.plan not in ("monthly", "quarterly", "yearly"):
        raise HTTPException(400, detail="plan must be: monthly, quarterly, yearly")
    args = ["/usr/local/bin/snell-manage", "quota", "renew", username]
    if body.plan:
        args.append(body.plan)
    try:
        _run(args)
    except RuntimeError as e:
        raise HTTPException(500, detail=str(e))
    return _quota_info(username)

# ── Mount router ──────────────────────────────────────────────────────────────

app.include_router(router)
