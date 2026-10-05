"""Verify token login then cookie-only terminal access without displaying secrets."""
import argparse
import http.cookiejar
import json
from pathlib import Path
import secrets
import ssl
import urllib.error
import urllib.parse
import urllib.request


def verify(launch_url, host=None, origin=None):
    import websocket
    parsed = urllib.parse.urlsplit(launch_url)
    base = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, '', '', ''))
    token = urllib.parse.parse_qs(parsed.query)['token'][0]
    origin = origin or base
    headers = {'Host': host} if host else {}
    context = ssl.create_default_context()
    try:
        urllib.request.urlopen(urllib.request.Request(base + '/api/status',
            headers={**headers, 'Accept': 'application/json'}), context=context, timeout=15)
    except urllib.error.HTTPError as exc:
        if exc.code != 403:
            raise ValueError('Unexpected unauthenticated status') from None
    else:
        raise ValueError('Unauthenticated access was allowed')
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar),
                                        urllib.request.HTTPSHandler(context=context))
    with opener.open(urllib.request.Request(base + '/login?token=' + urllib.parse.quote(token),
                                           headers=headers), timeout=15) as response:
        response.read()
    # Subsequent HTTP and WebSocket operations contain no token.
    with opener.open(urllib.request.Request(base + '/api/status', headers=headers), timeout=15) as response:
        if response.status != 200:
            raise ValueError('Cookie authentication failed')
    xsrf = next(cookie.value for cookie in jar if cookie.name == '_xsrf')
    request = urllib.request.Request(base + '/api/terminals', data=b'{}', method='POST',
        headers={**headers, 'Content-Type': 'application/json', 'X-XSRFToken': xsrf})
    with opener.open(request, timeout=15) as response:
        name = json.load(response)['name']
    ws = None
    try:
        ws_url = base.replace('https://', 'wss://', 1).replace('http://', 'ws://', 1)
        cookie = '; '.join(f'{item.name}={item.value}' for item in jar)
        try:
            rejected = websocket.create_connection(
                ws_url + '/terminals/websocket/' + urllib.parse.quote(name),
                cookie=cookie, origin='https://unauthorized.invalid',
                host=host or parsed.netloc, timeout=15)
        except websocket.WebSocketBadStatusException as exc:
            if exc.status_code != 403:
                raise ValueError('Unexpected origin rejection status') from None
        else:
            rejected.close()
            raise ValueError('Foreign terminal origin was allowed')
        ws = websocket.create_connection(ws_url + '/terminals/websocket/' + urllib.parse.quote(name),
            cookie=cookie, origin=origin, host=host or parsed.netloc, timeout=15)
        # Split marker so terminal command echo alone cannot pass this assertion.
        left, right = secrets.token_hex(8), secrets.token_hex(8)
        marker = left + right
        ws.send(json.dumps(['stdin', "printf '%s%s\\n' '" + left + "' '" + right + "'\r"]))
        output = ''
        for _ in range(30):
            message = json.loads(ws.recv())
            if message[0] == 'stdout':
                output += message[1]
                if marker in output:
                    break
        else:
            raise ValueError('Terminal command output not received')
    finally:
        if ws:
            ws.close()
        with opener.open(urllib.request.Request(base + '/api/terminals/' + urllib.parse.quote(name),
            method='DELETE', headers={**headers, 'X-XSRFToken': xsrf}), timeout=15):
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--launch-file', type=Path, required=True)
    args = parser.parse_args()
    receipt = json.loads(args.launch_file.read_text())
    verify(receipt['launch_url'])
    print('Verified: unauthenticated rejection, token login, cookie-only terminal and command output.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        raise SystemExit(f'Jupyter verification failed ({type(exc).__name__}); details withheld to protect credentials.')
