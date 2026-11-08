"""Optional native post authoring; the ordinary entry loader and grader remain authoritative."""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from marketing.config import JudgeConfig
from marketing.entry import ENTRY_SCHEMA, EntryMeta, _safe_extract, media_kind_for
from marketing.judge import ModelClient

AUTHOR_SCHEMA = "softmax-post-author/1"
MAX_REQUEST_BYTES = 131072


class AuthorAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    path: str = Field(min_length=1, max_length=400)
    context: str = Field(default="", max_length=2000)


class AuthorSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, hide_input_in_errors=True)
    schema_: Literal["softmax-post-author/1"] = Field(alias="schema")
    model: str = Field(min_length=1, max_length=200)
    brief: str = Field(min_length=1, max_length=8000)
    assets: list[AuthorAsset] = Field(default_factory=list, max_length=32)
    max_tokens: int = Field(default=1024, ge=200, le=4000)
    temperature: float | None = Field(default=None, ge=0, le=1.5)
    timeout_seconds: float = Field(default=90, gt=0, le=300)
    text_max_ascii_chars: int | None = Field(default=None, ge=1, le=240)


def bounded_entry_format(maximum: int) -> dict[str, Any]:
    """Constrain generation without editing the literal response or the ordinary rubric."""
    schema = EntryMeta.model_json_schema(by_alias=True)

    def portable(node: dict[str, Any]) -> None:
        for key in ("properties", "$defs"):
            for child in node.get(key, {}).values():
                portable(child)
        if isinstance(node.get("items"), dict):
            portable(node["items"])
        for key in ("anyOf", "oneOf", "allOf"):
            for child in node.get(key, []):
                portable(child)
        # Use a common structural subset. Original parser validators still
        # enforce their bounds; the text pattern carries this policy's cap.
        for key in ("title", "description", "default", "minLength", "maxLength", "minItems", "maxItems"):
            node.pop(key, None)
        if node.get("type") == "object":
            node["required"] = list(node["properties"])
            node["additionalProperties"] = False

    portable(schema)
    schema["properties"]["schema"] = {"type": "string", "const": ENTRY_SCHEMA}
    schema["properties"]["text"]["pattern"] = rf"^[ -~\n]{{0,{maximum}}}$"
    return {"type": "json_schema", "json_schema": {"name": "softmax_post_entry", "strict": True, "schema": schema}}


def system_prompt() -> str:
    try:
        raw = resources.files("marketing").joinpath("POLICY_GUIDE.md").read_text(encoding="utf-8")
    except FileNotFoundError:
        raw = (Path(__file__).resolve().parent.parent / "docs/POLICY_GUIDE.md").read_text(encoding="utf-8")
    _, separator, body = raw.partition("\n---\n")
    return (body if separator else raw).strip()


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def asset_path(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or "\\" in name:
        raise ValueError("Author asset paths must stay inside the registered package")
    path = root.joinpath(*relative.parts)
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Author asset is missing or escapes its registered package")
    if media_kind_for(path) == "none":
        raise ValueError("Author asset must be an ordinary supported media file")
    return path


def write_generated_package(
    reply: str,
    root: Path,
    allowed_assets: set[str],
    output: Path,
    *,
    text_max_ascii_chars: int | None = None,
) -> None:
    """Preserve literal model JSON and media bytes in a reproducible native ZIP."""
    meta = EntryMeta.model_validate_json(reply)
    if meta.schema_ != ENTRY_SCHEMA:
        raise ValueError("Author response must use the ordinary entry schema")
    if text_max_ascii_chars is not None and (not meta.text.isascii() or len(meta.text) > text_max_ascii_chars):
        raise ValueError("Author response violates its declared ASCII text bound")
    paths = [item.path for item in meta.media]
    if len(paths) != len(set(paths)) or not set(paths) <= allowed_assets:
        raise ValueError("Author response selects duplicated or undeclared media")
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_STORED) as archive:
        for name, content in [
            ("entry.json", reply.encode("utf-8")),
            *[(name, asset_path(root, name)) for name in paths],
        ]:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            with archive.open(info, "w") as target:
                if isinstance(content, bytes):
                    target.write(content)
                else:
                    with content.open("rb") as source:
                        while chunk := source.read(1024**2):
                            target.write(chunk)


@dataclass
class AuthoredEntry:
    package: Path
    capture: dict[str, Any]


