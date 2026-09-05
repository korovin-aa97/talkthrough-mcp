# URL acceptance corpus for `process_url`

The live URLs behind the release QA of URL ingestion. CI stays offline (unit
tests stub the network); this corpus is for **manual regression runs** before
a release and after a yt-dlp bump. Pages change hands, get deleted, or change
layout: an entry that fails is a finding to look at, not a broken build.
State recorded on **2026-09-05** against 0.4.0 / 0.4.1-rc, yt-dlp 2026.8.19.

## How to run

Always against a throwaway store, and with caps that stop a run as soon as
the path under test has been exercised:

```bash
export TALKTHROUGH_HOME="$(mktemp -d)"
export TALKTHROUGH_WHISPER_MODEL=tiny
# full runs: leave the caps at their defaults
talkthrough-mcp process-url "<url>" --json
# cap/refusal paths: stop before or right after the first bytes
TALKTHROUGH_MAX_DOWNLOAD_BYTES=1 talkthrough-mcp process-url "<url>" --json
TALKTHROUGH_MAX_SECONDS=120 talkthrough-mcp process-url "<url>" --json
talkthrough-mcp gc --keep-days 30   # must leave urls/ without orphan .lock files
```

Statuses: **FULL** — the source was downloaded and the local pipeline
finished; **CAP** — the provider or direct file was recognised, then stopped
on purpose by a small byte or duration cap; **FAIL** — a real provider/URL
error with a readable reason; **NEG** — a negative contract case.

Every run must satisfy the contract regardless of status: no raw URL, query
or userinfo in stdout, stderr, the manifest, `urls/`, or the MCP response
(use a harmless canary such as `?qa_token=TTSECRET4242` and grep for it);
no `.part`, job or index entry after a failure; `--json` yields one JSON
document on stdout on both outcomes.

## 0.4.1 regressions (run first)

| Finding | URL | Expect |
|---|---|---|
| F-01 log leak | `https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4?qa_token=TTSECRET4242` with `TALKTHROUGH_MAX_DOWNLOAD_BYTES=1024` | the cap error; `TTSECRET4242` and `HTTP Request` absent from stderr |
| F-02 multi-video page | `https://www.loom.com/share/folder/997db4db046f43e5912f10dc5f817b5c` with `TALKTHROUGH_MAX_DOWNLOAD_BYTES=1` | `the page contains N videos — pass a link to one video`, no `downloading source` stage |
| F-03 refresh metadata | `https://youtu.be/nHfGfEiVdE8` once; edit `media.origin.title` in the manifest; run again with `--refresh` | the title comes back from the provider, `downloaded_at` is fresh, same job id |
| F-04 JSON errors | any refused URL with `--json` | exit 2, `{"error": {"type": …, "message": …}}` on stdout |
| F-05 extractor crash | `https://www.ted.com/talks/candace_parker_how_to_break_down_barriers_and_not_accept_limits` | `the page reader failed on https://www.ted.com/… (TypeError: …)` with the two ways out (until yt-dlp fixes its TED extractor) |
| F-07 lock files | after the runs above, `talkthrough-mcp gc --keep-days 30` | reports `urls/<key>.lock` for the refused/failed URLs; only locks of live mappings remain |
| F-08 version | `talkthrough-mcp --version` | `talkthrough-mcp <v> (python …; url extra: yt-dlp …; diarization extra: sherpa-onnx …)` |

## Full end-to-end

| Status | URL | Result on 2026-09-05 |
|---|---|---|
| FULL | https://youtu.be/nHfGfEiVdE8 (official demo) | 78.041 s, STT + frames + OCR |
| FULL | https://www.tiktok.com/@neildegrassetyson/video/7482856405934918955 | 53.717 s, 14 segments, 52 unique frames |
| FULL | https://www.instagram.com/reel/DZYDt3_pEH4/ | 86.870 s, speechless, 102 unique frames |
| FULL | https://commons.wikimedia.org/wiki/File:Caminandes-_Llama_Drama_-_Short_Movie.ogv | 89.908 s, OGV, 107 unique frames |
| FULL | https://www.reddit.com/r/videos/comments/6rrwyj/that_small_heart_attack/ | 11.796 s, 9 unique frames |
| FULL | https://www.loom.com/share/43d05f362f734614a2e81b4694a3a523 | 27.047 s, 19 unique frames |
| FULL | https://vk.com/video205387401_165548505 | 9.686 s, 13 unique frames |
| FULL | https://www.facebook.com/reel/1195289147628387 | 9.567 s, no audio stream, 16 unique frames |
| FULL | https://soundcloud.com/jaimemf/youtube-dl-test-video-a-y-baw/s-8Pjrp | 9.87 s audio job |
| FULL | https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/360/Big_Buck_Bunny_360_10s_1MB.mp4 | 10 s H.264, 5 unique frames |
| FULL | https://test-videos.co.uk/vids/bigbuckbunny/mp4/av1/360/Big_Buck_Bunny_360_10s_1MB.mp4 | 10 s AV1, 5 unique frames |
| FULL | https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.webm | 5.059 s VP8, 4 unique frames |
| FULL | https://storage.googleapis.com/shaka-demo-assets/angel-one-widevine/v-0240p-0400k-libx264.mp4 | 60 s video-only, 36 unique frames |
| FULL | https://samplelib.com/lib/preview/mp3/sample-3s.mp3 | 3.196 s audio |
| FULL | https://interactive-examples.mdn.mozilla.net/media/cc0-audio/t-rex-roar.mp3 | 2.074 s audio |
| FULL | https://upload.wikimedia.org/wikipedia/commons/c/c8/Example.ogg | 6.104 s audio |

