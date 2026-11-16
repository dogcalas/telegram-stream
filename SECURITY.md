# Security policy

telegram-stream uses a Telegram **user account**, not a bot token. Its saved
session can grant access to that account. Treat the session and its backups as
account credentials.

## Supported versions

This project is in alpha. Security fixes target the latest code on the default
branch; older releases and snapshots do not have guaranteed security backports.
Update to the latest revision and rebuild your deployment when applying a fix.

Keep Python, the host OS, Docker, and media/browser dependencies updated too.
Updating Python packages alone does not update FFmpeg or Chromium in an existing
container image.

## Reporting a vulnerability

**Do not report vulnerabilities, credentials, or exploitable details in public
issues or pull requests.**

Use the repository's **Security → Report a vulnerability** option if available.
Otherwise, email maintainer Reinier Hernández at
[sasuke.reinier@gmail.com](mailto:sasuke.reinier@gmail.com) with the subject
`[telegram-stream security] Brief description`.

Include:

- The affected revision or version and deployment method.
- A description of the issue, its impact, and required access or preconditions.
- Minimal reproduction steps or a proof of concept using dummy credentials.
- Relevant OS, Python, Docker, and dependency versions.
- Sanitized logs or configuration, and any suggested mitigation.

Never send real Telegram sessions, login codes, 2FA passwords, API hashes, RTMP
keys, cookies, signed media URLs, or state-volume backups, even in a private
report. Reproduce issues only against accounts, channels, hosts, and media you
are authorized to test.

Please coordinate public disclosure with the maintainer so a fix or mitigation
can be prepared. There is no guaranteed response or remediation timetable.

## Security model and limits

- This is an operator-configured, single-channel service. It is not a public URL
  ingestion API, a multi-tenant service, or a sandbox for untrusted media.
- Sources and web pages can cause outbound requests, including to networks
  reachable from the deployment. Use trusted inputs and network-level egress
  restrictions where needed; URL validation is not network isolation.
- The optional RTMP ingest authenticates publishers with `RTMP_STREAM_KEY`, but
  RTMP itself is unencrypted. It binds to loopback by default; anyone who can
  observe traffic to an exposed port can read the key and replace the feed.
- Host and Docker administrators can read environment variables, mounted files,
  and process arguments. RTMP keys may appear in FFmpeg's arguments. The service
  cannot hide credentials from an administrator or a compromised host.
- Session and state volumes are **not encrypted** by the application. Log
  redaction reduces accidental disclosure but does not replace reviewing output
  before sharing it.
- The single-writer lock coordinates processes sharing the same state directory,
  not separate hosts or volumes. Run only one publisher deployment per channel.

## Secure deployment

- Restrict access to the host, Docker daemon, `.env`, cookies, and backups. Use
  mode `0600` for `.env` and private state directories with mode `0700`. For
  container bind mounts, ensure the application user (UID/GID `10001`) has the
  necessary access without making credentials world-readable or writable.
- Native cookie capture requires operator confirmation (or `--yes`) on the
  machine with the browser. It may read/copy that browser's cookie database
  locally to decrypt it, but exports only unexpired YouTube-domain cookies.
  Never run capture as root or mount a real browser profile/keychain into Docker.
  Managed imports travel over stdin and atomically replace a mode-0600 file.
  Optional online checks validate a staged candidate before replacing old cookies.
- Use encrypted disks and backups where appropriate. Stop the service before
  backing up its state, and retain the session and ownership state together.
  Do not share or casually delete the state volume to troubleshoot a problem.
- Preserve the supplied Compose protections: non-root execution, a read-only
  root filesystem, dropped capabilities, and `no-new-privileges`. Do not add
  privileged mode or mount the Docker socket.
- The default Compose configuration publishes no host ports. Keep the browser
  HLS server private and Chrome DevTools on loopback; do not expose either to
  the Internet.
- Keep the browser sidecar free of Telegram credentials, `.env`, session/state
  volumes, and media mounts. It runs Chromium with `--no-sandbox` for container
  compatibility. Container restrictions are **not** a replacement for Chromium's
  sandbox: open only trusted pages and keep the image updated.
- Grant only the Telegram channel permissions required to publish Live Stories.
  Consider a dedicated user account, protect it with 2FA, and review its active
  sessions regularly. 2FA does not make a stolen authenticated session harmless.
- Keep `ADOPT_EXISTING_LIVE_STORY=false` unless deliberately recovering a verified
  story. Never bypass ownership checks or run another publisher against the same
  channel while this service is active.

See the [deployment and security guidance](README.md#linux--proxmox-deployment-and-security)
and [browser isolation notes](README.md#web-pages-optional-browser-sidecar) for
operational details.

## If credentials may have leaked

1. Stop the affected deployment and secure the host before authenticating again.
2. From a trusted Telegram client, review **Settings → Devices** and terminate
   the affected session or other unrecognized sessions. Deleting a local session
   file or container does not revoke a copy held elsewhere.
3. With publishers stopped, revoke or rotate the affected RTMP credentials using
   Telegram. Invalidate exposed website cookies or sessions at their provider,
   and replace other affected credentials where supported.
4. Remove exposed material from public locations and investigate logs, backups,
   and repository history for further copies. Removal alone does not undo a
   credential leak.
5. Reauthenticate only on a trusted deployment. Verify channel permissions,
   active stories, and ownership state before restarting a single publisher.

If the exposure resulted from a project vulnerability, also submit a private
report using the instructions above.