def materialize_author_package(
    submitted: Path,
    workdir: Path,
    *,
    slot: int,
    grader_config: JudgeConfig,
    capture: dict[str, Any],
    expected_input_digest: str = "",
    maximum_input_bytes: int | None = None,
) -> AuthoredEntry | None:
    if not zipfile.is_zipfile(submitted):
        return None
    with zipfile.ZipFile(submitted) as archive:
        candidates = [name for name in archive.namelist() if PurePosixPath(name).name == "authoring.json"]
        if not candidates:
            return None
        if len(candidates) != 1:
            raise ValueError("Author package must contain exactly one authoring.json")
        info = archive.getinfo(candidates[0])
        if info.file_size > 131072:
            raise ValueError("Author specification exceeds its byte limit")
        raw = archive.read(info)
        document = json.loads(raw)
        if not isinstance(document, dict) or document.get("schema") != AUTHOR_SCHEMA:
            return None  # Existing static packages do not opt in through arbitrary extra JSON files.
    capture.update(
        schema="marketing-native-author-capture/1",
        slot=slot,
        registered_input_sha256=digest(submitted),
        status="validating",
        calls=[],
    )
    if expected_input_digest and expected_input_digest.removeprefix("sha256:") != capture["registered_input_sha256"]:
        raise ValueError("Author input differs from the registered policy bytes")
    if maximum_input_bytes is not None and submitted.stat().st_size > maximum_input_bytes:
        raise ValueError("Author input exceeds the ordinary uploaded-file ceiling")
    spec = AuthorSpec.model_validate_json(raw)
    extracted = workdir / "author-input"
    problems = _safe_extract(submitted, extracted)
    if any(problem.startswith("fatal:") for problem in problems):
        raise ValueError("Invalid author input package: " + "; ".join(problems))
    root = extracted.joinpath(*PurePosixPath(candidates[0]).parent.parts)
    names = [item.path for item in spec.assets]
    if len(names) != len(set(names)):
        raise ValueError("Author input declares duplicate asset paths")
    assets = []
    for item in spec.assets:
        path = asset_path(root, item.path)
        assets.append(
            {
                "path": item.path,
                "context": item.context,
                "kind": media_kind_for(path),
                "bytes": path.stat().st_size,
                "sha256": digest(path),
            }
        )
    guide = system_prompt()
    user = {
        "schema": AUTHOR_SCHEMA,
        "brief": spec.brief,
        "assets": assets,
        "deliverable": "Return one softmax-post-entry/2 JSON object using only these media paths. No code fences.",
    }
    messages = [
        {"role": "system", "content": guide},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False, sort_keys=True)},
    ]
    body = {"model": spec.model, "messages": messages, "max_tokens": spec.max_tokens}
    response_format = bounded_entry_format(spec.text_max_ascii_chars) if spec.text_max_ascii_chars is not None else None
    if response_format is not None:
        body["response_format"] = response_format
    if spec.temperature is not None:
        body["temperature"] = spec.temperature
    if len(json.dumps(body).encode()) > MAX_REQUEST_BYTES:
        raise ValueError("Author request exceeds the pre-inference byte limit")
    calls: list[dict[str, Any]] = capture["calls"]
    capture.update(author_spec=spec.model_dump(mode="json", by_alias=True), messages=messages, status="generating")
    client = ModelClient(
        grader_config.model_copy(update={"max_tokens": spec.max_tokens, "timeout_seconds": spec.timeout_seconds}),
        on_call=calls.append,
    )
    client.model = spec.model  # The registered author policy selects its model, independently of the grader.
    if not client.available:
        raise ValueError("Native author endpoint is unavailable; no static fallback is permitted")
    reply = client.complete(messages, slot=slot, temperature=spec.temperature, response_format=response_format)
    capture["literal_reply"] = reply
    last = calls[-1]
    if last["http_status"] != 200 or not last["response_complete"] or not last["response_reader_joined"]:
        raise ValueError("Native author HTTP response is incomplete")
    payload = json.loads(last["raw_response"])
    choices = payload["choices"]
    if len(choices) != 1 or choices[0].get("finish_reason") != "stop":
        raise ValueError("Native author response must contain exactly one complete choice")
    message = choices[0]["message"]
    if (
        message.get("role") != "assistant"
        or not isinstance(message.get("content"), str)
        or message["content"] != reply
        or message.get("tool_calls")
        or message.get("function_call")
    ):
        raise ValueError("Native author response must be literal assistant text without tools")
    output = workdir / "authored-entry.zip"
    write_generated_package(reply, root, set(names), output, text_max_ascii_chars=spec.text_max_ascii_chars)
    semantic_input = {
        "guide_sha256": hashlib.sha256(guide.encode()).hexdigest(),
        "assets": sorted({item["sha256"] for item in assets}),
    }
    family = hashlib.sha256(json.dumps(semantic_input, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    capture.update(
        {
            "schema": "marketing-native-author-capture/1",
            "slot": slot,
            "status": "materialized",
            "registered_input_sha256": digest(submitted),
            "graded_package_sha256": digest(output),
            "scenario_family": "marketing-author-v1-" + family,
            "semantic_input": semantic_input,
            "author_spec": spec.model_dump(mode="json", by_alias=True),
            "messages": messages,
            "calls": calls,
            "literal_reply": reply,
            "parsed_entry": EntryMeta.model_validate_json(reply).model_dump(by_alias=True),
            "media_source": (
                "Registered asset bytes with operator-supplied descriptions; "
                "no image generation or pixel observation claimed"
            ),
        }
    )
    return AuthoredEntry(output, capture)
