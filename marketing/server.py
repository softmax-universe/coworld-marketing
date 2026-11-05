"""Softmax Marketing game container.

Game-hosted Coworld: each seat's player file is an X post (a zip with entry.json and up to four images, one video
or one gif; a bare media file; or bare text). The game validates and measures every attachment, runs the autograder
(technical panel + model craft panel), reads engagement from the feed (X metrics for posts that went out on the
company account, ship votes from the room for the rest), blends the two halves into the score, publishes the jury
to the global viewer, writes per-seat logs and artifacts, then the replay (the jury page data) and results.

Media is addressed by seat and index everywhere (`/media/{slot}/{index}`, `replay.media[slot][index]`), because a
post is one to four files; `/media/{slot}` still serves index 0 for the single-file viewers.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Response, WebSocket
from fastapi.responses import HTMLResponse

from marketing import __version__
from marketing.authoring import materialize_author_package
from marketing.config import GameConfig, MediaKind
from marketing.engagement import Feed, PostRecord, blend, load_feed, seat_engagement
from marketing.entry import Entry, EntryMeta, MediaItem, MediaMeta, load_entry, weighted_length
from marketing.io import artifact_method, decode_replay_bytes, read_data, uri_to_path, write_data
from marketing.judge import (
    RUBRIC_VERSION,
    CraftReview,
    JudgeImagery,
    ModelClient,
    TechnicalReview,
    cache_record,
    craft_review,
    from_cache,
    judge_score,
    technical_review,
)
from marketing.probe import Measurement, contact_sheet, frame_at, measure, still_image

logger = logging.getLogger("marketing.game")
STATIC_DIR = Path(__file__).parent / "static"
REPLAY_VERSION = 3
GAME_NAME = "marketing"
MEDIA_TYPES = {"video": "video/mp4", "image": "image/jpeg", "gif": "image/gif", "none": "application/octet-stream"}
SUFFIX_TYPES = {
    ".png": "image/png",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}


def mime_for(item: MediaItem) -> str:
    """The content type the browser is told: by suffix when the package names one, by sniffed kind otherwise."""
    return SUFFIX_TYPES.get(item.path.suffix.lower()) or MEDIA_TYPES[item.kind]


@dataclass
class Seat:
    slot: int
    name: str
    file_uri: str = ""
    log_uri: str = ""
    artifact_uri: str = ""
    content_hash: str = ""
    size_bytes: int = 0
    status: str = "waiting"  # waiting | intake | judging | judged | failed
    entry: Entry | None = None
    measurements: list[Measurement] = field(default_factory=list)  # one per attachment, in entry order
    technical: TechnicalReview | None = None
    craft: CraftReview | None = None
    craft_unavailable: bool = False
    judge: float = 0.0
    engagement: float = 0.0
    post: PostRecord | None = None
    room_ships: int = 0
    score: float = 0.0
    posters_b64: list[str] = field(default_factory=list)  # one JPEG per attachment: the still, or the video's poster
    sheet_b64: str = ""
    end_b64: str = ""
    log_lines: list[str] = field(default_factory=list)
    workdir: Path | None = None
    authoring: dict[str, Any] = field(default_factory=dict)

    def log(self, message: str) -> None:
        self.log_lines.append(f"{time.strftime('%H:%M:%S')} {message}")

    @property
    def media(self) -> list[MediaItem]:
        return self.entry.media if self.entry else []

    @property
    def media_kind(self) -> MediaKind:
        return self.entry.media_kind if self.entry else "none"

    @property
    def media_mime(self) -> str:
        return mime_for(self.media[0]) if self.media else ""

    @property
    def poster_b64(self) -> str:
        return self.posters_b64[0] if self.posters_b64 else ""

    @property
    def measurement(self) -> Measurement | None:
        return self.measurements[0] if self.measurements else None

    @property
    def eligible(self) -> bool:
        return bool(self.technical and self.technical.eligible)

    def media_public(self) -> list[dict[str, Any]]:
        """The attachments as the viewers see them: index, kind, alt text, URL, poster and dimensions."""
        out: list[dict[str, Any]] = []
        for index, item in enumerate(self.media):
            m = self.measurements[index] if index < len(self.measurements) else None
            out.append(
                {
                    "index": index,
                    "kind": item.kind,
                    "alt_text": item.alt_text,
                    "url": f"/media/{self.slot}/{index}",
                    "path": item.display_name,
                    "mime": mime_for(item),
                    "poster": self.posters_b64[index] if index < len(self.posters_b64) else "",
                    "width": m.width if m and m.ok else None,
                    "height": m.height if m and m.ok else None,
                    "duration": round(m.duration, 2) if m and m.ok else None,
                }
            )
        return out

    def media_summary(self) -> list[dict[str, Any]]:
        """The attachments as results.json records them: path, kind, alt text."""
        return [{"path": item.display_name, "kind": item.kind, "alt_text": item.alt_text} for item in self.media]

    def public(self, *, include_notes: bool) -> dict[str, Any]:
        meta = self.entry.meta if self.entry else EntryMeta(title=self.name)
        m = self.measurement
        data: dict[str, Any] = {
            "slot": self.slot,
            "name": self.name,
            "status": self.status,
            "label": meta.label,
            "text": meta.text,
            "weighted_length": weighted_length(meta.text),
            "alt_text": meta.alt_text,
            "thesis": meta.thesis,
            "credits": meta.credits,
            "made_with": meta.made_with,
            "kind": self.entry.kind if self.entry else "",
            "media_kind": self.media_kind,
            "media_mime": self.media_mime,
            "media": self.media_public(),
            "duration": round(m.duration, 2) if m else None,
            "width": m.width if m else None,
            "height": m.height if m else None,
            "size_bytes": self.size_bytes,
            "content_hash": self.content_hash,
            "poster": self.poster_b64,
            "score": round(self.score, 2),
            "judge": round(self.judge, 2),
            "engagement": round(self.engagement, 2),
            "posted": self.post is not None,
            "post": self.post.to_dict() if self.post else None,
            "room_ships": self.room_ships,
            "eligible": self.eligible,
        }
        if include_notes:
            data["notes"] = meta.notes
            data["technical"] = self.technical.to_dict() if self.technical else None
            data["craft"] = self.craft.to_dict() if self.craft else None
            data["craft_unavailable"] = self.craft_unavailable
            data["problems"] = list(self.entry.problems) if self.entry else []
            data["measurement"] = m.to_dict() if m else None
            data["measurements"] = [x.to_dict() for x in self.measurements]
            data["sheet"] = self.sheet_b64
            data["end"] = self.end_b64
        return data


class Runtime:
    def __init__(
        self,
        config: GameConfig | None,
        *,
        seats_doc: dict[str, Any] | None = None,
        replay: dict[str, Any] | None = None,
    ):
        self.config = config
        self.replay = replay
        self.replay_mode = replay is not None
        self.seats: list[Seat] = []
        self.seats_doc = seats_doc or {}
        self.player_status_uri = self.seats_doc.get("player_status_uri", "")
        self.phase = "replay" if self.replay_mode else "waiting"
        self.started = False
        self.done = self.replay_mode
        self.results: dict[str, Any] | None = replay.get("results") if replay else None
        self.feed: Feed = Feed(available=False)
        self.events: list[dict[str, Any]] = []
        self.global_viewers: set[WebSocket] = set()
        self.player_sockets: dict[int, set[WebSocket]] = {}
        self._task: asyncio.Task[None] | None = None
        self.tmp = Path(tempfile.mkdtemp(prefix="mkt-"))
        self.media: dict[int, list[bytes]] = {}  # per slot, the attachments' bytes in entry order
        self.on_complete: Any = None
        self.finished_at: float | None = None
        if config is not None:
            for slot, player in enumerate(config.players):
                self.seats.append(Seat(slot=slot, name=player.name))
            for seat_doc in self.seats_doc.get("seats", []):
                slot = int(seat_doc["slot"])
                if 0 <= slot < len(self.seats):
                    seat = self.seats[slot]
                    seat.file_uri = seat_doc.get("file_uri", "")
                    seat.log_uri = seat_doc.get("log_uri", "")
                    seat.artifact_uri = seat_doc.get("artifact_uri", "")
                    seat.content_hash = seat_doc.get("content_hash", "")
                    seat.size_bytes = int(seat_doc.get("size_bytes", 0))

    # ------------------------------------------------------------------ lifecycle

    async def startup(self) -> None:
        if self.replay_mode or self.config is None:
            return
        self.started = True
        self._task = asyncio.create_task(self._guarded_episode())

    async def shutdown(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _guarded_episode(self) -> None:
        try:
            await self.run_episode()
        except Exception:  # noqa: BLE001
            logger.exception("episode failed")
            raise

    # ------------------------------------------------------------------ episode

    async def run_episode(self) -> None:
        assert self.config is not None
        cfg = self.config
        self.phase = "intake"
        await self.publish({"type": "phase", "phase": self.phase})
        self.feed = await asyncio.to_thread(load_feed, cfg.engagement)
        await self.publish(
            {
                "type": "feed",
                "available": self.feed.available,
                "generated_at": self.feed.generated_at,
                "posts": len(self.feed.posts),
                "error": self.feed.error,
            }
        )
        for seat in self.seats:
            await asyncio.to_thread(self._intake, seat)
            await self.publish({"type": "seat", "seat": seat.public(include_notes=False)})

        self.phase = "judging"
        await self.publish({"type": "phase", "phase": self.phase})
        client = ModelClient(cfg.judge) if cfg.judge.mode == "panel" else None
        if cfg.judge.mode == "panel" and client is not None and not client.available:
            logger.warning("judge mode is panel but no model endpoint is configured; craft panel unavailable")
        judged_by_hash: dict[str, Seat] = {}
        for seat in self.seats:
            seat.status = "judging"
            await self.publish({"type": "seat", "seat": seat.public(include_notes=False)})
            twin = judged_by_hash.get(seat.content_hash) if seat.content_hash else None
            if twin is not None and twin.technical is not None:
                seat.technical, seat.craft, seat.craft_unavailable = twin.technical, twin.craft, twin.craft_unavailable
                seat.log("identical bytes to an earlier seat; judgement copied")
            else:
                await asyncio.to_thread(self._judge, seat, client)
                if seat.content_hash:
                    judged_by_hash[seat.content_hash] = seat
            self._score(seat)
            await self.publish({"type": "seat", "seat": seat.public(include_notes=True)})

        self.phase = "complete"
        self.results = self._results()
        await self.publish({"type": "final", "results": self.results})
        await asyncio.to_thread(self._write_outputs)
        self.done = True
        self.finished_at = time.monotonic()
        await self._notify_players_final()
        logger.info("episode complete scores=%s pick=%s", self.results["scores"], self.results["pick"])
        if cfg.linger_seconds:
            await asyncio.sleep(cfg.linger_seconds)
        if self.on_complete is not None:
            outcome = self.on_complete()
            if outcome is not None:
                await outcome

    def _intake(self, seat: Seat) -> None:
        assert self.config is not None
        seat.status = "intake"
        seat.workdir = self.tmp / f"seat{seat.slot}"
        seat.workdir.mkdir(parents=True, exist_ok=True)
        path = uri_to_path(seat.file_uri) if seat.file_uri else None
        if path is None and seat.file_uri:
            staged = seat.workdir / "file"
            try:
                staged.write_bytes(read_data(seat.file_uri))
                path = staged
            except Exception as error:  # noqa: BLE001
                seat.log(f"could not fetch player file: {error}")
                path = None
        if path is None or not path.exists():
            problem = "fatal: no player file staged for this seat"
            seat.entry = Entry(EntryMeta(title=seat.name), [], "invalid", [problem])
            seat.log(seat.entry.problems[0])
            return
        if not seat.content_hash:
            seat.content_hash = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        if not seat.size_bytes:
            seat.size_bytes = path.stat().st_size
        try:
            authored = materialize_author_package(
                path,
                seat.workdir,
                slot=seat.slot,
                grader_config=self.config.judge,
                capture=seat.authoring,
                expected_input_digest=seat.content_hash,
                maximum_input_bytes=self.config.limits.max_file_bytes,
            )
            if authored is not None:
                path = authored.package
                seat.content_hash = "sha256:" + authored.capture["graded_package_sha256"]
                seat.size_bytes = path.stat().st_size
        except Exception as error:
            seat.authoring.update(status="failed", error_type=type(error).__name__, error=str(error))
            seat.entry = Entry(EntryMeta(title=seat.name), [], "invalid", ["fatal: native authoring failed"])
            seat.log("native authoring failed; details retained in the private author capture")
            return
        seat.entry = load_entry(path, seat.workdir, self.config.limits, default_title=seat.name)
        for problem in seat.entry.problems:
            seat.log(problem)
        seat.post = self.feed.post_for(seat.content_hash)
        seat.room_ships = self.feed.ships_for(seat.content_hash)
        if seat.post is not None:
            seat.log(
                f"posted on @{self.feed.account or self.config.account} as {seat.post.tweet_id}: "
                f"{json.dumps(seat.post.metrics)}"
            )
        elif seat.room_ships:
            seat.log(f"room: {seat.room_ships} ships on the linked forum post")
        if not seat.entry.media:
            return
        blobs: list[bytes] = []
        for index, item in enumerate(seat.entry.media):
            m = measure(item.path, item.kind)
            seat.measurements.append(m)
            label = f"media {index + 1}/{len(seat.entry.media)} ({item.display_name})"
            seat.log(f"measurement {label}: {json.dumps(m.to_dict())}")
            seat.posters_b64.append("")
            blob = b""
            with suppress(OSError):
                blob = item.path.read_bytes()
            blobs.append(blob)
            if not m.ok:
                continue
            if item.kind == "video":
                poster = seat.workdir / "poster.jpg"
                if frame_at(item.path, min(m.duration * 0.4, max(m.duration - 0.5, 0)), poster):
                    seat.posters_b64[index] = base64.b64encode(poster.read_bytes()).decode()
                sheet = seat.workdir / "sheet.jpg"
                if contact_sheet(item.path, m.duration, self.config.judge.frames, sheet):
                    seat.sheet_b64 = base64.b64encode(sheet.read_bytes()).decode()
                end = seat.workdir / "end.jpg"
                if frame_at(item.path, max(m.duration - 1.0, 0), end):
                    seat.end_b64 = base64.b64encode(end.read_bytes()).decode()
            else:
                still = seat.workdir / f"still{index}.jpg"
                if still_image(item.path, still):
                    seat.posters_b64[index] = base64.b64encode(still.read_bytes()).decode()
        self.media[seat.slot] = blobs

    def _judge(self, seat: Seat, client: ModelClient | None) -> None:
        assert self.config is not None
        cfg = self.config
        entry = seat.entry or Entry(EntryMeta(title=seat.name), [], "invalid", ["fatal: no entry"])
        model = client.model if client is not None else ""
        if cfg.judge.use_cache and client is not None and seat.content_hash in self.feed.judge_cache:
            cached = from_cache(self.feed.judge_cache[seat.content_hash], model)
            if cached is not None:
                seat.technical, seat.craft = cached
                seat.log(f"judgement reused from the feed's judge cache (rubric {RUBRIC_VERSION}, {model})")
                return
            seat.log("judge cache entry ignored: different rubric or model")
        seat.technical = technical_review(entry, seat.measurements, cfg.limits)
        for check in seat.technical.failures:
            seat.log(f"technical: {check.id} failed: {check.detail}")
        if not seat.technical.eligible:
            seat.log("ineligible: a hard gate failed")
            return
        if client is None:
            return
        workdir = seat.workdir or self.tmp
        stills: list[bytes] = []
        if entry.media_kind in {"image", "gif"}:
            for index in range(len(entry.media)):
                still = workdir / f"still{index}.jpg"
                if still.exists():
                    stills.append(still.read_bytes())
        imagery = JudgeImagery(
            stills=stills,
            sheet=(workdir / "sheet.jpg") if (workdir / "sheet.jpg").exists() else None,
            end=(workdir / "end.jpg") if (workdir / "end.jpg").exists() else None,
        )
        rendered = imagery.sheet is not None if entry.media_kind == "video" else len(stills) == len(entry.media)
        if entry.media and not rendered:
            seat.craft_unavailable = True
            seat.log("craft panel skipped: media could not be rendered for the judge")
            return
        seat.craft = craft_review(client, entry, seat.measurements, imagery, cfg.brief, cfg.account, slot=seat.slot)
        if seat.craft is None:
            seat.craft_unavailable = client.available
            seat.log("craft panel unavailable; judged on the technical panel only")
        else:
            seat.log(f"craft: {json.dumps(seat.craft.to_dict())}")

    def _score(self, seat: Seat) -> None:
        assert self.config is not None
        cfg = self.config
        seat.judge = judge_score(seat.technical, seat.craft, cfg.judge) if seat.technical else 0.0
        seat.engagement = seat_engagement(seat.post.metrics if seat.post else None, seat.room_ships, cfg.engagement)
        seat.score = blend(seat.judge, seat.engagement, cfg.engagement) if seat.eligible else 0.0
        seat.status = "judged" if seat.eligible else "failed"
        source = "X metrics" if seat.post else f"room, {seat.room_ships} ships"
        seat.log(
            f"score {seat.score:.1f} = judge {seat.judge:.1f} "
            f"(technical {seat.technical.score if seat.technical else 0:.0f}, "
            f"craft {seat.craft.score if seat.craft else 'n/a'}) blended with engagement {seat.engagement:.1f} "
            f"({source})"
        )

    # ------------------------------------------------------------------ outputs

    def _results(self) -> dict[str, Any]:
        assert self.config is not None
        cfg = self.config
        eligible = [s.eligible for s in self.seats]
        scores = [round(s.score, 2) for s in self.seats]
        judge = [round(s.judge, 2) for s in self.seats]
        postable = [s.eligible and s.post is None and s.judge >= cfg.judge.postable_threshold for s in self.seats]
        candidates = [s.slot for s, p in zip(self.seats, postable, strict=True) if p]
        pick = max(candidates, key=lambda i: judge[i]) if candidates else None
        craft_values = [None if s.craft is None else round(s.craft.score, 1) for s in self.seats]
        if cfg.judge.mode == "technical":
            judge_mode = "technical"
        elif all(c is None for c in craft_values):
            judge_mode = "technical-fallback"
        else:
            judge_mode = "panel"
        return {
            "scores": scores,
            "eligible": eligible,
            "judge": judge,
            "technical": [round(s.technical.score, 1) if s.technical else 0.0 for s in self.seats],
            "craft": craft_values,
            "engagement": [round(s.engagement, 2) for s in self.seats],
            "posted": [s.post is not None for s in self.seats],
            "tweet_ids": [s.post.tweet_id if s.post else "" for s in self.seats],
            "postable": postable,
            "player_names": [s.name for s in self.seats],
            "labels": [(s.entry.meta.label if s.entry else s.name) for s in self.seats],
            "texts": [(s.entry.meta.text if s.entry else "") for s in self.seats],
            "media_kinds": [s.media_kind for s in self.seats],
            "media": [s.media_summary() for s in self.seats],
            "room_ships": [s.room_ships for s in self.seats],
            "content_hashes": [s.content_hash for s in self.seats],
            "verdicts": [(s.craft.verdict if s.craft else self._technical_verdict(s)) for s in self.seats],
            "judge_records": [
                cache_record(s.technical, s.craft, cfg.judge.model) if s.technical else None for s in self.seats
            ],
            "judge_mode": judge_mode,
            "judge_model": cfg.judge.model if judge_mode == "panel" else "",
            "engagement_feed": {
                "available": self.feed.available,
                "generated_at": self.feed.generated_at,
                "posts": len(self.feed.posts),
                "error": self.feed.error,
            },
            "pick": pick,
            "top_score": max(scores) if scores else 0.0,
            "brief": cfg.brief,
            "account": cfg.account,
        }

    @staticmethod
    def _technical_verdict(seat: Seat) -> str:
        if seat.technical is None:
            return "Not judged."
        failures = seat.technical.failures
        if not seat.technical.eligible:
            return "Ineligible: " + "; ".join(c.detail for c in failures if c.gate)[:240]
        if not failures:
            return "Technical panel: every check passed."
        return ("Technical panel: " + "; ".join(f"{c.id} ({c.detail})" for c in failures))[:280]

    def replay_payload(self, *, include_media: bool) -> dict[str, Any]:
        assert self.config is not None
        payload: dict[str, Any] = {
            "version": REPLAY_VERSION,
            "game": GAME_NAME,
            "game_version": __version__,
            "config": self.config.model_dump(exclude={"tokens"}),
            "brief": self.config.brief,
            "account": self.config.account,
            "feed": {
                "available": self.feed.available,
                "generated_at": self.feed.generated_at,
                "account": self.feed.account,
                "posts": len(self.feed.posts),
                "error": self.feed.error,
            },
            "entries": [s.public(include_notes=True) for s in self.seats],
            "results": self.results,
            "events": [e for e in self.events if e.get("type") != "seat"],
            "media": {},
        }
        if include_media and self.results is not None:
            # A seat's attachments travel together or not at all, so a grid is never half-embedded.
            budget = self.config.replay_media_budget_bytes
            order = sorted(self.seats, key=lambda s: (-s.score, s.slot))
            used = 0
            for seat in order:
                blobs = self.media.get(seat.slot)
                if not blobs or used + sum(len(b) for b in blobs) > budget:
                    continue
                payload["media"][str(seat.slot)] = [base64.b64encode(b).decode() for b in blobs]
                used += sum(len(b) for b in blobs)
        return payload

    def _write_outputs(self) -> None:
        assert self.config is not None and self.results is not None
        for seat in self.seats:
            if seat.log_uri:
                text = "\n".join(seat.log_lines) + "\n"
                if seat.authoring:
                    text += "MARKETING_NATIVE_AUTHOR_CAPTURE " + json.dumps(seat.authoring, ensure_ascii=False) + "\n"
                with suppress(Exception):
                    write_data(seat.log_uri, text, content_type="text/plain")
            if seat.artifact_uri and seat.entry is not None:
                with suppress(Exception):
                    self._write_artifact(seat)
        captures = [seat.authoring for seat in self.seats if seat.authoring]
        trajectory_uri = os.environ.get("COGAME_SAVE_TRAJECTORY_URI")
        if captures and trajectory_uri:
            private = {
                "schema": "marketing-native-authoring-episode/1",
                "game": "marketing",
                "game_version": __version__,
                "runtime_episode_id": os.environ.get("COWORLD_EPISODE_ID") or os.environ.get("COGAME_EPISODE_ID"),
                "source_revision": os.environ.get("COWORLD_SOURCE_REVISION"),
                "authors": captures,
                "results": self.results,
                "provenance": "Native captures; authenticate engine, job and participant through service receipts",
            }
            write_data(
                trajectory_uri,
                json.dumps(private, ensure_ascii=False) + "\n",
                content_type="application/x-ndjson",
                http_method=artifact_method("COGAME_SAVE_TRAJECTORY_METHOD"),
            )
        if self.player_status_uri:
            status = {
                "schema_version": "1",
                "players": [
                    {
                        "slot": s.slot,
                        "state": "exited",
                        "exit_code": 0 if s.eligible else 1,
                        "reason": "Judged" if s.eligible else "Ineligible entry",
                        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                    for s in self.seats
                ],
            }
            with suppress(Exception):
                write_data(self.player_status_uri, json.dumps(status), content_type="application/json")
        replay_uri = os.environ.get("COGAME_SAVE_REPLAY_URI")
        if replay_uri:
            write_data(
                replay_uri,
                json.dumps(self.replay_payload(include_media=True), separators=(",", ":")),
                content_type="application/json",
                http_method=artifact_method("COGAME_SAVE_REPLAY_METHOD"),
            )
        results_uri = os.environ.get("COGAME_RESULTS_URI")
        if results_uri:
            write_data(
                results_uri,
                json.dumps(self.results, separators=(",", ":")),
                content_type="application/json",
                http_method=artifact_method("COGAME_RESULTS_METHOD"),
            )

    def _write_artifact(self, seat: Seat) -> None:
        assert seat.entry is not None
        path = self.tmp / f"artifact{seat.slot}.zip"
        # The artifact's entry.json names the attachments exactly as the archive carries them, so it reloads as a
        # current-schema package whatever shape the entrant uploaded.
        meta = seat.entry.meta.model_copy(
            update={"media": [MediaMeta(path=item.display_name, alt_text=item.alt_text) for item in seat.entry.media]}
        )
        blobs = self.media.get(seat.slot, [])
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("entry.json", meta.model_dump_json(by_alias=True, indent=2))
            archive.writestr("judge.json", json.dumps(seat.public(include_notes=True), indent=2))
            for item, blob in zip(seat.entry.media, blobs, strict=False):
                if blob:
                    archive.writestr(item.display_name, blob)
        write_data(seat.artifact_uri, path.read_bytes(), content_type="application/zip")

    # ------------------------------------------------------------------ live surfaces

    async def publish(self, event: dict[str, Any]) -> None:
        event = {**event, "t": round(time.time(), 3)}
        self.events.append(event)
        stale: list[WebSocket] = []
        for viewer in list(self.global_viewers):
            try:
                await viewer.send_json(event)
            except Exception:  # noqa: BLE001
                stale.append(viewer)
        for viewer in stale:
            self.global_viewers.discard(viewer)
        if event["type"] == "seat":
            slot = event["seat"]["slot"]
            for socket in list(self.player_sockets.get(slot, set())):
                with suppress(Exception):
                    await socket.send_json({"type": "observation", **event["seat"]})

    def snapshot(self, *, include_notes: bool) -> dict[str, Any]:
        return {
            "type": "snapshot",
            "phase": self.phase,
            "started": self.started,
            "done": self.done,
            "brief": self.config.brief if self.config else (self.replay or {}).get("brief", ""),
            "account": self.config.account if self.config else (self.replay or {}).get("account", ""),
            "feed": {
                "available": self.feed.available,
                "generated_at": self.feed.generated_at,
                "posts": len(self.feed.posts),
            },
            "entries": [s.public(include_notes=include_notes) for s in self.seats],
            "results": self.results,
        }

    async def connect_global(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.global_viewers.add(websocket)
        try:
            await websocket.send_json(self.snapshot(include_notes=self.done))
            async for _ in websocket.iter_text():
                pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.global_viewers.discard(websocket)

    def valid_token(self, slot: int, token: str) -> bool:
        return self.config is not None and 0 <= slot < len(self.config.tokens) and self.config.tokens[slot] == token

    async def connect_player(self, slot: int, token: str, websocket: WebSocket) -> None:
        if not self.valid_token(slot, token):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        self.player_sockets.setdefault(slot, set()).add(websocket)
        try:
            seat = self.seats[slot]
            await websocket.send_json({"type": "observation", **seat.public(include_notes=self.done)})
            if self.done:
                await websocket.send_json({"type": "final", "slot": slot, "score": seat.score, "results": self.results})
            async for _ in websocket.iter_text():
                pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.player_sockets.get(slot, set()).discard(websocket)

    async def _notify_players_final(self) -> None:
        for slot, sockets in self.player_sockets.items():
            for socket in list(sockets):
                with suppress(Exception):
                    await socket.send_json(
                        {"type": "final", "slot": slot, "score": self.seats[slot].score, "results": self.results}
                    )

    async def connect_replay(self, websocket: WebSocket) -> None:
        await websocket.accept()
        payload = self.replay if self.replay is not None else self.replay_payload(include_media=False)
        light = {k: v for k, v in payload.items() if k != "media"}
        await websocket.send_json({"type": "replay", **light})
        with suppress(Exception):
            async for _ in websocket.iter_text():
                await websocket.send_json({"type": "replay", **light})

    def replay_json(self) -> bytes:
        payload = self.replay if self.replay is not None else self.replay_payload(include_media=True)
        return json.dumps(payload, separators=(",", ":")).encode()

    def media_bytes(self, slot: int, index: int = 0) -> tuple[bytes, str] | None:
        if index < 0:
            return None
        if self.replay is not None:
            encoded = (self.replay.get("media") or {}).get(str(slot))
            if isinstance(encoded, str):  # replay version 2: one attachment per seat
                encoded = [encoded]
            if not encoded or index >= len(encoded):
                return None
            entry = next((e for e in (self.replay.get("entries") or []) if e.get("slot") == slot), {})
            items = entry.get("media") or []
            mime = (items[index].get("mime") if index < len(items) else "") or entry.get("media_mime") or ""
            return base64.b64decode(encoded[index]), (mime or "application/octet-stream")
        blobs = self.media.get(slot) or []
        if index >= len(blobs) or not blobs[index]:
            return None
        items = self.seats[slot].media
        mime = mime_for(items[index]) if index < len(items) else "application/octet-stream"
        return blobs[index], mime


def runtime_from_environment() -> Runtime:
    replay_uri = os.environ.get("COGAME_LOAD_REPLAY_URI")
    if replay_uri:
        return Runtime(None, replay=decode_replay_bytes(read_data(replay_uri)))
    config_uri = os.environ.get("COGAME_CONFIG_URI")
    if not config_uri:
        raise RuntimeError("COGAME_CONFIG_URI is required outside replay mode")
    config = GameConfig.model_validate_json(read_data(config_uri))
    seats_uri = os.environ.get("COGAME_PLAYER_SEATS_URI")
    seats_doc = json.loads(read_data(seats_uri)) if seats_uri else None
    if seats_doc is None:
        logger.warning("COGAME_PLAYER_SEATS_URI not set: every seat will be judged as missing")
    return Runtime(config, seats_doc=seats_doc)


def create_app(runtime: Runtime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await runtime.startup()
        yield
        await runtime.shutdown()

    app = FastAPI(title="Softmax Marketing", lifespan=lifespan)
    jury_html = (STATIC_DIR / "jury.html").read_text(encoding="utf-8")
    player_html = (STATIC_DIR / "player.html").read_text(encoding="utf-8")

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "mode": "replay" if runtime.replay_mode else "episode", "phase": runtime.phase}

    @app.get("/client/player")
    def player_client(slot: int = -1, token: str = "") -> HTMLResponse:
        if not runtime.valid_token(slot, token):
            raise HTTPException(status_code=403, detail="invalid slot or token")
        return HTMLResponse(player_html)

    @app.get("/client/global")
    def global_client() -> HTMLResponse:
        return HTMLResponse(jury_html)

    @app.get("/client/replay")
    def replay_client() -> HTMLResponse:
        return HTMLResponse(jury_html)

    @app.get("/replay.json")
    def replay_json() -> Response:
        return Response(runtime.replay_json(), media_type="application/json")

    @app.get("/media/{slot}")
    def media_first(slot: int) -> Response:
        return media(slot, 0)

    @app.get("/media/{slot}/{index}")
    def media(slot: int, index: int) -> Response:
        found = runtime.media_bytes(slot, index)
        if found is None:
            raise HTTPException(status_code=404, detail="no media at this index for this seat")
        blob, mime = found
        return Response(blob, media_type=mime)

    @app.websocket("/player")
    async def player_socket(websocket: WebSocket) -> None:
        try:
            slot = int(websocket.query_params.get("slot", "-1"))
        except ValueError:
            slot = -1
        await runtime.connect_player(slot, websocket.query_params.get("token", ""), websocket)

    @app.websocket("/global")
    async def global_socket(websocket: WebSocket) -> None:
        await runtime.connect_global(websocket)

    @app.websocket("/replay")
    async def replay_socket(websocket: WebSocket) -> None:
        await runtime.connect_replay(websocket)

    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runtime = runtime_from_environment()
    app = create_app(runtime)
    host = os.environ.get("COGAME_HOST", "0.0.0.0")
    port = int(os.environ.get("COGAME_PORT", "8080"))
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))

    async def stop_server() -> None:
        await asyncio.sleep(0.5)
        server.should_exit = True

    runtime.on_complete = stop_server
    server.run()


if __name__ == "__main__":
    main()
