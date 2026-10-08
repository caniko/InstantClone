# Managed Broadcast Desk

This public fork retains InstantClone's standalone behavior and adds an opt-in
Linux service mode. The implementation and tests require no fleet configuration,
production credentials, private flake inputs or provider accounts.

Set `INSTANTCLONE_MANAGED=1`, `INSTANTCLONE_TEMPLATE` to a non-secret template,
and `CONFIG_PATH` to a private runtime configuration file. Templates reference
decrypted credentials using `destination.N.stream_key_file` and optionally
`destination.N.custom_egress_url_file`. Protect a publisher with
`ingest_key_file=/path/to/ingest-key`; the provisioned key contains only letters,
numbers, `-` and `_`. Plaintext is read only at process startup. Direct
`ingest_key` and `stream_key` fields are rejected, including legacy and disabled entries.
Each enabled destination requires an explicit, nonempty, unique `destination.N.id`.
Destination indexes must be canonical decimal integers from `0` through `127`;
out-of-range or malformed indexes fail before credential reads or state creation.
Configuration and application lifecycle changes belong to the service manager.

Server URLs may have multi-segment application paths; managed mode always appends
the separate stream key. Enabled destinations without that key fail at startup
before creating managed state; disabled managed destination metadata needs no credential.
Standalone keeps its existing validation because its dock can enable destinations.
Do not embed the key in the server URL. Kick also accepts
a host-only `rtmps://host[:port]` server and adds `/app`. Userinfo, query strings,
fragments, traversal and empty path segments are rejected without echoing values,
for both inline servers and credential-file servers.

Managed startup rejects malformed booleans and routing enums instead of selecting
defaults: booleans are `true` or `false`, destination `stream_format` is
`horizontal` or `vertical`, and `audio_track` is `auto`, `both`, `1` or `2`.
It also rejects invalid numeric declarations instead of recovering to
different defaults: ports must be nonzero and distinct, `buffer_mb` must be
50–1048576, and `target_delay_ms`/`armed_delay_ms` must be at most 600000.
`auto_arm_delay_ms` must be 1–600000. SIGTERM and fatal ingest/dashboard listener
failures use graceful stream teardown and remove the ring file; listener failures
then report a nonzero exit so the service manager can restart the instance.

Set `XDG_RUNTIME_DIR` to an existing user-owned private directory. `CONFIG_PATH`
must be in a dedicated subdirectory of it. Managed templates must explicitly
set absolute `buffer_path` and `overlays_dir` paths. Traversal and symlinks in
these state paths are rejected before creating directories or reading secrets.
Existing final state directories must already be user-owned and mode `0700`;
preparation never changes the permissions of existing directories. Use trusted
parent directories controlled by the user/service manager for runtime and state.

Two independent instances can compose landscape and portrait programs. Both
single-track ingests use `stream_format=horizontal` to forward their primary
track; portrait composition is independent of the Enhanced Broadcasting selector.

Supply `INSTANTCLONE_DESK_LANDSCAPE_PORT` and `INSTANTCLONE_DESK_PORTRAIT_PORT`
with each local relay's web port (`0` means disabled). `INSTANTCLONE_DESK_CONFIG`
names a public JSON file such as:

```json
{"landscape":{"enabled":true,"ingestPort":1935},"portrait":{"enabled":true,"ingestPort":1936}}
```

### Protected desks

Upstream password authentication remains optional. For a protected service, put
its PBKDF2 hash in a private user-owned file and use
`dashboard_password_hash_file=/path/to/password-hash` in the template. The hash
format is `pbkdf2-sha256$ITERATIONS$SALT_HEX$DIGEST_HEX`, with a 16-byte salt,
32-byte digest and 1–1,000,000 iterations. Login/logout work in managed mode;
auth configuration mutations remain owned by the service manager.
Password-protected programs must also declare `dock_token_file`; incomplete
authentication/control-token pairing fails before persisting runtime settings.
Direct `dock_token` and `dashboard_password_hash` fields are rejected, even when
empty or declared alongside a credential-file directive in either order. Only
validated file credentials can supply the runtime authentication values.

