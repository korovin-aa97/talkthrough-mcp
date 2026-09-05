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
  signed query/userinfo into a manifest, the URL index, a log line, a
  progress message or an error, bypasses of the byte/duration/disk/redirect
  caps, hostile remote media or metadata reaching ffmpeg/ffprobe or a
  filename, and the downloader dependency (`yt-dlp`, `deno`) being driven
  with anything other than the allowlisted option set (no user config, no
  plugins, no cookies, no remote JavaScript components).

Out of scope: prompt-injection of the *calling* agent via transcript/OCR
content (inherent to the domain — mitigations and docs welcome, but it is
not a server vulnerability per se).
