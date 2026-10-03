//! Opt-in Nix/systemd integration. No secret is read until process startup.
use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::Path;

pub fn enabled() -> bool {
    std::env::var("INSTANTCLONE_MANAGED").as_deref() == Ok("1")
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn private_dir(path: &Path) -> io::Result<()> {
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
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))
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
/// credentials, query strings or an embedded stream key. Never echo the value.
fn server_url(path: &Path, name: &str) -> io::Result<String> {
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
            "server-URL secret must be rtmp(s)://host[:port]/app without credentials, query or stream key",
        ));
    }
    let text = std::str::from_utf8(&bytes).map_err(|_| {
        error("server-URL secret must be rtmp(s)://host[:port]/app without credentials, query or stream key")
    })?;
    let rest = text
        .strip_prefix("rtmp://")
        .or_else(|| text.strip_prefix("rtmps://"))
        .ok_or_else(|| {
            error("server-URL secret must be rtmp(s)://host[:port]/app without credentials, query or stream key")
        })?;
    let valid = (|| {
        let (authority, app) = rest.split_once('/')?;
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
        if app.is_empty()
            || app.contains('/')
            || !app
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
        {
            return None;
        }
        // ponytail: single-app-segment check only; full URL semantics belong to upstream.
        Some(())
    })();
    if valid.is_none() || !text.bytes().all(|b| b.is_ascii_graphic()) {
        return Err(error(
            "server-URL secret must be rtmp(s)://host[:port]/app without credentials, query or stream key",
        ));
    }
    Ok(text.to_owned())
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
    if !path.is_absolute() || !path.starts_with(&runtime) {
        return Err(invalid(
            "InstantClone: CONFIG_PATH must be inside XDG_RUNTIME_DIR",
        ));
    }
    private_dir(
        path.parent()
            .ok_or_else(|| invalid("InstantClone: invalid CONFIG_PATH"))?,
    )?;
    let text = fs::read_to_string(template)?;
    let fields: BTreeMap<_, _> = text
        .lines()
        .filter_map(|line| line.split_once('='))
        .collect();
    let mut rendered = String::new();
    for line in text.lines() {
        if let Some((key, value)) = line.split_once('=') {
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
                    server_url(&secret_path, name)?
                ));
                continue;
            }
            if key == "buffer_path" || key == "overlays_dir" {
                let directory = if key == "buffer_path" {
                    Path::new(value)
                        .parent()
                        .ok_or_else(|| invalid("InstantClone: invalid buffer path"))?
                } else {
                    Path::new(value)
                };
                private_dir(directory)?;
            }
        }
        rendered.push_str(line);
        rendered.push('\n');
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

/// Same-origin, fixed-target bridge for the two independent local relays.
/// Never accepts a URL, forwards credentials, or proxies configuration/logs.
pub async fn desk_request(method: &str, path: &str, body: &str) -> (&'static str, String) {
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
    let allowed = match method {
        "GET" => matches!(action, "state" | "destinations"),
        "POST" => matches!(
            action,
            "arm" | "activate" | "disarm" | "stop" | "cut-after" | "cancel-cut"
        ),
        _ => false,
    };
    if !allowed
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
    let result = tokio::time::timeout(std::time::Duration::from_secs(3), async {
        let mut socket = tokio::net::TcpStream::connect((std::net::Ipv4Addr::LOCALHOST, port)).await?;
        let action = if action == "cancel-cut" { "cut-after/cancel" } else { action };
        let request = format!("{method} /{action} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
        socket.write_all(request.as_bytes()).await?;
        let mut bytes = Vec::new();
        socket.take(1_048_577).read_to_end(&mut bytes).await?;
        if bytes.len() > 1_048_576 { return Err(invalid("oversized relay response")); }
        String::from_utf8(bytes).map_err(|_| invalid("invalid relay response"))
    }).await;
    match result {
        Ok(Ok(response)) if response.starts_with("HTTP/1.1 200 ") => {
            match response.split_once("\r\n\r\n") {
                Some((_, json)) => ("200 OK", json.into()),
                None => unavailable(),
            }
        }
        Ok(Ok(_)) => ("409 Conflict", r#"{"ok":false,"error":"Relay rejected the action; refresh its state before retrying."}"#.into()),
        _ => unavailable(),
    }
}

/// Keep delay controls, private overlay files and docks usable. Configuration,
/// updates, OBS integration, process launching and lifecycle belong to Nix.
pub fn request_allowed(method: &str, path: &str) -> bool {
    if !enabled() {
        return true;
    }
    if path.starts_with("/desk/") {
        return matches!(method, "GET" | "POST");
    }
    match method {
        "GET" => {
            matches!(
                path,
                "/" | "/dock"
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
                "/arm"
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
    #[tokio::test]
    async fn desk_rejects_arbitrary_targets_and_mutations() {
        for (method, path, body) in [
            ("GET", "/desk/portrait/config", ""),
            ("POST", "/desk/landscape/destinations", ""),
            ("POST", "/desk/portrait/arm", "ms=600001"),
            ("POST", "/desk/portrait/arm", "ms=1&extra=true"),
        ] {
            assert_eq!(desk_request(method, path, body).await.0, "400 Bad Request");
        }
        assert_eq!(
            desk_request("GET", "/desk/example.com/state", "").await.0,
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
        ] {
            fs::write(&path, text).unwrap();
            assert_eq!(
                server_url(&path, "test").unwrap(),
                text.trim_end_matches(['\r', '\n'])
            );
        }
        for bytes in [
            b"".as_slice(),
            b"\n",
            b"rtmp://host/live/key",
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
            let err = server_url(&path, "test").unwrap_err().to_string();
            assert!(err.starts_with("InstantClone destination 'test':"));
            assert!(!String::from_utf8_lossy(bytes)
                .split_whitespace()
                .any(|word| word.len() > 4 && err.contains(word)));
        }
        fs::remove_dir_all(dir).unwrap();
    }
}
