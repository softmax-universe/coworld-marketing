"""The autograder: a deterministic technical panel and an LLM craft review against the house rubric."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import statistics
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from marketing import __version__
from marketing.config import JudgeConfig, Limits
from marketing.entry import Entry, MediaItem, count_hashtags, count_links, weighted_length
from marketing.probe import Measurement

logger = logging.getLogger("marketing.judge")

RUBRIC_VERSION = "post/4"
# THE ONLY WINCES THERE ARE. A flag names a rule the post breaks (the rubric lists
# them with their evidence); taste lives in the six scores. Anything the model
# flags outside this list is dropped, so a judge's hunch about an end card or the
# point of view of an alt text can never read as a fault on the board.
WINCE_RULES = (
    "hashtag_pile",
    "exclamation",
    "emoji_punctuation",
    "hype",
    "engagement_bait",
    "product_copy",
    "self_congratulation",
    "unsupported_claim",
    "typo",
    "illegible_media",
    "mascot_cheering",
)
CRAFT_KEYS = ("hook", "clear", "specific", "voice", "legible", "craft", "repostable")
# `clear` is the stranger's share: a post the team loves but nobody outside
# would follow cannot score above the low seventies, whatever else it does.
CRAFT_WEIGHTS = {
    "hook": 0.15,
    "clear": 0.20,
    "specific": 0.15,
    "voice": 0.20,
    "legible": 0.10,
    "craft": 0.05,
    "repostable": 0.15,
}


def rubric_text() -> str:
    return resources.files("marketing").joinpath("rubric.md").read_text(encoding="utf-8")


@dataclass
class Check:
    id: str
    ok: bool
    detail: str
    penalty: float = 0.0  # points deducted from 100 when not ok
    gate: bool = False  # a failed gate makes the entry ineligible


@dataclass
class TechnicalReview:
    score: float
    eligible: bool
    checks: list[Check] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"score": round(self.score, 1), "eligible": self.eligible, "checks": [asdict(c) for c in self.checks]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TechnicalReview:
        checks = [
            Check(**{k: c.get(k) for k in ("id", "ok", "detail", "penalty", "gate")}) for c in data.get("checks", [])
        ]
        return cls(float(data.get("score", 0.0)), bool(data.get("eligible", False)), checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


def as_measurements(m: Sequence[Measurement] | Measurement | None) -> list[Measurement]:
    """Callers written for one attachment pass a Measurement or None; the review works on a list, one per item."""
    if m is None:
        return []
    if isinstance(m, Measurement):
        return [m]
    return list(m)


def item_label(item: MediaItem, index: int, count: int) -> str:
    """How the checks name an attachment: silent for a lone item, "image 2/3 (b.jpg)" when there are several."""
    return "" if count < 2 else f"{item.kind} {index + 1}/{count} ({item.display_name})"


def technical_review(entry: Entry, m: Sequence[Measurement] | Measurement | None, limits: Limits) -> TechnicalReview:
    """The deterministic panel. Text checks run once; media checks run per attachment and the entry's score is the
    minimum across attachments, so one bad image fails the post and its check names the item that failed."""
    measurements = as_measurements(m)
    checks: list[Check] = []

    def add(id_: str, ok: bool, detail: str, penalty: float = 0.0, gate: bool = False) -> None:
        checks.append(Check(id_, ok, detail, penalty if not ok else 0.0, gate))

    fatal = [p for p in entry.problems if p.startswith("fatal:")]
    add("package", not fatal, fatal[0] if fatal else f"{entry.kind} package accepted", gate=True)
    if fatal:
        return TechnicalReview(0.0, False, checks)

    text = entry.meta.text
    has_text = bool(text.strip())
    has_media = bool(entry.media)
    add(
        "content",
        has_text or has_media,
        "post has text or media" if (has_text or has_media) else "empty post",
        gate=True,
    )
    if not (has_text or has_media):
        return TechnicalReview(0.0, False, checks)

    weighted = weighted_length(text)
    add(
        "text_length",
        weighted <= limits.text_max_weighted_chars,
        f"text weighs {weighted} of {limits.text_max_weighted_chars} characters (URLs count 23)",
        gate=True,
    )
    add("text_present", has_text, "post text present" if has_text else "media with no post text", penalty=10)
    hashtags = count_hashtags(text)
    add("hashtags", hashtags <= limits.max_hashtags, f"{hashtags} hashtags (limit {limits.max_hashtags})", penalty=5)
    links = count_links(text)
    add("links", links <= limits.max_links, f"{links} links (limit {limits.max_links})", penalty=5)
    if has_media:
        add(
            "media_count",
            len(entry.media) <= limits.max_media_items,
            f"{len(entry.media)} attachment{'s' if len(entry.media) != 1 else ''} (limit {limits.max_media_items})",
            gate=True,
        )

    item_penalties: list[float] = []
    for index, item in enumerate(entry.media):
        measurement = measurements[index] if index < len(measurements) else None
        item_checks = _media_checks(item, item_label(item, index, len(entry.media)), measurement, limits)
        checks.extend(item_checks)
        item_penalties.append(sum(c.penalty for c in item_checks))

    json_problems = [p for p in entry.problems if p.startswith(("entry.json", "no entry.json", "media ", "legacy"))]
    add("entry_json", not json_problems, "; ".join(json_problems) or "entry.json valid", penalty=5)

    eligible = all(c.ok for c in checks if c.gate)
    if not eligible:
        return TechnicalReview(0.0, False, checks)
    common = sum(c.penalty for c in checks) - sum(item_penalties)
    worst_item = max(item_penalties, default=0.0)
    return TechnicalReview(max(0.0, 100.0 - common - worst_item), True, checks)


def _media_checks(item: MediaItem, label: str, m: Measurement | None, limits: Limits) -> list[Check]:
    checks: list[Check] = []
    prefix = f"{label}: " if label else ""

    def add(id_: str, ok: bool, detail: str, penalty: float = 0.0, gate: bool = False) -> None:
        checks.append(Check(id_, ok, prefix + detail, penalty if not ok else 0.0, gate))

    if m is None:
        add("decodes", False, "not measured", gate=True)
        return checks
    add("decodes", m.ok, m.error or f"{m.kind} {m.codec} {m.width}x{m.height}", gate=True)
    if not m.ok:
        return checks
    alt_text = item.alt_text.strip()
    add("alt_text", bool(alt_text), "alt text present" if alt_text else "media without alt text", penalty=5)
    add(
        "alt_text_length",
        len(item.alt_text) <= limits.alt_text_max_chars,
        f"alt text {len(item.alt_text)} chars (limit {limits.alt_text_max_chars})",
        penalty=3,
    )
    small = min(m.width, m.height) >= limits.min_dimension
    add("dimensions", small, f"{m.width}x{m.height} (minimum side {limits.min_dimension})", gate=True)
    aspect_ok = limits.aspect_min <= m.aspect <= limits.aspect_max
    add(
        "aspect",
        aspect_ok,
        f"aspect {m.aspect:.2f} (X accepts {limits.aspect_min:.2f} to {limits.aspect_max:.2f})",
        gate=True,
    )
    if item.kind == "video":
        add(
            "video_duration_max",
            m.duration <= limits.video_max_seconds,
            f"{m.duration:.1f} s (X limit {limits.video_max_seconds:.0f} s)",
            gate=True,
        )
        add(
            "video_duration_min",
            m.duration >= limits.video_min_seconds,
            f"{m.duration:.2f} s (minimum {limits.video_min_seconds} s)",
            gate=True,
        )
        add(
            "video_size",
            m.size_bytes <= limits.video_max_bytes,
            f"{m.size_bytes / 1048576:.1f} MiB (limit {limits.video_max_bytes / 1048576:.0f} MiB)",
            gate=True,
        )
        add(
            "video_resolution",
            m.height >= limits.video_min_height or m.width >= limits.video_min_height,
            f"{m.width}x{m.height} (minimum {limits.video_min_height} on the short side for landscape)",
            penalty=10,
        )
        frozen_body = max(0.0, m.frozen_seconds - min(m.frozen_tail_seconds, 7.0))
        frozen_fraction = frozen_body / m.duration if m.duration else 0.0
        add(
            "video_motion",
            frozen_fraction <= limits.video_max_frozen_fraction,
            f"{frozen_body:.1f} s frozen ({frozen_fraction:.0%}; limit {limits.video_max_frozen_fraction:.0%})",
            penalty=10,
        )
        if m.loudness_lufs is not None:
            in_range = limits.loudness_lufs_min <= m.loudness_lufs <= limits.loudness_lufs_max
            add(
                "video_loudness",
                in_range,
                f"{m.loudness_lufs:.1f} LUFS (range {limits.loudness_lufs_min:.0f} to {limits.loudness_lufs_max:.0f})",
                penalty=5,
            )
    elif item.kind == "gif":
        add(
            "gif_size",
            m.size_bytes <= limits.gif_max_bytes,
            f"{m.size_bytes / 1048576:.1f} MiB (X limit {limits.gif_max_bytes / 1048576:.0f} MiB)",
            gate=True,
        )
    else:
        add(
            "image_size",
            m.size_bytes <= limits.image_max_bytes,
            f"{m.size_bytes / 1048576:.2f} MiB (X limit {limits.image_max_bytes / 1048576:.0f} MiB)",
            gate=True,
        )
    return checks


@dataclass
class CraftReview:
    scores: dict[str, int]
    cringe_flags: list[str]
    notes: str
    verdict: str
    model: str
    attempts: int
    raw: str = ""
    cached: bool = False
    # How many independent readings the review is the median of, and how far
    # apart their totals were (0 to 100). A wide spread is the judge saying it
    # is unsure; the board shows it next to the grade.
    samples: int = 1
    spread: float = 0.0

    @property
    def score(self) -> float:
        return 10.0 * sum(CRAFT_WEIGHTS[k] * self.scores.get(k, 0) for k in CRAFT_KEYS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 1),
            "scores": self.scores,
            "cringe_flags": self.cringe_flags,
            "notes": self.notes,
            "verdict": self.verdict,
            "model": self.model,
            "attempts": self.attempts,
            "cached": self.cached,
            "samples": self.samples,
            "spread": round(self.spread, 1),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CraftReview:
        scores = {k: _clamp_score((data.get("scores") or {}).get(k)) for k in CRAFT_KEYS}
        return cls(
            scores=scores,
            cringe_flags=[str(f)[:120] for f in (data.get("cringe_flags") or [])][:12],
            notes=str(data.get("notes", ""))[:800],
            verdict=str(data.get("verdict", ""))[:280],
            model=str(data.get("model", "")),
            attempts=int(data.get("attempts", 0) or 0),
            cached=True,
            samples=max(1, int(data.get("samples", 1) or 1)),
            spread=float(data.get("spread", 0.0) or 0.0),
        )


class ModelClient:
    """OpenAI-chat-shaped client. Hosted: the Coworld LLM sidecar. Local: an OpenRouter-compatible endpoint + key."""

    def __init__(self, cfg: JudgeConfig, *, on_call: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.cfg = cfg
        self.on_call = on_call
        sidecar = os.environ.get("COWORLD_LLM_ENDPOINT", "").rstrip("/")
        if sidecar:
            self.base = f"{sidecar}/v1/chat/completions"
            self.key = ""
            self.source = "sidecar"
        else:
            base = os.environ.get("JUDGE_API_BASE", "https://openrouter.ai/api/v1").rstrip("/")
            self.base = f"{base}/chat/completions"
            self.key = os.environ.get("JUDGE_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or ""
            self.source = "local" if self.key else "none"
        self.model = os.environ.get("JUDGE_MODEL") or cfg.model

    @property
    def available(self) -> bool:
        return self.source != "none"

    # Some frontier models refuse sampling controls outright: OpenRouter answers
    # 404 "No provider supports this model with the requested parameter
    # combination" for Opus 5.5 with a temperature set. The first such refusal
    # drops the control for the rest of the run; readings then differ only by
    # the model's own nondeterminism, which is still several readings.
    supports_temperature: bool = True

    def _record_call(
        self,
        body: dict[str, Any],
        slot: int | None,
        started: float,
        status: int | None,
        headers: Any,
        raw: bytes | None,
        complete: bool,
    ) -> None:
        if self.on_call is None:
            return
        pairs = [[key, value] for key, value in headers.items()] if headers is not None else []
        identities = [value for key, value in pairs if key.lower() == "x-softmax-llm-call-id"]
        self.on_call(
            {
                "attempt_id": str(uuid4()),
                "platform_call_id": identities[0] if len(identities) == 1 else None,
                "slot": slot,
                "request": body,
                "http_status": status,
                "response_header_pairs": pairs,
                "raw_response": raw.decode("utf-8", errors="replace") if raw is not None else None,
                "response_body_b64": base64.b64encode(raw).decode() if raw is not None else None,
                "response_complete": complete,
                "response_reader_joined": complete,
                "latency_ms": (time.monotonic() - started) * 1000,
            }
        )

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        slot: int | None,
        temperature: float | None = 0.2,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        body: dict[str, Any] = {"model": self.model, "messages": messages, "max_tokens": self.cfg.max_tokens}
        if response_format is not None:
            body["response_format"] = response_format
        # Known Opus 5.5 routes reject this control. Its successful compatibility
        # retry already omits it; avoid that failed request up front. Inspect the
        # current model because native authors can override the grader's model.
        if self.supports_temperature and temperature is not None and self.model != "anthropic/claude-opus-5.5":
            body["temperature"] = temperature
        headers = {"Content-Type": "application/json", "User-Agent": f"coworld-marketing/{__version__}"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        if slot is not None and self.source == "sidecar":
            headers["X-Coworld-Player-Slot"] = str(slot)
        request = Request(self.base, data=json.dumps(body).encode(), headers=headers, method="POST")
        started = time.monotonic()
        response_headers, status = None, None
        try:
            with urlopen(request, timeout=self.cfg.timeout_seconds) as response:
                response_headers, status = getattr(response, "headers", None), getattr(response, "status", None)
                raw = response.read()
        except HTTPError as error:
            raw, complete = None, False
            try:
                raw = error.read()
                complete = True
            except Exception:
                pass
            self._record_call(body, slot, started, error.code, error.headers, raw, complete)
            detail = raw.decode(errors="replace") if raw is not None else ""
            if (
                self.supports_temperature
                and "temperature" in body
                and error.code in {400, 404}
                and "parameter" in detail.lower()
            ):
                logger.warning(
                    "model %s refuses sampling controls (%s); retrying without temperature", self.model, error.code
                )
                self.supports_temperature = False
                return self.complete(messages, slot=slot, temperature=temperature, response_format=response_format)
            raise
        except Exception as error:
            partial = getattr(error, "partial", None)
            self._record_call(
                body, slot, started, status, response_headers, partial if isinstance(partial, bytes) else None, False
            )
            raise
        self._record_call(body, slot, started, status, response_headers, raw, True)
        payload = json.loads(raw)
        choice = payload["choices"][0]
        if choice.get("finish_reason") == "error":
            raise RuntimeError("model returned finish_reason=error")
        content = choice["message"]["content"]
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return str(content)


def _data_uri(image: Path | bytes) -> str:
    data = image if isinstance(image, bytes) else image.read_bytes()
    return "data:image/jpeg;base64," + base64.b64encode(data).decode()


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start : end + 1])


def _clamp_score(value: Any) -> int:
    try:
        return max(0, min(10, int(round(float(value)))))
    except (TypeError, ValueError):
        return 0


@dataclass
class JudgeImagery:
    """What the craft panel sees. Pictures come as `stills`, one JPEG per attachment in order; a video comes as its
    contact sheet and final frame. `still` remains as the single-picture fallback for older callers."""

    stills: list[bytes] = field(default_factory=list)  # one rendering per image or gif, in entry order
    still: Path | None = None  # the image, or the video's poster frame (fallback when stills is empty)
    sheet: Path | None = None  # contact sheet for video
    end: Path | None = None  # final frame for video


def build_messages(
    entry: Entry,
    m: Sequence[Measurement] | Measurement | None,
    imagery: JudgeImagery,
    brief: str,
    account: str,
) -> list[dict[str, Any]]:
    meta = entry.meta
    measurements = as_measurements(m)
    count = len(entry.media)
    if not entry.media:
        media_line = "Media: none (text-only post)"
    elif entry.media_kind == "image":
        media_line = f"Media: {count} image{'s' if count != 1 else ''}"
    else:
        media_line = f"Media: {entry.media_kind}"
    # THE STRANGER'S VIEW FIRST: the post and its media, nothing else, so the
    # reading that decides `hook`, `clear` and `voice` happens before the
    # judge knows what the entrant meant. The brief, the alt text, the thesis
    # and the notes follow, labelled as context for checking facts.
    post_block = [
        f"Account: @{account}",
        f"THE POST, as a stranger on X sees it ({weighted_length(meta.text)} of 280 weighted characters):",
        meta.text.strip() or "(no text; media only)",
        "",
        media_line,
    ]
    for index, item in enumerate(entry.media):
        measurement = measurements[index] if index < len(measurements) else None
        label = item_label(item, index, count) or item.kind.capitalize()
        if measurement is not None and measurement.ok:
            if item.kind == "video":
                post_block.append(
                    f"Video: {measurement.duration:.0f} s, {measurement.width}x{measurement.height}, "
                    f"audio: {'yes' if measurement.has_audio else 'no'}"
                )
            else:
                post_block.append(f"{label}: {measurement.width}x{measurement.height}")
    content: list[dict[str, Any]] = [{"type": "text", "text": "\n".join(post_block)}]
    if entry.media_kind == "video" and imagery.sheet is not None and imagery.sheet.exists():
        content.append(
            {"type": "text", "text": "Contact sheet, frames in time order, left to right then top to bottom:"}
        )
        content.append({"type": "image_url", "image_url": {"url": _data_uri(imagery.sheet)}})
        if imagery.end is not None and imagery.end.exists():
            content.append({"type": "text", "text": "The final frame:"})
            content.append({"type": "image_url", "image_url": {"url": _data_uri(imagery.end)}})
    elif imagery.stills:
        total = len(imagery.stills)
        for index, still in enumerate(imagery.stills):
            content.append({"type": "text", "text": f"Image {index + 1} of {total}"})
            content.append({"type": "image_url", "image_url": {"url": _data_uri(still)}})
    elif imagery.still is not None and imagery.still.exists():
        content.append({"type": "text", "text": "The attached picture:"})
        content.append({"type": "image_url", "image_url": {"url": _data_uri(imagery.still)}})
    context_block = [
        "CONTEXT THE STRANGER DOES NOT SEE. Use it to check facts and the alt text, "
        "not to supply meaning the post lacks.",
        f"Brief: {brief}",
    ]
    for index, item in enumerate(entry.media):
        label = item_label(item, index, count) or item.kind.capitalize()
        context_block.append(f"Alt text{'' if count < 2 else f' for {label}'}: {item.alt_text.strip() or '(none)'}")
    context_block += [
        f"Entrant's thesis: {meta.thesis.strip() or '(none)'}",
        f"Entrant's notes: {meta.notes.strip() or '(none)'}",
    ]
    content.append({"type": "text", "text": "\n".join(context_block)})
    content.append({"type": "text", "text": "Score it. Return only the JSON object."})
    return [{"role": "system", "content": rubric_text()}, {"role": "user", "content": content}]


def known_flags(flags: Any) -> list[str]:
    """Keep only flags that name a rule in WINCE_RULES ("rule: evidence"), trimmed; drop the judge's hunches."""
    if not isinstance(flags, list):
        flags = [flags] if flags else []
    kept: list[str] = []
    for flag in flags:
        text = str(flag).strip()
        rule = text.split(":", 1)[0].strip().lower().replace(" ", "_").replace("-", "_")
        if rule in WINCE_RULES and text[:120] not in kept:
            kept.append(text[:120])
    return kept[:12]


