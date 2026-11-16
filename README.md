# telegram-stream

A small, single-channel service that continuously publishes YouTube Live, HLS, or
looped local video to **Telegram Channel Live Stories** using a Telegram **user**
account, MTProto, and FFmpeg. Requires Python 3.12+ or Docker Compose v2.

This is **not** a traditional channel livestream. It obtains RTMP credentials with
`phone.getGroupCallStreamRtmpUrl(live_story=True, revoke=False)` and creates stories
with `stories.startLive(rtmp_stream=True)`. It never calls `phone.createGroupCall`.

**24/7 means supervised operation and recovery, not an unlimited Telegram session
or gap-free video.** Telegram can terminate stories; sources can disappear; API
eligibility, rate limits, and YouTube access restrictions still apply.

## Telegram prerequisites

1. Obtain your own `api_id` and `api_hash` at <https://my.telegram.org/apps>.
   A BotFather token does not work.
2. Join the broadcast channel with a Telegram user account and make it an admin
   with permission to post stories. Grant management of video chats/live streams
   where required by Telegram. Channel ownership also works.
3. Verify that the channel/account is eligible to publish Live Stories in Telegram.
   Boost, account, regional, or server-side restrictions cannot be bypassed by this
   service. The API's permission/eligibility errors are authoritative.
4. Use **one publisher deployment per channel**, with a dedicated persistent volume.
   Do not start another publisher or rotate its RTMP key in Telegram while running.

The locked Telethon **1.45.0** wheel ships **MTProto layer 229**, including
`stories.StartLiveRequest.rtmp_stream`,
`phone.GetGroupCallStreamRtmpUrlRequest.live_story`, `MessageMediaVideoStream`, and
Live Story updates. Startup checks the schema, and tests verify serialized flags.
Do not downgrade to an older Telethon/Pyrogram build without verifying its schema.

## Quick start: Docker Compose

Run these commands from the repository root:

```sh
cp .env.example .env
chmod 600 .env
mkdir -p media
```

Edit `.env`: set `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_CHANNEL`, and
`SOURCE`. Use `@channel_username` or the channel's numeric `-100…` peer ID.

```sh
docker compose build
docker compose run --rm streamer auth
docker compose up -d
docker compose logs -f --tail=100 streamer
```

`auth` interactively asks for the phone number, Telegram login code, and optional
2FA password. Codes may arrive in an existing Telegram app rather than SMS. No
login code/password is saved. The resulting **user session is saved** to the
`telegram-state` volume; it effectively grants access to your account.

Authenticate **before** starting the service. For reauthentication:

```sh
docker compose stop streamer
docker compose run --rm streamer auth
docker compose up -d
```

The image runs as UID/GID **10001**, uses an init process through Compose, drops
Linux capabilities, and has a read-only root filesystem. A new named volume is
initialized with the image's `/data` ownership. If using an existing bind mount,
prepare it for UID/GID 10001 with directory mode 0700. Do not make it world-writable.

There are no published ports and no incoming network requirement. Allow outbound
DNS, HTTPS/MTProto connections, and the RTMP/RTMPS endpoint returned by Telegram.
The image includes FFmpeg/ffprobe, yt-dlp, its EJS challenge scripts, and Deno.

## Sources

### YouTube Live

```dotenv
SOURCE_TYPE=youtube
SOURCE=https://www.youtube.com/watch?v=YOUR_LIVE_VIDEO_ID
```

The video must currently be live. A channel's `/live` URL can also be used.
Resolution runs again for every publisher restart to renew expiring media URLs.
Muxed HLS is preferred; separate video/audio inputs are supported when necessary.
Extractor HTTP headers are passed to FFmpeg. Playlists and downloading are disabled.

Public, unrestricted streams are the simplest deployment. YouTube may block
datacenter IPs, request cookies/PO tokens, or change extraction behavior. This
service does not bypass those restrictions or guarantee every YouTube URL works.
Only restream material you have permission to distribute.

