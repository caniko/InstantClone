"""Local-only managed-mode/secret/forwarding checks. No provider is contacted."""
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait(check, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if check():
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    raise AssertionError('timed out waiting for local test condition')


def stop(proc):
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise AssertionError('process did not shut down cleanly')


def run(binary, store_template, package_derivation):
    with tempfile.TemporaryDirectory(prefix='instantclone-test-') as tmp:
        root = Path(tmp)
        runtime = root / 'runtime'
        runtime.mkdir(mode=0o700)
        config = runtime / 'instantclone/config'
        template = root / 'template'
        ingest, web, receiver_a, receiver_b = [port() for _ in range(4)]
        keys = [secrets.token_hex(24), secrets.token_hex(24)]
        ingest_key, control_token = secrets.token_hex(24), secrets.token_hex(32)
        control_file = root / 'control.token'
        control_file.write_text(control_token + '\n')
        control_file.chmod(0o600)
        key_paths = [root / 'a.key', root / 'b.key']
        # Real age encryption/decryption, using disposable test-only identities.
        identity = root / 'identity'
        subprocess.run(['age-keygen', '-o', str(identity)], capture_output=True, check=True)
        recipient = subprocess.check_output(['age-keygen', '-y', str(identity)], text=True).strip()
        for i, value in enumerate(keys):
            encrypted = root / f'{i}.age'
            subprocess.run(['age', '-r', recipient, '-o', str(encrypted)], input=(value + '\n').encode(), check=True)
            subprocess.run(['age', '-d', '-i', str(identity), '-o', str(key_paths[i]), str(encrypted)], check=True)
        # Destination 1 uses the secret-server profile: its URL is decrypted
        # from its own age file, exactly like a stream key.
        server_url = f'rtmp://127.0.0.1:{receiver_b}/live'
        server_path = root / 'server.key'
        encrypted_server = root / 'server.age'
        subprocess.run(['age', '-r', recipient, '-o', str(encrypted_server)], input=(server_url + '\n').encode(), check=True)
        subprocess.run(['age', '-d', '-i', str(identity), '-o', str(server_path), str(encrypted_server)], check=True)
        base = [
            'configured=true', f'ingest_port={ingest}', 'ingest_bind_all=false',
            f'ingest_key={ingest_key}', f'dock_token_file={control_file}',
            f'web_port={web}', 'web_bind_all=false', 'buffer_mb=50',
            f'buffer_path={root}/cache/stream.buf', f'overlays_dir={root}/state/overlays',
            'tracing_enabled=false', 'update_check_enabled=false', 'open_dashboard_on_launch=false',
        ]
        for i, receiver in enumerate([receiver_a, receiver_b]):
            prefix = f'destination.{i}'
            base += [f'{prefix}.id=local-{i}', f'{prefix}.name=local-{i}',
                     f'{prefix}.enabled=true', f'{prefix}.platform=custom',
                     f'{prefix}.audio_track=1', f'{prefix}.stream_format=horizontal',
                     f'{prefix}.stream_key_file={key_paths[i]}']
            if i == 1:
                base += [f'{prefix}.custom_egress_url_file={server_path}']
            else:
                base += [f'{prefix}.custom_egress_url=rtmp://127.0.0.1:{receiver}/live']
        template.write_text('\n'.join(base) + '\n')
        env = os.environ | {
            'XDG_RUNTIME_DIR': str(runtime), 'CONFIG_PATH': str(config),
            'INSTANTCLONE_MANAGED': '1', 'INSTANTCLONE_TEMPLATE': str(template),
            'INSTANTCLONE_NO_TRACE': '1',
        }
        logs = root / 'proxy.log'
        children = []
        def start():
            with logs.open('ab') as log:
                proc = subprocess.Popen([binary, '--no-browser'], env=env, cwd=runtime, stdout=log, stderr=log)
            children.append(proc)
            wait(lambda: proc.poll() is not None or request('/state')[0] == 200)
            assert proc.poll() is None, 'proxy exited before becoming healthy'
            return proc

        def request(path, method='GET'):
            req = urllib.request.Request(f'http://127.0.0.1:{web}{path}', method=method,
                                         headers={'Accept-Encoding': 'gzip'})
            try:
                with urllib.request.urlopen(req, timeout=1) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as error:
                return error.code, error.read()

        try:
            original_template = template.read_text()
            for setting, reason in [
                ('web_port=0', b'web_port must be > 0'),
                (f'web_port={ingest}', b'ingest_port and web_port must differ'),
                ('buffer_mb=1', b'buffer_mb must be at least'),
            ]:
                template.write_text(original_template + setting + '\n')
                result = subprocess.run([binary, '--no-browser'], env=env, cwd=runtime,
                                        capture_output=True, timeout=10, check=False)
                assert result.returncode != 0 and reason in result.stderr, setting
                assert not config.exists(), 'invalid declaration persisted a sanitized config'
            template.write_text(original_template)
            proc = start()
            assert config.stat().st_mode & 0o777 == 0o600
            assert config.parent.stat().st_mode & 0o777 == 0o700
            content = config.read_text()
            assert all(f'destination.{i}.stream_key={key}' in content for i, key in enumerate(keys))
            assert 'stream_key_file=' not in content
            assert f'destination.0.custom_egress_url=rtmp://127.0.0.1:{receiver_a}/live' in content
            assert f'destination.1.custom_egress_url={server_url}' in content
            assert 'custom_egress_url_file=' not in content
            with socket.create_connection(('127.0.0.1', ingest), timeout=2):
                pass
            # Exact IPv4 binds, no bonus IPv6 ingest listener in managed mode.
            for table in ['/proc/net/tcp', '/proc/net/tcp6']:
                for row in Path(table).read_text().splitlines()[1:]:
                    fields = row.split()
                    if fields[3] == '0A' and int(fields[1].split(':')[1], 16) in [ingest, web]:
                        assert table.endswith('/tcp') and fields[1].startswith('0100007F:')
            assert request('/')[0] == 200
            for path in ['/config', '/update/apply', '/app/restart', '/app/quit', '/obs/register', '/obs/setup-vod-eb']:
                assert request(path, 'POST')[0] == 403, path
            assert request('/update-check')[0] == 403
            for path in ['/state', '/config', '/destinations', '/logs']:
                body = request(path)[1]
                assert all(key.encode() not in body for key in keys)
                assert ingest_key.encode() not in body and control_token.encode() not in body, path
                assert server_url.encode() not in body, path
            command = Path(f'/proc/{proc.pid}/cmdline').read_bytes()
            assert all(key.encode() not in command for key in keys)
            environment = Path(f'/proc/{proc.pid}/environ').read_bytes()
            assert all(key.encode() not in environment for key in keys)
            # Two independent receivers; ffmpeg is only the test publisher.
            receivers = []
            for i, receiver in enumerate([receiver_a, receiver_b]):
                log = (root / f'sink-{i}.log').open('wb')
                child = subprocess.Popen([binary, 'sink', '--port', str(receiver), '--web-port', '0',
                                          '--file', str(root / f'{i}.flv'), '--max-mb', '5'],
                                         stdout=log, stderr=log, cwd=root)
                log.close()
                receivers.append(child)
                children.append(child)
            publisher = subprocess.Popen([
                'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-re',
                '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=10', '-f', 'lavfi',
                '-i', 'sine=frequency=1000:sample_rate=44100', '-t', '45',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency', '-g', '10',
                '-c:a', 'aac', '-f', 'flv', f'rtmp://127.0.0.1:{ingest}/live/{ingest_key}',
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            children.append(publisher)
            def received(i):
                path = root / f'{i}.flv'
                return path.exists() and path.stat().st_size > 10000
            wait(lambda: received(0) and received(1), 25)
            stop(receivers[0])
            size = (root / '1.flv').stat().st_size
            wait(lambda: (root / '1.flv').stat().st_size > size + 10000)
            assert proc.poll() is None
            log = (root / 'sink-reconnect.log').open('wb')
            again = subprocess.Popen([binary, 'sink', '--port', str(receiver_a), '--web-port', '0',
                                      '--file', str(root / 'reconnected.flv'), '--max-mb', '5'], stdout=log, stderr=log, cwd=root)
            log.close()
            children.append(again)
            wait(lambda: (root / 'reconnected.flv').exists() and (root / 'reconnected.flv').stat().st_size > 10000, 15)
            # A systemd stop uses SIGTERM while the relay is actively forwarding.
            assert (root / 'cache/stream.buf').exists()
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)
            assert proc.returncode == 0
            assert not (root / 'cache/stream.buf').exists()
            assert b'Shutting down (sigterm)' in logs.read_bytes()
            stop(publisher)
            # Runtime edits are discarded, and fresh secret bytes read on restart.
            rotated = secrets.token_urlsafe(3)
            keys.append(rotated)
            key_paths[0].write_bytes((rotated + '\r\n').encode())
            config.write_text('configured=false\n')
            proc = start()
            assert f'destination.0.stream_key={rotated}' in config.read_text()
            assert 'configured=true' in config.read_text()
            for path in ['/state', '/config', '/destinations', '/logs']:
                assert rotated.encode() not in request(path)[1], 'short secret leaked'
            stop(proc)
            for path in [key_paths[0]]:
                for payload in [b'', b'\n', b'bad\nkey', b'bad\x00key', b'bad/key', b' key', b'key\n\n']:
                    path.write_bytes(payload)
                    result = subprocess.run([binary, '--no-browser'], env=env, cwd=runtime, capture_output=True, timeout=10, check=False)
                    assert result.returncode != 0
                    assert b"destination 'local-0'" in result.stderr and b'bad' not in result.stderr
                path.unlink()
                result = subprocess.run([binary], env=env, cwd=runtime, capture_output=True, timeout=10, check=False)
                assert result.returncode != 0 and b'missing or unreadable' in result.stderr
                path.write_text(rotated)
            # Secret server URLs fail closed without echoing their bytes.
            for payload in [b'', b'\n', b'bad\nkey', b'rtmp://127.0.0.1/live?key=secret', b'https://127.0.0.1/live']:
                server_path.write_bytes(payload)
                result = subprocess.run([binary, '--no-browser'], env=env, cwd=runtime, capture_output=True, timeout=10, check=False)
                assert result.returncode != 0
                assert b"destination 'local-1'" in result.stderr
                first = payload.split(b'\n')[0]
                assert not first or first not in result.stderr
            server_path.write_text(server_url)
            for occupied in [ingest, web]:
                with socket.socket() as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind(('127.0.0.1', occupied))
                    sock.listen()
                    result = subprocess.run([binary], env=env, cwd=runtime, capture_output=True, timeout=10, check=False)
                    assert result.returncode != 0 and b'port is unavailable' in result.stderr
            # Search relevant immutable artifacts and ordinary proxy output explicitly.
            artifacts = [logs, Path(store_template), Path(package_derivation)]
            artifacts += [path for path in Path(binary).parent.parent.rglob('*') if path.is_file()]
            for artifact in artifacts:
                data = artifact.read_bytes()
                assert all(key.encode() not in data for key in keys), 'secret leakage detected'
            assert server_url.encode() not in logs.read_bytes(), 'secret server URL leaked to journal'
            assert f'127.0.0.1:{receiver_b}'.encode() not in logs.read_bytes(), 'secret endpoint leaked to journal'
            print('PASS: strict declarations, runtime secrets, exact binds, dashboard, SIGTERM cleanup, restart, rotation, port collisions, two local RTMP receivers and reconnect')
        finally:
            for child in reversed(children):
                stop(child)


if __name__ == '__main__':
    run(sys.argv[1], sys.argv[2], sys.argv[3])
