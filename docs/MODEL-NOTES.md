# Model compatibility notes — talkthrough-mcp v0.2.0

*Snapshot: July 2026. A methodology and a data cut, not a leaderboard —
models drift, sample sizes are small (n in every cell), and the corpus
is one team's real recordings. Read it as "what tier of agent reliably
drives this MCP for which job".*

## v0.2.1 addendum (feature-grid battery, 2026-07-18)

A 36-run feature grid re-ran on the v0.2.1 server — all six v0.2.0 runner
configs (haiku / sonnet / opus / gpt-5.5 medium / gpt-5.5 high /
gpt-5.4-mini) × the tasks the release touches: T5 slide-hunt on three real
recordings (73-min RU meeting, 26-min EN call, 43-min EN workshop), T0s
naive bug-screencast triage, T6 findings-JSON triage, T2 point lookup on
the 35-min interview. Same verbatim prompts and mechanical evidence checks
as v0.2.0; every mechanical zero was adjudicated by reading the raw output
(the v0.2.0 judge-audit rule — it caught three checker artifacts, no agent
failures). Results:

- **T5: 18/18 runs returned an existing screenshot path** across every
  tier and recording (v0.2.0 had sonnet at 1.0 and opus at 1.5 here). One
  run (sonnet, RU meeting) cited the new validity span verbatim — picked
  the summary slide *because* it "stays on screen for ~6.6 minutes
  (3283500–3681375 ms) — the longest of any slide" — turning a
  `duplicate_of`-chain inference into a payload lookup. gpt-5.5 used
  `extract_frame` for an exact instant. Spontaneous span *citation* stayed
  rare (1/18): the data is served to and consumed by all models, but only
  the stronger tiers surface it as justification.
- **T0s: 6/6 runs did no wasteful diarization** on the single-narrator
  screencast (the inverse invariant), and the Claude runs on a
  v0.2.1-processed job quoted the on-screen Russian text («Я готовлю вашу
  заявку…») in their findings — strings absent from the OCR index before
  the auto-selected `eslav` pack.
- **T6: 6/6 runs produced evidence-backed findings JSON** — real frame
  files cited, narrator quotes verbatim against the transcript.
  gpt-5.4-mini deviated from the canonical key names (`quotes[]`/
  `evidence[]` instead of `quote`/`frame_refs`) — content correct, schema
  loose, consistent with its v0.2.0 profile.
- **T2: 6/6** named the right speaker with an exact timestamp (one
  checker false-negative: gpt-5.5-high answered in `hh:mm:ss.mmm`).
- **Harness note for future batteries:** `codex exec` does NOT pass the
  parent env to its MCP servers, so `TALKTHROUGH_HOME` isolation silently
  fell back to the real store for the codex runs (read-only tasks, store
  mtimes verified untouched). Declare env inside the codex MCP-server
  config next time.
- **Vocabulary honesty note** (engine-level, no agents): re-transcribing
  the 73-min RU meeting on `small` with attendee names in `vocabulary`
  kept every ground-truth name verbatim at its reference point («Влад,
  Дим, дайте какую-то обратную связь», «Меня зовут Александр Коровин»)
  with zero look-alike substitutions — but whisper echoed the name list
  into the first ~60 s of quiet opening chatter (a known `initial_prompt`
  trait, present since 0.1.0). If you pass `vocabulary`, treat the opening
  seconds of the transcript with suspicion before quoting them.

## Method

132 agent runs: 6 runner configs (3 Claude models, Codex
gpt-5.5 at two reasoning efforts + gpt-5.4-mini) × 10 task prompts
(verbatim-identical across runners) × 5 recordings (real meetings and
screencasts, 30 s – 73 min, RU/EN, 1–5 true speakers, counts confirmed
by the recording owner). Scoring: an LLM judge with a strict rubric
(fabricated names/quotes = 0) plus mechanical evidence checks — every
quoted span string-matched into the transcript+OCR, every returned file
path checked on disk, expected speaker labels precomputed from the
manifests. Scores: 2 = correct and fully evidenced, 1 = partial, 0 =
failed or fabricated. Judge verdicts were audited; instrument errors
were fixed and adjudications are marked in the raw data.

## Matrix 1 — task × model (mean score, n)

