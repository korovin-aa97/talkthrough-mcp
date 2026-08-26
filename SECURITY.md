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
  downloads, no telemetry) — any violation is treated as a vulnerability.

Out of scope: prompt-injection of the *calling* agent via transcript/OCR
content (inherent to the domain — mitigations and docs welcome, but it is
not a server vulnerability per se).
