# Managed Broadcast Desk

This public fork retains InstantClone's standalone behavior and adds an opt-in
Linux service mode. The implementation and tests require no fleet configuration,
production credentials, private flake inputs or provider accounts.

Set `INSTANTCLONE_MANAGED=1`, `INSTANTCLONE_TEMPLATE` to a non-secret template,
and `CONFIG_PATH` to a private runtime configuration file. Templates reference
decrypted credentials using `destination.N.stream_key_file` and optionally
`destination.N.custom_egress_url_file`. Plaintext is read only at process startup.
Configuration and application lifecycle changes belong to the service manager.

Server URLs may have multi-segment application paths; managed mode always appends
the separate stream key. Do not embed the key in the server URL. Kick also accepts
a host-only `rtmps://host[:port]` server and adds `/app`. Userinfo, query strings,
fragments, traversal and empty path segments are rejected without echoing values.

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

## Public qualification

Simit generates `nix-builds.yaml` from `simit.toml`. GitHub-hosted Linux runners
build the production package (including Rust tests), exercise single and dual
relay forwarding with disposable age credentials and local RTMP sinks, and run
headless Chromium interaction tests. No provider is contacted and CI does not
deploy or publish runtime closures. Exact revisions and Nix result receipts are
retained as workflow artifacts.

`managed-security` additionally exercises real HTTP login/logout, anonymous and
invalid credential rejection, protected self/peer bridging, private-file guards,
and real-server Chromium interactions. Rust tests reject expired sessions over
HTTP. Startup regressions prove shared directory permissions and uncreated
state directories are unchanged on unsafe path rejection. Duplicate labels retain
every destination's stable ID; ambiguous cross-program labels render separately.

Regenerate or verify the workflow with `simit init ci --platform github
--ci-provider actions --runtime nix` (add `--check --diff` for verification).
Formatting runs through `treefmt`.