| Task | haiku (default) | sonnet (default) | opus (default) | gpt-5.5 (medium) | gpt-5.5 (high) | gpt-5.4-mini (low) |
|---|---|---|---|---|---|---|
| T0 — Naive «analyze this meeting» (zero hints) | **1.0** (2)<br>389s · 568k tok | **0.5** (2)<br>146s · 429k tok | **1.5** (2)<br>216s · 287k tok | **1.0** (2)<br>82s · 48k tok | **2.0** (2)<br>93s · 129k tok | **1.5** (2)<br>42s · 34k tok |
| T0s — Naive «triage this bug screencast» (inverse: must NOT diarize) | **1.0** (1)<br>69s · 255k tok | **2.0** (1)<br>205s · 532k tok | **2.0** (1)<br>307s · 908k tok | **2.0** (1)<br>80s · 60k tok | **2.0** (1)<br>69s · 80k tok | **1.0** (1)<br>108s · 56k tok |
| T1 — Ingest with who-said-what intent (parameter choice) | **2.0** (3)<br>31s · 87k tok | **1.3** (3)<br>30s · 114k tok | **1.7** (3)<br>42s · 79k tok | **1.7** (3)<br>54s · 51k tok | **1.0** (3)<br>56s · 41k tok | **1.7** (3)<br>31s · 21k tok |
| T2 — Point lookup: who said <known quote> + when | **2.0** (3)<br>19s · 110k tok | **2.0** (3)<br>15s · 111k tok | **2.0** (3)<br>33s · 108k tok | **2.0** (3)<br>22s · 34k tok | **2.0** (3)<br>33s · 35k tok | **2.0** (3)<br>23s · 29k tok |
| T3 — Map speaker labels to real names (evidence required) | **1.3** (3)<br>187s · 1022k tok | **1.7** (3)<br>358s · 982k tok | **0.7** (3)<br>363s · 1549k tok | **1.7** (3)<br>159s · 181k tok | **2.0** (3)<br>240s · 247k tok | **1.0** (3)<br>56s · 78k tok |
| T4 — Meeting minutes with owners (source language) | **0.0** (2)<br>75s · 393k tok | **0.5** (2)<br>303s · 262k tok | **2.0** (2)<br>510s · 1345k tok | **0.5** (2)<br>77s · 104k tok | **2.0** (2)<br>114s · 188k tok | **0.5** (2)<br>63s · 69k tok |
| T5 — Find the key slide, return screenshot path | **2.0** (2)<br>64s · 593k tok | **1.0** (2)<br>55s · 318k tok | **1.5** (2)<br>136s · 257k tok | **2.0** (2)<br>54s · 102k tok | **2.0** (2)<br>62s · 72k tok | **2.0** (2)<br>35s · 67k tok |
| T6 — Bug triage to findings JSON (verbatim quotes) | **1.0** (1)<br>64s · 203k tok | **2.0** (1)<br>195s · 319k tok | **2.0** (1)<br>192s · 151k tok | **2.0** (1)<br>66s · 52k tok | **2.0** (1)<br>83s · 59k tok | **2.0** (1)<br>63s · 52k tok |

*Cell: **mean score** (n judged)<br>median wall · median tokens.
Token accounting differs per family (Anthropic API usage incl. cache
vs Codex self-reported totals) — compare within a column family, not
across.*

T7 failure literacy is checked mechanically, not judged: nonexistent
job id → clean error surfaced, nothing fabricated: **18/18**;
YouTube URL → refused per design: **12/12**.

## Matrix 2 — recording × model (mean score, n)

| Recording | len | lang | speakers | haiku | sonnet | opus | gpt-5.5 | gpt-5.5 | gpt-5.4-mini |
|---|---|---|---|---|---|---|---|---|---|
| 73-min RU team meeting (1 dominant presenter + Q&A) | 73m | ru | 5 | **1.3** (6)<br>52s · 370k tok | **1.3** (6)<br>55s · 209k tok | **1.5** (6)<br>149s · 195k tok | **1.2** (6)<br>62s · 63k tok | **1.8** (6)<br>89s · 111k tok | **1.7** (6)<br>30s · 32k tok |
| 26-min EN knowledge-transfer call | 26m | en | 3 | **1.5** (2)<br>23s · 165k tok | **2.0** (2)<br>24s · 237k tok | **1.0** (2)<br>37s · 200k tok | **2.0** (2)<br>27s · 35k tok | **2.0** (2)<br>30s · 32k tok | **1.0** (2)<br>24s · 35k tok |
| 43-min EN UX-research workshop (fast turn-taking) | 43m | en | 5 | **1.5** (6)<br>37s · 188k tok | **0.8** (6)<br>38s · 226k tok | **1.7** (6)<br>62s · 158k tok | **1.7** (6)<br>57s · 41k tok | **1.7** (6)<br>52s · 52k tok | **1.3** (6)<br>36s · 30k tok |
| 2-min RU narrated bug screencast | 2m | ru | 1 | **1.0** (2)<br>66s · 229k tok | **2.0** (2)<br>200s · 426k tok | **2.0** (2)<br>250s · 530k tok | **2.0** (2)<br>73s · 56k tok | **2.0** (2)<br>76s · 70k tok | **1.5** (2)<br>86s · 54k tok |
| 30-sec EN two-voice synthetic fixture | 0.5m | en | 2 | **2.0** (1)<br>31s · 86k tok | **2.0** (1)<br>29s · 114k tok | **2.0** (1)<br>40s · 78k tok | **2.0** (1)<br>45s · 10k tok | **2.0** (1)<br>34s · 21k tok | **2.0** (1)<br>31s · 18k tok |

## Matrix 3 — corpus axes (all models pooled)