def _flag_rule(flag: str) -> str:
    return flag.split(":", 1)[0].strip().lower()


def aggregate_samples(samples: list[dict[str, Any]], *, model: str, attempts: int, raw: str) -> CraftReview:
    """One review out of several readings: per-dimension medians, majority flags, the middle reading's words.

    The median is robust to one reading that woke up grumpy; a flag has to be
    raised by more than half the readings to count; the notes and verdict come
    from the reading whose total sits closest to the median total, so the words
    match the number.
    """
    assert samples, "aggregate_samples needs at least one sample"
    scored = [{k: _clamp_score(d.get(k)) for k in CRAFT_KEYS} for d in samples]
    scores = {k: int(statistics.median(sorted(sc[k] for sc in scored)) + 0.5) for k in CRAFT_KEYS}
    totals = [10.0 * sum(CRAFT_WEIGHTS[k] * sc[k] for k in CRAFT_KEYS) for sc in scored]
    median_total = statistics.median(totals)
    middle = min(range(len(samples)), key=lambda i: abs(totals[i] - median_total))
    counts: dict[str, int] = {}
    wording: dict[str, str] = {}
    for d in samples:
        seen: set[str] = set()
        for flag in known_flags(d.get("cringe_flags")):
            rule = _flag_rule(flag)
            if rule in seen:
                continue
            seen.add(rule)
            counts[rule] = counts.get(rule, 0) + 1
            wording.setdefault(rule, flag)
    needed = len(samples) // 2 + 1
    flags = [wording[rule] for rule in WINCE_RULES if counts.get(rule, 0) >= needed]
    return CraftReview(
        scores=scores,
        cringe_flags=flags,
        notes=str(samples[middle].get("notes", ""))[:800],
        verdict=str(samples[middle].get("verdict", ""))[:280],
        model=model,
        attempts=attempts,
        raw=raw[:4000],
        samples=len(samples),
        spread=round(max(totals) - min(totals), 1),
    )


