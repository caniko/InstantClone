//! Opt-in Nix/systemd integration. No secret is read until process startup.
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Component, Path};

pub fn enabled() -> bool {
    std::env::var("INSTANTCLONE_MANAGED").as_deref() == Ok("1")
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn private_dir(path: &Path) -> io::Result<()> {
    validate_state_path(path)?;
    validate_private_directory(path)?;
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(path)?;
    let meta = fs::symlink_metadata(path)?;
    // SAFETY: geteuid has no preconditions and does not mutate process state.
    if !meta.is_dir() || meta.uid() != unsafe { libc::geteuid() } {
        return Err(invalid(
            "InstantClone: state directory must be owned by this user",
        ));
    }
    if meta.permissions().mode() & 0o777 != 0o700 {
        return Err(invalid(
            "InstantClone: existing state directory must already be private (0700)",
        ));
    }
    Ok(())
}

fn validate_state_path(path: &Path) -> io::Result<()> {
    if !path.is_absolute()
        || path
            .components()
            .any(|c| !matches!(c, Component::RootDir | Component::Normal(_)))
    {
        return Err(invalid(
            "InstantClone: state paths must be absolute without traversal",
        ));
    }
    let mut current = std::path::PathBuf::new();
    for component in path.components() {
        current.push(component);
        match fs::symlink_metadata(&current) {
            Ok(meta) if meta.file_type().is_symlink() => {
                return Err(invalid(
                    "InstantClone: state paths must not contain symlinks",
                ));
            }
            Ok(_) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

fn validate_private_directory(path: &Path) -> io::Result<()> {
    match fs::symlink_metadata(path) {
        Ok(meta) => {
            // SAFETY: geteuid has no preconditions.
            if !meta.is_dir()
                || meta.uid() != unsafe { libc::geteuid() }
                || meta.permissions().mode() & 0o777 != 0o700
            {
                return Err(invalid(
                    "InstantClone: existing state directory must already be owned and private (0700)",
                ));
            }
            Ok(())
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error),
    }
}

fn secret(path: &Path, name: &str) -> io::Result<String> {
    let error = |reason| invalid(&format!("InstantClone destination '{name}': {reason}"));
    let mut bytes = Vec::new();
    fs::File::open(path)
        .and_then(|file| file.take(4099).read_to_end(&mut bytes))
        .map_err(|_| error("stream-key secret is missing or unreadable"))?;
    if bytes.ends_with(b"\r\n") {
        bytes.truncate(bytes.len() - 2);
    } else if bytes.ends_with(b"\n") {
        bytes.pop();
    }
    // '/' moves part of the key into EgressUrl.app, which upstream logs.
    // Whitespace would be trimmed by Settings::load. No escaping exists.
    if bytes.is_empty()
        || bytes.len() > 4096
        || !bytes.iter().all(|b| b.is_ascii_graphic() && *b != b'/')
    {
        return Err(error(
            "stream-key secret must be 1-4096 printable ASCII characters without whitespace or '/'",
        ));
    }
    String::from_utf8(bytes).map_err(|_| error("invalid stream-key secret"))
}

/// Secret server URLs use the same shape as open `server` values, without
/// credentials or query strings. Managed destinations always append the separate
/// stream key, even when their server has a multi-segment app path.
fn server_url(path: &Path, name: &str, platform: &str) -> io::Result<String> {
    let error = |reason| invalid(&format!("InstantClone destination '{name}': {reason}"));
    let mut bytes = Vec::new();
    fs::File::open(path)
        .and_then(|file| file.take(4099).read_to_end(&mut bytes))
        .map_err(|_| error("server-URL secret is missing or unreadable"))?;
    if bytes.ends_with(b"\r\n") {
        bytes.truncate(bytes.len() - 2);
    } else if bytes.ends_with(b"\n") {
        bytes.pop();
    }
    if bytes.is_empty() || bytes.len() > 4096 {
        return Err(error(
            "server-URL secret must be rtmp(s)://host[:port]/app without credentials or query",
        ));
    }
    let text = std::str::from_utf8(&bytes).map_err(|_| {
        error("server-URL secret must be rtmp(s)://host[:port]/app without credentials or query")
    })?;
    validate_server_url(text, name, platform)?;
    Ok(text.to_owned())
}

/// Inline and credential-file endpoints share one managed server contract.
fn validate_server_url(text: &str, name: &str, platform: &str) -> io::Result<()> {
    let error = || {
        invalid(&format!(
            "InstantClone destination '{name}': server URL must be rtmp(s)://host[:port]/app without credentials or query"
        ))
    };
    if text.is_empty() || text.len() > 4096 {
        return Err(error());
    }
    let rest = text
        .strip_prefix("rtmp://")
        .or_else(|| text.strip_prefix("rtmps://"))
        .ok_or_else(error)?;
    let valid = (|| {
        let (authority, app) = rest.split_once('/').unwrap_or((rest, ""));
        if authority.is_empty() || authority.bytes().any(|b| b == b'@') {
            return None;
        }
        let (host, port) = match authority.split_once(':') {
            Some((host, port)) => {
                let port: u16 = port.parse().ok()?;
                if port == 0 {
                    return None;
                }
                (host, Some(port))
            }
            None => (authority, None),
        };
        let _ = port;
        if host.is_empty()
            || !host
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'-'))
        {
            return None;
        }
        let app = app.strip_suffix('/').unwrap_or(app);
        if (app.is_empty() && platform != "kick")
            || (!app.is_empty()
                && !app.split('/').all(|segment| {
                    !segment.is_empty()
                        && segment
                            .bytes()
                            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.'))
                        && !matches!(segment, "." | "..")
                }))
        {
            return None;
        }
        Some(())
    })();
    if valid.is_none() || !text.bytes().all(|b| b.is_ascii_graphic()) {
        return Err(error());
    }
    Ok(())
}

pub fn prepare() -> io::Result<()> {
    if !enabled() {
        return Ok(());
    }
    // SAFETY: called before the Tokio runtime or any worker threads exist.
    unsafe {
        libc::umask(0o077);
    }
    let config = std::env::var("CONFIG_PATH")
        .map_err(|_| invalid("InstantClone: managed mode requires CONFIG_PATH"))?;
    let template = std::env::var("INSTANTCLONE_TEMPLATE")
        .map_err(|_| invalid("InstantClone: managed mode requires INSTANTCLONE_TEMPLATE"))?;
    let runtime = std::env::var("XDG_RUNTIME_DIR")
        .map_err(|_| invalid("InstantClone: XDG_RUNTIME_DIR is missing"))?;
    let path = Path::new(&config);
    validate_state_path(path)?;
    validate_state_path(Path::new(&runtime))?;
    validate_private_directory(Path::new(&runtime))?;
    if !path.starts_with(&runtime) || path.parent() == Some(Path::new(&runtime)) {
        return Err(invalid(
            "InstantClone: CONFIG_PATH must be inside XDG_RUNTIME_DIR",
        ));
    }
    let text = fs::read_to_string(template)?;
    let fields: BTreeMap<_, _> = text
        .lines()
        .map(str::trim)
        .filter(|line| !line.starts_with('#'))
        .filter_map(|line| {
            line.split_once('=')
                .map(|(key, value)| (key.trim(), value.trim()))
        })
        .collect();
    // Reject credentials in the non-secret source, even on disabled destinations
    // or alongside a file directive. Only runtime credential reads may render keys.
    if fields.keys().any(|key| {
        *key == "stream_key" || (key.starts_with("destination.") && key.ends_with(".stream_key"))
    }) {
        return Err(invalid(
            "InstantClone: managed templates require stream_key_file instead of plaintext stream keys",
        ));
    }
    if fields.contains_key("ingest_key") {
        return Err(invalid(
            "InstantClone: managed templates require ingest_key_file instead of a plaintext ingest key",
        ));
    }
    if (fields.contains_key("dashboard_password_hash_file")
        || fields
            .get("dashboard_password_hash")
            .is_some_and(|hash| !hash.is_empty()))
        && !fields.contains_key("dock_token_file")
    {
        return Err(invalid(
            "InstantClone: dashboard authentication requires dock_token_file for program control",
        ));
    }
    // The standalone parser recovers malformed numbers by retaining defaults.
    // Declarative startup must reject these before parsing or reading credentials.
    for (key, value) in &fields {
        let valid = match *key {
            "ingest_port" | "web_port" => value.parse::<u16>().is_ok(),
            "buffer_mb" => value.parse::<u64>().is_ok(),
            "target_delay_ms" | "armed_delay_ms" | "auto_arm_delay_ms" => {
                value.parse::<u32>().is_ok()
            }
            "configured"
            | "ingest_bind_all"
            | "web_bind_all"
            | "tracing_enabled"
            | "auto_arm_on_connect"
            | "auto_activate_when_ready"
            | "update_check_enabled"
            | "open_dashboard_on_launch"
            | "overlays_seeded" => matches!(*value, "true" | "false"),
            key if key.starts_with("destination.") => match key.rsplit('.').next() {
                Some("enabled" | "vod_audio" | "vod_audio_inject_eb") => {
                    matches!(*value, "true" | "false")
                }
                Some("stream_format") => matches!(*value, "horizontal" | "vertical"),
                Some("audio_track") => matches!(*value, "auto" | "both" | "1" | "2"),
                _ => true,
            },
            _ => true,
        };
        if !valid {
            return Err(invalid(&format!("InstantClone: invalid declared {key}")));
        }
        if let Some(prefix) = key.strip_suffix(".custom_egress_url") {
            validate_server_url(
                value,
                fields
                    .get(format!("{prefix}.name").as_str())
                    .copied()
                    .unwrap_or("unnamed"),
                fields
                    .get(format!("{prefix}.platform").as_str())
                    .copied()
                    .unwrap_or("custom"),
            )?;
        }
    }
    let mut directories = vec![path
        .parent()
        .ok_or_else(|| invalid("InstantClone: invalid CONFIG_PATH"))?];
    for key in ["buffer_path", "overlays_dir"] {
        let value = fields.get(key).ok_or_else(|| {
            invalid("InstantClone: managed templates require explicit buffer_path and overlays_dir")
        })?;
        let state_path = Path::new(value);
        validate_state_path(state_path)?;
        directories.push(if key == "buffer_path" {
            state_path
                .parent()
                .ok_or_else(|| invalid("InstantClone: invalid buffer path"))?
        } else {
            state_path
        });
    }
    // Validate every destination before any directory creation or secret read.
    for directory in &directories {
        validate_private_directory(directory)?;
    }
    let mut rendered = String::new();
    for line in text.lines() {
        if let Some((key, value)) = line
            .trim()
            .split_once('=')
            .filter(|(key, _)| !key.starts_with('#'))
            .map(|(key, value)| (key.trim(), value.trim()))
        {
            if key == "ingest_key_file" {
                let secret_path = value
                    .strip_prefix("${XDG_RUNTIME_DIR}/")
                    .map(|p| Path::new(&runtime).join(p))
                    .unwrap_or_else(|| value.into());
                rendered.push_str(&format!("ingest_key={}\n", secret(&secret_path, "ingest")?));
                continue;
            }
            if key == "dock_token_file" {
                let secret_path = value
                    .strip_prefix("${XDG_RUNTIME_DIR}/")
                    .map(|p| Path::new(&runtime).join(p))
                    .unwrap_or_else(|| value.into());
                rendered.push_str(&format!(
                    "dock_token={}\n",
                    control_token_file(&secret_path)?
                ));
                continue;
            }
            if key == "dashboard_password_hash_file" {
                let secret_path = value
                    .strip_prefix("${XDG_RUNTIME_DIR}/")
                    .map(|p| Path::new(&runtime).join(p))
                    .unwrap_or_else(|| value.into());
                let hash = private_credential(&secret_path)?;
                let parts: Vec<_> = hash.split('$').collect();
                if parts.len() != 4
                    || parts[0] != "pbkdf2-sha256"
                    || !parts[1]
                        .parse::<u32>()
                        .ok()
                        .is_some_and(|n| n > 0 && n <= 1_000_000)
                    || parts[2].len() != 32
                    || parts[3].len() != 64
                    || !parts[2]
                        .bytes()
                        .chain(parts[3].bytes())
                        .all(|b| b.is_ascii_hexdigit())
                {
                    return Err(invalid(
                        "InstantClone: invalid dashboard password hash credential",
                    ));
                }
                rendered.push_str(&format!("dashboard_password_hash={hash}\n"));
                continue;
            }
            if let Some(prefix) = key.strip_suffix(".stream_key_file") {
                let name = fields
                    .get(format!("{prefix}.name").as_str())
                    .ok_or_else(|| {
                        invalid("InstantClone: secret directive has no destination name")
                    })?;
                let secret_path = value
                    .strip_prefix("${XDG_RUNTIME_DIR}/")
                    .map(|p| Path::new(&runtime).join(p))
                    .unwrap_or_else(|| value.into());
                rendered.push_str(&format!(
                    "{prefix}.stream_key={}\n",
                    secret(&secret_path, name)?
                ));
                continue;
            }
            if let Some(prefix) = key.strip_suffix(".custom_egress_url_file") {
                let name = fields
                    .get(format!("{prefix}.name").as_str())
                    .ok_or_else(|| {
                        invalid("InstantClone: secret directive has no destination name")
                    })?;
                let secret_path = value
                    .strip_prefix("${XDG_RUNTIME_DIR}/")
                    .map(|p| Path::new(&runtime).join(p))
                    .unwrap_or_else(|| value.into());
                rendered.push_str(&format!(
                    "{prefix}.custom_egress_url={}\n",
                    server_url(
                        &secret_path,
                        name,
                        fields
                            .get(format!("{prefix}.platform").as_str())
                            .copied()
                            .unwrap_or("custom")
                    )?
                ));
                continue;
            }
        }
        rendered.push_str(line);
        rendered.push('\n');
    }
    let settings = crate::config::Settings::from_text_unclamped(&rendered);
    // Managed URLs contain only the server application. They never embed a key,
    // so every enabled provider needs its separately provisioned credential.
    let mut ids = BTreeSet::new();
    for (index, destination) in settings.destinations.iter().enumerate() {
        if destination.enabled {
            let id = fields
                .get(format!("destination.{index}.id").as_str())
                .filter(|id| !id.is_empty())
                .ok_or_else(|| {
                    invalid(
                        "InstantClone: explicit nonempty id required for every enabled destination",
                    )
                })?;
            if !ids.insert(*id) {
                return Err(invalid("InstantClone: duplicate destination id"));
            }
        }
        if destination.enabled
            && destination.platform != "sink"
            && destination.stream_key.is_empty()
        {
            return Err(invalid(&format!(
                "InstantClone destination '{}': separate stream key required",
                destination.name
            )));
        }
    }
    let errors = settings.validate();
    if !errors.is_empty() {
        return Err(invalid(&format!("InstantClone: {}", errors.join("; "))));
    }
    for directory in directories {
        private_dir(directory)?;
    }
    let tmp = path.with_extension(format!("{}.tmp", std::process::id()));
    let result = (|| {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&tmp)?;
        file.write_all(rendered.as_bytes())?;
        file.sync_all()?;
        fs::rename(&tmp, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    result
}

fn private_credential(path: &Path) -> io::Result<String> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NONBLOCK)
        .open(path)
        .map_err(|_| invalid("InstantClone: control credential unavailable"))?;
    let meta = file.metadata()?;
    // SAFETY: geteuid has no preconditions.
    if !meta.is_file()
        || meta.uid() != unsafe { libc::geteuid() }
        || meta.permissions().mode() & 0o077 != 0
        || meta.len() > 256
    {
        return Err(invalid(
            "InstantClone: control credential must be private and user-owned",
        ));
    }
    let mut text = String::new();
    file.take(257).read_to_string(&mut text)?;
    if let Some(value) = text
        .strip_suffix("\r\n")
        .or_else(|| text.strip_suffix('\n'))
    {
        return Ok(value.to_owned());
    }
    Ok(text)
}

fn control_token_file(path: &Path) -> io::Result<String> {
    let token = private_credential(path)?;
    if !(16..=128).contains(&token.len()) || !token.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(invalid("InstantClone: invalid control credential"));
    }
    Ok(token)
}

/// Authenticated callers may operate the configured programs. Peer credentials
/// are explicit private files; administrator cookies are never forwarded.
pub async fn desk_request(
    method: &str,
    path: &str,
    body: &str,
    settings: &crate::config::Settings,
) -> (&'static str, String) {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let unavailable = || {
        (
            "503 Service Unavailable",
            r#"{"ok":false,"error":"Relay unavailable; check its user service."}"#.into(),
        )
    };
    if path == "/desk/info" && method == "GET" {
        return match std::env::var("INSTANTCLONE_DESK_CONFIG")
            .ok()
            .and_then(|p| fs::read_to_string(p).ok())
        {
            Some(info) => ("200 OK", info),
            None => unavailable(),
        };
    }
    let parts: Vec<_> = path.trim_start_matches('/').split('/').collect();
    if parts.len() != 3 || parts[0] != "desk" {
        return ("404 Not Found", "{}".into());
    }
    let variable = match parts[1] {
        "landscape" => "INSTANTCLONE_DESK_LANDSCAPE_PORT",
        "portrait" => "INSTANTCLONE_DESK_PORTRAIT_PORT",
        _ => return ("404 Not Found", "{}".into()),
    };
    let action = parts[2];
    if !crate::managed_routes::control(method, path)
        || (action == "arm"
            && !(body
                .strip_prefix("ms=")
                .and_then(|s| s.parse::<u32>().ok())
                .is_some_and(|ms| ms <= 600_000)))
    {
        return (
            "400 Bad Request",
            r#"{"ok":false,"error":"Unsupported desk action or delay."}"#.into(),
        );
    }
    let Some(port) = std::env::var(variable)
        .ok()
        .and_then(|s| s.parse::<u16>().ok())
        .filter(|p| *p != 0)
    else {
        return (
            "409 Conflict",
            r#"{"ok":false,"error":"Program is not configured."}"#.into(),
        );
    };
    let token =
        if port == settings.web_port {
            settings.dock_token.clone()
        } else if let Ok(file) =
            std::env::var(format!("{}_TOKEN_FILE", variable.trim_end_matches("_PORT")))
        {
            match control_token_file(Path::new(&file)) {
                Ok(token) => token,
                Err(_) => return (
                    "401 Unauthorized",
                    r#"{"ok":false,"error":"Program control credential unavailable or invalid."}"#
                        .into(),
                ),
            }
        } else {
            String::new()
        };
    let cookie = if token.is_empty() {
        String::new()
    } else {
        format!("Cookie: ic_dock={token}\r\n")
    };
    let result = tokio::time::timeout(std::time::Duration::from_secs(3), async {
        let mut socket = tokio::net::TcpStream::connect((std::net::Ipv4Addr::LOCALHOST, port)).await?;
        let action = if action == "cancel-cut" { "cut-after/cancel" } else { action };
        let request = format!("{method} /{action} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{cookie}Content-Type: application/x-www-form-urlencoded\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
        socket.write_all(request.as_bytes()).await?;
        let mut bytes = Vec::new();
        socket.take(1_048_577).read_to_end(&mut bytes).await?;
        if bytes.len() > 1_048_576 { return Err(invalid("oversized relay response")); }
        String::from_utf8(bytes).map_err(|_| invalid("invalid relay response"))
    }).await;
    match result {
        Ok(Ok(response)) => desk_response(&response),
        _ => unavailable(),
    }
}

fn desk_response(response: &str) -> (&'static str, String) {
    let unavailable = || {
        (
            "503 Service Unavailable",
            r#"{"ok":false,"error":"Relay unavailable; check its user service."}"#.into(),
        )
    };
    let Some((headers, body)) = response.split_once("\r\n\r\n") else {
        return unavailable();
    };
    // This is a bounded response from a configured fixed-loopback control
    // endpoint. Keep its JSON reason and known status rather than replacing a
    // buffer/phase rejection with advice that cannot resolve it.
    let status = match headers.lines().next().and_then(|line| line.split_whitespace().nth(1)) {
        Some("200") => "200 OK",
        Some("400") => "400 Bad Request",
        Some("404") => "404 Not Found",
        Some("409") => "409 Conflict",
        Some("500") => "500 Internal Server Error",
        Some("503") => "503 Service Unavailable",
        Some("401" | "403") => return (
            "401 Unauthorized",
            r#"{"ok":false,"error":"Program authentication required; check its control credential."}"#.into(),
        ),
        _ => return unavailable(),
    };
    (status, body.into())
}

/// Keep delay controls, private overlay files and docks usable. Configuration,
/// updates, OBS integration, process launching and lifecycle belong to Nix.
pub fn request_allowed(method: &str, path: &str) -> bool {
    if !enabled() {
        return true;
    }
    if path.starts_with("/desk/") {
        return crate::managed_routes::control(method, path);
    }
    match method {
        "GET" => {
            matches!(
                path,
                "/" | "/dock"
                    | "/login"
                    | "/dock.js"
                    | "/overlay-runtime.js"
                    | "/state"
                    | "/config"
                    | "/platforms"
                    | "/profiles"
                    | "/logs"
                    | "/destinations"
                    | "/events"
                    | "/overlay-state"
                    | "/overlay-events"
                    | "/overlay"
                    | "/overlays"
                    | "/docks"
                    | "/twitch_ingests"
            ) || path.starts_with("/overlay/")
                || path.starts_with("/docks/")
        }
        "POST" => {
            matches!(
                path,
                "/login"
                    | "/logout"
                    | "/arm"
                    | "/activate"
                    | "/stop"
                    | "/disarm"
                    | "/delay"
                    | "/go-live"
                    | "/cut-after"
                    | "/cut-after/cancel"
                    | "/logs/clear"
            ) || path.starts_with("/docks/")
                || path.starts_with("/overlays/")
        }
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn preparation_never_chmods_a_shared_directory() {
        let dir =
            std::env::temp_dir().join(format!("instantclone-shared-test-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o755)).unwrap();
        assert!(private_dir(&dir).is_err());
        assert_eq!(
            fs::metadata(&dir).unwrap().permissions().mode() & 0o777,
            0o755
        );
        fs::remove_dir_all(dir).unwrap();
    }

    #[tokio::test]
    async fn desk_rejects_arbitrary_targets_and_mutations() {
        let settings = crate::config::Settings::defaults();
        for (method, path, body) in [
            ("GET", "/desk/portrait/config", ""),
            ("POST", "/desk/landscape/destinations", ""),
            ("POST", "/desk/portrait/arm", "ms=600001"),
            ("POST", "/desk/portrait/arm", "ms=1&extra=true"),
        ] {
            assert_eq!(
                desk_request(method, path, body, &settings).await.0,
                "400 Bad Request"
            );
        }
        assert_eq!(
            desk_request("GET", "/desk/example.com/state", "", &settings)
                .await
                .0,
            "404 Not Found"
        );
    }
    #[test]
    fn validates_secret_bytes_without_disclosing_them() {
        let dir =
            std::env::temp_dir().join(format!("instantclone-secret-test-{}", std::process::id()));
        private_dir(&dir).unwrap();
        let path = dir.join("key");
        for bytes in [b"test-token".as_slice(), b"test-token\n", b"test-token\r\n"] {
            fs::write(&path, bytes).unwrap();
            assert_eq!(secret(&path, "test").unwrap(), "test-token");
        }
        for bytes in [
            b"".as_slice(),
            b"\n",
            b"bad\nkey",
            b"bad\0key",
            b"bad/key",
            b" key",
            b"key\n\n",
        ] {
            fs::write(&path, bytes).unwrap();
            let err = secret(&path, "test").unwrap_err().to_string();
            assert!(err.starts_with("InstantClone destination 'test':"));
            assert!(!err.contains("bad"));
        }
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn validates_server_url_bytes_without_disclosing_them() {
        let dir =
            std::env::temp_dir().join(format!("instantclone-server-test-{}", std::process::id()));
        private_dir(&dir).unwrap();
        let path = dir.join("server");
        for text in [
            "rtmp://host/live",
            "rtmps://host:443/app/",
            "rtmp://127.0.0.1:1935/live2",
            "rtmp://host/live\n",
            "rtmps://host/app\r\n",
            "rtmp://host/group/app",
        ] {
            fs::write(&path, text).unwrap();
            assert_eq!(
                server_url(&path, "test", "custom").unwrap(),
                text.trim_end_matches(['\r', '\n'])
            );
        }
        for bytes in [
            b"".as_slice(),
            b"\n",
            b"rtmp://host/live//key",
            b"rtmp://u:secret@host/live",
            b"rtmp://host/live?key=secret",
            b"rtmp://host:65536/live",
            b"rtmp://host:0/live",
            b"rtmp://host/app\nconfigured=true",
            b"https://host/app",
            b"rtmp://ho st/live",
            b"rtmp:///live",
            b"rtmp://host/",
        ] {
            fs::write(&path, bytes).unwrap();
            let err = server_url(&path, "test", "custom").unwrap_err().to_string();
            assert!(err.starts_with("InstantClone destination 'test':"));
            assert!(!String::from_utf8_lossy(bytes)
                .split_whitespace()
                .any(|word| word.len() > 4 && err.contains(word)));
        }
        for text in ["rtmps://host", "rtmps://host/", "rtmps://host:443"] {
            fs::write(&path, text).unwrap();
            assert_eq!(server_url(&path, "test", "kick").unwrap(), text);
            assert!(server_url(&path, "test", "custom").is_err());
        }
        fs::remove_dir_all(dir).unwrap();
    }
}