*The axes are cuts of one corpus, not a controlled experiment — length,*
*language and speaker count correlate across these five recordings.*

- **Duration** — 0.5 min: 2.0 (n=6) · 2 min: 1.8 (n=12) · 26 min: 1.6 (n=12) · 43 min: 1.4 (n=36) · 73 min: 1.5 (n=36)
- **Language** — en: 1.5 (n=54) · ru: 1.5 (n=48)
- **True speakers** — 1: 1.8 (n=12) · 2: 2.0 (n=6) · 3: 1.6 (n=12) · 5: 1.5 (n=72)

### Cross-tab: true speakers × model (mean score)

| true speakers | haiku | sonnet | opus | gpt-5.5 | gpt-5.5 | gpt-5.4-mini |
|---|---|---|---|---|---|---|
| 1 | 1.0 | 2.0 | 2.0 | 2.0 | 2.0 | 1.5 |
| 2 | 2.0 | 2.0 | 2.0 | 2.0 | 2.0 | 2.0 |
| 3 | 1.5 | 2.0 | 1.0 | 2.0 | 2.0 | 1.0 |
| 5 | 1.4 | 1.1 | 1.6 | 1.4 | 1.8 | 1.5 |

### Cross-tab: duration × model (mean score)

| duration | haiku | sonnet | opus | gpt-5.5 | gpt-5.5 | gpt-5.4-mini |
|---|---|---|---|---|---|---|
| 0.5m | 2.0 | 2.0 | 2.0 | 2.0 | 2.0 | 2.0 |
| 2m | 1.0 | 2.0 | 2.0 | 2.0 | 2.0 | 1.5 |
| 26m | 1.5 | 2.0 | 1.0 | 2.0 | 2.0 | 1.0 |
| 43m | 1.5 | 0.8 | 1.7 | 1.7 | 1.7 | 1.3 |
| 73m | 1.3 | 1.3 | 1.5 | 1.2 | 1.8 | 1.7 |

## Matrix 4 — behavior before/after server+guidance fixes

The battery is also how v0.2.0 was hardened; three shipped fixes came
out of it, each verified by re-running the failing scenario:

| Behavior | Before | After |
|---|---|---|
| Naive «analyze this meeting» → speaker labels included | Claude 1–3/6 runs, Codex 0/6 | **Claude 5/6** (sonnet/opus 2/2 stable); Codex unchanged — see the payload note |
| Raw threshold cluster count reported as headcount («28 participants», «123 speakers») | 3 models affected | **0/12 re-runs** — server now serves `speakers_with_30s_plus` + a note |
| Naive bug screencast triggers pointless diarization | n/a (guard test) | **0/6** — short single-voice recordings stay cheap |

**The transferable finding:** prose in tool descriptions reached only the
Claude runners; the Codex runners ignored every description-level nudge —
but data fields in the tool *response* were read by every model tested.
If you are building an MCP server: put the facts agents must not miss
into the payload, not the description.

## Engine-level: speaker detection by recording (no agents involved)

| Recording | true | threshold mode (0.5) | with `num_speakers` |
|---|---|---|---|
| 30-sec EN fixture | 2 | 2 | 2/2 exact turns |
| 2-min RU screencast | 1 | **1** (no false split) | 1 |
| 26-min EN call | 3 | ~30 clusters, 3 dominant | **3 clean** (k=3) |
| 43-min EN workshop | 5 | 123 clusters, 13 ≥30 s | **5 clean** (k=5) |
| 73-min RU meeting | 5 | 28 clusters, 5 ≥30 s | 5 (k=5; quiet 5th ≈ dust) |
| 35-min EN interview | 2 | 53 clusters, 6 ≥30 s | 2 dominant (agent-picked k) |

Threshold mode over-segments real meetings (documented); `num_speakers`
collapses it every time — which is why the guidance pushes agents to pass
it and the server reports `speakers_with_30s_plus` when it can't.

## What a user actually waits

| Scenario (M-series CPU, whisper `small`) | Wall |
|---|---|
| «Analyze this 43-min meeting», cold file, zero hints → full summary | ~10 min |
| «Analyze this 35-min interview», cold → names, roles, insights | ~12 min |
| Add speakers to an already-processed 73-min meeting (amend) | ~6 min |
| Re-ask anything on a processed recording | seconds |
| Diarization stage alone | RTF ≈ 0.08 (26 min → ~2 min), ~1–1.5 GB peak RSS on hour-plus files |

## Practical model guidance

- **Point lookups and search** («who said X», «find the slide») worked on
  every tier tested — 18/18 on the known-quote task including the smallest.
- **Meeting minutes with owners** and **evidence-disciplined name mapping**
  want the top tiers (opus / gpt-5.5-high); mid tiers partially succeed,
  small tiers either fabricate or refuse.
- **Reasoning effort matters more than family**: gpt-5.5 medium→high moved
  64%→88% full-pass on identical prompts.
- Failure literacy was universal: every model surfaced our error messages
  verbatim instead of hallucinating around them — write actionable errors.

