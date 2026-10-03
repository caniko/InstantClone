"""Real HTTP authentication and bridge isolation with disposable credentials.

Usage: python auth.py INSTANTCLONE [CHROMIUM]
All publishers and receivers are local; no provider is contacted.
"""
import hashlib
import http.client
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from runtime_dual import port, stop, wait


def run(binary, chromium=None, screenshot=None):
    with tempfile.TemporaryDirectory(prefix='instantclone-auth-') as tmp:
        root = Path(tmp)
        runtime = root / 'runtime'
        runtime.mkdir(mode=0o700)
        web_h, web_v, ingest_h, ingest_v = [port() for _ in range(4)]
        tokens = [secrets.token_hex(32), secrets.token_hex(32)]
        token_files = [root / 'h.token', root / 'v.token']
        for file, token in zip(token_files, tokens):
            file.write_text(token)
            file.chmod(0o600)
        password = 'disposable-login'
        salt = bytes.fromhex('00112233445566778899aabbccddeeff')
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 210000).hex()
        password_hash = f'pbkdf2-sha256$210000${salt.hex()}${digest}'
        password_file = root / 'password.hash'
        password_file.write_text(password_hash + '\n')
        password_file.chmod(0o600)
        info = root / 'desk.json'
        info.write_text(json.dumps({'landscape':{'enabled':True,'ingestPort':ingest_h},
                                    'portrait':{'enabled':True,'ingestPort':ingest_v}}))

        def request(web, path, body=None, cookie='', origin=''):
            connection = http.client.HTTPConnection('127.0.0.1', web, timeout=5)
            headers = {'Cookie':cookie, 'Content-Type':'application/x-www-form-urlencoded'}
            if origin:
                headers['Origin'] = origin
            connection.request('GET' if body is None else 'POST', path, body=body, headers=headers)
            response = connection.getresponse()
            result = response.status, response.read().decode(), response.getheader('Set-Cookie')
            connection.close()
            return result

        children = []
        try:
            for index, (web, ingest) in enumerate([(web_h,ingest_h),(web_v,ingest_v)]):
                template = root / f'template-{index}'
                template.write_text('\n'.join([
                    'configured=true', f'web_port={web}', 'web_bind_all=false',
                    f'ingest_port={ingest}', 'ingest_bind_all=false', 'buffer_mb=50',
                    f'buffer_path={root}/cache-{index}/stream.buf',
                    f'overlays_dir={root}/state-{index}/overlays',
                    'tracing_enabled=false', 'update_check_enabled=false',
                    'open_dashboard_on_launch=false',
                    f'dashboard_password_hash_file={password_file}', f'dock_token_file={token_files[index]}',
                    # Duplicate labels with distinct IDs must remain visible.
                    'destination.0.id=first', 'destination.0.name=YouTube',
                    'destination.0.enabled=false', 'destination.0.platform=youtube',
                    'destination.1.id=second', 'destination.1.name=YouTube',
                    'destination.1.enabled=false', 'destination.1.platform=youtube',
                ]) + '\n')
                env = os.environ | {
                    'INSTANTCLONE_MANAGED':'1', 'INSTANTCLONE_NO_TRACE':'1',
                    'INSTANTCLONE_TEMPLATE':str(template), 'XDG_RUNTIME_DIR':str(runtime),
                    'CONFIG_PATH':str(runtime / f'instance-{index}/config'),
                    'INSTANTCLONE_DESK_CONFIG':str(info),
                    'INSTANTCLONE_DESK_LANDSCAPE_PORT':str(web_h),
                    'INSTANTCLONE_DESK_PORTRAIT_PORT':str(web_v),
                    'INSTANTCLONE_DESK_LANDSCAPE_TOKEN_FILE':str(token_files[0]),
                    'INSTANTCLONE_DESK_PORTRAIT_TOKEN_FILE':str(token_files[1]),
                }
                with (root / f'relay-{index}.log').open('wb') as log:
                    child = subprocess.Popen([binary,'--no-browser'],env=env,cwd=root,stdout=log,stderr=log)
                children.append(child)
                wait(lambda: child.poll() is not None or request(web,'/state')[0] == 401)
                assert child.poll() is None, 'managed relay exited during startup'

            cookie = f'ic_dock={tokens[0]}'
            for path in ['/dock','/desk/app.js','/desk/info','/desk/landscape/state','/desk/portrait/state']:
                assert request(web_h,path)[0] == 401, f'anonymous desk access: {path}'
                assert request(web_h,path,cookie=cookie)[0] == 200, f'authorized desk access failed: {path}'
            assert request(web_h,'/desk/info',cookie='ic_dock=invalid')[0] == 401
            assert request(web_h,'/desk/info',cookie='ic_session=invalid')[0] == 401
            assert request(web_v,'/state',cookie=cookie)[0] == 401, 'tokens crossed protected relays'
            for path in ['/config','/app/restart','/auth/set-password','/desk/portrait/config']:
                assert request(web_h,path,body='',cookie=cookie)[0] == 403
            assert request(web_h,'/desk/portrait/disarm',body='',cookie=cookie,origin='http://evil.invalid')[0] == 403
            assert request(web_h,'/login')[0] == 200
            assert request(web_h,'/login',body='password=wrong')[0] == 401
            status, _, session = request(web_h,'/login',body=f'password={password}')
            assert status == 200 and session and 'HttpOnly' in session
            session_cookie = session.split(';')[0]
            assert request(web_h,'/desk/portrait/state',cookie=session_cookie)[0] == 200
            assert request(web_h,'/logout',body='',cookie=session_cookie)[0] == 200
            assert request(web_h,'/desk/info',cookie=session_cookie)[0] == 401, 'revoked session still authorized'

            for value in [secrets.token_hex(32), tokens[1] + '\r\nInjected: yes']:
                token_files[1].write_text(value)
                status, body, _ = request(web_h,'/desk/portrait/state',cookie=cookie)
                assert status == 401 and tokens[1] not in body, 'peer authentication error concealed or leaked'
                assert request(web_h,'/desk/landscape/state',cookie=cookie)[0] == 200
            token_files[1].write_text(tokens[1])
            token_files[1].chmod(0o644)
            assert request(web_h,'/desk/portrait/state',cookie=cookie)[0] == 401
            token_files[1].chmod(0o600)
            token_files[1].unlink()
            assert request(web_h,'/desk/portrait/state',cookie=cookie)[0] == 401
            token_files[1].write_text(tokens[1])
            token_files[1].chmod(0o600)

            for ingest, size in [(ingest_h,'160x90'),(ingest_v,'90x160')]:
                publisher = subprocess.Popen([
                    'ffmpeg','-nostdin','-v','error','-re','-f','lavfi','-i',f'testsrc2=size={size}:rate=10',
                    '-t','60','-c:v','libx264','-preset','ultrafast','-tune','zerolatency','-g','10',
                    '-f','flv',f'rtmp://127.0.0.1:{ingest}/live/local',
                ],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                children.append(publisher)
            def state(program):
                status, body, _ = request(web_h,f'/desk/{program}/state',cookie=cookie)
                assert status == 200
                return json.loads(body)
            wait(lambda: state('landscape')['ingest_alive'] and state('portrait')['ingest_alive'])
            status, body, _ = request(web_h,'/desk/portrait/arm',body='ms=5000',cookie=cookie)
            assert status == 200 and json.loads(body)['armed_delay_ms'] == 5000
            assert state('landscape')['armed_delay_ms'] == 0, 'portrait action changed landscape'
            assert request(web_h,'/desk/portrait/disarm',body='',cookie=cookie)[0] == 200
            if chromium:
                browser_check(chromium,root,f'http://127.0.0.1:{web_h}',tokens[0],screenshot)
            for file in root.glob('relay-*.log'):
                assert all(token not in file.read_text() for token in tokens), 'control token leaked into logs'
            print('PASS: managed login, dock tokens, protected peer bridge, revoked sessions and action isolation')
        finally:
            for child in reversed(children):
                stop(child)


def browser_check(chromium, root, base, token, screenshot):
    profile = root / 'browser-profile'
    env = os.environ.copy()
    for name in ['HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME']:
        directory = root / name
        directory.mkdir(mode=0o700)
        env[name] = str(directory)
    with (root / 'browser.log').open('wb') as log:
        browser = subprocess.Popen([chromium,'--headless','--no-sandbox','--disable-gpu',
                                    '--no-first-run','--disable-background-networking',
                                    '--remote-debugging-port=0',f'--user-data-dir={profile}','about:blank'],
                                   env=env,stdout=log,stderr=log)
    try:
        active = profile / 'DevToolsActivePort'
        wait(lambda: active.exists() or browser.poll() is not None)
        assert browser.poll() is None, 'headless browser exited'
        debug_port, path = active.read_text().splitlines()[:2]
        result = subprocess.run(['node',str(Path(__file__).with_name('live.cjs')),
                                 f'ws://127.0.0.1:{debug_port}{path}',base,token,screenshot or ''],
                                capture_output=True,text=True,timeout=45)
        assert result.returncode == 0, result.stderr[-2000:]
        print(result.stdout.strip())
    finally:
        stop(browser)


if __name__ == '__main__':
    run(sys.argv[1],sys.argv[2] if len(sys.argv)>2 else None,sys.argv[3] if len(sys.argv)>3 else None)
