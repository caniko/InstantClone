"""Startup path validation must reject before mutating any managed directory."""
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from runtime_dual import port, stop, wait


def disabled_credentials(binary, root, runtime):
    template = root / 'disabled-template'
    config = runtime / 'disabled-instance/config'
    key, server = root / 'retired.key', root / 'retired.server'
    key.write_text('disposable-retired-key')
    server.write_text('rtmp://127.0.0.1:1/live')
    key.chmod(0o600)
    server.chmod(0o600)
    env = os.environ | {'INSTANTCLONE_MANAGED':'1','INSTANTCLONE_TEMPLATE':str(template),
                        'XDG_RUNTIME_DIR':str(runtime),'CONFIG_PATH':str(config)}
    cases = [
        (root / 'missing-key', root / 'missing-server', 'before'),
        (key, root / 'missing-server', 'after'),
        (key, server, 'override'),
    ]
    for key_path, server_path, placement in cases:
        web, ingest = port(), port()
        directives = (f'destination.0.stream_key_file={key_path}\n'
                      f'destination.0.custom_egress_url_file={server_path}\n')
        declarations = 'destination.0.enabled=false\n'
        if placement == 'before':
            declarations += directives
        else:
            declarations = ('destination.0.enabled=true\n' if placement == 'override' else '') + directives + declarations
        text = (f'configured=true\nbuffer_path={root}/disabled-cache/stream.buf\n'
                f'overlays_dir={root}/disabled-overlays\nweb_port={web}\ningest_port={ingest}\nbuffer_mb=50\n'
                '# comments without equals and blank lines remain valid\n\n'
                'update_check_enabled=false\nopen_dashboard_on_launch=false\n'
                'destination.0.name=Retired\ndestination.0.id=retired\ndestination.0.platform=custom\n'
                + declarations)
        template.write_text(text)
        with (root / f'disabled-{placement}.log').open('wb') as log:
            proc = subprocess.Popen([binary,'--no-browser'],env=env,cwd=root,stdout=log,stderr=log)
        try:
            def fetch(path, web=web):
                connection = http.client.HTTPConnection('127.0.0.1',web,timeout=2)
                try:
                    connection.request('GET',path)
                    response = connection.getresponse()
                    assert response.status == 200
                    return json.loads(response.read())
                finally:
                    connection.close()
            wait(lambda: fetch('/state').get('destinations'), 5)
            destination = fetch('/state')['destinations'][0]
            assert destination['enabled'] is False and destination['alive'] is False
            assert fetch('/config')['destinations'][0]['stream_key_set'] is False
            rendered = config.read_text()
            assert 'stream_key_file=' not in rendered and 'custom_egress_url_file=' not in rendered
            fields = dict(line.split('=', 1) for line in rendered.splitlines() if '=' in line and not line.startswith('#'))
            assert not fields.get('destination.0.stream_key') and not fields.get('destination.0.custom_egress_url')
            assert 'disposable-retired-key' not in rendered and 'rtmp://127.0.0.1:1/live' not in rendered
            assert proc.poll() is None
        finally:
            stop(proc)
        if not key_path.exists() or not server_path.exists():
            template.write_text(text + 'destination.0.enabled=true\n')
            result = subprocess.run([binary,'--no-browser'],env=env,cwd=root,capture_output=True,timeout=5,check=False)
            assert result.returncode != 0 and b'secret is missing or unreadable' in result.stderr
            assert config.read_text() == rendered, 'failed re-enable overwrote the valid runtime config'
    print('PASS: final disabled destinations omit retained credentials regardless of declaration order; re-enable requires secrets')


