"""Dual-relay H+V concurrency check. No provider is contacted.

Two managed InstantClone proxies run concurrently with isolated ingests,
runtimes, buffers and local RTMP sinks:

- H relay: landscape publisher (160x90) -> ingestH -> receiverH
- V relay: portrait publisher (90x160) -> ingestV -> receiverV

Both relays use stream_format=horizontal: each isolated single-track ingest
forwards its own primary (TrackId 0). Upstream drops single-track legacy tags
for vertical targets, so an isolated portrait ingest with stream_format=vertical
would send no video. EB Dual-Format vertical selection belongs to a
single-ingest multitrack setup, not to this dual-ingest layout.

Proves: concurrent forwarding, correct portrait/landscape routing by dimension,
per-relay dashboard isolation, and that restarting one relay leaves the other
forwarding.
"""
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
        ingest_h, web_h, recv_h, ingest_v, web_v, recv_v = [port() for _ in range(6)]
        keys = [secrets.token_hex(24), secrets.token_hex(24)]
        # Real age round-trip with disposable identities, mirroring runtime.py.
        identity = root / 'identity'
        subprocess.run(['age-keygen', '-o', str(identity)], capture_output=True, check=True)
        recipient = subprocess.check_output(['age-keygen', '-y', str(identity)], text=True).strip()
        key_paths = [root / 'h.key', root / 'v.key']
        for i, value in enumerate(keys):
            enc = root / f'{i}.age'
            subprocess.run(['age', '-r', recipient, '-o', str(enc)], input=(value + '\n').encode(), check=True)
            subprocess.run(['age', '-d', '-i', str(identity), '-o', str(key_paths[i]), str(enc)], check=True)

        def template(path, ingest, web, receiver, key_path, cache_name):
            lines = [
                'configured=true', f'ingest_port={ingest}', 'ingest_bind_all=false',
                f'web_port={web}', 'web_bind_all=false', 'buffer_mb=50',
                f'buffer_path={root}/cache-{cache_name}/stream.buf',
                f'overlays_dir={root}/state-{cache_name}/overlays',
                'tracing_enabled=false', 'update_check_enabled=false',
                'open_dashboard_on_launch=false',
                'destination.0.id=local', 'destination.0.name=local',
                'destination.0.enabled=true', 'destination.0.platform=custom',
                'destination.0.audio_track=1',
                # Isolated single-track ingest: horizontal forwards the primary.
                'destination.0.stream_format=horizontal',
                f'destination.0.stream_key_file={key_path}',
                f'destination.0.custom_egress_url=rtmp://127.0.0.1:{receiver}/live',
            ]
            path.write_text('\n'.join(lines) + '\n')

        template(template_h, ingest_h, web_h, recv_h, key_paths[0], 'h')
        template(template_v, ingest_v, web_v, recv_v, key_paths[1], 'v')

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
                '-t', '60',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency', '-g', '10',
                '-c:a', 'aac', '-f', 'flv', f'rtmp://127.0.0.1:{ingest}/live/local',
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            children.append(child)
            return child

        try:
            proc_h = start_proxy(config_h, template_h, logs_h, web_h, 'h')
            proc_v = start_proxy(config_v, template_v, logs_v, web_v, 'v')
            for cfg in [config_h, config_v]:
                assert cfg.stat().st_mode & 0o777 == 0o600
                assert cfg.parent.stat().st_mode & 0o777 == 0o700
            assert f'destination.0.stream_key={keys[0]}' in config_h.read_text()
            assert f'destination.0.stream_key={keys[1]}' in config_v.read_text()
            # Dashboards are independent and secret-free.
            assert request(web_h, '/')[0] == 200
            assert request(web_v, '/')[0] == 200
            assert b'Broadcast Desk' in request(web_h, '/dock')[1]
            for program in ['landscape', 'portrait']:
                assert request(web_h, f'/desk/{program}/state')[0] == 200
                body = request(web_h, f'/desk/{program}/destinations')[1]
                assert all(k.encode() not in body for k in keys)
            assert request(web_h, '/desk/portrait/config')[0] == 400
            for web in [web_h, web_v]:
                for p in ['/state', '/config', '/destinations', '/logs']:
                    body = request(web, p)[1]
                    assert all(k.encode() not in body for k in keys)

            out_h = root / 'h.flv'
            out_v = root / 'v.flv'
            sink_h = start_sink(recv_h, out_h, 'h')
            start_sink(recv_v, out_v, 'v')
            pub_h = start_publisher(ingest_h, '160x90', 'h')
            start_publisher(ingest_v, '90x160', 'v')

            def received(p, minimum=10000):
                return p.exists() and p.stat().st_size > minimum
            wait(lambda: received(out_h) and received(out_v), 30)
            # Let both accumulate enough for a stable header, then verify orientation.
            time.sleep(3)
            wh, hh = dims(out_h)
            wv, hv = dims(out_v)
            assert hh < wh, f'H relay must stay landscape, got {wh}x{hh}'
            assert hv > wv, f'V relay must stay portrait, got {wv}x{hv}'
            assert proc_h.poll() is None and proc_v.poll() is None

            # Restart isolation: stopping H must not stall V.
            size_v = out_v.stat().st_size
            stop(proc_h)
            # H publisher loses its ingest; stop it to avoid wedging the test.
            stop(pub_h)
            wait(lambda: out_v.stat().st_size > size_v + 10000, 20)
            assert proc_v.poll() is None
            # H sink holds no new bytes while its proxy is down (allow a small
            # grace for in-flight tags, then require stall).
            time.sleep(2)
            stalled = out_h.stat().st_size
            time.sleep(2)
            assert out_h.stat().st_size < stalled + 5000, 'H sink kept growing without its proxy'

            # Fresh V baseline before H restart, to prove V keeps forwarding
            # across the H restart (not just before it).
            v_pre_restart = out_v.stat().st_size
            # A new recording ensures decoding cannot reuse pre-restart headers/frames.
            stop(sink_h)
            out_h_restart = root / 'h-restart.flv'
            start_sink(recv_h, out_h_restart, 'h-restart')
            # Restart H with the same template/receiver.
            proc_h = start_proxy(config_h, template_h, logs_h, web_h, 'h')
            pub_h = start_publisher(ingest_h, '160x90', 'h-restart')
            wait(lambda: received(out_h_restart), 25)
            # Fresh V growth across the H restart: V must advance beyond its
            # pre-restart size while H is restarting/resuming.
            wait(lambda: out_v.stat().st_size > v_pre_restart + 10000, 20)
            assert proc_v.poll() is None and proc_h.poll() is None
            wh3, hh3 = dims(out_h_restart)
            assert hh3 < wh3, f'restarted H relay must stay landscape, got {wh3}x{hh3}'
            decoded = subprocess.run(
                ['ffmpeg', '-nostdin', '-v', 'error', '-i', str(out_h_restart),
                 '-frames:v', '1', '-f', 'null', '-', '-progress', 'pipe:1'],
                capture_output=True, text=True, timeout=10, check=True)
            assert 'frame=1\n' in decoded.stdout, 'no fresh post-restart H frame decoded'

            # No cross-talk: portrait file never became landscape and vice versa.
            wv2, hv2 = dims(out_v)
            assert hv2 > wv2, f'V relay must remain portrait after H restart, got {wv2}x{hv2}'

            stop(pub_h)
            # V publisher may have finished its 60s window; stop idempotently.
            stop(proc_h)
            stop(proc_v)
            assert proc_h.returncode == 0
            assert proc_v.returncode == 0
            print('PASS: dual relays forward landscape+portrait concurrently with restart isolation')
        finally:
            for child in reversed(children):
                try:
                    stop(child)
                except AssertionError:
                    pass


if __name__ == '__main__':
    run(sys.argv[1], sys.argv[2])