def _one_reading(
    client: ModelClient, messages: list[dict[str, Any]], *, slot: int, temperature: float
) -> tuple[dict[str, Any], str, int]:
    """One parsed reading from the model, with the retry loop the panel always had."""
    last_error = ""
    for attempt in range(1, client.cfg.retries + 2):
        try:
            raw = client.complete(messages, slot=slot, temperature=temperature)
            return _extract_json(raw), raw, attempt
        except HTTPError as error:
            body = ""
            with suppress(Exception):
                body = error.read().decode(errors="replace")[:300]
            last_error = f"HTTP {error.code} {body}"
            retry_after = error.headers.get("Retry-After-Ms") or error.headers.get("Retry-After")
            if error.code == 429 and retry_after:
                try:
                    wait = float(retry_after) / (1000.0 if "Ms" in str(error.headers.keys()) else 1.0)
                except ValueError:
                    wait = 5.0
                time.sleep(min(max(wait, 1.0), 30.0))
            elif error.code in {400, 403}:
                break
        except (URLError, TimeoutError, OSError, ValueError, KeyError, RuntimeError, json.JSONDecodeError) as error:
            last_error = f"{type(error).__name__}: {error}"
        logger.warning("craft reading attempt %d failed for slot %d: %s", attempt, slot, last_error)
        time.sleep(min(2.0 * attempt, 6.0))
    raise RuntimeError(last_error or "no reading")


