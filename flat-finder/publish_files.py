#!/usr/bin/env python3
"""Print the files a refresh needs to publish, as JSON for the Artifact tool's `files` list.

Run after scrape.py and before committing: lists listings.json plus every photo that is new or
changed compared with the last commit. Paths are relative to web/, which is the publish root.
"""
import json
import subprocess
from pathlib import Path

WEB = Path(__file__).resolve().parent / "web"
out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all", "--", "img"],
                     cwd=WEB, capture_output=True, text=True, check=True).stdout
photos = sorted(line[3:].split("web/", 1)[-1] for line in out.splitlines()
                if line[:2].strip() in ("A", "M", "??") and line.endswith(".jpg"))
print(json.dumps([{"path": "listings.json"}] + [{"path": p} for p in photos]))
