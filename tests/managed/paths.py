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
            if platform == 'sink' and enabled:
                assert b'managed local sinks require isolated ports' in result.stderr
                assert not config.exists(), 'non-isolated sink persisted settings'
            elif uses_url:
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


def superseded_credentials(binary, root, runtime):
    template = root / 'layered-template'
    credentials = {
        'ingest_key': 'disposable-effective-ingest',
        'dock_token': 'abcd' * 16,
        'dashboard_password_hash': 'pbkdf2-sha256$1000$' + 'ab' * 16 + '$' + 'cd' * 32,
        'discord_webhook_url': 'https://127.0.0.1/disposable-webhook',
        'destination.0.stream_key': 'disposable-effective-key',
        'destination.0.custom_egress_url': 'rtmp://127.0.0.1:1/live',
    }
    files = {}
    for field, value in credentials.items():
        path = root / (field + '.credential')
        path.write_text(value)
        path.chmod(0o600)
        files[field + '_file'] = path
    invalid = root / 'superseded-invalid-credential'
    invalid.write_text('disposable invalid credential\nsecond line')
    invalid.chmod(0o600)
    missing = root / 'superseded-missing-credential'
    for index, field in enumerate(files):
        config = runtime / f'layered-instance-{index}/config'
        env = os.environ | {'INSTANTCLONE_MANAGED':'1','INSTANTCLONE_TEMPLATE':str(template),
                            'XDG_RUNTIME_DIR':str(runtime),'CONFIG_PATH':str(config)}
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            header = (f'configured=true\ningest_port={occupied.getsockname()[1]}\nweb_port={port()}\n'
                      f'buffer_path={root}/layered-cache/stream.buf\noverlays_dir={root}/layered-overlays\n'
                      'buffer_mb=50\ningest_bind_all=false\nweb_bind_all=false\n'
                      'update_check_enabled=false\nopen_dashboard_on_launch=false\n'
                      'destination.0.name=Layered\ndestination.0.id=layered\n'
                      'destination.0.enabled=true\ndestination.0.platform=custom\n')
            final = ''.join(f' {name} = {path} \n' * 2 for name, path in files.items())
            for stale in [missing, invalid]:
                template.write_text(header + f'{field}={stale}\n# replaced credential\n\n' + final)
                result = subprocess.run([binary,'--no-browser'],env=env,cwd=root,capture_output=True,timeout=5,check=False)
                assert result.returncode != 0 and b'configured ingest port is unavailable' in result.stderr, (field, result.stderr)
                rendered = config.read_text()
                declarations = [line.split('=', 1) for line in rendered.splitlines() if '=' in line and not line.startswith('#')]
                for name, value in credentials.items():
                    assert [v for k, v in declarations if k == name] == [value], (field, name)
                assert 'superseded-' not in rendered and 'disposable invalid credential' not in rendered
            template.write_text(header + final + f'{field}={missing}\n')
            result = subprocess.run([binary,'--no-browser'],env=env,cwd=root,capture_output=True,timeout=5,check=False)
            assert result.returncode != 0 and b'configured ingest port is unavailable' not in result.stderr
            assert config.read_text() == rendered, 'unavailable final credential replaced prior runtime settings'
            assert all(value.encode() not in result.stderr for value in credentials.values())
    print('PASS: superseded missing/malformed credential files are skipped; final credentials render once and remain mandatory')


