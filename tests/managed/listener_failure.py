"""Deterministic listener failures after preflight, including active RTMP teardown."""
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from runtime import port, stop, wait


# Interpose only socket operations on the selected disposable local port.
# Production source has no failure-injection switches. The first bind is the
# preflight probe; the second is the real Tokio listener. An active failure is
# armed by a file only after the receiver has observed media.
INTERPOSER = r'''
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <unistd.h>

static int selected(const struct sockaddr *address) {
    return address && address->sa_family == AF_INET &&
        ntohs(((const struct sockaddr_in *)address)->sin_port) ==
        atoi(getenv("IC_TEST_FAIL_PORT"));
}
static int armed(void) {
    const char *marker = getenv("IC_TEST_FAIL_MARKER");
    return marker && access(marker, F_OK) == 0;
}
int bind(int fd, const struct sockaddr *address, socklen_t length) {
    static int calls;
    int (*real_bind)(int, const struct sockaddr *, socklen_t) = dlsym(RTLD_NEXT, "bind");
    if (selected(address) && ++calls > 1 &&
        (!getenv("IC_TEST_FAIL_MARKER") || armed())) {
        errno = EADDRINUSE;
        return -1;
    }
    return real_bind(fd, address, length);
}
int accept4(int fd, struct sockaddr *address, socklen_t *length, int flags) {
    int (*real_accept)(int, struct sockaddr *, socklen_t *, int) = dlsym(RTLD_NEXT, "accept4");
    struct sockaddr_storage local;
    socklen_t size = sizeof(local);
    if (armed() && getsockname(fd, (struct sockaddr *)&local, &size) == 0 &&
        selected((struct sockaddr *)&local)) {
        errno = EIO;
        return -1;
    }
    return real_accept(fd, address, length, flags);
}
'''


def run(binary):
    with tempfile.TemporaryDirectory(prefix='instantclone-listener-test-') as tmp:
        root = Path(tmp)
        source, preload = root / 'failure.c', root / 'failure.so'
        source.write_text(INTERPOSER)
        subprocess.run([os.environ.get('CC', 'cc'), '-shared', '-fPIC', '-o', str(preload),
                        str(source), '-ldl'], check=True)
        for kind in ['ingest', 'web', 'active-ingest']:
            case = root / kind
            case.mkdir()
            runtime = case / 'runtime'
            runtime.mkdir(mode=0o700)
            ingest, web, receiver = [port() for _ in range(3)]
            key = case / 'key'
            key.write_text('disposable-test-key\n')
            template = case / 'template'
            ring = case / 'cache/stream.buf'
            template.write_text('\n'.join([
                'configured=true', f'ingest_port={ingest}', f'web_port={web}',
                'ingest_bind_all=false', 'web_bind_all=false', 'buffer_mb=50',
                f'buffer_path={ring}', f'overlays_dir={case}/state/overlays',
                'tracing_enabled=false', 'update_check_enabled=false',
                'open_dashboard_on_launch=false', 'destination.0.id=local',
                'destination.0.name=local', 'destination.0.platform=custom',
                f'destination.0.custom_egress_url=rtmp://127.0.0.1:{receiver}/live',
                f'destination.0.stream_key_file={key}',
            ]) + '\n')
            marker = case / 'fail-now'
            env = os.environ | {
                'XDG_RUNTIME_DIR': str(runtime), 'CONFIG_PATH': str(runtime / 'instantclone/config'),
                'INSTANTCLONE_MANAGED': '1', 'INSTANTCLONE_TEMPLATE': str(template),
                'INSTANTCLONE_NO_TRACE': '1', 'LD_PRELOAD': str(preload),
                'IC_TEST_FAIL_PORT': str(web if kind == 'web' else ingest),
            }
            if kind == 'active-ingest':
                env['IC_TEST_FAIL_MARKER'] = str(marker)
            children = []
            logs = case / 'proxy.log'
            try:
                with logs.open('wb') as log:
                    proc = subprocess.Popen([binary, '--no-browser'], env=env, cwd=runtime,
                                            stdout=log, stderr=log)
                children.append(proc)
                if kind == 'active-ingest':
                    media = case / 'received.flv'
                    with (case / 'sink.log').open('wb') as log:
                        sink = subprocess.Popen([binary, 'sink', '--port', str(receiver),
                                                 '--web-port', '0', '--file', str(media)],
                                                cwd=case, stdout=log, stderr=log)
                    children.append(sink)
                    wait(lambda: ring.exists())
                    publisher = subprocess.Popen([
                        'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-re',
                        '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=10',
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency', '-g', '10',
                        '-f', 'flv', f'rtmp://127.0.0.1:{ingest}/live/key',
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    children.append(publisher)
                    wait(lambda: media.exists() and media.stat().st_size > 10000)
                    marker.touch()
                    # Wake the listener's pending accept so the injected I/O
                    # error, not a signal or publisher disconnect, triggers shutdown.
                    with socket.create_connection(('127.0.0.1', ingest), timeout=2):
                        pass
                started = time.monotonic()
                proc.wait(timeout=10)
                assert proc.returncode != 0, kind
                assert b'Shutting down (' in logs.read_bytes(), kind
                assert b'supervisor exited)' in logs.read_bytes(), kind
                assert not ring.exists(), f'{kind}: ring file left behind'
                assert time.monotonic() - started >= 0.7, f'{kind}: teardown grace was bypassed'
                print(f'PASS: {kind} listener failure reports failure after graceful teardown and ring cleanup')
            finally:
                for child in reversed(children):
                    stop(child)


if __name__ == '__main__':
    run(sys.argv[1])
