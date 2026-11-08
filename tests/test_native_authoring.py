"""Whole native episodes against a loopback author/grader; no real model or training receipt."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest
from jsonschema import validate

from marketing.authoring import system_prompt, write_generated_package
from marketing.config import GameConfig
from marketing.entry import EntryMeta
from marketing.io import decode_replay_bytes
from marketing.server import Runtime


@pytest.fixture
def sidecar(monkeypatch):
    state = {"mode": "ok", "calls": [], "reply": ""}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["calls"].append({"body": body, "slot": self.headers.get("X-Coworld-Player-Slot")})
            author = body["model"] == "fixture-author"
            if author and state["mode"] == "ok" and "response_format" in body:
                validate(json.loads(state["reply"]), body["response_format"]["json_schema"]["schema"])
            status = 503 if author and state["mode"] == "http-error" else 200
            content = (
                state["reply"]
                if author
                else json.dumps(
                    {
                        "scores": {
                            name: 8 for name in ["hook", "clear", "specific", "voice", "legible", "craft", "repostable"]
                        },
                        "cringe_flags": [],
                        "notes": "Synthetic grader fixture",
                        "verdict": "Synthetic grader fixture",
                    }
                )
            )
            payload = {
                "model": body["model"],
                "choices": [
                    {
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "length" if author and state["mode"] == "truncated" else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            }
            raw = json.dumps(payload).encode()
            if author and state["mode"] == "nonliteral":
                payload["choices"][0]["message"]["content"] = [{"type": "text", "text": content}]
                raw = json.dumps(payload).encode()
            if author and state["mode"] == "tool-call":
                payload["choices"][0]["message"]["tool_calls"] = [{"id": "synthetic", "type": "function"}]
                raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw) + (10 if author and state["mode"] == "broken-body" else 0)))
            self.send_header("X-Softmax-LLM-Call-ID", str(uuid4()))  # Synthetic authority, never admitted teacher data.
            self.end_headers()
            self.wfile.write(raw)
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    monkeypatch.setenv("COWORLD_LLM_ENDPOINT", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("JUDGE_MODEL", "fixture-grader")
    for key in ["JUDGE_API_KEY", "OPENROUTER_API_KEY", "ENGAGEMENT_FEED_URI"]:
        monkeypatch.delenv(key, raising=False)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
        assert not worker.is_alive()


def package(path: Path, spec: dict, media: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("authoring.json", json.dumps(spec))
        archive.writestr(
            "entry.json", json.dumps({"schema": "softmax-post-entry/2", "text": "STATIC FALLBACK MUST NOT RUN"})
        )
        archive.write(media, "asset" + media.suffix)
    return path


async def episode(tmp_path, monkeypatch, original, *, registered_hash=None):
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("A small model, a clear rule, and an ordinary game to test it on.")
    paths = [original, baseline]
    seats = []
    for index, path in enumerate(paths):
        seats.append(
            {
                "slot": index,
                "file_uri": path.as_uri(),
                "content_hash": registered_hash
                if index == 0 and registered_hash
                else "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                "log_uri": (tmp_path / f"private-{index}.log").as_uri(),
                "artifact_uri": (tmp_path / f"graded-{index}.zip").as_uri(),
            }
        )
    monkeypatch.setenv("COGAME_RESULTS_URI", (tmp_path / "results.json").as_uri())
    monkeypatch.setenv("COGAME_SAVE_REPLAY_URI", (tmp_path / "replay").as_uri())
    monkeypatch.setenv("COGAME_SAVE_TRAJECTORY_URI", (tmp_path / "private-trajectory.jsonl").as_uri())
    config = GameConfig.model_validate(
        {
            "brief": "Native author fixture under the ordinary Marketing rubric",
            "players": [{"name": "Author"}, {"name": "Baseline"}],
            "tokens": ["local-0", "local-1"],
            "linger_seconds": 0,
            "judge": {"mode": "panel", "model": "fixture-grader", "samples": 1, "retries": 0},
        }
    )
    runtime = Runtime(config, seats_doc={"schema": "coworld-player-seats/1", "seats": seats})
    await runtime.run_episode()
    return runtime


@pytest.mark.parametrize("media_fixture", ["tiny_image", "tiny_video"])
@pytest.mark.parametrize("bounded", [False, True])
async def test_native_author_episode_preserves_literal_bytes_and_private_context(
    request, tmp_path, monkeypatch, sidecar, media_fixture, bounded
):
    media = request.getfixturevalue(media_fixture)
    name = "asset" + media.suffix
    spec = {
        "schema": "softmax-post-author/1",
        "model": "fixture-author",
        "brief": "PRIVATE AUTHOR BRIEF",
        "assets": [{"path": name, "context": "Synthetic QA media"}],
    }
    if bounded:
        spec["text_max_ascii_chars"] = 240
    original = package(tmp_path / "input.zip", spec, media)
    before = original.read_bytes()
    document = {
        "schema": "softmax-post-entry/2",
        "text": "A visible native post, written from the supplied asset.",
        "media": [{"path": name, "alt_text": "Synthetic QA media"}],
    }
    if bounded:
        document.update(title="", thesis="", notes="", credits="", made_with=[])
    reply = json.dumps(document, indent=2)
    sidecar["reply"] = reply
    runtime = await episode(tmp_path, monkeypatch, original)
    assert runtime.results["eligible"][0]
    assert original.read_bytes() == before
    record = runtime.seats[0].authoring
    assert record["status"] == "materialized"
    assert len(record["calls"]) == 1
    call = record["calls"][0]
    assert call["request"]["model"] == "fixture-author" and call["http_status"] == 200
    if bounded:
        output = call["request"]["response_format"]
        assert output["type"] == "json_schema" and output["json_schema"]["strict"]
        assert output["json_schema"]["schema"]["properties"]["text"]["pattern"] == r"^[ -~\n]{0,240}$"
        assert "title" in output["json_schema"]["schema"]["properties"]
    else:
        assert "response_format" not in call["request"]
    assert all("response_format" not in x["body"] for x in sidecar["calls"] if x["body"]["model"] != "fixture-author")
    raw_guide = (Path(__file__).resolve().parent.parent / "docs/POLICY_GUIDE.md").read_text()
    assert call["request"]["messages"][0]["content"] == raw_guide.partition("\n---\n")[2].strip()
    assert call["response_complete"] and call["response_reader_joined"]
    assert base64.b64decode(call["response_body_b64"]) == call["raw_response"].encode()
    assert [item for item in sidecar["calls"] if item["body"]["model"] == "fixture-author"][0]["slot"] == "0"
    generated = runtime.seats[0].workdir / "authored-entry.zip"
    with zipfile.ZipFile(generated) as archive:
        assert archive.read("entry.json") == reply.encode()
        assert archive.read(name) == media.read_bytes()
    assert runtime.results["content_hashes"][0] == "sha256:" + record["graded_package_sha256"]
    assert record["registered_input_sha256"] == hashlib.sha256(before).hexdigest()
    assert runtime.results["content_hashes"][0] != "sha256:" + record["registered_input_sha256"]
    reconstructed = tmp_path / "reconstructed.zip"
    write_generated_package(reply, runtime.seats[0].workdir / "author-input", {name}, reconstructed)
    assert reconstructed.read_bytes() == generated.read_bytes()
    private = json.loads((tmp_path / "private-trajectory.jsonl").read_bytes())
    assert private["authors"][0] == record and private["results"] == runtime.results
    replay = decode_replay_bytes((tmp_path / "replay").read_bytes())
    assert "PRIVATE AUTHOR BRIEF" not in json.dumps(replay)
    assert system_prompt() not in json.dumps(replay)
    assert "MARKETING_NATIVE_AUTHOR_CAPTURE " in (tmp_path / "private-0.log").read_text()


@pytest.mark.parametrize("text", ["x" * 241, "A non-ASCII caption: é"])
async def test_bounded_author_rejects_nonconforming_literal_reply_without_rewrite(
    tmp_path, monkeypatch, sidecar, tiny_image, text
):
    spec = {
        "schema": "softmax-post-author/1",
        "model": "fixture-author",
        "brief": "One bounded post, without an externally supplied caption.",
        "assets": [{"path": "asset.png"}],
        "text_max_ascii_chars": 240,
    }
    original = package(tmp_path / "input.zip", spec, tiny_image)
    sidecar["mode"] = "provider-violates-bound"
    reply = json.dumps(EntryMeta(text=text).model_dump(by_alias=True))
    sidecar["reply"] = reply
    runtime = await episode(tmp_path, monkeypatch, original)
    capture = runtime.seats[0].authoring
    assert not runtime.results["eligible"][0]
    assert capture["status"] == "failed" and capture["literal_reply"] == reply
    assert len(capture["calls"]) == 1 and capture["calls"][0]["http_status"] == 200
    assert "STATIC FALLBACK MUST NOT RUN" not in runtime.seats[0].entry.meta.text
    assert not (runtime.seats[0].workdir / "authored-entry.zip").exists()


@pytest.mark.parametrize(
    "mode",
    [
        "truncated",
        "http-error",
        "malformed",
        "undeclared-media",
        "wrong-input",
        "nonliteral",
        "tool-call",
        "broken-body",
    ],
)
async def test_failed_author_episode_cannot_become_a_static_fallback(tmp_path, monkeypatch, sidecar, tiny_image, mode):
    spec = {
        "schema": "softmax-post-author/1",
        "model": "fixture-author",
        "brief": "Failure QA",
        "assets": [{"path": "asset.png"}],
    }
    original = package(tmp_path / "input.zip", spec, tiny_image)
    sidecar["mode"] = mode
    sidecar["reply"] = (
        "bad JSON"
        if mode == "malformed"
        else json.dumps(
            {
                "schema": "softmax-post-entry/2",
                "text": "A native author reply.",
                "media": [{"path": "unregistered.png" if mode == "undeclared-media" else "asset.png"}],
            }
        )
    )
    runtime = await episode(
        tmp_path, monkeypatch, original, registered_hash="sha256:" + "f" * 64 if mode == "wrong-input" else None
    )
    assert not runtime.results["eligible"][0]
    assert runtime.seats[0].authoring["status"] == "failed"
    assert "STATIC FALLBACK MUST NOT RUN" not in runtime.seats[0].entry.meta.text
    if mode == "wrong-input":
        assert not any(item["body"]["model"] == "fixture-author" for item in sidecar["calls"])
    else:
        assert len(runtime.seats[0].authoring["calls"]) == 1
    if mode == "broken-body":
        call = runtime.seats[0].authoring["calls"][0]
        assert call["http_status"] == 200 and not call["response_complete"]
        assert not call["response_reader_joined"] and base64.b64decode(call["response_body_b64"])
