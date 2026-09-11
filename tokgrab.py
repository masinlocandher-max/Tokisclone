"""tokgrab - one-command bulk TikTok profile archiver.

Design goal: one profile URL in, every public video out, clean rendition,
straight into your own Google Drive - running on your own machine and your
own internet connection.

Why this file exists alongside the queue/worker/Actions machinery:

TikTok profile enumeration is the part that breaks. It breaks because the
public web API that lists a user's posts is signed and is aggressively gated
against datacenter IPs (GitHub Actions runners, cloud VMs, serverless). No
amount of retry logic fixes that from a datacenter.

So tokgrab splits the job in two and uses the right tool for each half:

  1. ENUMERATE with a real browser you have logged into once. A real Chrome
     profile scrolling a real profile page is the only method that reliably
     sees the complete post list.
  2. FETCH each individual video URL with yt-dlp. Single video pages are far
     more forgiving than profile pages, and yt-dlp already handles the
     format/CDN details.

Everything is resumable. State lives in one JSON file, so a run that dies at
video 300 of 500 picks up at 301 instead of starting over.

Usage:
    python tokgrab.py doctor
    python tokgrab.py login
    python tokgrab.py run @username

Only archive content you own or are otherwise allowed to copy.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is optional
    pass


DEFAULT_WORKDIR = Path(os.getenv("TOKGRAB_HOME", "~/.tokisclone/tokgrab")).expanduser()
DEFAULT_PROFILE_DIR = DEFAULT_WORKDIR / "chrome-profile"
PROFILE_SCROLL_IDLE_ROUNDS = 6
PROFILE_SCROLL_MAX_ROUNDS = 400

# TikTok serves the marked rendition from ``download_addr``. yt-dlp surfaces it
# with a format id that *contains* "download" - it is not always exactly
# "download", which is why an equality check silently lets marked files
# through. Match on substrings instead.
MARKED_FORMAT_HINTS = ("download",)
MARKED_NOTE_HINTS = ("watermark", "marked")
MARKED_URL_HINTS = ("watermark=1", "playwm", "/play/wm/")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def log(message: str) -> None:
    print(message, flush=True)


def normalize_username(value: str) -> str:
    """Accept @name, name, or any tiktok.com profile URL."""
    value = (value or "").strip()
    match = re.search(r"tiktok\.com/@([A-Za-z0-9._-]+)", value)
    if match:
        return match.group(1)
    return value.lstrip("@").strip("/")


def profile_url(username: str) -> str:
    return f"https://www.tiktok.com/@{username}"


def safe_name(value: str, fallback: str = "unknown") -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or fallback).strip())
    return value.strip("._-")[:80] or fallback


def jitter_sleep(base: float) -> None:
    """Sleep with jitter. Constant-interval requests are what gets you throttled."""
    if base <= 0:
        return
    time.sleep(base * random.uniform(0.7, 1.6))


class State:
    """Resumable per-video state, stored as one JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {"videos": {}}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("videos"), dict):
                    self.data = loaded
            except (OSError, json.JSONDecodeError):
                log(f"[warn] could not read {path}, starting fresh state")

    @property
    def videos(self) -> dict[str, Any]:
        return self.data["videos"]

    def entry(self, video_id: str) -> dict[str, Any]:
        return self.videos.setdefault(video_id, {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


# --------------------------------------------------------------------------
# browser session: login + cookie export
# --------------------------------------------------------------------------


def _chrome_executable() -> str | None:
    candidates = [
        os.getenv("CHROME_BIN"),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    ]
    return next((c for c in candidates if c and Path(c).exists()), None)


def _launch_context(playwright: Any, profile_dir: Path, headless: bool) -> Any:
    profile_dir.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "headless": headless,
        "locale": "en-US",
        "viewport": {"width": 1280, "height": 900},
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    executable = _chrome_executable()
    if executable:
        # Real Chrome beats bundled Chromium here: the fingerprint is ordinary.
        kwargs["executable_path"] = executable
    return playwright.chromium.launch_persistent_context(**kwargs)


def cmd_login(args: argparse.Namespace) -> int:
    from playwright.sync_api import sync_playwright

    profile_dir = Path(args.profile_dir).expanduser()
    log("Opening Chrome. Log into TikTok in the window, then come back here.")
    log(f"Session is saved in {profile_dir} and reused by every later command.")

    with sync_playwright() as playwright:
        context = _launch_context(playwright, profile_dir, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.tiktok.com/login", wait_until="domcontentloaded")
        input("\nPress Enter here once you are logged in and can see your feed... ")
        cookies = context.cookies()
        _write_netscape_cookies(cookies, Path(args.cookies).expanduser())
        context.close()

    logged_in = any(c.get("name") == "sessionid" for c in cookies)
    log(f"Saved {len(cookies)} cookies to {args.cookies}")
    if logged_in:
        log("Found a TikTok session cookie. You are logged in.")
        return 0
    log("[warn] No sessionid cookie found. You may not be fully logged in.")
    return 1


def _write_netscape_cookies(cookies: Iterable[dict[str, Any]], path: Path) -> None:
    """Write cookies in the Netscape format yt-dlp expects."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Netscape HTTP Cookie File", "# Written by tokgrab. Keep this private.", ""]
    for cookie in cookies:
        domain = cookie.get("domain", "")
        if not domain:
            continue
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        expires = int(cookie.get("expires") or 0)
        if expires <= 0:
            expires = int(time.time()) + 60 * 60 * 24 * 365
        lines.append(
            "\t".join(
                [
                    domain,
                    include_sub,
                    cookie.get("path", "/"),
                    secure,
                    str(expires),
                    cookie.get("name", ""),
                    cookie.get("value", ""),
                ]
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


# --------------------------------------------------------------------------
# enumerate: scroll a real profile page and collect every video id
# --------------------------------------------------------------------------


def _items_from_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    items = payload.get("itemList") or payload.get("items") or []
    return [item for item in items if isinstance(item, dict)]


def _normalize_item(item: dict[str, Any], fallback_username: str) -> dict[str, Any] | None:
    video_id = str(item.get("id") or item.get("itemId") or "")
    if not video_id.isdigit():
        return None
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    username = str(author.get("uniqueId") or fallback_username).lstrip("@")
    stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
    video = item.get("video") if isinstance(item.get("video"), dict) else {}
    return {
        "id": video_id,
        "url": f"https://www.tiktok.com/@{username}/video/{video_id}",
        "username": username,
        "title": item.get("desc") or None,
        "duration": video.get("duration"),
        "created": item.get("createTime"),
        "view_count": stats.get("playCount"),
        "like_count": stats.get("diggCount"),
    }


def scan_profile(
    username: str,
    profile_dir: Path,
    *,
    headless: bool,
    scroll_pause: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Scroll the profile to the end and return every video the page reveals.

    Two collectors run at once because either can fail on its own:
      - the post-list API responses the page fetches while scrolling (rich)
      - a regex over the rendered HTML (poor metadata, but survives API changes)
    """
    from playwright.sync_api import sync_playwright

    url = profile_url(username)
    found: dict[str, dict[str, Any]] = {}
    api_hits = 0
    api_errors: list[str] = []

    with sync_playwright() as playwright:
        context = _launch_context(playwright, profile_dir, headless=headless)
        page = context.pages[0] if context.pages else context.new_page()

        def on_response(response: Any) -> None:
            nonlocal api_hits
            if "/api/post/item_list/" not in response.url:
                return
            api_hits += 1
            try:
                payload = response.json()
            except Exception as exc:  # noqa: BLE001 - any parse failure is just a miss
                api_errors.append(f"{response.status}: {type(exc).__name__}")
                return
            for item in _items_from_payload(payload):
                row = _normalize_item(item, username)
                if row:
                    found.setdefault(row["id"], row)

        page.on("response", on_response)
        log(f"Opening {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(4000)

        if "login" in page.url or "verify" in page.url:
            log("[warn] TikTok redirected to a login/verification page.")
            log("       Run `python tokgrab.py login` (or rerun with --show) and finish it by hand.")

        idle_rounds = 0
        previous = -1
        for round_index in range(PROFILE_SCROLL_MAX_ROUNDS):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(int(scroll_pause * 1000 * random.uniform(0.8, 1.4)))

            for video_id in re.findall(r"/video/(\d{15,25})", page.content()):
                found.setdefault(
                    video_id,
                    {
                        "id": video_id,
                        "url": f"https://www.tiktok.com/@{username}/video/{video_id}",
                        "username": username,
                        "source": "dom",
                    },
                )

            total = len(found)
            if total == previous:
                idle_rounds += 1
            else:
                idle_rounds = 0
                log(f"  scroll {round_index + 1}: {total} videos found")
            previous = total

            if idle_rounds >= PROFILE_SCROLL_IDLE_ROUNDS:
                log(f"  reached the end after {round_index + 1} scrolls")
                break

        cookies = context.cookies()
        context.close()

    if api_hits == 0:
        log("[warn] TikTok's post-list API never responded during the scroll.")
        log("       Results came from page HTML only and may be incomplete.")
    if api_errors:
        log(f"[warn] {len(api_errors)} post-list responses could not be parsed")

    return sorted(found.values(), key=lambda row: int(row["id"]), reverse=True), cookies


def cmd_scan(args: argparse.Namespace) -> int:
    username = normalize_username(args.username)
    if not username:
        log("Give me a username, e.g. python tokgrab.py scan @someone")
        return 2

    workdir = Path(args.workdir).expanduser() / safe_name(username)
    videos, cookies = scan_profile(
        username,
        Path(args.profile_dir).expanduser(),
        headless=not args.show,
        scroll_pause=args.scroll_pause,
    )
    _write_netscape_cookies(cookies, Path(args.cookies).expanduser())

    inventory = workdir / "inventory.json"
    inventory.parent.mkdir(parents=True, exist_ok=True)
    inventory.write_text(
        json.dumps(
            {"username": username, "profile_url": profile_url(username), "count": len(videos), "videos": videos},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    state = State(workdir / "state.json")
    for row in videos:
        entry = state.entry(row["id"])
        entry.setdefault("url", row["url"])
        entry.setdefault("title", row.get("title"))
    state.save()

    log(f"\n{len(videos)} videos catalogued -> {inventory}")
    if not videos:
        log("Nothing found. Rerun with --show to watch the browser and see what TikTok is doing.")
        return 1
    return 0


# --------------------------------------------------------------------------
# fetch: download the clean rendition of each video
# --------------------------------------------------------------------------


def classify_format(fmt: dict[str, Any]) -> dict[str, Any]:
    """Describe one yt-dlp format, including whether it looks marked.

    This is a heuristic, not a guarantee. `tokgrab probe` prints the same
    table so you can check a real video with your own eyes before trusting it.
    """
    format_id = str(fmt.get("format_id") or "").lower()
    note = str(fmt.get("format_note") or "").lower()
    url = str(fmt.get("url") or "").lower()

    reasons: list[str] = []
    if any(hint in format_id for hint in MARKED_FORMAT_HINTS):
        reasons.append(f"format_id contains {format_id!r}")
    if any(hint in note for hint in MARKED_NOTE_HINTS):
        reasons.append(f"format_note says {note!r}")
    if any(hint in url for hint in MARKED_URL_HINTS):
        reasons.append("media URL looks like a marked rendition")

    return {
        "format_id": fmt.get("format_id"),
        "format_note": fmt.get("format_note"),
        "ext": fmt.get("ext"),
        "width": fmt.get("width"),
        "height": fmt.get("height"),
        "vcodec": fmt.get("vcodec"),
        "acodec": fmt.get("acodec"),
        "tbr": fmt.get("tbr"),
        "filesize": fmt.get("filesize") or fmt.get("filesize_approx"),
        "marked": bool(reasons),
        "marked_reasons": reasons,
    }


def choose_format(formats: list[dict[str, Any]], max_height: int | None) -> dict[str, Any] | None:
    """Pick the best clean, self-contained rendition.

    TikTok serves muxed mp4 (video and audio in one stream) and publishes no
    audio-only formats. A `bestvideo+bestaudio` selector therefore never
    matches and quietly falls through - which is a good way to end up with
    nothing downloaded, or with the marked file.
    """
    candidates = []
    for fmt in formats:
        described = classify_format(fmt)
        if described["marked"]:
            continue
        if fmt.get("vcodec") in (None, "none"):
            continue
        if max_height and (described["height"] or 0) > max_height:
            continue
        candidates.append((fmt, described))

    if not candidates:
        return None

    def rank(pair: tuple[dict[str, Any], dict[str, Any]]) -> tuple:
        fmt, described = pair
        height = described["height"] or 0
        size = described["filesize"] or 0
        bitrate = described["tbr"] or 0
        # h264 first: it plays everywhere. bytevc1/h265 is smaller but chokes
        # on older editors and phones.
        codec_rank = 1 if "avc" in str(fmt.get("vcodec") or "").lower() or "h264" in str(fmt.get("vcodec") or "").lower() else 0
        has_audio = 1 if fmt.get("acodec") not in (None, "none") else 0
        return (has_audio, height, codec_rank, size, bitrate)

    candidates.sort(key=rank, reverse=True)
    return candidates[0][0]


def _ydl(extra: dict[str, Any], cookies: Path | None) -> Any:
    import yt_dlp

    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
    }
    if cookies and cookies.exists():
        options["cookiefile"] = str(cookies)
    options.update(extra)
    return yt_dlp.YoutubeDL(options)


def probe_video(url: str, cookies: Path | None) -> dict[str, Any]:
    with _ydl({"skip_download": True}, cookies) as client:
        info = client.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise RuntimeError("yt-dlp returned no metadata")
    return info


def cmd_probe(args: argparse.Namespace) -> int:
    info = probe_video(args.url, Path(args.cookies).expanduser())
    formats = [classify_format(f) for f in info.get("formats") or []]
    if not formats:
        log("yt-dlp reported no formats for this URL.")
        return 1

    log(f"\n{info.get('title') or info.get('id')}\n")
    header = f"{'format_id':<28} {'res':<11} {'codec':<10} {'size':>10}  marked"
    log(header)
    log("-" * len(header))
    for fmt in formats:
        res = f"{fmt['width'] or '?'}x{fmt['height'] or '?'}"
        size = f"{(fmt['filesize'] or 0) / 1_048_576:.1f}MB" if fmt["filesize"] else "-"
        flag = "YES  " + "; ".join(fmt["marked_reasons"]) if fmt["marked"] else "no"
        log(f"{str(fmt['format_id']):<28} {res:<11} {str(fmt['vcodec'])[:10]:<10} {size:>10}  {flag}")

    chosen = choose_format(info.get("formats") or [], args.max_height)
    log("")
    if chosen:
        log(f"tokgrab would download: {chosen.get('format_id')} "
            f"({chosen.get('width')}x{chosen.get('height')})")
    else:
        log("tokgrab would REFUSE this video: no clean rendition available.")
    return 0


def fetch_one(
    video_id: str,
    url: str,
    dest: Path,
    cookies: Path | None,
    *,
    max_height: int | None,
) -> dict[str, Any]:
    info = probe_video(url, cookies)
    chosen = choose_format(info.get("formats") or [], max_height)
    if not chosen:
        raise RuntimeError(
            "no clean rendition offered for this video - refusing rather than "
            "saving the marked copy"
        )

    uploader = safe_name(str(info.get("uploader_id") or info.get("uploader") or "tiktok"))
    stem = f"{uploader}_{video_id}"
    outtmpl = str(dest / f"{stem}.%(ext)s")

    with _ydl(
        {
            "format": str(chosen.get("format_id")),
            "outtmpl": outtmpl,
            "writeinfojson": False,
            "continuedl": True,
        },
        cookies,
    ) as client:
        client.download([url])

    produced = sorted(dest.glob(f"{stem}.*"), key=lambda p: p.stat().st_size, reverse=True)
    media = [p for p in produced if p.suffix.lower() in {".mp4", ".webm", ".mkv", ".mov"}]
    if not media:
        raise RuntimeError("download finished but no media file appeared")

    meta_path = dest / f"{stem}.json"
    meta_path.write_text(
        json.dumps(
            {
                "id": video_id,
                "url": url,
                "title": info.get("title"),
                "description": info.get("description"),
                "uploader": info.get("uploader"),
                "uploader_id": info.get("uploader_id"),
                "duration": info.get("duration"),
                "upload_date": info.get("upload_date"),
                "view_count": info.get("view_count"),
                "like_count": info.get("like_count"),
                "chosen_format": classify_format(chosen),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return {
        "path": str(media[0]),
        "meta_path": str(meta_path),
        "bytes": media[0].stat().st_size,
        "format_id": chosen.get("format_id"),
        "height": chosen.get("height"),
    }


def cmd_fetch(args: argparse.Namespace) -> int:
    username = normalize_username(args.username)
    workdir = Path(args.workdir).expanduser() / safe_name(username)
    state = State(workdir / "state.json")
    if not state.videos:
        log(f"No inventory for @{username}. Run `python tokgrab.py scan @{username}` first.")
        return 2

    dest = workdir / "media"
    dest.mkdir(parents=True, exist_ok=True)
    cookies = Path(args.cookies).expanduser()

    pending = [
        (video_id, entry)
        for video_id, entry in state.videos.items()
        if not entry.get("path") or not Path(entry["path"]).exists()
    ]
    if args.retry_failed:
        for _, entry in pending:
            entry.pop("error", None)
    else:
        pending = [(vid, e) for vid, e in pending if not e.get("error")]

    log(f"{len(pending)} of {len(state.videos)} videos left to download.")
    ok = failed = 0

    for index, (video_id, entry) in enumerate(pending, start=1):
        url = entry.get("url") or f"https://www.tiktok.com/@{username}/video/{video_id}"
        label = f"[{index}/{len(pending)}] {video_id}"
        try:
            result = fetch_one(video_id, url, dest, cookies, max_height=args.max_height)
        except Exception as exc:  # noqa: BLE001 - one bad video must not kill the run
            entry["error"] = f"{type(exc).__name__}: {exc}"
            failed += 1
            log(f"{label} FAILED - {entry['error']}")
        else:
            entry.pop("error", None)
            entry.update(result)
            ok += 1
            log(f"{label} ok - {result['height']}p, {result['bytes'] / 1_048_576:.1f}MB")
        state.save()
        if index < len(pending):
            jitter_sleep(args.delay)

    log(f"\nDownloaded {ok}, failed {failed}. Files in {dest}")
    return 0 if failed == 0 else 1


# --------------------------------------------------------------------------
# push: upload to your own Google Drive
# --------------------------------------------------------------------------


def cmd_push(args: argparse.Namespace) -> int:
    from drive_storage import DriveStorage

    username = normalize_username(args.username)
    workdir = Path(args.workdir).expanduser() / safe_name(username)
    state = State(workdir / "state.json")
    if not state.videos:
        log(f"No state for @{username}. Nothing to upload.")
        return 2

    storage = DriveStorage(args.drive_folder)
    folder = storage.creator_folder("tiktok", username)
    log(f"Uploading into Drive folder {folder['name']} ({folder['id']})")

    pending = [
        (video_id, entry)
        for video_id, entry in state.videos.items()
        if entry.get("path") and Path(entry["path"]).exists() and not entry.get("drive_file_id")
    ]
    log(f"{len(pending)} files to upload.")

    ok = failed = 0
    for index, (video_id, entry) in enumerate(pending, start=1):
        path = Path(entry["path"])
        label = f"[{index}/{len(pending)}] {path.name}"

        existing = storage.find_video("tiktok", video_id)
        if existing:
            entry["drive_file_id"] = existing["id"]
            state.save()
            log(f"{label} already in Drive, skipped")
            continue

        try:
            uploaded = storage.upload_file(
                path,
                parent_id=folder["id"],
                mime_type="video/mp4",
                properties={"kind": "video", "platform": "tiktok", "video_id": video_id},
            )
            meta_path = entry.get("meta_path")
            if meta_path and Path(meta_path).exists():
                storage.upload_file(
                    meta_path,
                    parent_id=folder["id"],
                    mime_type="application/json",
                    properties={"kind": "metadata", "platform": "tiktok", "video_id": video_id},
                )
        except Exception as exc:  # noqa: BLE001 - keep going through the batch
            entry["upload_error"] = f"{type(exc).__name__}: {exc}"
            failed += 1
            log(f"{label} FAILED - {entry['upload_error']}")
        else:
            entry.pop("upload_error", None)
            entry["drive_file_id"] = uploaded["id"]
            ok += 1
            log(f"{label} uploaded")
            if args.delete_local:
                path.unlink(missing_ok=True)
        state.save()

    log(f"\nUploaded {ok}, failed {failed}.")
    return 0 if failed == 0 else 1


# --------------------------------------------------------------------------
# run + doctor
# --------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    # A scan that found nothing must stop the pipeline. Running fetch and push
    # over an empty inventory just buries the real error under two more.
    code = cmd_scan(args)
    if code != 0:
        return code

    fetch_code = cmd_fetch(args)
    if fetch_code == 2:
        return fetch_code

    # Push whatever did download, even if some videos failed.
    push_code = cmd_push(args)
    return fetch_code or push_code


def cmd_setup(args: argparse.Namespace) -> int:
    """Install everything tokgrab needs, then walk the two logins."""
    import shutil
    import subprocess

    def run(label: str, command: list[str]) -> bool:
        log(f"\n-> {label}")
        try:
            subprocess.check_call(command)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            log(f"   failed: {exc}")
            return False
        return True

    root = Path(__file__).resolve().parent
    reqs = root / "requirements-worker.txt"

    ok = run(
        "Installing python packages",
        [sys.executable, "-m", "pip", "install", "-q", "-U", "-r", str(reqs)],
    )
    if not ok:
        log("Could not install dependencies. Fix pip, then rerun `setup`.")
        return 1

    run("Installing the browser playwright drives", [sys.executable, "-m", "playwright", "install", "chromium"])

    if not shutil.which("ffmpeg"):
        log("\n[warn] ffmpeg is not on your PATH. Install it before a big run:")
        log("       macOS: brew install ffmpeg")
        log("       Ubuntu/Debian: sudo apt install ffmpeg")
        log("       Windows: winget install Gyan.FFmpeg")

    log("\n-> Google Drive")
    token = Path(os.getenv("GOOGLE_DRIVE_TOKEN_FILE", "token.json"))
    folder = args.drive_folder or os.getenv("GOOGLE_DRIVE_ROOT_FOLDER_ID")
    if not (root / "client_secret.json").exists():
        log("   Missing client_secret.json.")
        log("   Google Cloud Console -> create an OAuth *Desktop* client -> download")
        log(f"   the JSON and save it as {root / 'client_secret.json'}")
    elif token.exists():
        log(f"   Already authorized ({token}).")
    else:
        run("   Authorizing Google Drive", [sys.executable, str(root / "authorize_drive.py")])

    if not folder:
        log("\n   Set GOOGLE_DRIVE_ROOT_FOLDER_ID in .env. Open your Drive folder;")
        log("   the id is the last part of the URL after /folders/.")

    log("\n-> TikTok")
    cookies = Path(args.cookies).expanduser()
    if cookies.exists() and "sessionid" in cookies.read_text(encoding="utf-8", errors="ignore"):
        log("   Session already saved.")
    else:
        cmd_login(args)

    log("\nRunning doctor to confirm:\n")
    return cmd_doctor(args)


def cmd_doctor(args: argparse.Namespace) -> int:
    problems = 0

    def check(label: str, ok: bool, hint: str = "") -> None:
        nonlocal problems
        log(f"  {'OK  ' if ok else 'MISS'}  {label}")
        if not ok:
            problems += 1
            if hint:
                log(f"        {hint}")

    log("Environment:")
    check(f"python {sys.version.split()[0]}", sys.version_info >= (3, 10), "python 3.10+ required")

    try:
        import yt_dlp

        check(f"yt-dlp {yt_dlp.version.__version__}", True)
    except ImportError:
        check("yt-dlp", False, "pip install -U yt-dlp")

    try:
        import playwright  # noqa: F401

        check("playwright", True)
    except ImportError:
        check("playwright", False, "pip install playwright && playwright install chromium")

    import shutil

    check("ffmpeg", shutil.which("ffmpeg") is not None, "install ffmpeg for reliable remuxing")
    check("real Chrome", _chrome_executable() is not None, "optional, but a real Chrome fingerprint helps")

    log("\nCredentials:")
    cookies = Path(args.cookies).expanduser()
    has_session = cookies.exists() and "sessionid" in cookies.read_text(encoding="utf-8", errors="ignore")
    check(f"cookies at {cookies}", has_session, "run: python tokgrab.py login")
    check(
        "GOOGLE_DRIVE_ROOT_FOLDER_ID",
        bool(args.drive_folder or os.getenv("GOOGLE_DRIVE_ROOT_FOLDER_ID")),
        "put your Drive folder id in .env",
    )
    token = Path(os.getenv("GOOGLE_DRIVE_TOKEN_FILE", "token.json"))
    check(f"Drive token at {token}", token.exists(), "run: python authorize_drive.py")

    log("\nNetwork:")
    log("  tokgrab is meant to run on your own machine and home internet.")
    log("  TikTok gates profile enumeration hard on datacenter IPs, so a cloud")
    log("  VM or a GitHub Actions runner will usually return an empty profile.")

    log(f"\n{problems} thing(s) to fix." if problems else "\nAll good.")
    return 0 if problems == 0 else 1


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tokgrab",
        description="Bulk-archive a TikTok profile to your own Google Drive.",
    )
    parser.add_argument("--workdir", default=str(DEFAULT_WORKDIR), help="where inventory, state and media live")
    parser.add_argument("--profile-dir", default=str(DEFAULT_PROFILE_DIR), help="persistent Chrome profile")
    parser.add_argument("--cookies", default=str(DEFAULT_WORKDIR / "cookies.txt"), help="Netscape cookie file for yt-dlp")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check that everything is installed and configured")
    doctor.add_argument("--drive-folder", default=None)
    doctor.set_defaults(func=cmd_doctor)

    setup = sub.add_parser("setup", help="install dependencies and walk both logins")
    setup.add_argument("--drive-folder", default=None)
    setup.set_defaults(func=cmd_setup)

    login = sub.add_parser("login", help="log into TikTok once in a real browser")
    login.set_defaults(func=cmd_login)

    scan = sub.add_parser("scan", help="list every public video on a profile")
    scan.add_argument("username")
    scan.add_argument("--show", action="store_true", help="show the browser window instead of running headless")
    scan.add_argument("--scroll-pause", type=float, default=1.6)
    scan.set_defaults(func=cmd_scan)

    probe = sub.add_parser("probe", help="print the real format table for one video")
    probe.add_argument("url")
    probe.add_argument("--max-height", type=int, default=None)
    probe.set_defaults(func=cmd_probe)

    fetch = sub.add_parser("fetch", help="download every catalogued video")
    fetch.add_argument("username")
    fetch.add_argument("--delay", type=float, default=3.0, help="seconds between videos (jittered)")
    fetch.add_argument("--max-height", type=int, default=None)
    fetch.add_argument("--retry-failed", action="store_true")
    fetch.set_defaults(func=cmd_fetch)

    push = sub.add_parser("push", help="upload downloaded videos to Google Drive")
    push.add_argument("username")
    push.add_argument("--drive-folder", default=None)
    push.add_argument("--delete-local", action="store_true", help="free disk as each file lands in Drive")
    push.set_defaults(func=cmd_push)

    run = sub.add_parser("run", help="scan, fetch and push in one go")
    run.add_argument("username")
    run.add_argument("--show", action="store_true")
    run.add_argument("--scroll-pause", type=float, default=1.6)
    run.add_argument("--delay", type=float, default=3.0)
    run.add_argument("--max-height", type=int, default=None)
    run.add_argument("--retry-failed", action="store_true")
    run.add_argument("--drive-folder", default=None)
    run.add_argument("--delete-local", action="store_true")
    run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        log("\nStopped. Progress is saved - rerun the same command to continue.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
