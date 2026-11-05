# Native post author packages

An explicitly opted-in input ZIP can contain `authoring.json` and a bank of ordinary media assets. Static `entry.json`, plain text and bare-media submissions keep their existing intake and grading path. A recognized author specification uses this shape:

```json
{
  "schema": "softmax-post-author/1",
  "model": "provider/model-id",
  "brief": "Write one post from the supplied asset and its factual context.",
  "assets": [{"path": "clip.mp4", "context": "An operator description of this exact clip."}],
  "max_tokens": 1024,
  "temperature": null,
  "timeout_seconds": 90
}
```

Use the registered model ID available to the native endpoint. Supplying a model ID does not create credentials or authorization. Null temperature omits that parameter. The author request uses the packaged canonical `POLICY_GUIDE.md` as its system message (excluding the informational introduction before the first separator, matching the existing research author gate) and a user message containing the brief and each asset's original byte digest, type, size and operator description. No image generation or pixel observation is claimed. The grader retains its own model and configuration.

The model must return one complete assistant string containing an ordinary `softmax-post-entry/2` JSON object, without tools, code fences or undeclared media paths. The game preserves those literal JSON bytes and selected media bytes in a reproducible ZIP, then runs the original loader, media measurements, rubric, engagement calculation and postability threshold. Authoring errors are ineligible; a static placeholder cannot rescue them. The ordinary uploaded-file ceiling also applies to an author package. Asset paths cannot escape the ZIP. Requests are bounded to 128 KiB; briefs to 8,000 characters; asset declarations to 32; output to 200–4,000 tokens; timeout to 300 seconds. These limits do not authorize paid calls.

An episode may incur one author call in addition to its ordinary grader calls; the existing client's sampling-parameter compatibility retry remains captured. Before hosted execution, the operator must separately authorize and bound all author, retry, grader, worker and storage costs. This protocol makes no league submission or external post. Publishing remains a human act.

The private seat log contains `MARKETING_NATIVE_AUTHOR_CAPTURE` followed by the request, exact response body/header pairs, completion status, observed platform call ID, literal reply, input/output digests and family identity. If `COGAME_SAVE_TRAJECTORY_URI` is configured, the episode also writes a private `marketing-native-authoring-episode/1` capture. The public replay excludes the private brief and author request. Registered input ZIP, generated post ZIP and final judge-decorated artifact are distinct objects; retain and authenticate their identities separately.

These captures are evidence, not authenticated training receipts. A future collector must join the original registered participant, engine/source, episode, input package and observed call ID to genuine service receipts, verify the generated package and native result, and run the standard training admission path. Never admit grader calls as authors or relabel another engine's receipt as Marketing. Loopback tests create synthetic call IDs and supply no genuine teacher targets.

A physical scenario family hashes the canonical guide and sorted unique asset-byte identities. Brief wording, path aliases, model and decoder changes do not create independent families. In particular, all text-only author packages share one asset-free family; use genuinely disjoint media banks for independent training/evaluation evidence.

The source test suite runs complete ordinary episodes against a loopback author/grader, including image/video preservation and refusal of wrong input binding, malformed or undeclared output, truncation, HTTP error, nonliteral content, tools and an incomplete response body. It also retains the original static certification, manifest, score and media checks. These checks establish source behavior only; hosted release, authentic receipts, useful real-model outcomes and learning remain separate requirements.
