from __future__ import annotations

import base64
import json
from pathlib import Path

from marketing.config import EngagementConfig, JudgeConfig, Limits
from marketing.engagement import blend, engagement_score, metric_fraction, parse_feed, room_score, seat_engagement
from marketing.entry import Entry, EntryMeta, MediaItem, MediaMeta, load_entry
from marketing.judge import (
    RUBRIC_VERSION,
    CraftReview,
    JudgeImagery,
    TechnicalReview,
    build_messages,
    cache_record,
    from_cache,
    judge_score,
    technical_review,
)
from marketing.probe import Measurement, contact_sheet, frame_at, measure, still_image


def test_measure_video_and_image(tiny_video: Path, tiny_image: Path) -> None:
    m = measure(tiny_video, "video")
    assert m.ok and 2.5 < m.duration < 3.5 and m.width == 1280 and m.height == 720 and m.has_audio
    assert m.loudness_lufs is not None
    i = measure(tiny_image, "image")
    assert i.ok and i.kind == "image" and i.width == 640 and i.height == 360 and i.duration == 0


def test_the_wall_passes_the_technical_panel(root: Path, tmp_path: Path) -> None:
    from marketing.check import pack_directory

    package = pack_directory(root / "players/the-wall")
    entry = load_entry(package, tmp_path / "w", Limits(), default_title="The Wall")
    assert entry.media_path is not None
    m = measure(entry.media_path, "video")
    review = technical_review(entry, m, Limits())
    assert review.eligible
    assert {c.id for c in review.failures} <= {"video_loudness"}
    assert review.score >= 90
    assert contact_sheet(entry.media_path, m.duration, 12, tmp_path / "sheet.jpg")
    assert frame_at(entry.media_path, m.duration - 1, tmp_path / "end.jpg")


def test_text_only_post_is_judged_on_text_rules(tmp_path: Path) -> None:
    post = tmp_path / "file"
    post.write_text("One true thing about the work. softmax.com #alignment #ai #agents", encoding="utf-8")
    entry = load_entry(post, tmp_path / "w", Limits(), default_title="t")
    review = technical_review(entry, None, Limits())
    assert review.eligible
    assert {c.id for c in review.failures} == {"hashtags"}
    assert review.score == 95


def test_overlong_text_fails_the_gate(tmp_path: Path) -> None:
    post = tmp_path / "file"
    post.write_text("x" * 281, encoding="utf-8")
    entry = load_entry(post, tmp_path / "w", Limits(), default_title="t")
    review = technical_review(entry, None, Limits())
    assert not review.eligible and review.score == 0
    assert any(c.id == "text_length" and not c.ok for c in review.checks)


def test_image_post_without_alt_text_is_penalised_not_gated(tiny_image: Path, tmp_path: Path) -> None:
    entry = load_entry(tiny_image, tmp_path / "w", Limits(), default_title="t")
    review = technical_review(entry, measure(tiny_image, "image"), Limits())
    assert review.eligible
    assert {c.id for c in review.failures} == {"text_present", "alt_text"}
    assert still_image(tiny_image, tmp_path / "still.jpg")


def test_video_gates(tiny_video: Path, tmp_path: Path) -> None:
    entry = load_entry(tiny_video, tmp_path / "w", Limits(), default_title="t")
    review = technical_review(entry, measure(tiny_video, "video"), Limits(video_max_seconds=2))
    assert not review.eligible and review.score == 0


def test_judge_score_and_blend() -> None:
    cfg = JudgeConfig()
    tech = TechnicalReview(score=90, eligible=True)
    craft = CraftReview(
        scores={"hook": 8, "clear": 8, "specific": 9, "voice": 7, "legible": 8, "craft": 8, "repostable": 8},
        cringe_flags=[],
        notes="",
        verdict="",
        model="m",
        attempts=1,
    )
    assert abs(craft.score - 79.5) < 1e-6
    # Technical faults subtract point for point; a clean package adds nothing.
    assert judge_score(tech, craft, cfg) == 69.5
    assert judge_score(TechnicalReview(score=100, eligible=True), craft, cfg) == 79.5
    placeholder = CraftReview(
        scores={k: (1 if k == "craft" else 0) for k in craft.scores},
        cringe_flags=[],
        notes="",
        verdict="",
        model="m",
        attempts=1,
    )
    assert judge_score(TechnicalReview(score=100, eligible=True), placeholder, cfg) == 0.5
    assert judge_score(tech, None, cfg) == 22.5
    assert judge_score(TechnicalReview(score=0, eligible=False), craft, cfg) == 0.0
    eng = EngagementConfig()
    assert blend(80.0, 0.0, eng) == 40.0
    assert blend(80.0, 60.0, eng) == 70.0


