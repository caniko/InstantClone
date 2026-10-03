"""Startup path validation must reject before mutating any managed directory."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def run(binary):
    with tempfile.TemporaryDirectory(prefix='instantclone-paths-') as tmp:
        root = Path(tmp)
        runtime = root / 'runtime'
        runtime.mkdir(mode=0o700)
        shared = root / 'shared'
        shared.mkdir(mode=0o755)
        shared.chmod(0o755)
        (root / 'link').symlink_to(shared, target_is_directory=True)
        template = root / 'template'
        safe_config = runtime / 'new-instance/config'
        cases = [
            (safe_config, 'stream.buf', root / 'new-overlays'),
            (safe_config, root / 'shared/stream.buf', root / 'new-overlays'),
            (safe_config, root / 'new-cache/stream.buf', shared),
            (safe_config, root / 'link/stream.buf', root / 'new-overlays'),
            (safe_config, root / 'new-cache/stream.buf', root / 'link/overlays'),
            (safe_config, f'{root}/new-cache/../shared/stream.buf', root / 'new-overlays'),
            (f'{runtime}/../escape/config', root / 'new-cache/stream.buf', root / 'new-overlays'),
            (runtime / 'config', root / 'new-cache/stream.buf', root / 'new-overlays'),
            (safe_config, root / 'new-cache/stream.buf', '.'),
        ]
        for config, buffer, overlays in cases:
            # Whitespace must be parsed exactly as Settings::load parses it.
            template.write_text(f'configured=true\n buffer_path = {buffer} \n overlays_dir = {overlays} \n')
            env = os.environ | {'INSTANTCLONE_MANAGED':'1','INSTANTCLONE_TEMPLATE':str(template),
                                'XDG_RUNTIME_DIR':str(runtime),'CONFIG_PATH':str(config)}
            result = subprocess.run([binary,'--no-browser'],env=env,cwd=shared,
                                    capture_output=True,text=True,timeout=5)
            assert result.returncode != 0, 'unsafe managed path was accepted'
            assert shared.stat().st_mode & 0o777 == 0o755, 'shared directory mode changed'
            for untouched in ['runtime/new-instance','new-cache','new-overlays','escape']:
                assert not (root / untouched).exists(), f'validation mutated {untouched}'
            assert not (shared / 'stream.buf').exists()
        # The config itself and any intermediate parent must not be symlinks.
        (runtime / 'linked-instance').symlink_to(shared, target_is_directory=True)
        template.write_text(f'buffer_path={root}/new-cache/stream.buf\noverlays_dir={root}/new-overlays\n')
        for config in [runtime / 'linked-instance/config', runtime / 'linked-instance']:
            env = os.environ | {'INSTANTCLONE_MANAGED':'1','INSTANTCLONE_TEMPLATE':str(template),
                                'XDG_RUNTIME_DIR':str(runtime),'CONFIG_PATH':str(config)}
            assert subprocess.run([binary,'--no-browser'],env=env,cwd=shared,
                                  capture_output=True,timeout=5).returncode != 0
            assert shared.stat().st_mode & 0o777 == 0o755
            assert not (root / 'new-cache').exists()
        print('PASS: relative/traversing/symlink/shared paths rejected before directory creation or chmod')


if __name__ == '__main__':
    run(sys.argv[1])
