"""Browser integration test for tokgrab's profile scanner.

Serves a local page that lazy-loads /video/<id> links on scroll, the way a real
profile grid does, then runs the actual scan_profile against it. This checks
the browser launch, the scroll loop, link collection, dedupe, ordering and
end-of-feed detection without touching TikTok.

Skips itself if no browser is installed.

Run with:  python test_tokgrab_browser.py
"""

import http.server
import pathlib
import socketserver
import sys
import tempfile
import threading

import tokgrab

TOTAL = 57
PER_PAGE = 12
PORT = 8765

PAGE = """<html><body style="height:400vh">
<div id=grid></div>
<script>
let page=0;
const ids=[];
for(let i=0;i<%d;i++) ids.push("712345678901234"+String(i).padStart(4,"0"));
function load(){
  const s=page*%d, e=Math.min(s+%d, ids.length);
  if(s>=ids.length) return;
  let h="";
  for(let i=s;i<e;i++) h+='<a href="/@tester/video/'+ids[i]+'">v</a>';
  document.getElementById('grid').innerHTML += h;
  document.body.style.height = (400 + (page+1)*300) + "vh";
  page++;
}
load();
window.addEventListener('scroll', ()=>{ setTimeout(load, 50); });
</script></body></html>""" % (TOTAL, PER_PAGE, PER_PAGE)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(PAGE.encode())

    def log_message(self, *args):
        pass


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed")
        return 0

    # Probe using the same executable resolution _launch_context uses, so this
    # guard cannot skip a browser the real code would happily have driven.
    try:
        with sync_playwright() as p:
            kwargs = {"args": ["--no-sandbox"]}
            executable = tokgrab._chrome_executable()
            if executable:
                kwargs["executable_path"] = executable
            browser = p.chromium.launch(**kwargs)
            browser.close()
    except Exception as exc:  # noqa: BLE001 - no browser binary available
        print(f"SKIP: no usable browser ({type(exc).__name__})")
        return 0

    server = socketserver.TCPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    tokgrab.profile_url = lambda username: f"http://127.0.0.1:{PORT}/@{username}"
    tokgrab.PROFILE_SCROLL_IDLE_ROUNDS = 4

    with tempfile.TemporaryDirectory() as tmp:
        videos, _cookies = tokgrab.scan_profile(
            "tester",
            pathlib.Path(tmp) / "profile",
            headless=True,
            scroll_pause=0.35,
        )

    server.shutdown()

    assert len(videos) == TOTAL, f"expected {TOTAL} videos, collected {len(videos)}"
    assert all(v["id"].isdigit() for v in videos), "collected a non-numeric id"
    assert videos[0]["url"].endswith(videos[0]["id"]), "url does not match id"
    ids = [int(v["id"]) for v in videos]
    assert ids == sorted(ids, reverse=True), "inventory is not newest-first"

    print(f"browser scan collected all {TOTAL} lazy-loaded videos, deduped and ordered  PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
