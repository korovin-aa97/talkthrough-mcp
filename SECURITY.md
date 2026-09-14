# Security Policy

## Supported versions

Only the latest released version is supported with security fixes.

## Reporting a vulnerability

Please use this repository's
[private vulnerability reporting form](https://github.com/korovin-aa97/talkthrough-mcp/security/advisories/new).
This sends the report privately to the maintainer; do not include vulnerability
details in a public issue. If the private form is unavailable, open a minimal
public issue that says "security — requesting private contact" without details,
and a private channel will be arranged.

You can expect an acknowledgement within 3 business days. Please include a
reproduction and your assessment of impact. We will coordinate disclosure with
you and aim to publish a fix or status update within 90 days, depending on the
severity and complexity of the vulnerability.

## Threat-model notes for reporters

Things this project treats as security-relevant:

- The MCP server executes ffmpeg/ffprobe on user-supplied media paths —
  path handling, argument injection, and decoder crashes on malicious media
  are in scope.
- `process_media` accepts arbitrary local paths from the connected agent; the
  server intentionally runs with the invoking user's privileges. Anything
  that lets a crafted *file* (as opposed to the user's own agent) escalate
  is in scope.
- The privacy promise (no runtime network beyond one-time model/tool
  downloads and the one explicit `process_url` download, no telemetry, no
  upload of media anywhere) — any violation is treated as a vulnerability.
- `process_url` is the server's only runtime network boundary. In scope:
  server-side request forgery (a URL, a redirect or a DNS answer that
  reaches a private, loopback, link-local or cloud-metadata address), the
  redirect and DNS re-validation on every hop, leakage of the raw URL or its
  signed query/userinfo into a manifest, the URL index, a Talkthrough log
  line, progress message or tool-body error, bypasses of the byte/duration/disk/redirect
  caps, hostile remote media or metadata reaching ffmpeg/ffprobe or a
  filename, and the downloader dependency (`yt-dlp`, `deno`) being driven
  with anything other than the allowlisted option set (no user config, no
  plugins, no cookies, no remote JavaScript components).
- Known residual, by design: for a video *page* (any site yt-dlp can read)
  Talkthrough gates the host the user named; the embedded players and
  redirects that yt-dlp then follows use yt-dlp's own client and are not
  re-checked against the private-address gate. Direct media links and
  YouTube do not have this gap. A report that shows a page steering the
  page reader to a private address is in scope.

Out of scope: prompt-injection of the *calling* agent via transcript/OCR
content (inherent to the domain — mitigations and docs welcome, but it is
not a server vulnerability per se).

## URL error redaction boundary

Talkthrough sanitizes its expected and unexpected URL errors before they
leave the worker. Per-call context tracks the submitted URL and direct
redirects, including known queries and escaped/percent-encoded forms, and
is cleared at the end of the call. The CLI also filters foreign logs and
tracebacks. This does not sanitize a client's own record of its input.
The MCP SDK validates arguments before the tool body runs: a wrong-type
argument can be echoed in its validation response. Do not treat SDK
validation errors or client transcripts as a secret-free output channel.

## Downloader runtime and dependency resolution

The `[url]` extra installs yt-dlp, `yt-dlp-ejs` and the PyPI Deno runtime.
Talkthrough supplies Deno's path explicitly and disables downloader plugins,
user configuration, cookies and remote components. YouTube's JavaScript
challenge is still executed locally by Deno; this is not a claim that
untrusted JavaScript is never evaluated.

In the release-tested yt-dlp 2026.8.19 stack, the Deno runner uses
`--no-prompt`, `--no-remote`, `--no-config`, `--no-lock`,
`--node-modules-dir=none`, `--no-code-cache`, and `--cached-only`; the ordinary
bundled solver also uses `--no-npm`. It passes no `--allow-*` permissions.
The optional cached npm solver path is not the same as enabling a remote
component. These are dependency implementation details, not a sandbox for
the entire Python downloader, ffmpeg, or the server process.

`uv.lock` pins the development/CI environment. A launcher pinned to
`talkthrough-mcp==0.4.2` pins this package, but resolves transitive dependencies
from its declared ranges; a fresh `uvx` installation can get newer yt-dlp,
Deno or EJS versions. Release QA therefore checks both frozen dependencies
and a fresh wheel installation. A runtime behavior change in those
components remains relevant to the threat model above.