def url_source_overrides(binary, root, runtime):
    template = root / 'url-source-template'
    key = root / 'url-source.key'
    key.write_text('disposable-url-source-key')
    key.chmod(0o600)
    server = root / 'url-source.server'
    server.write_text('rtmp://127.0.0.1:1/file-app')
    server.chmod(0o600)
    invalid = root / 'url-source-invalid.server'
    invalid.write_text('retired malformed server\nsecond line')
    invalid.chmod(0o600)
    missing = root / 'url-source-missing.server'
    inline = 'rtmp://127.0.0.1:1/inline-app'
    cases = [(f'custom_egress_url_file={stale}\ncustom_egress_url={inline}', inline)
             for stale in [missing, invalid, server]]
    cases += [(f'custom_egress_url={stale}\ncustom_egress_url_file={server}', server.read_text())
              for stale in ['', 'obsolete-server', inline]]
    for platform in ['custom', 'kick']:
        config = runtime / f'url-source-{platform}/config'
        env = os.environ | {'INSTANTCLONE_MANAGED':'1','INSTANTCLONE_TEMPLATE':str(template),
                            'XDG_RUNTIME_DIR':str(runtime),'CONFIG_PATH':str(config)}
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            header = (f'configured=true\ningest_port={occupied.getsockname()[1]}\nweb_port={port()}\n'
                      f'buffer_path={root}/url-source-cache/stream.buf\noverlays_dir={root}/url-source-overlays\n'
                      'buffer_mb=50\ningest_bind_all=false\nweb_bind_all=false\n'
                      'update_check_enabled=false\nopen_dashboard_on_launch=false\n'
                      'destination.0.name=URL source\ndestination.0.id=url-source\ndestination.0.enabled=true\n'
                      f'destination.0.platform={platform}\ndestination.0.stream_key_file={key}\n')
            for directives, expected in cases:
                template.write_text(header + ''.join(f'destination.0.{line}\n' for line in directives.splitlines()))
                result = subprocess.run([binary,'--no-browser'],env=env,cwd=root,capture_output=True,timeout=5,check=False)
                assert result.returncode != 0 and b'configured ingest port is unavailable' in result.stderr, (platform, result.stderr)
                rendered = config.read_text()
                values = [line.split('=', 1)[1].strip() for line in rendered.splitlines()
                          if line.startswith('destination.0.custom_egress_url=')]
                assert values == [expected], (platform, values)
                assert 'custom_egress_url_file=' not in rendered and 'obsolete-server' not in rendered
                assert 'retired malformed server' not in rendered
            for final in [f'custom_egress_url_file={missing}', 'custom_egress_url=obsolete-server']:
                template.write_text(header + f'destination.0.custom_egress_url={inline}\n'
                                    + f'destination.0.custom_egress_url_file={server}\n' + f'destination.0.{final}\n')
                result = subprocess.run([binary,'--no-browser'],env=env,cwd=root,capture_output=True,timeout=5,check=False)
                assert result.returncode != 0 and b'configured ingest port is unavailable' not in result.stderr
                assert config.read_text() == rendered, 'invalid final URL source replaced runtime config'
                assert key.read_bytes() not in result.stderr
    print('PASS: Custom/Kick URL file and inline forms share final-source semantics; invalid final sources preserve runtime config')


def sink_credentials(binary, root, runtime):
    template = root / 'sink-template'
    valid = root / 'sink-retired.key'
    valid.write_text('disposable-retired-sink-key')
    valid.chmod(0o600)
    invalid = root / 'sink-invalid.key'
    invalid.write_text('invalid retired key\nsecond line')
    invalid.chmod(0o600)
    missing = root / 'sink-missing.key'
    for index, stale in enumerate([missing, invalid, valid]):
        config = runtime / f'sink-instance-{index}/config'
        env = os.environ | {'INSTANTCLONE_MANAGED':'1','INSTANTCLONE_TEMPLATE':str(template),
                            'XDG_RUNTIME_DIR':str(runtime),'CONFIG_PATH':str(config)}
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            header = (f'configured=true\ningest_port={occupied.getsockname()[1]}\nweb_port={port()}\n'
                      f'buffer_path={root}/sink-cache/stream.buf\noverlays_dir={root}/sink-overlays\n'
                      'buffer_mb=50\ningest_bind_all=false\nweb_bind_all=false\n'
                      'update_check_enabled=false\nopen_dashboard_on_launch=false\n'
                      'destination.0.name=Sink\ndestination.0.id=sink\ndestination.0.enabled=false\n'
                      'destination.0.platform=custom\n')
            for declarations in ['before', 'after']:
                sink = 'destination.0.platform=sink\ndestination.0.enabled=true\n'
                credential = f'destination.0.stream_key_file={stale}\n'
                text = header + (sink + credential if declarations == 'before' else credential + sink)
                template.write_text(text)
                prior = config.read_text() if config.exists() else None
                result = subprocess.run([binary,'--no-browser'],env=env,cwd=root,capture_output=True,timeout=5,check=False)
                assert result.returncode != 0 and b'managed local sinks require isolated ports' in result.stderr, result.stderr
                assert (config.read_text() if config.exists() else None) == prior, 'non-isolated sink changed runtime settings'
                if prior is None:
                    assert not (root / 'sink-cache').exists() and not (root / 'sink-overlays').exists()
                template.write_text(text + 'destination.0.platform=custom\ndestination.0.custom_egress_url=rtmp://127.0.0.1:1/live\n')
                result = subprocess.run([binary,'--no-browser'],env=env,cwd=root,capture_output=True,timeout=5,check=False)
                if stale != valid:
                    assert result.returncode != 0 and b'configured ingest port is unavailable' not in result.stderr
                    assert (config.read_text() if config.exists() else None) == prior, 'missing/malformed provider key changed runtime config'
                else:
                    assert result.returncode != 0 and b'configured ingest port is unavailable' in result.stderr
                    assert f'destination.0.stream_key={valid.read_text()}\n' in config.read_text()
                assert valid.read_bytes() not in result.stderr
    print('PASS: managed sinks reject before credential reads or state mutation; final provider configurations require credentials')


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
        superseded_credentials(binary, root, runtime)
        url_source_overrides(binary, root, runtime)
        sink_credentials(binary, root, runtime)


if __name__ == '__main__':
    run(sys.argv[1])
