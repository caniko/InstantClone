"""Dual-relay H+V concurrency check. No provider is contacted.

Two managed InstantClone proxies run concurrently with isolated ingests,
runtimes, buffers and local RTMP sinks:

- H relay: landscape publisher (160x90) -> ingestH -> four independent local sinks
- V relay: portrait publisher (90x160) -> ingestV -> four independent local sinks

Both relays use stream_format=horizontal: each isolated single-track ingest
forwards its own primary (TrackId 0). Upstream drops single-track legacy tags
for vertical targets, so an isolated portrait ingest with stream_format=vertical
would send no video. EB Dual-Format vertical selection belongs to a
single-ingest multitrack setup, not to this dual-ingest layout.

Proves: eight concurrent local egresses, correct portrait/landscape routing by dimension,
per-relay dashboard isolation, and that restarting one relay leaves the other
forwarding.
"""
import os
import re
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


def wait(check, timeout=25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if check():
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    raise AssertionError('timed out waiting for dual-relay condition')


def stop(proc):
    if proc is not None and proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise AssertionError('process did not shut down cleanly')


def dims(path):
    """Return (width, height) via ffprobe, falling back to ffmpeg -i parse."""
    try:
        out = subprocess.check_output(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height', '-of', 'csv=p=0', str(path)],
            text=True, timeout=10)
        w, h = out.strip().split(',')
        return int(w), int(h)
    except (OSError, ValueError, subprocess.CalledProcessError):
        proc = subprocess.run(
            ['ffmpeg', '-hide_banner', '-i', str(path)],
            capture_output=True, text=True, timeout=10, check=False)
        err = proc.stderr
        import re
        m = re.search(r'(\d{2,4})x(\d{2,4})', err)
        assert m, f'could not parse dimensions from ffmpeg output for {path}'
        return int(m.group(1)), int(m.group(2))


def run(binary, package_derivation):
    with tempfile.TemporaryDirectory(prefix='instantclone-dual-') as tmp:
        root = Path(tmp)
        runtime = root / 'runtime'
        runtime.mkdir(mode=0o700)
        (runtime / 'instantclone').mkdir(mode=0o700)
        (runtime / 'instantclone-vertical').mkdir(mode=0o700)
        config_h = runtime / 'instantclone/config'
        config_v = runtime / 'instantclone-vertical/config'
        template_h = root / 'template-h'
        template_v = root / 'template-v'
        ports = []
        while len(ports) < 12:
            candidate = port()
            if candidate not in ports:
                ports.append(candidate)
        ingest_h, web_h, ingest_v, web_v = ports[:4]
        recv_h, recv_v = ports[4:8], ports[8:12]
        keys = [secrets.token_hex(24) for _ in range(8)]
        # Real age round-trip with disposable identities, mirroring runtime.py.
        identity = root / 'identity'
        subprocess.run(['age-keygen', '-o', str(identity)], capture_output=True, check=True)
        recipient = subprocess.check_output(['age-keygen', '-y', str(identity)], text=True).strip()
        key_paths = [root / f'{i}.key' for i in range(8)]
        for i, value in enumerate(keys):
            enc = root / f'{i}.age'
            subprocess.run(['age', '-r', recipient, '-o', str(enc)], input=(value + '\n').encode(), check=True)
            subprocess.run(['age', '-d', '-i', str(identity), '-o', str(key_paths[i]), str(enc)], check=True)

        def template(path, ingest, web, receivers, program_keys, cache_name):
            lines = [
                'configured=true', f'ingest_port={ingest}', 'ingest_bind_all=false',
                f'web_port={web}', 'web_bind_all=false', 'buffer_mb=50',
                f'buffer_path={root}/cache-{cache_name}/stream.buf',
                f'overlays_dir={root}/state-{cache_name}/overlays',
                'tracing_enabled=false', 'update_check_enabled=false',
                'open_dashboard_on_launch=false',
            ]
            for index, (receiver, key_path) in enumerate(zip(receivers, program_keys, strict=True)):
                prefix = f'destination.{index}.'
                lines += [
                    f'{prefix}id=local-{index}', f'{prefix}name=local-{index}',
                    f'{prefix}enabled=true', f'{prefix}platform=custom',
                    f'{prefix}audio_track=1',
                    # Isolated single-track ingest: horizontal forwards the primary.
                    f'{prefix}stream_format=horizontal',
                    f'{prefix}stream_key_file={key_path}',
                    f'{prefix}custom_egress_url=rtmp://127.0.0.1:{receiver}/live',
                ]
            path.write_text('\n'.join(lines) + '\n')

        template(template_h, ingest_h, web_h, recv_h, key_paths[:4], 'h')
        template(template_v, ingest_v, web_v, recv_v, key_paths[4:], 'v')

        def env_for(cfg, tmpl):
            return os.environ | {
                'XDG_RUNTIME_DIR': str(runtime), 'CONFIG_PATH': str(cfg),
                'INSTANTCLONE_MANAGED': '1', 'INSTANTCLONE_TEMPLATE': str(tmpl),
                'INSTANTCLONE_NO_TRACE': '1',
                'INSTANTCLONE_DESK_LANDSCAPE_PORT': str(web_h),
                'INSTANTCLONE_DESK_PORTRAIT_PORT': str(web_v),
            }

        def request(web, path, method='GET'):
            req = urllib.request.Request(f'http://127.0.0.1:{web}{path}', method=method,
                                         headers={'Accept-Encoding': 'gzip'})
            try:
                with urllib.request.urlopen(req, timeout=2) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()

        children = []
        logs_h = root / 'proxy-h.log'
        logs_v = root / 'proxy-v.log'

        def start_proxy(cfg, tmpl, log_path, web, cwd_sub):
            with log_path.open('ab') as log:
                proc = subprocess.Popen([binary, '--no-browser'],
                                        env=env_for(cfg, tmpl), cwd=runtime,
                                        stdout=log, stderr=log)
            children.append(proc)
            wait(lambda: proc.poll() is not None or request(web, '/state')[0] == 200)
            assert proc.poll() is None, 'proxy exited before becoming healthy'
            return proc

        def start_sink(receiver, out_file, tag):
            log = (root / f'sink-{tag}.log').open('wb')
            child = subprocess.Popen(
                [binary, 'sink', '--port', str(receiver), '--web-port', '0',
                 '--file', str(out_file), '--max-mb', '5'],
                stdout=log, stderr=log, cwd=root)
            log.close()
            children.append(child)
            return child

        def start_publisher(ingest, size, tag):
            child = subprocess.Popen([
                'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-re',
                '-f', 'lavfi', '-i', f'testsrc2=size={size}:rate=10',
                '-f', 'lavfi', '-i', 'sine=frequency=1000:sample_rate=44100',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency', '-g', '10',
                '-c:a', 'aac', '-f', 'flv', f'rtmp://127.0.0.1:{ingest}/live/local',
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            children.append(child)
            return child

        def assert_publish_key(tag, expected):
            # These are disposable test credentials. Check the actual publish
            # playpath at every receiver, not just rendered settings or media.
            text = (root / f'sink-{tag}.log').read_text()
            observed = set(re.findall(r'\[sink\] publish accepted \(stream key: "([^"]*)"\)', text))
            assert observed == {expected}, f'wrong wire credential mapping at {tag}'

        try:
            proc_h = start_proxy(config_h, template_h, logs_h, web_h, 'h')
            proc_v = start_proxy(config_v, template_v, logs_v, web_v, 'v')
            for cfg in [config_h, config_v]:
                assert cfg.stat().st_mode & 0o777 == 0o600
                assert cfg.parent.stat().st_mode & 0o777 == 0o700
            for config, program_keys in [(config_h, keys[:4]), (config_v, keys[4:])]:
                fields = dict(line.split('=', 1) for line in config.read_text().splitlines() if '=' in line)
                for index, key in enumerate(program_keys):
                    assert fields[f'destination.{index}.stream_key'] == key, f'wrong rendered credential mapping at {config.name}/{index}'
            # Dashboards are independent and secret-free.
            assert request(web_h, '/')[0] == 200
            assert request(web_v, '/')[0] == 200
            assert b'Broadcast Desk' in request(web_h, '/dock')[1]
            for program in ['landscape', 'portrait']:
                assert request(web_h, f'/desk/{program}/state')[0] == 200
                body = request(web_h, f'/desk/{program}/destinations')[1]
                assert all(k.encode() not in body for k in keys)
            assert request(web_h, '/desk/portrait/config')[0] == 403
            for web in [web_h, web_v]:
                for p in ['/state', '/config', '/destinations', '/logs']:
                    body = request(web, p)[1]
                    assert all(k.encode() not in body for k in keys)

            out_h = [root / f'h-{i}.flv' for i in range(4)]
            out_v = [root / f'v-{i}.flv' for i in range(4)]
            sinks_h = [start_sink(receiver, path, f'h-{i}') for i, (receiver, path) in enumerate(zip(recv_h, out_h, strict=True))]
            for i, (receiver, path) in enumerate(zip(recv_v, out_v, strict=True)):
                start_sink(receiver, path, f'v-{i}')
            pub_h = start_publisher(ingest_h, '160x90', 'h')
            pub_v = start_publisher(ingest_v, '90x160', 'v')

            def received(p, minimum=10000):
                return p.exists() and p.stat().st_size > minimum
            wait(lambda: all(received(path) for path in out_h + out_v), 30)
            # Let both accumulate enough for a stable header, then verify orientation.
            time.sleep(3)
            for paths, expected in [(out_h, (160, 90)), (out_v, (90, 160))]:
                for path in paths:
                    assert dims(path) == expected, f'wrong program dimensions in {path}'
            for program, program_keys in [('h', keys[:4]), ('v', keys[4:])]:
                for index, key in enumerate(program_keys):
                    assert_publish_key(f'{program}-{index}', key)
            assert proc_h.poll() is None and proc_v.poll() is None

            # Restart isolation: stopping H must not stall V.
            size_v = [path.stat().st_size for path in out_v]
            stop(proc_h)
            # H publisher loses its ingest; stop it to avoid wedging the test.
            stop(pub_h)
            wait(lambda: all(path.stat().st_size > size + 10000 for path, size in zip(out_v, size_v, strict=True)), 20)
            assert proc_v.poll() is None
            # H sink holds no new bytes while its proxy is down (allow a small
            # grace for in-flight tags, then require stall).
            time.sleep(2)
            stalled = [path.stat().st_size for path in out_h]
            time.sleep(2)
            assert all(path.stat().st_size < size + 5000 for path, size in zip(out_h, stalled, strict=True)), 'H sink kept growing without its proxy'

            # Fresh V baseline before H restart, to prove V keeps forwarding
            # across the H restart (not just before it).
            v_pre_restart = [path.stat().st_size for path in out_v]
            # A new recording ensures decoding cannot reuse pre-restart headers/frames.
            for sink in sinks_h:
                stop(sink)
            out_h_restart = [root / f'h-restart-{i}.flv' for i in range(4)]
            for i, (receiver, path) in enumerate(zip(recv_h, out_h_restart, strict=True)):
                start_sink(receiver, path, f'h-restart-{i}')
            # Restart H with the same template/receiver.
            proc_h = start_proxy(config_h, template_h, logs_h, web_h, 'h')
            pub_h = start_publisher(ingest_h, '160x90', 'h-restart')
            wait(lambda: all(received(path) for path in out_h_restart), 25)
            # Fresh V growth across the H restart: V must advance beyond its
            # pre-restart size while H is restarting/resuming.
            wait(lambda: all(path.stat().st_size > size + 10000 for path, size in zip(out_v, v_pre_restart, strict=True)), 20)
            assert proc_v.poll() is None and proc_h.poll() is None
            assert pub_v.poll() is None, 'portrait publisher ended before restart-isolation assertions'
            for path in out_h_restart:
                assert dims(path) == (160, 90), f'restarted H relay sent wrong dimensions to {path}'
                decoded = subprocess.run(
                    ['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path),
                     '-frames:v', '1', '-f', 'null', '-', '-progress', 'pipe:1'],
                    capture_output=True, text=True, timeout=10, check=True)
                assert 'frame=1\n' in decoded.stdout, f'no fresh post-restart H frame decoded from {path}'
            for index, key in enumerate(keys[:4]):
                assert_publish_key(f'h-restart-{index}', key)

            # No cross-talk: portrait file never became landscape and vice versa.
            for path in out_v:
                assert dims(path) == (90, 160), f'V relay changed dimensions after H restart in {path}'

            stop(pub_h)
            stop(pub_v)
            stop(proc_h)
            stop(proc_v)
            assert proc_h.returncode == 0
            assert proc_v.returncode == 0
            print('PASS: dual relays forward four landscape and four portrait egresses concurrently with restart isolation and fresh decoded recovery')
        finally:
            for child in reversed(children):
                try:
                    stop(child)
                except AssertionError:
                    pass


if __name__ == '__main__':
    run(sys.argv[1], sys.argv[2])
