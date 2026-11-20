#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=10"]
# ///
"""덱 폴더(out/<slug>)를 예약 게시 큐에 올린다.

  uv run scripts/enqueue.py <덱폴더> <캡션.txt> --at "2026-11-21 08:00" [--slug gtx-c] [--no-push]

- 01.png, 02.png … 를 JPEG(품질 92, sRGB, 1080×1350 그대로)로 바꿔 queue/<YYYY-MM-DD_HHMM>_<slug>/ 에 넣는다.
- 1장이면 단일 이미지, 2장 이상이면 캐러셀로 게시된다.
- 같은 시각(분 단위) 항목이 있거나 시각이 과거면 거절한다.
- 커밋·push 까지 한다 (--no-push 면 폴더만 만들고 멈춤).
"""
import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageCms

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "queue"
SIZE = (1080, 1350)
QUALITY = 92
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def fail(msg: str) -> None:
    print(f"거절: {msg}", file=sys.stderr)
    sys.exit(2)


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, check=check, capture_output=True, text=True)


def ascii_slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", name)
    s = re.sub(r"_+", "_", s).strip("_-")
    return s[:40] or "post"


def to_srgb_jpeg(png: Path) -> bytes:
    im = Image.open(png)
    if im.size != SIZE:
        fail(f"{png.name} 크기 {im.size}, 1080×1350 이어야 합니다")
    icc = im.info.get("icc_profile")
    rgba = im.convert("RGBA")
    bg = Image.new("RGBA", SIZE, (255, 255, 255, 255))
    bg.alpha_composite(rgba)
    rgb = bg.convert("RGB")
    srgb = ImageCms.createProfile("sRGB")
    if icc:
        src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        rgb = ImageCms.profileToProfile(rgb, src, srgb, outputMode="RGB")
    buf = io.BytesIO()
    rgb.save(buf, "JPEG", quality=QUALITY, subsampling=0, optimize=True,
             icc_profile=ImageCms.ImageCmsProfile(srgb).tobytes())
    data = buf.getvalue()
    if len(data) > MAX_IMAGE_BYTES:
        fail(f"{png.name} JPEG {len(data)}B > 8MB")
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("deck", type=Path, help="01.png… 가 있는 폴더 (예: out/B_GTX-C_착공)")
    ap.add_argument("caption", type=Path, help="캡션.txt")
    ap.add_argument("--at", required=True, help='게시 시각 KST, 예: "2026-11-21 08:00"')
    ap.add_argument("--slug", help="폴더 이름용 영문 slug (기본: 덱 폴더 이름에서 영문·숫자만)")
    ap.add_argument("--no-push", action="store_true", help="큐 폴더만 만들고 커밋·push 안 함")
    args = ap.parse_args()

    try:
        at = datetime.strptime(args.at.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    except ValueError:
        fail('--at 형식은 "YYYY-MM-DD HH:MM" (KST)')
    now = datetime.now(KST)
    if at <= now:
        fail(f"게시 시각 {at:%Y-%m-%d %H:%M} 이 이미 지났습니다 (지금 {now:%H:%M})")
    if at - now < timedelta(minutes=10):
        print("주의: 10분 이내 예약. raw 주소 반영이 늦으면 다음 실행(15분 뒤)으로 밀릴 수 있습니다")

    deck = args.deck.resolve()
    pngs = [deck / f"{n:02d}.png" for n in range(1, 21) if (deck / f"{n:02d}.png").exists()]
    if not pngs:
        fail(f"{deck} 에 01.png 가 없습니다")
    for png in pngs:
        with Image.open(png) as im:
            if im.size != SIZE:
                fail(f"{png.name} 크기 {im.size}, 1080×1350 이어야 합니다")
    caption = args.caption.read_text("utf-8").strip()
    tags = re.findall(r"(?<!\w)#\w+", caption)
    if not 1 <= len(caption) <= 2200 or len(tags) > 30:
        fail(f"캡션 {len(caption)}자·해시태그 {len(tags)}개: 제한(2200자·30개) 초과")

    if not args.no_push:
        if git("status", "--porcelain").stdout.strip():
            fail("저장소에 커밋 안 된 변경이 있습니다. 먼저 정리하세요")
        git("pull", "--rebase", "-q", "origin", "main")

    key = at.strftime("%Y-%m-%d_%H%M")
    clash = [p.name for p in QUEUE.glob(f"{key}_*")] if QUEUE.exists() else []
    if clash:
        fail(f"같은 시각 항목이 이미 큐에 있습니다: {', '.join(clash)}")
    slug = ascii_slug(args.slug or deck.name)
    folder = QUEUE / f"{key}_{slug}"
    folder.mkdir(parents=True)

    images = []
    for png in pngs:
        data = to_srgb_jpeg(png)
        name = f"{png.stem}.jpg"
        (folder / name).write_bytes(data)
        images.append({"file": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        print(f"  {name} {len(data) / 1024:.0f}KB")
    (folder / "caption.txt").write_text(caption + "\n", "utf-8")
    meta = {
        "publish_at": at.isoformat(timespec="seconds"),
        "caption": "caption.txt",
        "caption_sha256": hashlib.sha256(caption.encode()).hexdigest(),
        "images": images,
        "kind": "carousel" if len(images) > 1 else "image",
        "status": "queued",
        "attempts": 0,
        "enqueued_at": now.isoformat(timespec="seconds"),
        "source": deck.name,
    }
    (folder / "post.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", "utf-8")
    lines = caption.splitlines()
    print(f"큐 등록: queue/{folder.name} ({meta['kind']}, {len(images)}장, 캡션 {len(caption)}자)")
    print(f"  첫 줄: {lines[0]}")

    if args.no_push:
        print("--no-push: 커밋·push 안 함")
        return 0
    git("add", str(folder.relative_to(ROOT)))
    git("commit", "-q", "-m", f"queue: {folder.name}")
    push = git("push", "-q", "origin", "HEAD:main", check=False)
    if push.returncode != 0:
        print(push.stderr, file=sys.stderr)
        fail("push 실패. git pull --rebase 후 git push 를 직접 하세요")
    print(f"push 완료. {at:%m-%d %H:%M} 이후 첫 실행(15분 간격, 지연 가능)에 게시됩니다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
