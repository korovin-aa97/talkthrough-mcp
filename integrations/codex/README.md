# OpenAI Codex CLI

Server command (stdio): `uvx --python ">=3.11,<3.14" "talkthrough-mcp[diarization,url]"`

Config: `~/.codex/config.toml (or project-scoped .codex/config.toml in trusted projects)`

```toml
[mcp_servers.talkthrough]
command = "uvx"
args = ["--python", ">=3.11,<3.14", "talkthrough-mcp[diarization,url]"]
```

Skills: this repo ships the talkthrough skill at `.agents/skills/talkthrough/` — Codex discovers it automatically inside a checkout; for global use copy it to `~/.agents/skills/` and invoke with `$talkthrough`.

Optional env vars: TALKTHROUGH_WHISPER_MODEL (default `small`; use `large-v3-turbo` for non-English narration — agents can also pass `model=` per call), TALKTHROUGH_OCR (`off` to disable), TALKTHROUGH_OCR_LANG (on-screen-text script, e.g. `ru`, `ja`, `ko`), TALKTHROUGH_HOME (job store root, default `~/.talkthrough`), TALKTHROUGH_MAX_DOWNLOAD_BYTES (cap for `process_url` downloads, default 2 GiB). Speaker diarization is included but off per call — agents pass `diarize=true` (plus `num_speakers` when known). URL ingestion is the only tool that uses the network: `process_url(url)` downloads one public video/audio URL once (YouTube needs the `[url]` extra carried by this config); the minimal server without the diarization engine and without YouTube support is `uvx --python ">=3.11,<3.14" talkthrough-mcp`.

Verify: the client should list 9 tools (process_media, process_url, get_transcript, get_frames, get_moment, search, label_speakers, extract_frame, list_jobs). A `list_jobs` call returning an empty list is a healthy first run.

Engine docs: <https://developers.openai.com/codex/mcp>

Agent-followable install steps for any client: [`llms-install.md`](../../llms-install.md).
