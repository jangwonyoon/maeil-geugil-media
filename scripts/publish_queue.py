"""queue/ 의 예약 항목을 Instagram 에 게시한다. GitHub Actions 에서 15분마다 실행된다.

표준 라이브러리만 쓴다. 토큰은 환경변수 IG_TOKEN 에서 읽고 어떤 출력에도 남기지 않는다.

환경변수
  IG_TOKEN    Instagram Login 장기 토큰 (필수)
  DRY_RUN     "true" 면 큐 읽기·이미지 확인·컨테이너 생성까지만 하고 게시·커밋 안 함
  MAX_POSTS   한 번에 게시할 최대 개수 (기본 2)

항목 상태 (post.json status)
  queued → posted (posted/ 로 이동)
         → stale  (stale/ 로 이동, 예약 시각 3시간 초과)
         → failed (failed/ 로 이동, 3회 실패 또는 중복 캡션)
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

GRAPH = "https://graph.instagram.com/v26.0"
HANDLE = "maeil.geugil"
KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parent.parent
QUEUE, POSTED, STALE, FAILED = (ROOT / d for d in ("queue", "posted", "stale", "failed"))
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_ATTEMPTS = 3
STALE_AFTER = timedelta(hours=3)
NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}_[A-Za-z0-9_-]+$")

TOKEN = os.environ.get("IG_TOKEN", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
MAX_POSTS = int(os.environ.get("MAX_POSTS", "2"))
REPO = os.environ.get("GITHUB_REPOSITORY", "jangwonyoon/maeil-geugil-media")


class PublishError(RuntimeError):
    pass


class Defer(RuntimeError):
    """이번 실행에서는 넘기고 다음 실행에 다시 본다 (시도 횟수에 넣지 않음)."""


def redact(text: str) -> str:
    return text.replace(TOKEN, "<redacted>") if TOKEN else text


def log(msg: str) -> None:
    print(redact(msg), flush=True)


def annotate(level: str, msg: str) -> None:
    print(f"::{level}::{redact(msg)}", flush=True)


def summary(line: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(redact(line) + "\n")


def now() -> datetime:
    return datetime.now(KST)


def stamp() -> str:
    return now().isoformat(timespec="seconds")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect())


def call(method: str, path: str, data: dict) -> dict:
    encoded = urllib.parse.urlencode(data).encode()
    url = GRAPH + path + ("?" + encoded.decode() if method == "GET" and data else "")
    req = urllib.request.Request(url, data=encoded if method == "POST" else None,
                                 headers={"Authorization": f"Bearer {TOKEN}"}, method=method)
    try:
        with OPENER.open(req, timeout=60) as res:
            out = json.load(res)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:600]
        e.close()
        raise PublishError(redact(f"{method} {path} HTTP {e.code}: {body}")) from None
    except (OSError, ValueError) as e:
        raise PublishError(redact(f"{method} {path} 응답 확인 실패: {type(e).__name__}")) from None
    if not isinstance(out, dict) or "error" in out:
        raise PublishError(redact(f"{method} {path} 응답 오류: {str(out)[:400]}"))
    return out


def object_id(result: dict, key="id") -> str:
    value = str(result.get(key, ""))
    if not re.fullmatch(r"[0-9]+", value):
        raise PublishError("API 응답에서 ID를 확인하지 못했습니다")
    return value


def account() -> str:
    """토큰 확인. 실패하면 워크플로 실패."""
    if not TOKEN or any(c.isspace() for c in TOKEN):
        raise PublishError("IG_TOKEN 이 없거나 형식이 올바르지 않습니다 (저장소 시크릿 확인)")
    me = call("GET", "/me", {"fields": "user_id,username,account_type"})
    if me.get("username", "").casefold() != HANDLE:
        raise PublishError(f"토큰 계정이 @{me.get('username')} 입니다. @{HANDLE} 아님")
    return object_id(me, "user_id")


def quota(user_id: str) -> tuple[int, int]:
    res = call("GET", f"/{user_id}/content_publishing_limit", {"fields": "quota_usage,config"})
    row = (res.get("data") or [{}])[0]
    used = int(row.get("quota_usage", 0))
    total = int((row.get("config") or {}).get("quota_total", 0)) or 50
    return used, total


def recent_media(user_id: str, limit=25) -> list[dict]:
    return call("GET", f"/{user_id}/media",
                {"fields": "id,timestamp,caption,permalink,media_type", "limit": limit}).get("data", [])


def norm(text: str) -> str:
    return " ".join((text or "").split())


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("+0000", "+00:00")).astimezone(KST)


def wait_ready(container_id: str) -> None:
    for _ in range(30):
        code = call("GET", f"/{container_id}", {"fields": "status_code"}).get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED", "PUBLISHED"):
            raise PublishError(f"컨테이너 {container_id} 상태: {code}")
        time.sleep(10)
    raise PublishError(f"컨테이너 {container_id} 가 5분 안에 FINISHED 되지 않았습니다")


# ---------- git ----------

def git(*args: str, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, check=check, capture_output=True, text=True)


def head_sha() -> str:
    return git("rev-parse", "HEAD").stdout.strip()


def commit_push(message: str) -> None:
    if DRY_RUN:
        return
    git("add", "-A", "queue", "posted", "stale", "failed")
    if not git("status", "--porcelain").stdout.strip():
        return
    git("commit", "-m", message)
    for i in range(4):
        if git("push", "origin", "HEAD:main", check=False).returncode == 0:
            return
        git("pull", "--rebase", "origin", "main", check=False)
        time.sleep(3 * (i + 1))
    raise PublishError("git push 실패 (게시 기록 커밋을 올리지 못함)")


# ---------- 항목 ----------

class Item:
    def __init__(self, folder: Path):
        self.folder = folder
        self.name = folder.name
        self.meta_path = folder / "post.json"
        self.meta = json.loads(self.meta_path.read_text("utf-8"))
        self.publish_at = datetime.fromisoformat(self.meta["publish_at"]).astimezone(KST)

    def save(self) -> None:
        tmp = self.meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.meta, ensure_ascii=False, indent=2) + "\n", "utf-8")
        os.replace(tmp, self.meta_path)

    def move(self, dest_root: Path) -> None:
        dest_root.mkdir(exist_ok=True)
        dest = dest_root / self.name
        if dest.exists():
            raise PublishError(f"{dest_root.name}/{self.name} 가 이미 있습니다")
        if DRY_RUN:
            return
        git("mv", str(self.folder.relative_to(ROOT)), str(dest.relative_to(ROOT)))
        self.folder = dest
        self.meta_path = dest / "post.json"

    def caption(self) -> str:
        text = (self.folder / self.meta.get("caption", "caption.txt")).read_text("utf-8").strip()
        tags = re.findall(r"(?<!\w)#\w+", text)
        if not 1 <= len(text) <= 2200 or len(tags) > 30:
            raise PublishError(f"캡션 {len(text)}자·해시태그 {len(tags)}개: 제한(2200자·30개) 초과")
        want = self.meta.get("caption_sha256")
        if want and want != sha256(text.encode()):
            raise PublishError("caption.txt 가 등록 때와 다릅니다")
        return text

    def images(self) -> list[tuple[Path, dict]]:
        imgs = self.meta.get("images") or []
        if not 1 <= len(imgs) <= 20:
            raise PublishError(f"이미지 {len(imgs)}장: 1~20장이어야 합니다")
        out = []
        for img in imgs:
            path = self.folder / img["file"]
            data = path.read_bytes()
            if not data.startswith(b"\xff\xd8\xff") or len(data) > MAX_IMAGE_BYTES:
                raise PublishError(f"{img['file']}: JPEG 아님 또는 8MB 초과")
            if img.get("sha256") and img["sha256"] != sha256(data):
                raise PublishError(f"{img['file']} 가 등록 때와 다릅니다")
            out.append((path, img))
        return out

    def image_url(self, path: Path, sha: str) -> str:
        rel = urllib.parse.quote(str(path.relative_to(ROOT)))
        return f"https://raw.githubusercontent.com/{REPO}/{sha}/{rel}"


def check_url(url: str, path: Path) -> None:
    """raw CDN 이 200 image/jpeg 를 주고 내용이 같은지. 아직 아니면 Defer."""
    try:
        with OPENER.open(urllib.request.Request(url, method="HEAD"), timeout=30) as res:
            ctype = res.headers.get_content_type()
            if res.status != 200 or ctype != "image/jpeg":
                raise Defer(f"이미지 주소 HEAD {res.status} {ctype}")
        with OPENER.open(urllib.request.Request(url), timeout=60) as res:
            body = res.read(MAX_IMAGE_BYTES + 1)
    except urllib.error.HTTPError as e:
        e.close()
        raise Defer(f"이미지 주소 HTTP {e.code} (raw CDN 반영 전일 수 있음)") from None
    except OSError as e:
        raise Defer(f"이미지 주소 확인 실패: {type(e).__name__}") from None
    if sha256(body) != sha256(path.read_bytes()):
        raise PublishError(f"공개 주소의 {path.name} 내용이 저장소 파일과 다릅니다")


def already_posted(user_id: str, item: Item, caption: str) -> dict | None:
    """같은 캡션 게시물. 등록 이후 것이면 이전 실행이 게시 후 기록 전에 끊긴 것으로 보고 복구,
    등록 이전 것이면 중복으로 본다."""
    enq = datetime.fromisoformat(item.meta.get("enqueued_at", item.meta["publish_at"])).astimezone(KST)
    for m in recent_media(user_id):
        if norm(m.get("caption")) == norm(caption):
            m["_after_enqueue"] = parse_ts(m["timestamp"]) >= enq - timedelta(minutes=1)
            return m
    return None


def build_containers(user_id: str, item: Item, urls: list[str], caption: str) -> str:
    if len(urls) == 1:
        cid = object_id(call("POST", f"/{user_id}/media", {"image_url": urls[0], "caption": caption}))
        log(f"  단일 이미지 컨테이너 {cid}")
        wait_ready(cid)
        return cid
    children = []
    for u in urls:
        children.append(object_id(call("POST", f"/{user_id}/media", {"image_url": u, "is_carousel_item": "true"})))
    log(f"  캐러셀 children {children}")
    for c in children:
        wait_ready(c)
    cid = object_id(call("POST", f"/{user_id}/media",
                         {"media_type": "CAROUSEL", "children": ",".join(children), "caption": caption}))
    log(f"  캐러셀 컨테이너 {cid}")
    wait_ready(cid)
    return cid


def record_posted(item: Item, media_id: str, recovered=False) -> None:
    info = call("GET", f"/{media_id}", {"fields": "permalink,timestamp,media_type"})
    item.meta.update(status="posted", media_id=media_id, permalink=info.get("permalink"),
                     media_type=info.get("media_type"),
                     published_at=parse_ts(info["timestamp"]).isoformat(timespec="seconds")
                     if info.get("timestamp") else stamp())
    if recovered:
        item.meta["recovered"] = True
    item.meta.pop("last_error", None)
    item.save()
    item.move(POSTED)
    commit_push(f"posted: {item.name}")
    log(f"  게시 완료 {media_id} {info.get('permalink')}")
    summary(f"| {item.name} | posted | {info.get('permalink')} |")


def process(user_id: str, item: Item, sha: str) -> None:
    caption = item.caption()
    imgs = item.images()
    log(f"  캡션 {len(caption)}자, 이미지 {len(imgs)}장 ({'캐러셀' if len(imgs) > 1 else '단일'})")
    found = already_posted(user_id, item, caption)
    if found:
        if found["_after_enqueue"] and not item.meta.get("test"):
            log("  같은 캡션 게시물이 등록 이후에 있음 → 이전 실행 결과로 보고 기록만 복구")
            record_posted(item, object_id(found), recovered=True)
            return
        raise PublishError(f"같은 캡션 게시물이 이미 있습니다 ({found.get('permalink')}). 중복 게시 중단",)
    urls = [item.image_url(p, sha) for p, _ in imgs]
    for (p, _), u in zip(imgs, urls):
        check_url(u, p)
    log(f"  이미지 주소 {len(urls)}개 200 image/jpeg·내용 일치")
    used, total = quota(user_id)
    log(f"  게시 한도 {used}/{total} (24시간)")
    if used + 1 > total:
        raise Defer(f"게시 한도 소진 {used}/{total}")
    cid = build_containers(user_id, item, urls, caption)
    if DRY_RUN or item.meta.get("test"):
        log(f"  dry_run: 컨테이너 {cid} FINISHED 까지 확인, 게시 안 함")
        summary(f"| {item.name} | dry_run OK (컨테이너 {cid}) | - |")
        return
    res = call("POST", f"/{user_id}/media_publish", {"creation_id": cid})
    record_posted(item, object_id(res))


def load_items() -> list[Item]:
    items = []
    if not QUEUE.exists():
        return items
    for folder in sorted(QUEUE.iterdir()):
        if not folder.is_dir():
            continue
        if not NAME_RE.match(folder.name) or not (folder / "post.json").exists():
            annotate("warning", f"queue/{folder.name}: 이름 형식 또는 post.json 이상, 건너뜀")
            continue
        try:
            items.append(Item(folder))
        except (ValueError, KeyError, OSError) as e:
            annotate("warning", f"queue/{folder.name}: post.json 읽기 실패 {type(e).__name__}")
    return sorted(items, key=lambda i: (i.publish_at, i.name))


def main() -> int:
    log(f"실행 {stamp()} KST, dry_run={DRY_RUN}, 최대 {MAX_POSTS}개")
    summary(f"### 예약 게시 {stamp()} (dry_run={DRY_RUN})\n\n| 항목 | 결과 | 링크 |\n|---|---|---|")
    if not DRY_RUN:
        git("config", "user.name", "github-actions[bot]")
        git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    try:
        user_id = account()
    except PublishError as e:
        annotate("error", f"토큰 확인 실패: {e}")
        summary("| - | 토큰 확인 실패 | - |")
        return 1
    log(f"토큰 확인 @{HANDLE} OK")
    sha = head_sha()
    failed = False
    done = 0
    t = now()
    for item in load_items():
        if done >= MAX_POSTS:
            break
        if item.meta.get("status", "queued") != "queued":
            continue
        test = bool(item.meta.get("test"))
        if test and not DRY_RUN:
            continue  # 시험 항목은 실제 실행에서 절대 다루지 않는다
        if not test:
            if item.publish_at > t:
                continue
            if t - item.publish_at > STALE_AFTER:
                late = int((t - item.publish_at).total_seconds() // 60)
                annotate("error", f"{item.name}: 예약 시각 {late}분 지남 → stale, 게시 안 함")
                summary(f"| {item.name} | stale ({late}분 지남) | - |")
                item.meta.update(status="stale", stale_at=stamp())
                if not DRY_RUN:
                    item.save()
                    item.move(STALE)
                    commit_push(f"stale: {item.name}")
                failed = True
                continue
        log(f"[{item.name}] 예약 {item.publish_at.isoformat(timespec='minutes')}{' (시험)' if test else ''}")
        try:
            process(user_id, item, sha)
            done += 1
        except Defer as e:
            annotate("warning", f"{item.name}: 다음 실행으로 미룸 — {e}")
            summary(f"| {item.name} | 미룸: {e} | - |")
        except (PublishError, OSError, ValueError, KeyError) as e:
            msg = redact(str(e))
            item.meta["attempts"] = int(item.meta.get("attempts", 0)) + 1
            item.meta.update(last_error=msg[:800], last_attempt_at=stamp())
            final = item.meta["attempts"] >= MAX_ATTEMPTS or "중복 게시" in msg
            if final:
                item.meta["status"] = "failed"
                failed = True
            annotate("error" if final else "warning",
                     f"{item.name}: 실패 {item.meta['attempts']}/{MAX_ATTEMPTS} — {msg}")
            summary(f"| {item.name} | 실패 {item.meta['attempts']}/{MAX_ATTEMPTS}{' → failed/' if final else ''} | - |")
            if not DRY_RUN:
                item.save()
                if final:
                    item.move(FAILED)
                commit_push(f"{'failed' if final else 'retry'}: {item.name} ({item.meta['attempts']}/{MAX_ATTEMPTS})")
    log("끝")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
