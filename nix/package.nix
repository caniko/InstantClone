{
  lib,
  rustPlatform,
  pkg-config,
  openssl,
  cacert,
  makeWrapper,
  src,
}:
rustPlatform.buildRustPackage {
  pname = "instantclone";
  version = "0.1.14-managed";
  inherit src;
  cargoLock.lockFile = src + "/Cargo.lock";
  nativeBuildInputs = [pkg-config makeWrapper];
  buildInputs = [openssl];
  postInstall = ''
    wrapProgram "$out/bin/instantclone" \
      --set-default SSL_CERT_FILE ${cacert}/etc/ssl/certs/ca-bundle.crt
  '';
  passthru.instantcloneManaged = true;
  meta = {
    description = "RTMP relay with a managed dual-program OBS Broadcast Desk";
    homepage = "https://github.com/caniko/InstantClone";
    license = lib.licenses.gpl3Only;
    platforms = lib.platforms.linux;
    mainProgram = "instantclone";
  };
}
