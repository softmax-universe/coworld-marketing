# Softmax Marketing

A continuous Coworld where people and agents compete to write the X posts the Softmax research account publishes. Submit a post; an autograder that encodes the team's taste scores it within a couple of minutes; the best unpublished post is the next one the team puts on the feed; once it is live, the audience scores the other half. Half judge, half engagement, always on.

Players submit posts, not programs. The Coworld uses the game-hosted runtime: your "policy" is a small directory with `entry.json` (the post text) and optional media (up to four pictures, or one gif, or one short video), uploaded with `coworld upload-policy --file`. A plain text file works too. No Docker image is needed to enter.

## The game

- One episode grades one or more posts. The league runs a one-seat `grade` episode for every new submission as it arrives (and again for published posts when their engagement is refreshed); the `showcase` variant judges up to eight side by side for comparisons.
- The game validates each post against X's limits and measures its media with ffmpeg, then runs the autograder: a deterministic technical panel (25 percent) and a craft panel (75 percent) in which a model reads the post as it would appear, against the house rubric. That is the judge half.
- The engagement half comes from an engagement feed the marketing agent writes outside the platform: impressions, likes, reposts and replies of the posts the team has published, matched to entries by their text. Until a post is published, its engagement is the room: ship votes on the forum post the submission links to, 10 points a ship, capped at 100.
- `results.scores` is one number per seat from 0 to 100: judge and engagement blended, half and half. `results.pick` is the next post to publish: the best judge score among eligible, unpublished entries at or above 70. Details in `docs/JUDGING.md`.
- The replay is the jury page: each post as a card, its media, its live metrics when published, both panels' findings, and the pick's text ready to copy.

## Make a post

Read `docs/POLICY_GUIDE.md`. It is the default policy guide and is written to be used as the system prompt for an agent writing these posts: what Softmax is, what the team has found cringe, what has held, the deliverable spec, and how the judge scores.

Then:

```bash
# layout
my-post/
  entry.json           {"schema":"softmax-post-entry/2","text":"...","media":[{"path":"one.png","alt_text":"..."}]}
  one.png              optional: up to four images, or one gif, or one video; listed in entry.json in order

# check it the way the game will
marketing-check ./my-post            # pip install . in this repo, or: python -m marketing.check ./my-post

# upload and submit
uv run coworld upload-policy --file ./my-post --name my-post
uv run coworld submit my-post --league <league_id>
```

`entry.json` fields and limits are in `docs/player_protocol.md`. A bare `.txt` is a text-only post; a bare image or video is media with no text (and loses the points for text).

Known Opus5.5 grader requests omit its unsupported temperature control before HTTP. This matches the payload already used after its compatibility retry; panels, rubric, retries and scoring stay the same. Other models and explicit author-model overrides preserve their sampling controls.

## Optional native model authoring

Version 0.4.0 adds an explicit `authoring.json` input package. The native game asks the declared model for ordinary post JSON through its existing model endpoint, packages that literal response with selected original media, then applies the ordinary loader and grader. Static post submissions retain their existing behavior. Author failures produce an ineligible entry and cannot fall back to a placeholder.

See [the author package protocol](docs/AUTHORING.md) for input, private evidence, costs and limits. This source feature does not deploy a hosted world, publish a post, establish authentic training targets or qualify a model for posttraining.

## Bundled baselines

Three entries ship as the certification players and the floor to beat:

- `players/the-wall`: a video post, "The Wall", the 77 s narrated fable the team approved, with its post text.
- `players/read-the-room`: a video post, the 49 s "Read the Room" teaser the team called mostly cringe. The judge should rank it below The Wall.
- `players/plain-post`: a text-only post in the house voice, so the game's text path is exercised with no media at all.

## Run locally

Requires Docker, ffmpeg and the Coworld CLI (`uv run coworld ...` from a metta checkout, or `pip install coworld`).

```bash
python tools/sync_docs.py                                   # inline docs into the manifest template
uv run coworld build --project . --version 0.4.1            # -> dist/coworld_manifest.json
uv run coworld run-episode dist/coworld_manifest.json       # certification fixture: three baselines, technical judge
uv run coworld run-episode dist/coworld_manifest.json ./players/plain-post ./my-post   # yours against a baseline
uv run coworld certify dist/coworld_manifest.json
```

`run-episode` prints the artifact directory: `results.json`, the replay, a private log per seat and a per-seat artifact zip. Open the replay with `uv run coworld replay dist/coworld_manifest.json <replay-file>`.

To run the craft panel locally, give the game a model endpoint. Hosted episodes use the Coworld LLM sidecar automatically; locally the game reads `JUDGE_API_KEY` (or `OPENROUTER_API_KEY`), `JUDGE_API_BASE` (default OpenRouter) and `JUDGE_MODEL`. To exercise the engagement half locally, point `ENGAGEMENT_FEED_URI` at a feed file (`marketing-agent once --feed-out feed.json ...` writes one, or hand-write one in the `softmax-engagement-feed/1` shape shown in `marketing/engagement.py`).

Tests: `uv venv && uv pip install -e '.[dev]' && .venv/bin/pytest`.

## Variants

| id | seats | judge | use |
| --- | --- | --- | --- |
| `grade` | 1 | panel | the continuous league's grading episode (default) |
| `showcase` | 8 | panel | side-by-side comparisons, experience requests |
| `offline-technical` | 2 | technical | free local smoke runs |

Certification seats the three baselines with the technical judge so it runs offline.

## League and agent

`docs/LEAGUE.md` has the continuous-ladder settings (`league/ladder_settings.json`: grade on arrival, `latest` standing, one seat per user), the room-votes convention (a submission's `notes` holds `{"post_id": "post_..."}`) and the posting loop. The marketing agent (`marketing-agent`, `marketing/agent.py`) is the long-running piece outside the platform: it matches published posts to graded entries, pulls their metrics from X, reads each entry's room ships from its linked forum post, writes the engagement feed into the Coworld secret the game reads, and asks the league to re-grade published posts and posts whose ships moved. Publishing itself stays a human act.

## Layout

```
compose.yaml, coworld_manifest_template.json, Dockerfile
marketing/        game server, entry loader, ffmpeg probe, judge, engagement, agent, rubric.md, static/ (jury and player pages)
players/          bundled file players (entry.json + media)
docs/             POLICY_GUIDE.md, JUDGING.md, LEAGUE.md, player_protocol.md, global_protocol.md
tools/            build_replay_viewer.sh (static bundle hook), sync_docs.py, league_setup.py
league/           ladder_settings.json
tests/
```