def test_engagement_curve_and_feed() -> None:
    cfg = EngagementConfig()
    assert metric_fraction(0, 100) == 0.0
    assert metric_fraction(100, 100) == 1.0
    assert 0.85 < metric_fraction(50, 100) < 0.95
    assert metric_fraction(10_000, 100) == 1.0
    assert engagement_score(None, cfg) == 0.0
    full = engagement_score({"impressions": 10_000, "likes": 100, "reposts": 25, "replies": 10}, cfg)
    assert full == 100.0
    partial = engagement_score({"impressions": 1_000, "likes": 10, "reposts": 0, "replies": 0}, cfg)
    assert 40 < partial < 70

    feed = parse_feed(
        json.dumps(
            {
                "schema": "softmax-engagement-feed/1",
                "account": "softmaxresearch",
                "generated_at": "2026-10-09T00:00:00Z",
                "posts": {
                    "sha256:abc": {
                        "tweet_id": "1",
                        "url": "https://x.com/softmaxresearch/status/1",
                        "posted_at": "2026-10-08T00:00:00Z",
                        "fetched_at": "2026-10-09T00:00:00Z",
                        "metrics": {"impressions": "1200", "likes": 31, "reposts": 4, "replies": 2, "quotes": 1},
                    }
                },
                "judge_cache": {
                    "sha256:abc": {
                        "rubric": RUBRIC_VERSION,
                        "model": "m",
                        "technical": {"score": 95, "eligible": True, "checks": []},
                        "craft": None,
                    }
                },
            }
        ),
        source="test",
    )
    assert feed.available and feed.post_for("sha256:abc") is not None
    assert (
        feed.post_for("sha256:abc").metrics["impressions"] == 1200
        and feed.post_for("sha256:abc").metrics["quotes"] == 1
    )
    assert parse_feed(b"not json", source="t").available is False
    assert parse_feed(json.dumps({"schema": "other/1"}), source="t").available is False


def test_judge_cache_round_trip() -> None:
    tech = TechnicalReview(score=95, eligible=True)
    craft = CraftReview(
        scores={"hook": 7, "clear": 7, "specific": 7, "voice": 7, "legible": 7, "craft": 7, "repostable": 7},
        cringe_flags=["a flag"],
        notes="n",
        verdict="v",
        model="m",
        attempts=1,
    )
    record = cache_record(tech, craft, "m")
    assert record["rubric"] == RUBRIC_VERSION
    restored = from_cache(record, "m")
    assert restored is not None
    r_tech, r_craft = restored
    assert r_tech.score == 95 and r_tech.eligible and r_craft is not None and r_craft.cached
    assert r_craft.score == craft.score and r_craft.cringe_flags == ["a flag"]
    assert from_cache(record, "other-model") is None
    assert from_cache({**record, "rubric": "post/0"}, "m") is None
    assert EntryMeta(text="hello").label == "hello"


# --------------------------------------------------------------------------- several attachments


def _picture(width: int = 1200, height: int = 800, size: int = 300_000) -> Measurement:
    return Measurement(ok=True, kind="image", width=width, height=height, codec="png", size_bytes=size)


def _image_entry(names: list[str], alts: list[str]) -> Entry:
    pairs = list(zip(names, alts, strict=True))
    meta = EntryMeta(text="three pictures, one idea", media=[MediaMeta(path=n, alt_text=a) for n, a in pairs])
    items = [MediaItem(Path("/nonexistent") / n, "image", a, n) for n, a in pairs]
    return Entry(meta, items, "zip", [])


def test_technical_score_is_the_minimum_across_items_and_names_the_item() -> None:
    entry = _image_entry(["a.png", "b.png", "c.png"], ["alt a", "", "alt c"])
    review = technical_review(entry, [_picture(), _picture(), _picture()], Limits())
    assert review.eligible
    # One missing alt text costs 5 once (the worst item), not 5 per item and not 15.
    assert review.score == 95
    failed = review.failures
    assert [c.id for c in failed] == ["alt_text"]
    assert failed[0].detail.startswith("image 2/3 (b.png): ")
    # A single bad picture fails the whole post, and the check says which one.
    review = technical_review(entry, [_picture(), _picture(), _picture(width=10, height=10)], Limits())
    assert not review.eligible and review.score == 0
    bad = [c for c in review.failures if c.gate]
    assert bad and bad[0].id == "dimensions" and bad[0].detail.startswith("image 3/3 (c.png): ")
    # A picture that was never measured is a decode failure, not a pass.
    review = technical_review(entry, [_picture(), _picture()], Limits())
    assert not review.eligible and any(c.id == "decodes" and "c.png" in c.detail for c in review.failures)


