#!/usr/bin/env python3
"""Inline README.md and docs/*.md into coworld_manifest_template.json so uploaded docs match the repo.

Usage: python tools/sync_docs.py [--check]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "coworld_manifest_template.json"

PAGES = [
    ("authoring.md", "Optional native model authoring", "docs/AUTHORING.md"),
    ("policy_guide.md", "Agent guide: competing in Softmax Marketing", "docs/POLICY_GUIDE.md"),
    ("judging.md", "How entries are judged", "docs/JUDGING.md"),
    ("league.md", "League setup and the daily feed workflow", "docs/LEAGUE.md"),
    ("video_guide.md", "How to make a video for a Softmax post", "docs/VIDEO_GUIDE.md"),
]
PROTOCOLS = {"player": "docs/player_protocol.md", "global": "docs/global_protocol.md"}


def build(template: dict) -> dict:
    game = template["game"]
    game["docs"]["readme"] = {"type": "text", "value": (ROOT / "README.md").read_text(encoding="utf-8")}
    game["docs"]["pages"] = [
        {"id": page_id, "title": title, "content": {"type": "text", "value": (ROOT / path).read_text(encoding="utf-8")}}
        for page_id, title, path in PAGES
    ]
    for key, path in PROTOCOLS.items():
        game["protocols"][key] = {"type": "text", "value": (ROOT / path).read_text(encoding="utf-8")}
    return template


def main() -> int:
    check = "--check" in sys.argv
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    updated = build(json.loads(json.dumps(template)))
    text = json.dumps(updated, indent=2, ensure_ascii=False) + "\n"
    current = TEMPLATE.read_text(encoding="utf-8")
    if check:
        if text != current:
            print("coworld_manifest_template.json is out of date; run tools/sync_docs.py", file=sys.stderr)
            return 1
        print("manifest docs in sync")
        return 0
    TEMPLATE.write_text(text, encoding="utf-8")
    print(f"synced {len(PAGES)} pages, README and {len(PROTOCOLS)} protocol docs into {TEMPLATE.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