def craft_review(
    client: ModelClient,
    entry: Entry,
    m: Sequence[Measurement] | Measurement | None,
    imagery: JudgeImagery,
    brief: str,
    account: str,
    *,
    slot: int,
) -> CraftReview | None:
    """The craft panel: `cfg.samples` independent readings of the same prompt, folded into one review.

    One reading at temperature 0.2 when `samples` is 1 (the old behaviour);
    otherwise each reading runs at `sample_temperature` so they can disagree,
    and `aggregate_samples` takes the median. A reading that fails after its
    retries is skipped; the review is None only when none succeeded.
    """
    if not client.available:
        return None
    messages = build_messages(entry, m, imagery, brief, account)
    wanted = max(1, client.cfg.samples)
    temperature = client.cfg.sample_temperature if wanted > 1 else 0.2
    readings: list[dict[str, Any]] = []
    raws: list[str] = []
    attempts = 0
    for _ in range(wanted):
        try:
            data, raw, used = _one_reading(client, messages, slot=slot, temperature=temperature)
        except RuntimeError as error:
            logger.warning("craft reading unavailable for slot %d: %s", slot, error)
            continue
        readings.append(data)
        raws.append(raw)
        attempts += used
    if not readings:
        logger.warning("craft review unavailable for slot %d", slot)
        return None
    return aggregate_samples(readings, model=client.model, attempts=attempts, raw="\n---\n".join(raws))