## Recognised, then stopped by a cap

| Status | URL | Observation |
|---|---|---|
| CAP | https://youtu.be/ERLlHNiX_d4 | 62 s metadata, byte estimate caught before the download |
| CAP | https://streamable.com/dnd1 | 61.516 s video recognised |
| CAP | https://drive.google.com/file/d/0ByeS4oOUV-49Zzh4R1J6R09zazQ/edit?pli=1 | 45.069 s media recognised |
| CAP | https://bsky.app/profile/bsky.app/post/3l3vgf77uco2g | metadata present; one m3u8 probe gave a 30 s timeout warning |
| CAP | https://archive.org/details/Cops1922 | duration cap: 1092 s > test cap of 120 s |
| CAP | https://clips.twitch.tv/FaintLightGullWholeWheat | 32 s media recognised; byte cap during the download |
| CAP | https://www.bilibili.com/bangumi/play/ep21495/ | duration 1421 s capped; one probe answered HTTP 412 |
| CAP | https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4 | direct Content-Length cap before the body |
| CAP | https://samplelib.com/lib/preview/wav/sample-3s.wav | direct Content-Length cap |
| CAP | https://filesamples.com/samples/audio/flac/sample1.flac | streaming cap without Content-Length |
| CAP | https://filesamples.com/samples/audio/wav/sample1.wav | streaming cap without Content-Length |
| CAP | https://filesamples.com/samples/audio/m4a/sample1.m4a | streaming cap |
| CAP | https://filesamples.com/samples/video/mov/sample_640x360.mov | streaming cap |
| CAP | https://filesamples.com/samples/video/mkv/sample_640x360.mkv | streaming cap |
| CAP | https://filesamples.com/samples/video/ogv/sample_640x360.ogv | streaming cap |
| CAP | https://devstreaming-cdn.apple.com/videos/streaming/examples/img_bipbop_adv_example_fmp4/master.m3u8 | yt-dlp metadata, 600 s duration cap |
| CAP | https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8 | yt-dlp metadata, 634 s duration cap |
| CAP | https://storage.googleapis.com/shaka-demo-assets/sintel/dash.mpd | DASH download path reached the byte cap |

## Failures and negative cases

| Status | URL | Result |
|---|---|---|
| FAIL | https://vimeo.com/423808888 | anonymous extractor needs a login; clean refusal |
| FAIL | https://www.dailymotion.com/video/x2iuewm_steam-machine-models-pricing-listed-on-steam-store-ign-news_videogames | asset deleted upstream: Not found |
| FAIL | https://www.ted.com/talks/candace_parker_how_to_break_down_barriers_and_not_accept_limits | yt-dlp TED extractor crashes on the current page (F-05 wording) |
| FAIL | https://rutube.ru/video/3eac3b4561676c17df9132a9a1e62e3e/ | no formats / connection reset |
| FAIL | https://imgur.com/crGpqCV | metadata 404/timeout |
| FAIL | https://i.imgur.com/crGpqCV.mp4 | redirects to HTML, then metadata 404 |
| FAIL | https://samplelib.com/lib/preview/flac/sample-3s.flac | provider HTTP 403 |
| NEG | https://www.loom.com/share/folder/997db4db046f43e5912f10dc5f817b5c | multi-video page: refused since 0.4.1 (F-02) |
| NEG | https://www.youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf | refused as a playlist by classification, no network |
| NEG | https://www.youtube.com/watch?v=jfKfPfyJRdk | active live stream: refused |
| NEG | https://youtu.be/AAAAAAAAAAA | unavailable id: provider error with a reason |

## Convergence and extra regression URLs

- https://www.youtube.com/shorts/nHfGfEiVdE8 and
  https://www.youtube.com/embed/nHfGfEiVdE8 must serve the official demo's
  stored job without network (`reused_url_mapping: true`).
- https://upload.wikimedia.org/wikipedia/commons/d/d0/Caminandes-_Llama_Drama_-_Short_Movie.ogv
  is the direct counterpart of the Wikimedia file page above; if the upstream
  filename changes, take the new one from the file page first.
- https://download.blender.org/durian/trailer/sintel_trailer-480p.mp4 — a
  longer direct H.264 file with sound.
- Two concurrent calls on one direct MP3 URL must produce one job: one caller
  downloads, the other finds the mapping.

Part of the direct-stream URLs come from the public
[video-commander/public-test-streams](https://github.com/video-commander/public-test-streams)
set.