Give each program a distinct 16–128-character hex control token, stored in a
private user-owned file (mode `0400` or `0600`). Set
`dock_token_file=/path/to/this-program-token` in its template, and configure the
bridge with `INSTANTCLONE_DESK_LANDSCAPE_TOKEN_FILE` and
`INSTANTCLONE_DESK_PORTRAIT_TOKEN_FILE`. These variables contain file paths,
never credentials. Peer files are read per request so peer rotation takes effect
without restarting the desk's host relay; restart the rotated target to load its
new token. Agenix-style symlinks to private credential files are supported.

The local program uses its own loaded control token. Peer requests use only the
explicit peer token, never the caller's administrator session. Missing, invalid,
insecure or rejected peer credentials return `401` with a credential diagnostic;
an offline program returns `503`. A dock token permits exactly the desk assets,
telemetry and supported delay controls, with no configuration/lifecycle writes.

For OBS, open `/dock?token=YOUR_PROGRAM_TOKEN` once to establish an HttpOnly,
SameSite cookie, or log in using `/login` for an administrator session. Both
authorized entry points load `/desk/app.js` and each configured program's data.

Add `http://127.0.0.1:8080/dock` to OBS's Custom Browser Docks. The root URL
opens setup and diagnostics. The desk shows both programs and every destination,
marks unavailable telemetry unknown, and scopes delay actions to one program.
Connected transport is distinct from measured media delivery; neither proves a
provider has published the broadcast. Returning to real time does not stop OBS.
Setup reads `ingest_key_set` from fresh telemetry. Protected ingests instruct OBS
to use that program's provisioned key without displaying it; unprotected ingests
accept any non-empty key. Credential errors are shown separately from outages,
and stale or unauthenticated program controls remain paused.

The same-origin bridge only contacts fixed loopback ports and exposes state,
destination summaries and supported delay actions. Credentials and endpoint URLs
are omitted from managed dashboard responses and connection logs.
When tracing is enabled, managed wire traces retain event categories and timing
and redact all detail strings, including endpoints and remote status descriptions.
Standalone wire tracing retains its byte-level diagnostic details.

## Public qualification

Simit generates `nix-builds.yaml` from `simit.toml`. GitHub-hosted Linux runners
build the production package (including Rust tests), exercise single and dual
relay forwarding with disposable age credentials and local RTMP sinks, and run
headless Chromium interaction tests. No provider is contacted and CI does not
deploy or publish runtime closures. Exact revisions and Nix result receipts are
retained as workflow artifacts.

The runtime gate also injects listener failures after successful preflight and
during active local forwarding. It verifies failure exit status, teardown grace
and ring-file cleanup without adding failure-injection switches to the application.

The dual-relay gate forwards four independent landscape destinations and four
independent portrait destinations concurrently. It checks all eight recordings'
dimensions, continued growth of every portrait sink while landscape is stopped,
and fresh decoded frames at all four landscape sinks after restart. This proves
local fan-out and restart isolation; provider/account acceptance is a separate gate.
All managed qualification publishers remain alive until their assertions finish
and are explicitly stopped during cleanup. Startup, reconnect and browser budgets
cannot expire an input before its media/authentication assertions.

`managed-security` additionally exercises real HTTP login/logout, anonymous and
invalid credential rejection, protected self/peer bridging, private-file guards,
and real-server Chromium interactions. Rust tests reject expired sessions over
HTTP. Startup regressions prove shared directory permissions and uncreated
state directories are unchanged on unsafe path rejection. Duplicate labels retain
every destination's stable ID; ambiguous cross-program labels render separately.

Regenerate or verify the workflow with `simit init ci --platform github
--ci-provider actions --runtime nix` (add `--check --diff` for verification).
Formatting runs through `treefmt`.
