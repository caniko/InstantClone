//! Portable least-privilege route policy for the managed Broadcast Desk.
pub fn control(method: &str, path: &str) -> bool {
    if method == "GET" && matches!(path, "/desk/app.js" | "/desk/info") {
        return true;
    }
    let parts: Vec<_> = path.split('/').collect();
    parts.len() == 4
        && parts[0].is_empty()
        && parts[1] == "desk"
        && matches!(parts[2], "landscape" | "portrait")
        && match method {
            "GET" => matches!(parts[3], "state" | "destinations"),
            "POST" => matches!(
                parts[3],
                "arm" | "activate" | "disarm" | "stop" | "cut-after" | "cancel-cut"
            ),
            _ => false,
        }
}
