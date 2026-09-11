# tokgrab — one link in, whole profile in your Drive

This is the short path. One command archives every public video on a TikTok
profile into your own Google Drive, clean rendition, resumable.

```bash
python tokgrab.py setup           # installs everything, walks both logins
python tokgrab.py run @username   # scan -> download -> upload to Drive
```

Run it **on your own computer, on your normal home internet.** That is not a
style preference. It is the whole reason this works and the older paths did not.

---

## Why the previous attempts kept failing

Three separate problems were stacked on top of each other. Each one alone is
enough to produce a run that finishes with zero videos.

### 1. Profile enumeration was running from a datacenter

The hard part of this job is not downloading a video. It is getting the *list*
of a profile's videos. TikTok's post-list endpoint is signed and is gated hard
against datacenter IP ranges — GitHub Actions runners, cloud VMs, serverless
functions. From those addresses the profile page commonly returns an empty list
or a verification wall, and no amount of retry logic changes that.

The repo already half-knew this — `README.md` notes that "consumer media sites
can behave differently on datacenter IPs." That warning is the actual finding.
`tiktok_guest.py`, `tiktok_embedded.py`, and `tiktok_browser.py` are three
attempts at the same wall.

**tokgrab's answer:** enumerate with a real Chrome profile you have logged into
once, scrolling the real page, on your own connection. Then hand the individual
video URLs to yt-dlp — single video pages are far more forgiving than profile
pages.

### 2. The format selector could not match a TikTok video

`media_core.video_format_selector()` builds this:

```
bv*[format_id!=download]+ba/b[format_id!=download]
```

`bv*+ba` asks for a separate video stream plus a separate audio stream. TikTok
serves **muxed** mp4 — one stream carrying both — and publishes no audio-only
format. So the first half of that selector can never match. With a height
filter added (`[height<=1080]`), any rendition where yt-dlp reports no height is
dropped from the fallback too, which is how a run ends with nothing downloaded
and no obvious error.

**tokgrab's answer:** list the real formats, score them in Python, and download
the chosen `format_id` directly. `tokgrab probe <url>` prints that table so you
can see it rather than trust it.

### 3. The clean-only guarantee was not actually being enforced

Both the selector above and `media_core._format_is_marked()` test
`format_id == "download"` — exact string equality. yt-dlp reports TikTok's
marked rendition with an id that *contains* `download` (commonly
`download_addr-0`), not one equal to it. An exact-match test excludes nothing,
so the product's headline promise was resting on a check that silently passes
everything.

**tokgrab's answer:** substring matching on `format_id`, `format_note`, and the
media URL, covered by `test_tokgrab.py`. If no clean rendition is on offer,
tokgrab **fails that video** instead of quietly saving the marked copy — the
same rule the README states, actually enforced.

> I have not changed `media_core.py`. Its selector is pinned by an assertion in
> `.github/workflows/ci.yml`, so fixing it properly means changing that
> assertion and re-testing the queue worker, the MCP server, and the REST API
> that all depend on it. That is a separate decision and it is yours to make.
> The finding above is what I would fix first if you want the old path repaired.

### One more thing worth saying plainly

I could not test any of this against live TikTok. The container I worked in
blocks outbound traffic to tiktok.com and has no yt-dlp installed, so
`test_tokgrab.py` covers the logic — format choice, watermark detection, state,
resumability — and nothing else. The browser and network behaviour is reasoned
from how the code and the platform work, not observed. **Your first real run is
the test.** Start with a small profile.

---

## Setup, once

```bash
python tokgrab.py setup
```

That installs the python packages and the browser, then walks you through both
logins. Two things it cannot do for you:

1. **The Google OAuth client.** Google Cloud Console -> create an OAuth
   **Desktop** client -> download the JSON as `client_secret.json` in this
   folder. `setup` will tell you if it is missing.
2. **Your Drive folder id.** Make a folder in your Drive, open it, copy the last
   part of the URL (`drive.google.com/drive/folders/THIS_PART`), then
   `cp .env.example .env` and set `GOOGLE_DRIVE_ROOT_FOLDER_ID` to it.

Use **your own** Google account, not a service account. Service accounts have no
storage quota of their own and files they upload are owned by the robot, not by
you - that is how you end up with an archive you cannot reach.

Rerun `python tokgrab.py login` if downloads start failing after a few weeks.
TikTok sessions expire.

## Commands

| Command | What it does |
|---|---|
| `setup` | Installs dependencies and walks the Drive + TikTok logins |
| `doctor` | Checks python, yt-dlp, playwright, ffmpeg, cookies, Drive credentials |
| `login` | Opens real Chrome, saves your TikTok session |
| `scan @user` | Scrolls the profile to the end, writes `inventory.json` |
| `probe <video-url>` | Prints the real format table and which one tokgrab would take |
| `fetch @user` | Downloads everything catalogued, resumable |
| `push @user` | Uploads to Drive, skipping anything already there |
| `run @user` | scan → fetch → push |

Useful flags:

```bash
python tokgrab.py scan @user --show          # watch the browser work
python tokgrab.py fetch @user --delay 6      # slower, gentler on rate limits
python tokgrab.py fetch @user --retry-failed # retry only what failed
python tokgrab.py push @user --delete-local  # free disk as each file lands
```

Everything is resumable. Progress lives in
`~/.tokisclone/tokgrab/<username>/state.json`. Ctrl-C is safe — rerun the same
command and it continues. A run that dies at video 300 of 500 resumes at 301.

---

## When something goes wrong

**`scan` finds 0 videos.** Run `--show` and watch. If you see a login or
verification page, finish it by hand in that window — the session persists.
If the page loads fine but nothing is collected, TikTok changed the post-list
endpoint; the DOM regex backstop should still catch ids, so check whether the
warning about the post-list API appeared.

**`scan` finds ~30 and stops.** The scroll ended early. Raise `--scroll-pause`
to 3 or 4 — slow connections need longer for each page of posts to render.

**Every video fails with "no clean rendition offered."** Run `probe` on one of
them and read the table. If every row is flagged, TikTok is only offering the
marked file to your session — usually a sign of being logged out or rate
limited. tokgrab refusing here is correct behaviour, not a bug.

**Downloads start failing partway through a big profile.** You are being rate
limited. Stop, wait an hour, rerun with `--delay 8`. Progress is saved.

**Drive uploads fail with a quota error.** Your Drive is full, or you pointed at
a folder you do not own.

---

## If the videos are your own

TikTok's data export (Settings -> Account -> Download your data) hands you your
posted videos officially, with no scraping. I could not verify from here whether
that export is watermark-free - request one and check a single file before
building on it. `TIKTOK_AUTHORIZED_DOWNLOAD.md` stubs the Data Portability API
for the same purpose; note its documented EEA/UK scope limit.

## What tokgrab does with watermarks

It selects a rendition TikTok itself serves without a mark. It does not blur,
crop, paint over, or erase a burned-in watermark, because those produce a
visibly degraded file rather than a clean master.
