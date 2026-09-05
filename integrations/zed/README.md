# Zed

Server command (stdio): `uvx --python ">=3.11,<3.14" "talkthrough-mcp[diarization,url]"`

Config: `settings.json (Zed)`

```json
{
  "context_servers": {
    "talkthrough": {
      "source": "custom",
      "command": {
        "path": "uvx",
        "args": [
          "--python",
          ">=3.11,<3.14",
          "talkthrough-mcp[diarization,url]"
        ]
      }
    }
  }
}
```

Optional env vars: TALKTHROUGH_WHISPER_MODEL (default `small`; use `large-v3-turbo` for non-English narration — agents can also pass `model=` per call), TALKTHROUGH_OCR (`off` to disable), TALKTHROUGH_OCR_LANG (on-screen-text script, e.g. `ru`, `ja`, `ko`), TALKTHROUGH_HOME (job store root, default `~/.talkthrough`), TALKTHROUGH_MAX_DOWNLOAD_BYTES (cap for `process_url` downloads, default 2 GiB). Speaker diarization is included but off per call — agents pass `diarize=true` (plus `num_speakers` when known). URL ingestion is the only tool that uses the network: `process_url(url)` downloads one public video/audio URL once (YouTube needs the `[url]` extra carried by this config); the minimal server without the diarization engine and without YouTube support is `uvx --python ">=3.11,<3.14" talkthrough-mcp`.

Verify: the client should list 9 tools (process_media, process_url, get_transcript, get_frames, get_moment, search, label_speakers, extract_frame, list_jobs). A `list_jobs` call returning an empty list is a healthy first run.

Engine docs: <https://zed.dev/docs/ai/mcp>

Agent-followable install steps for any client: [`llms-install.md`](../../llms-install.md).
