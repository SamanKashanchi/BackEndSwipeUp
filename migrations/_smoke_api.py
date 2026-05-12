"""Smoke-test the dashboard API endpoints the video-library page uses."""
import urllib.request, json, sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

ENDPOINTS = [
    "/api/niches",
    "/api/creators",
    "/api/videos?limit=2",
    "/api/videos/count",
    "/api/monitor-stats",
]

for ep in ENDPOINTS:
    url = "http://127.0.0.1:5001" + ep
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
            code = r.status
            preview = body[:200].decode("utf-8", errors="replace")
            print(f"[{code}] {ep}  {len(body)}B  preview={preview}")
    except urllib.error.HTTPError as e:
        body = e.read()[:600].decode("utf-8", errors="replace")
        print(f"[{e.code}] {ep}  ERROR  {body}")
    except Exception as e:
        print(f"[xxx] {ep}  EXC  {e}")