def unused_custom_urls(binary, root, runtime):
    key = root / 'url-test.key'
    key.write_text('disposable-platform-key')
    template = root / 'url-template'
    cases = [
        ('twitch', True, ''), ('youtube', True, ''), ('sink', True, ''),
        ('twitch', True, 'obsolete-endpoint'), (None, True, ''),
        ('custom', False, ''), ('custom', False, 'obsolete-endpoint'),
        ('kick', False, 'obsolete-endpoint'),
        ('custom', True, ''), ('kick', True, ''),
        ('custom', True, 'rtmp://user:disposable-platform-key@host/live'),
    ]
    for index, (platform, enabled, url) in enumerate(cases):
        config = runtime / f'url-instance-{index}/config'
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            ingest = occupied.getsockname()[1]
            # Stop at the real ingest preflight after template preparation,
            # before any runtime/egress/provider activity can start.
            text = (f'configured=true\ningest_port={ingest}\nweb_port={port()}\n'
                    f'buffer_path={root}/url-cache/stream.buf\noverlays_dir={root}/url-overlays\n'
                    'buffer_mb=50\ningest_bind_all=false\nweb_bind_all=false\n'
                    'tracing_enabled=false\nupdate_check_enabled=false\nopen_dashboard_on_launch=false\n'
                    'destination.0.name=Platform\ndestination.0.id=platform\n'
                    'destination.0.enabled=true\n'
                    + ('destination.0.platform=custom\n' if platform else '')
                    + f'destination.0.stream_key_file={key}\n'
                    f'destination.0.custom_egress_url={url}\n'
                    + (f'destination.0.platform={platform}\n' if platform else '')
                    + f'destination.0.enabled={str(enabled).lower()}\n')
            uses_url = enabled and platform in ['custom', 'kick']
            if not uses_url:
                text += f'destination.0.custom_egress_url_file={root}/unavailable-server\n'
            template.write_text(text)
            env = os.environ | {'INSTANTCLONE_MANAGED':'1','INSTANTCLONE_TEMPLATE':str(template),
                                'XDG_RUNTIME_DIR':str(runtime),'CONFIG_PATH':str(config)}
            result = subprocess.run([binary,'--no-browser'],env=env,cwd=root,capture_output=True,timeout=5,check=False)
            assert result.returncode != 0
            if uses_url:
                assert b'server URL must be' in result.stderr, (platform, url)
                assert not config.exists(), 'active custom URL failure persisted settings'
            else:
                assert b'configured ingest port is unavailable' in result.stderr, (platform, url, result.stderr)
                assert config.exists(), 'unused custom URL prevented template preparation'
                rendered = config.read_text()
                assert 'destination.0.custom_egress_url' not in rendered
                assert 'obsolete-endpoint' not in rendered
            assert b'disposable-platform-key' not in result.stderr, 'URL validation echoed credentials'
    print('PASS: unused/disabled custom URL metadata is omitted; active custom/Kick URL validation remains strict without provider contact')


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
                                    capture_output=True,text=True,timeout=5,check=False)
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
                                  capture_output=True,timeout=5,check=False).returncode != 0
            assert shared.stat().st_mode & 0o777 == 0o755
            assert not (root / 'new-cache').exists()
        print('PASS: relative/traversing/symlink/shared paths rejected before directory creation or chmod')
        template.write_text(f'configured=true\nbuffer_path={root}/cache/stream.buf\noverlays_dir={root}/overlays\n'
                            'destination.0.name=Missing key\ndestination.0.id=missing\n'
                            'destination.0.enabled=true\ndestination.0.platform=custom\n'
                            'destination.0.custom_egress_url=rtmp://host.invalid/group/app\n')
        env = os.environ | {'INSTANTCLONE_MANAGED':'1','INSTANTCLONE_TEMPLATE':str(template),
                            'XDG_RUNTIME_DIR':str(runtime),'CONFIG_PATH':str(runtime / 'instance/config')}
        result = subprocess.run([binary,'--no-browser'],env=env,cwd=root,capture_output=True,timeout=5,check=False)
        assert result.returncode != 0, 'multi-segment server with no separate key accepted'
        assert b'Missing key' in result.stderr, 'missing-key diagnostic lost destination identity'
        assert b'separate stream key required' in result.stderr
        for untouched in ['runtime/instance','cache','overlays']:
            assert not (root / untouched).exists(), f'missing-key validation mutated {untouched}'
        print('PASS: managed multi-segment server cannot be mistaken for an embedded key')
        web, ingest = port(), port()
        template.write_text(template.read_text().replace('destination.0.enabled=true','destination.0.enabled=false')
                            + f'web_port={web}\ningest_port={ingest}\nbuffer_mb=50\n'
                            + 'update_check_enabled=false\nopen_dashboard_on_launch=false\n')
        with (root / 'disabled.log').open('wb') as log:
            proc = subprocess.Popen([binary,'--no-browser'],env=env,cwd=root,stdout=log,stderr=log)
        try:
            def state():
                connection = http.client.HTTPConnection('127.0.0.1',web,timeout=2)
                try:
                    connection.request('GET','/state')
                    response = connection.getresponse()
                    assert response.status == 200
                    return json.loads(response.read())
                finally:
                    connection.close()
            wait(lambda: state().get('destinations'))
            destinations = state()['destinations']
            assert len(destinations) == 1 and destinations[0]['enabled'] is False
            assert destinations[0]['alive'] is False
            assert proc.poll() is None
        finally:
            stop(proc)
        print('PASS: disabled managed metadata starts without keys or an egress connection')
        disabled_credentials(binary, root, runtime)
        unused_custom_urls(binary, root, runtime)


if __name__ == '__main__':
    run(sys.argv[1])