def test_single_item_checks_keep_their_plain_wording(tiny_image: Path, tmp_path: Path) -> None:
    entry = load_entry(tiny_image, tmp_path / "w", Limits(), default_title="t")
    review = technical_review(entry, [measure(tiny_image, "image")], Limits())
    alt = next(c for c in review.checks if c.id == "alt_text")
    assert alt.detail == "media without alt text"


def test_rubric_version_bumped_for_the_stranger_read() -> None:
    assert RUBRIC_VERSION == "post/4"


# --------------------------------------------------------------------------- readings, medians and rule winces


def test_known_flags_keep_rule_winces_and_drop_hunches() -> None:
    from marketing.judge import known_flags

    flags = [
        "hype: 'excited to announce' in line one",
        "final frame states the thesis as a slogan over a logo card",
        "alt text breaks the first-person frame",
        "Typo: 'recieve'",
        "hype: the same rule twice is one wince",
        "hype: 'excited to announce' in line one",
    ]
    kept = known_flags(flags)
    assert kept == [
        "hype: 'excited to announce' in line one",
        "Typo: 'recieve'",
        "hype: the same rule twice is one wince",
    ]
    assert known_flags("not a list") == []
    assert known_flags(None) == []


def _reading(total: int, flags: list[str] | None = None, verdict: str = "v") -> dict:
    return {
        **{k: total for k in ("hook", "clear", "specific", "voice", "legible", "craft", "repostable")},
        "cringe_flags": flags or [],
        "notes": f"notes {total}",
        "verdict": verdict,
    }


def test_aggregate_samples_takes_the_median_and_majority_flags() -> None:
    from marketing.judge import aggregate_samples

    review = aggregate_samples(
        [
            _reading(8, ["typo: 'teh'"], verdict="eight"),
            _reading(3, ["typo: 'teh'", "hype: 'best ever'"], verdict="three"),
            _reading(7, ["product_copy: lists features"], verdict="seven"),
        ],
        model="m",
        attempts=3,
        raw="r",
    )
    assert review.scores == {k: 7 for k in review.scores}
    assert review.samples == 3
    assert review.spread == 50.0
    assert review.verdict == "seven" and review.notes == "notes 7"
    # typo was raised by two of three readings; hype and product_copy by one each.
    assert review.cringe_flags == ["typo: 'teh'"]
    assert review.to_dict()["samples"] == 3 and review.to_dict()["spread"] == 50.0


def test_aggregate_single_reading_is_itself() -> None:
    from marketing.judge import CraftReview, aggregate_samples

    review = aggregate_samples([_reading(6, ["exclamation: 'Wow!'"])], model="m", attempts=1, raw="r")
    assert review.samples == 1 and review.spread == 0.0 and review.cringe_flags == ["exclamation: 'Wow!'"]
    restored = CraftReview.from_dict(review.to_dict())
    assert restored.samples == 1 and restored.cringe_flags == review.cringe_flags


def test_build_messages_with_three_stills() -> None:
    entry = _image_entry(["a.png", "b.png", "c.png"], ["first", "", "third"])
    stills = [b"\xff\xd8\xff" + bytes([i]) * 8 for i in range(3)]
    measurements = [_picture(), _picture(640, 360), _picture()]
    messages = build_messages(entry, measurements, JudgeImagery(stills=stills), "brief", "softmaxresearch")
    assert messages[0]["role"] == "system" and "one to four images" in messages[0]["content"]
    content = messages[1]["content"]
    images = [part for part in content if part["type"] == "image_url"]
    assert len(images) == 3
    assert [part["image_url"]["url"] for part in images] == [
        "data:image/jpeg;base64," + base64.b64encode(s).decode() for s in stills
    ]
    texts = [part["text"] for part in content if part["type"] == "text"]
    facts = texts[0]
    assert "Media: 3 images" in facts and "image 2/3 (b.png): 640x360" in facts
    # The stranger's view carries no alt text; it follows the pictures, as context for checking facts.
    assert "Alt text" not in facts
    context = next(t for t in texts if t.startswith("CONTEXT THE STRANGER DOES NOT SEE"))
    assert "Alt text for image 1/3 (a.png): first" in context and "Alt text for image 2/3 (b.png): (none)" in context
    assert content.index(next(p for p in content if p["type"] == "text" and p["text"] is context)) > max(
        i for i, p in enumerate(content) if p["type"] == "image_url"
    )
    labels = [t for t in texts if t.startswith("Image ")]
    assert labels == ["Image 1 of 3", "Image 2 of 3", "Image 3 of 3"]
    # Each label immediately precedes its picture.
    for i, part in enumerate(content):
        if part["type"] == "image_url":
            assert content[i - 1]["type"] == "text" and content[i - 1]["text"].startswith("Image ")
    assert texts[-1].startswith("Score it")


