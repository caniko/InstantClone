# Managed Broadcast Desk

This public fork retains InstantClone's standalone behavior and adds an opt-in
Linux service mode. The implementation and tests require no fleet configuration,
production credentials, private flake inputs or provider accounts.

Set `INSTANTCLONE_MANAGED=1`, `INSTANTCLONE_TEMPLATE` to a non-secret template,
and `CONFIG_PATH` to a private runtime configuration file. Templates reference
decrypted credentials using `destination.N.stream_key_file` and optionally
`destination.N.custom_egress_url_file`. Plaintext is read only at process startup.
Configuration and application lifecycle changes belong to the service manager.

Two independent instances can compose landscape and portrait programs. Both
single-track ingests use `stream_format=horizontal` to forward their primary
track; portrait composition is independent of the Enhanced Broadcasting selector.

Supply `INSTANTCLONE_DESK_LANDSCAPE_PORT` and `INSTANTCLONE_DESK_PORTRAIT_PORT`
with each local relay's web port (`0` means disabled). `INSTANTCLONE_DESK_CONFIG`
names a public JSON file such as:

```json
{"landscape":{"enabled":true,"ingestPort":1935},"portrait":{"enabled":true,"ingestPort":1936}}
```

Add `http://127.0.0.1:8080/dock` to OBS's Custom Browser Docks. The root URL
opens setup and diagnostics. The desk shows both programs and every destination,
marks unavailable telemetry unknown, and scopes delay actions to one program.
Connected transport is distinct from measured media delivery; neither proves a
provider has published the broadcast. Returning to real time does not stop OBS.

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

Regenerate or verify the workflow with `simit init ci --platform github
--ci-provider actions --runtime nix` (add `--check --diff` for verification).
Formatting runs through `treefmt`.
