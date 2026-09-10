"""Logic tests for tokgrab. No network, no browser, no credentials needed.

Run with:  python test_tokgrab.py
"""

import sys, tempfile, pathlib

from tokgrab import (normalize_username, choose_format, classify_format,
                     safe_name, State, _write_netscape_cookies, _normalize_item)

fails = []
def eq(label, got, want):
    if got != want: fails.append(f"{label}: got {got!r} want {want!r}")

# username normalisation
eq("url", normalize_username("https://www.tiktok.com/@snackshort_drama"), "snackshort_drama")
eq("at", normalize_username("@someone"), "someone")
eq("bare", normalize_username("someone"), "someone")
eq("trailing", normalize_username("https://www.tiktok.com/@a.b_c-d/video/123"), "a.b_c-d")

# Realistic TikTok format shapes as yt-dlp reports them.
FORMATS = [
  {"format_id":"download_addr-0","vcodec":"h264","acodec":"aac","width":576,"height":1024,"filesize":3_000_000,"url":"https://v16.tiktokcdn.com/x?watermark=1"},
  {"format_id":"play_addr-0","vcodec":"h264","acodec":"aac","width":576,"height":1024,"filesize":2_800_000,"url":"https://v16.tiktokcdn.com/y"},
  {"format_id":"bytevc1_720p_900k","vcodec":"bytevc1","acodec":"aac","width":720,"height":1280,"filesize":2_000_000,"url":"https://v16.tiktokcdn.com/z"},
  {"format_id":"h264_720p_1200k","vcodec":"h264","acodec":"aac","width":720,"height":1280,"filesize":2_600_000,"url":"https://v16.tiktokcdn.com/w"},
]

# 1. the marked download_addr must never be chosen
best = choose_format(FORMATS, None)
eq("clean pick", best["format_id"], "h264_720p_1200k")

# 2. the OLD selector's equality check would have let it through
old_would_exclude = [f for f in FORMATS if f["format_id"] != "download"]
eq("old check is a no-op", len(old_would_exclude), 4)
eq("new check excludes it", classify_format(FORMATS[0])["marked"], True)
eq("clean not flagged", classify_format(FORMATS[1])["marked"], False)

# 3. height cap respected
eq("cap 1024", choose_format(FORMATS, 1024)["format_id"], "play_addr-0")

# 4. refuse rather than serve a marked file
eq("only marked -> None", choose_format([FORMATS[0]], None), None)

# 5. video-less formats rejected
eq("audio only rejected", choose_format([{"format_id":"a","vcodec":"none","acodec":"aac"}], None), None)

# safe_name
eq("safe", safe_name("@we!rd / name"), "we_rd_name")
eq("safe empty", safe_name("", "fb"), "fb")

# item normalisation
row = _normalize_item({"id":"7123456789012345678","desc":"hi","author":{"uniqueId":"bob"},
                       "stats":{"playCount":5},"video":{"duration":12}}, "fallback")
eq("item user", row["username"], "bob")
eq("item url", row["url"], "https://www.tiktok.com/@bob/video/7123456789012345678")
eq("non numeric id", _normalize_item({"id":"abc"}, "x"), None)

# state round-trip + resumability
with tempfile.TemporaryDirectory() as d:
    p = pathlib.Path(d)/"state.json"
    s = State(p); s.entry("111")["path"] = "/tmp/a.mp4"; s.save()
    eq("state persists", State(p).videos["111"]["path"], "/tmp/a.mp4")
    # corrupt file must not crash
    p.write_text("{ broken")
    eq("corrupt tolerated", State(p).videos, {})

    c = pathlib.Path(d)/"cookies.txt"
    _write_netscape_cookies([{"domain":".tiktok.com","path":"/","secure":True,
                              "expires":-1,"name":"sessionid","value":"v"}], c)
    body = c.read_text()
    eq("netscape header", body.splitlines()[0], "# Netscape HTTP Cookie File")
    eq("cookie row", body.splitlines()[-1].split("\t")[5], "sessionid")
    eq("session-expiry given a future date", int(body.splitlines()[-1].split("\t")[4]) > 0, True)

print("\n".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