Optional cookies should be exported in Netscape format into an ignored
`secrets/youtube-cookies.txt`. Add a Compose override:

```yaml
services:
  streamer:
    volumes:
      - ./secrets/youtube-cookies.txt:/run/secrets/youtube-cookies.txt:ro
```

Set `YOUTUBE_COOKIES_FILE=/run/secrets/youtube-cookies.txt` in `.env`, ensuring the
file is readable by UID 10001, not world-readable. The resolver makes a temporary
private copy because yt-dlp updates cookie jars. `YOUTUBE_EXTRACTOR_ARGS` accepts
yt-dlp extractor arguments, including externally obtained tokens when needed.
Cookies help extraction but do not guarantee access to restricted media URLs.
See [yt-dlp EJS](https://github.com/yt-dlp/yt-dlp/wiki/EJS) and its
[YouTube guidance](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#youtube).

### HLS / M3U8

```dotenv
SOURCE_TYPE=hls
SOURCE=https://example.com/live/playlist.m3u8
```

Signed URLs work until their authorization expires; configure a stable URL where
possible. Automatic signing/refresh of arbitrary HLS services is not implemented.
Only trusted HTTP(S) sources are supported; no arbitrary FFmpeg argument strings
are executed. This is an operator-configured service, not a public URL ingestion API.

### Local files

Place a readable video in `media/`:

```dotenv
SOURCE_TYPE=file
SOURCE=/media/video.mp4
```

Files loop indefinitely with real-time input pacing. Missing audio is replaced
with silence. At an FFmpeg restart, local playback resumes from the **beginning**,
not a saved playback position. Network failures can produce visible gaps.

`SOURCE_TYPE=auto` detects YouTube domains, treats other HTTP(S) URLs as HLS/network
inputs, and treats remaining values as filesystem paths.

## Configuration

See [`.env.example`](.env.example) for all settings. The application reads the
**process environment**; it does not implicitly load a `.env` file. Compose does.

| Setting | Default | Purpose |
| --- | --- | --- |
| `VIDEO_WIDTH`, `VIDEO_HEIGHT` | `1280`, `720` | Even output dimensions; aspect-preserving letterbox |
| `VIDEO_FPS` | `30` | Constant output frame rate |
| `VIDEO_BITRATE`, `AUDIO_BITRATE` | `2500k`, `128k` | H.264 / AAC bitrate |
| `ENCODER_PRESET` | `veryfast` | CPU/quality trade-off |
| `LIVE_STORY_CAPTION` | empty | Optional caption; omitted if Telegram rejects RTMP captions |
| `TELEGRAM_POLL_INTERVAL` | `30` | Authoritative story polling, seconds |
| `RPC_TIMEOUT` | `15` | Telegram RPC and media network-I/O timeout |
| `SOURCE_TIMEOUT` | `60` | Per extraction/probe subprocess timeout |
| `STARTUP_TIMEOUT`, `STALL_TIMEOUT` | `60`, `45` | Initial/advancing media-progress deadlines |
| `RETRY_INITIAL`, `RETRY_MAX` | `2`, `120` | Exponential retry envelope, with jitter |
| `SHUTDOWN_TIMEOUT` | `10` | Grace period per child process/disconnect |
| `ADOPT_EXISTING_LIVE_STORY` | `false` | Explicitly permit publishing into an unknown RTMP story |
| `END_LIVE_STORY_ON_SHUTDOWN` | `false` | End the owned story on graceful shutdown |
| `DATA_DIR` | `./data` | Local persistent directory; Compose overrides to `/data` |

Encoding uses H.264 main profile, `yuv420p`, AAC stereo, a two-second GOP, and FLV.
Software encoding is intentional; hardware/GPU passthrough is outside the MVP.
Recreate the container to apply `.env` changes: `docker compose up -d`.

## Recovery and ownership

- A volume-backed advisory lock prevents simultaneous service/auth processes from
  sharing the session. It does **not** coordinate separate hosts/volumes.
- The service fetches `stories.getPeerStories` and the Live Story's
  `MessageMediaVideoStream.call`, then checks `phone.getGroupCall`. It does not
  mistake a traditional livestream's `channelFull.call` for a Live Story.
- Creation's random ID is atomically persisted **before** the API call. Retrying
  an uncertain request reuses that ID rather than creating a fresh request.
- Owned active RTMP stories are resumed after restarts. A confirmed ended/missing
  story causes the old publisher to stop before a replacement is created.
- Updates trigger early reconciliation; polling covers missed updates/reconnects.
- API timeouts are **not** considered termination. A healthy media publisher keeps
  running while Telegram connectivity recovers.
- Source exits, expired YouTube media URLs, and non-advancing FFmpeg output cause
  source re-resolution and publisher restart, not blind story creation.
- Retries back off exponentially with jitter. Telegram FloodWait minimums are
  respected even if new updates arrive. Permanent permission/auth errors exit.
- Some Telegram deployments reject nonempty RTMP captions with `CAPTION_NOT_EMPTY`.
  The service retries that explicit rejection once without a caption, preserving
  the same random ID, and omits captions for the remainder of that process.
  Other errors are not silently retried with modified parameters.

An unknown active story is not silently taken over. If a creation response is
lost, the service first attempts recovery with its persisted random ID. Telegram
may still return `STORY_LIVE_ALREADY_<id>` without enough information to prove
ownership. In this ambiguous case it exits rather than claiming exactly-once
semantics or interrupting another publisher.

To recover deliberately: stop the service, verify the active story in Telegram,
temporarily set `ADOPT_EXISTING_LIVE_STORY=true`, start the service, and confirm
its status. Then set it back to `false` and recreate the container. Alternatively,
end the unknown story in Telegram and let the service create a new one.
Non-RTMP stories are never adopted or replaced.

SIGTERM/SIGINT stop/reap child process groups and close the session. By default
the story is left for restart reconciliation; Telegram may end it while no media
is being published. Enable `END_LIVE_STORY_ON_SHUTDOWN` to request termination of
the still-owned call on a graceful stop. Force-kill cannot guarantee cleanup.

## Health, logs, and troubleshooting

```sh
docker compose ps
docker compose exec streamer telegram-stream status
docker compose exec streamer telegram-stream healthcheck
docker compose logs --tail=100 streamer
```

`status` prints JSON including `healthy`, `ready`, phase, story ID, FFmpeg PID,
media-progress age, and last successful Telegram check. It never opens the session.
`healthy` checks supervisor liveness; **`ready` additionally requires recent media
progress and Telegram confirmation**. A source outage/FloodWait can be healthy
but not ready, preventing restart storms. Alerts should monitor readiness too.

FFmpeg is monitored using **advancing video frames and media timestamps**, not
merely a living PID. Advancing audio alone cannot hide a frozen video input, and
silent-audio fallback does not keep an ended video source alive indefinitely.
A bounded supervisor watchdog exits on internal stalls so the restart policy can
recover. Docker does **not** restart a container merely because its health status
is unhealthy; external monitoring is needed for a completely frozen event loop.
The watchdog window accounts for configured RPC/probe deadlines.

Logs are JSON on stdout/stderr; URLs, known keys, and extraction credentials are
redacted. Dependency traceback/request dumps are intentionally not emitted.
Useful events include `live_story_ready`, `source_retry`, `source_interrupted`,
`telegram_retry`, and `fatal`. Compose limits retained log size.

| Symptom | Action |
| --- | --- |
| Unauthorized session | Stop, run `auth`, restart; check account/session revocation |
| `CHAT_ADMIN_REQUIRED` / eligibility errors | Check user permissions and channel Live Story eligibility |
| Unknown/ambiguous active story | Follow the explicit adoption procedure above |
| `live_story_caption_omitted` | Telegram rejected the optional RTMP caption; streaming continues without it |
| Another process owns `DATA_DIR` | Stop the other service/auth process; do not delete lock files |
| Repeated YouTube extraction failures | Check the live URL, network/IP restrictions, cookies/tokens; update the locked yt-dlp dependency and rebuild |
| FFmpeg stalls / source retries | Check source reachability, expiring URLs, upload bandwidth, CPU |
| Volume permission denied | Ensure `/data` belongs to UID/GID 10001 with private permissions |

## Linux / Proxmox deployment and security

Prefer a small Debian/Ubuntu **VM** under Proxmox with Docker Engine and the Compose
plugin. Start with 2–4 vCPUs, 2–4 GB RAM, and enough disk for the image/logs; measure
software-encoding load on your hardware. Budget approximately the configured
video+audio bitrate in continuous upstream capacity, with headroom. Docker inside
LXC requires correct nesting/storage/UID configuration and is not assumed here.

Enable Docker at VM boot and retain the same Compose project name and named
volume. `restart: unless-stopped` resumes after daemon/server restarts unless you
explicitly stopped the container. Container restart loops cannot fix bad credentials.

Protect `.env`, cookies, volume data, and volume backups. Environment variables are
visible to Docker administrators; session/state volumes are **not encrypted**.
Restrict host/Docker access, use encrypted disks/backups where needed, and never
commit sessions or upload them to support channels. An RTMP key can also appear in
FFmpeg's process arguments to privileged host users. There is no way to hide
credentials from the host administrator.

Stop the service before backing up the named volume (session SQLite database,
`story.json`, and lock metadata). Restore the whole volume, not just the session,
to preserve ownership and pending creation IDs. Do not use `docker compose down -v`
unless intentionally destroying credentials and recovery state. Restoring a backup
does not guarantee its Telegram session is still authorized.

## Local development

Install Python 3.12+ and FFmpeg/ffprobe. `uv sync --locked` installs the Python
dependencies, including yt-dlp's pinned Deno runtime and EJS scripts.

```sh
uv sync --locked
# Export settings using your shell; .env.example is not automatically sourced.
export TELEGRAM_API_ID=123456
export TELEGRAM_API_HASH=your_32_character_api_hash
export TELEGRAM_CHANNEL=@your_channel
export SOURCE=/absolute/path/to/video.mp4
uv run --locked telegram-stream auth
uv run --locked telegram-stream run
```

Keep the existing `make setup`, `make format`, `make check`, `make test`, and
`make build` commands. The development interpreter pin is Python 3.14; the image
uses Python 3.12 to exercise the minimum supported version. No dev tools ship in
the runtime image. For a development container, override its entrypoint:

```sh
docker build --target development -t telegram-stream:dev .
docker run --rm --entrypoint ruff telegram-stream:dev check .
```

### Validation

Focused tests cover schema flags, durable/idempotent creation, response loss,
ownership conflicts, termination recovery, FloodWait classification, source URL
refresh, FFmpeg stalls, subprocess cleanup, health, locks, and log redaction.
They do not contact Telegram or publish a live story.

For real acceptance testing, use a dedicated channel: authenticate, stream each
source type, verify the result appears in the **story bar**, disconnect the source,
restart the container, and end the story in Telegram to observe recreation.
Check that only one FFmpeg publisher and one active Live Story exist. A
traditional channel livestream is not a successful Live Story acceptance test.

## References

- [Telegram Live Stories](https://core.telegram.org/api/group-calls#live-stories)
- [RTMP publishing flow](https://core.telegram.org/api/group-calls#creating-and-publishing-an-rtmp-livestream)
- [stories.startLive](https://core.telegram.org/method/stories.startLive)
- [phone.getGroupCallStreamRtmpUrl](https://core.telegram.org/method/phone.getGroupCallStreamRtmpUrl)
- [Telethon generated API](https://tl.telethon.dev/methods/stories/start_live.html)
