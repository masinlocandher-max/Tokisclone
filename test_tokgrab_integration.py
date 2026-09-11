"""Integration tests for tokgrab with the network faked out.

Exercises the real fetch/push code paths - format choice, file naming,
metadata, resumability, error isolation and Drive dedupe - without touching
TikTok or Google. No credentials needed.

Run with:  python test_tokgrab_integration.py
"""

import sys, json, types, tempfile, pathlib, argparse
import tokgrab

FORMATS = [
  {"format_id":"download_addr-0","vcodec":"h264","acodec":"aac","width":576,"height":1024,"filesize":3_000_000,"url":"https://cdn/x?watermark=1"},
  {"format_id":"h264_720p_1200k","vcodec":"h264","acodec":"aac","width":720,"height":1280,"filesize":2_600_000,"url":"https://cdn/w"},
]
INFO = {"id":"7111","title":"clip","uploader_id":"tester","uploader":"Tester",
        "duration":9,"upload_date":"20260101","view_count":5,"like_count":2,
        "description":"d","formats":FORMATS}

calls = {}
class FakeYDL:
    def __init__(self, opts): self.opts = opts
    def __enter__(self): return self
    def __exit__(self,*a): return False
    def extract_info(self, url, download=False): return INFO
    def download(self, urls):
        calls["format"] = self.opts["format"]
        out = self.opts["outtmpl"].replace("%(ext)s","mp4")
        pathlib.Path(out).write_bytes(b"\x00"*2048)

tokgrab._ydl = lambda extra, cookies: FakeYDL({**extra})

with tempfile.TemporaryDirectory() as d:
    dest = pathlib.Path(d)/"media"; dest.mkdir()
    r = tokgrab.fetch_one("7111","https://tiktok/v/7111",dest,None,max_height=None)

    assert calls["format"] == "h264_720p_1200k", calls          # never the marked one
    assert r["height"] == 1280 and r["bytes"] == 2048
    assert pathlib.Path(r["path"]).name == "tester_7111.mp4"
    meta = json.loads(pathlib.Path(r["meta_path"]).read_text())
    assert meta["chosen_format"]["marked"] is False
    assert meta["id"] == "7111"
    print("fetch_one: picks clean format, names file, writes metadata  PASS")

    # refuses when only the marked rendition exists
    INFO["formats"] = [FORMATS[0]]
    try:
        tokgrab.fetch_one("7112","u",dest,None,max_height=None); raise SystemExit("should have refused")
    except RuntimeError as e:
        assert "no clean rendition" in str(e)
    print("fetch_one: refuses marked-only video                        PASS")
    INFO["formats"] = FORMATS

    # ---- cmd_fetch: resumability + per-video error isolation ----
    wd = pathlib.Path(d)/"wd"
    st = tokgrab.State(wd/"tester"/"state.json")
    for vid in ("7111","7222","7333"):
        st.entry(vid)["url"] = f"https://tiktok/v/{vid}"
    st.save()

    boom = {"n":0}
    real = tokgrab.fetch_one
    def flaky(vid,url,dest,cookies,*,max_height):
        if vid == "7222":
            boom["n"] += 1; raise RuntimeError("simulated network failure")
        return real(vid,url,dest,cookies,max_height=max_height)
    tokgrab.fetch_one = flaky

    args = argparse.Namespace(username="tester", workdir=str(wd), cookies=str(wd/"c.txt"),
                              delay=0, max_height=None, retry_failed=False)
    code = tokgrab.cmd_fetch(args)
    st2 = tokgrab.State(wd/"tester"/"state.json")
    assert code == 1, code
    assert st2.videos["7111"]["path"] and st2.videos["7333"]["path"]
    assert "simulated network failure" in st2.videos["7222"]["error"]
    print("cmd_fetch: one bad video does not kill the run              PASS")

    # rerun skips completed, skips the errored one unless asked
    code = tokgrab.cmd_fetch(args)
    assert boom["n"] == 1, f"errored video retried without --retry-failed ({boom['n']})"
    print("cmd_fetch: resumes, skips done, honours error gate          PASS")

    args.retry_failed = True
    tokgrab.cmd_fetch(args)
    assert boom["n"] == 2, boom
    print("cmd_fetch: --retry-failed retries it                        PASS")

    # ---- cmd_push against a fake Drive ----
    tokgrab.fetch_one = real
    uploaded = []
    class FakeDrive:
        def __init__(self, folder=None): self.folder = folder
        def creator_folder(self,p,c): return {"id":"F1","name":c}
        def find_video(self,p,vid): return {"id":"PRE"} if vid=="7333" else None
        def upload_file(self,path,*,parent_id,mime_type=None,properties=None):
            uploaded.append((pathlib.Path(path).name, properties["kind"]))
            return {"id":"D-"+properties["video_id"]}
    sys.modules["drive_storage"] = types.SimpleNamespace(DriveStorage=FakeDrive)

    pargs = argparse.Namespace(username="tester", workdir=str(wd), cookies=str(wd/"c.txt"),
                               drive_folder="F", delete_local=False)
    assert tokgrab.cmd_push(pargs) == 0
    st3 = tokgrab.State(wd/"tester"/"state.json")
    assert st3.videos["7111"]["drive_file_id"] == "D-7111"
    assert st3.videos["7333"]["drive_file_id"] == "PRE"        # dedupe against Drive
    names = [n for n,k in uploaded]
    assert "tester_7111.mp4" in names and "tester_7111.json" in names
    print("cmd_push: uploads video+metadata, skips what Drive has      PASS")

    before = len(uploaded)
    tokgrab.cmd_push(pargs)
    assert len(uploaded) == before, "re-uploaded on second push"
    print("cmd_push: second run uploads nothing                        PASS")

print("\nALL INTEGRATION CHECKS PASSED")
