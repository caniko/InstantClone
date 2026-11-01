{
  description = "InstantClone managed Broadcast Desk and local-only qualification";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/56c02bc00adcf003215cc4bd996d6efaf4cff188";
  outputs = {
    self,
    nixpkgs,
  }: let
    system = "x86_64-linux";
    pkgs = nixpkgs.legacyPackages.${system};
    package = pkgs.callPackage ./nix/package.nix {src = self;};
    template = pkgs.writeText "instantclone-public-fixture" "configured=true\n";
  in {
    packages.${system} = {
      default = package;
      instantclone = package;
    };
    checks.${system} = {
      rust = package;
      runtime =
        pkgs.runCommand "instantclone-runtime" {
          nativeBuildInputs = [pkgs.python3 pkgs.age pkgs.ffmpeg-headless pkgs.stdenv.cc];
          packageDerivation = builtins.unsafeDiscardOutputDependency package.drvPath;
        } ''
          python ${./tests/managed/runtime.py} ${pkgs.lib.getExe package} ${template} "$packageDerivation"
          python ${self}/tests/managed/listener_failure.py ${pkgs.lib.getExe package}
          touch "$out"
        '';
      runtime-dual =
        pkgs.runCommand "instantclone-runtime-dual" {
          nativeBuildInputs = [pkgs.python3 pkgs.age pkgs.ffmpeg-headless];
          packageDerivation = builtins.unsafeDiscardOutputDependency package.drvPath;
        } ''
          python ${./tests/managed/runtime_dual.py} ${pkgs.lib.getExe package} "$packageDerivation"
          touch "$out"
        '';
      managed-security =
        pkgs.runCommand "instantclone-managed-security" {
          nativeBuildInputs = [pkgs.python3 pkgs.ffmpeg-headless pkgs.nodejs pkgs.chromium];
          FONTCONFIG_FILE = pkgs.makeFontsConf {
            fontDirectories = [pkgs.dejavu_fonts];
          };
        } ''
          mkdir -p "$out"
          python ${self}/tests/managed/paths.py ${pkgs.lib.getExe package}
          python ${self}/tests/managed/auth.py ${pkgs.lib.getExe package} ${pkgs.lib.getExe pkgs.chromium} "$out/real-desk.png"
        '';
      desk =
        pkgs.runCommand "instantclone-desk" {
          nativeBuildInputs = [pkgs.nodejs pkgs.python3 pkgs.chromium];
          FONTCONFIG_FILE = pkgs.makeFontsConf {
            fontDirectories = [pkgs.dejavu_fonts];
          };
        } ''
          mkdir -p "$out"
          DESK_JS=${./src/managed-desk.js} node --test ${./tests/managed/desk.test.cjs}
          python ${self}/tests/managed/desk_browser.py ${pkgs.lib.getExe pkgs.chromium} "$out/desk.png"
        '';
    };
  };
}
