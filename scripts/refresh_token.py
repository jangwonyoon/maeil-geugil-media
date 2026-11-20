"""매일 그 길 Instagram 장기 토큰을 갱신한다. 로컬 launchd 가 주 1회 실행한다.

1. ~/reel-studio/.env 의 INSTAGRAM_ACCESS_TOKEN_MAEIL_GEUGIL 을 읽는다 (python 으로만).
2. graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token 호출.
3. .env 를 백업(~/.local/state/maeil-geugil/env-backups, 600) 후 값만 교체 (권한 600).
4. gh secret set IG_TOKEN_MAEIL_GEUGIL 에 stdin 으로 전달.
로그(~/Library/Logs/maeil-geugil-token.log)에는 만료일·성공 여부만 남긴다. 토큰 값은 절대 남기지 않는다.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
ENV = Path.home() / "reel-studio/.env"
KEY = "INSTAGRAM_ACCESS_TOKEN_MAEIL_GEUGIL"
SECRET = "IG_TOKEN_MAEIL_GEUGIL"
REPO = "jangwonyoon/maeil-geugil-media"
LOG = Path.home() / "Library/Logs/maeil-geugil-token.log"
BACKUPS = Path.home() / ".local/state/maeil-geugil/env-backups"
KEEP_BACKUPS = 5
GH_CANDIDATES = ["/opt/homebrew/bin/gh", "/usr/local/bin/gh"]
LINE_RE = re.compile(rf"^(\s*(?:export\s+)?{KEY}\s*=\s*)(['\"]?)([^'\"\s]*)(\2)(\s*)$")

_secrets: list[str] = []


def log(msg: str) -> None:
    for s in _secrets:
        if s:
            msg = msg.replace(s, "<redacted>")
    line = f"{datetime.now(KST).isoformat(timespec='seconds')} {msg}\n"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line)
    sys.stdout.write(line)


def read_token() -> tuple[list[str], int, str]:
    lines = ENV.read_text("utf-8").splitlines(keepends=True)
    for i, line in enumerate(lines):
        m = LINE_RE.match(line.rstrip("\n"))
        if m:
            return lines, i, m.group(3)
    raise SystemExit(f"{ENV} 에 {KEY} 가 없습니다")


def refresh(token: str) -> dict:
    url = "https://graph.instagram.com/refresh_access_token?" + urllib.parse.urlencode(
        {"grant_type": "ig_refresh_token", "access_token": token})
    try:
        with urllib.request.urlopen(url, timeout=60) as res:
            out = json.load(res)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        e.close()
        raise RuntimeError(f"refresh HTTP {e.code}: {body}") from None
    except OSError as e:
        raise RuntimeError(f"refresh 연결 실패: {type(e).__name__}") from None
    new = str(out.get("access_token", ""))
    if not new or any(c.isspace() for c in new) or not out.get("expires_in"):
        raise RuntimeError("refresh 응답에 access_token·expires_in 이 없습니다")
    return out


def backup_env() -> Path:
    BACKUPS.mkdir(parents=True, exist_ok=True)
    os.chmod(BACKUPS, 0o700)
    dest = BACKUPS / f".env.{datetime.now(KST):%Y%m%d-%H%M%S}"
    shutil.copy2(ENV, dest)
    os.chmod(dest, 0o600)
    old = sorted(BACKUPS.glob(".env.*"))[:-KEEP_BACKUPS]
    for p in old:  # 토큰이 든 오래된 백업은 최근 5개만 남긴다
        p.unlink()
    return dest


def write_env(lines: list[str], idx: int, new: str) -> None:
    m = LINE_RE.match(lines[idx].rstrip("\n"))
    nl = "\n" if lines[idx].endswith("\n") else ""
    lines[idx] = f"{m.group(1)}{m.group(2)}{new}{m.group(4)}{m.group(5)}{nl}"
    fd, tmp = tempfile.mkstemp(dir=ENV.parent, prefix=".env.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ENV)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    os.chmod(ENV, 0o600)


def set_secret(new: str) -> None:
    gh = next((p for p in GH_CANDIDATES if os.path.exists(p)), shutil.which("gh"))
    if not gh:
        raise RuntimeError("gh 를 찾지 못했습니다")
    proc = subprocess.run([gh, "secret", "set", SECRET, "-R", REPO], input=new,
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"gh secret set 실패 (exit {proc.returncode}): {proc.stderr.strip()[:300]}")


def main() -> int:
    try:
        lines, idx, old = read_token()
        _secrets.append(old)
        out = refresh(old)
        new = out["access_token"]
        _secrets.append(new)
        expires = datetime.now(KST) + timedelta(seconds=int(out["expires_in"]))
        changed = new != old
        backup_env()
        write_env(lines, idx, new)
        if read_token()[2] != new:
            raise RuntimeError(".env 교체 확인 실패 (백업에서 복구 필요)")
        set_secret(new)
        log(f"OK 만료 {expires:%Y-%m-%d} (약 {int(out['expires_in']) // 86400}일) "
            f"토큰 {'바뀜' if changed else '같음'}, .env 갱신, 시크릿 {SECRET} 갱신")
        return 0
    except Exception as e:  # noqa: BLE001 — 어떤 오류든 토큰을 가린 한 줄만 남긴다
        log(f"FAIL {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
