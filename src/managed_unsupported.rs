//! Managed Linux services are opt-in; other platforms retain upstream behavior.
pub fn enabled() -> bool {
    false
}
pub fn prepare() -> std::io::Result<()> {
    Ok(())
}
pub fn request_allowed(_: &str, _: &str) -> bool {
    true
}
pub async fn desk_request(_: &str, _: &str, _: &str) -> (&'static str, String) {
    ("404 Not Found", "{}".into())
}