def judge_score(technical: TechnicalReview, craft: CraftReview | None, cfg: JudgeConfig) -> float:
    """The autograder's 0 to 100: the craft panel's score, less the technical panel's deductions.

    A clean package earns nothing by itself (a test string used to collect 25
    points for being well-formed); a flawed one loses what the technical panel
    deducted, point for point. Ineligible entries score 0. When the craft panel
    could not run at all, the technical score is scaled by `technical_weight`
    so the entry is visibly provisional rather than unscored.
    """
    if not technical.eligible:
        return 0.0
    if craft is None:
        return round(cfg.technical_weight * technical.score, 2)
    return round(max(0.0, craft.score - (100.0 - technical.score)), 2)


def cache_record(technical: TechnicalReview, craft: CraftReview | None, model: str) -> dict[str, Any]:
    return {
        "rubric": RUBRIC_VERSION,
        "model": model,
        "technical": technical.to_dict(),
        "craft": craft.to_dict() if craft else None,
    }


def from_cache(record: dict[str, Any], model: str) -> tuple[TechnicalReview, CraftReview | None] | None:
    """A cached judgement is reusable when it was made with the same rubric version and model."""
    if record.get("rubric") != RUBRIC_VERSION or record.get("model") != model:
        return None
    technical = record.get("technical")
    if not isinstance(technical, dict):
        return None
    craft = record.get("craft")
    return TechnicalReview.from_dict(technical), (CraftReview.from_dict(craft) if isinstance(craft, dict) else None)