def test_build_messages_text_only_has_no_images() -> None:
    entry = Entry(EntryMeta(text="just words"), [], "text", [])
    content = build_messages(entry, None, JudgeImagery(), "b", "a")[1]["content"]
    assert not [p for p in content if p["type"] == "image_url"]
    assert "Media: none (text-only post)" in content[0]["text"]


# --------------------------------------------------------------------------- the room


def test_room_score_and_seat_engagement() -> None:
    cfg = EngagementConfig()
    assert cfg.room_points_per_ship == 10.0
    assert room_score(0, cfg) == 0.0
    assert room_score(3, cfg) == 30.0
    assert room_score(12, cfg) == 100.0
    assert room_score(-4, cfg) == 0.0
    assert room_score(3, EngagementConfig(room_points_per_ship=25)) == 75.0
    # Not posted: the room is the engagement half.
    assert seat_engagement(None, 3, cfg) == 30.0
    assert seat_engagement(None, 0, cfg) == 0.0
    # Posted: X metrics replace the room, however many ships it has.
    metrics = {"impressions": 1_000, "likes": 10, "reposts": 0, "replies": 0}
    assert seat_engagement(metrics, 12, cfg) == engagement_score(metrics, cfg) < 100.0
    assert blend(80.0, 30.0, cfg) == 55.0


def test_feed_room_map_parses_leniently() -> None:
    base = {"schema": "softmax-engagement-feed/1", "account": "a", "generated_at": "", "posts": {}, "judge_cache": {}}
    feed = parse_feed(json.dumps(base), source="t")
    assert feed.available and feed.room == {} and feed.ships_for("sha256:x") == 0
    feed = parse_feed(
        json.dumps(
            {
                **base,
                "room": {
                    "sha256:a": {"ships": 3, "updated_at": "2026-10-09T01:00:00Z"},
                    "sha256:b": {"ships": "7"},
                    "sha256:c": {"ships": -2},
                    "sha256:d": 4,
                    "sha256:e": None,
                },
            }
        ),
        source="t",
    )
    assert feed.ships_for("sha256:a") == 3 and feed.room["sha256:a"].updated_at == "2026-10-09T01:00:00Z"
    assert feed.ships_for("sha256:b") == 7 and feed.ships_for("sha256:c") == 0 and feed.ships_for("sha256:d") == 4
    assert "sha256:e" not in feed.room and feed.ships_for("sha256:zzz") == 0
    assert parse_feed(json.dumps({**base, "room": "nonsense"}), source="t").room == {}


def test_model_client_drops_temperature_when_the_model_refuses_it(monkeypatch) -> None:
    import io as _io
    from urllib.error import HTTPError

    from marketing import judge as judge_module
    from marketing.judge import ModelClient

    monkeypatch.setenv("JUDGE_API_KEY", "k")
    client = ModelClient(JudgeConfig())
    client.model = "synthetic-future-model-with-unknown-parameter-support"
    bodies: list[dict] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()

    def fake_urlopen(request, timeout):
        body = json.loads(request.data)
        bodies.append(body)
        if "temperature" in body:
            raise HTTPError(
                request.full_url,
                404,
                "nf",
                {},
                _io.BytesIO(
                    b'{"error":{"message":"No provider supports this model with the requested parameter combination."}}'
                ),
            )
        return _Resp()

    monkeypatch.setattr(judge_module, "urlopen", fake_urlopen)
    assert client.complete([{"role": "user", "content": "x"}], slot=0, temperature=0.7) == "{}"
    assert [("temperature" in b) for b in bodies] == [True, False]
    assert client.supports_temperature is False
    client.complete([{"role": "user", "content": "x"}], slot=0, temperature=0.7)
    assert "temperature" not in bodies[-1]
