"""Private per-user credentials and RunPod REST deployment. Never logs API bodies."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import ssl
import stat
import urllib.error
import urllib.parse
import urllib.request


def private_dir(path):
    path = Path(path).expanduser()
    if path.is_symlink():
        raise ValueError('Private storage cannot be a symlink')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('Private storage must be owned by this OS user with mode 0700')
    return path


def private_write(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(value)


def user_token(store, user_id):
    # A hosted service must supply its authenticated user ID, never a shared default.
    folder = private_dir(private_dir(store) / hashlib.sha256(user_id.encode()).hexdigest())
    target = folder / 'jupyter-token'
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError('Token file must be private (0600) and owned by this user')
            value = stream.read().strip()
        if len(value) < 32:
            raise ValueError('Invalid token store; restore the private backup')
        return value, folder
    with os.fdopen(fd, 'w') as stream:
        value = secrets.token_urlsafe(32)
        stream.write(value + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    return value, folder


def deployment_request(request, token):
    # Caller supplies complete REST request, including their existing volume selection.
    body = dict(request)
    if body.get('volumeMountPath') != '/workspace':
        raise ValueError('Explicitly preserve your Global volume mount at /workspace')
    if not body.get('imageName'):
        raise ValueError('Provide the rebuilt image name/digest')
    if body.get('dockerStartCmd') or body.get('dockerEntrypoint'):
        # Preserve the inspected persistent-application bootstrap: it imports the
        # image launcher, so v8's Jupyter security configuration still runs.
        command = '\n'.join(body.get('dockerStartCmd', []))
        if '/opt/start.py' not in command or 'launcher.main()' not in command:
            raise ValueError('Command override must invoke the inspected image launcher')
    if '8888/http' not in body.get('ports', []):
        raise ValueError('REST request must expose 8888/http in its ports list')
    env = dict(body.get('env', {}))
    env.pop('JUPYTER_NO_AUTH', None)
    env.pop('RUNPOD_POD_ID', None)  # RunPod injects the newly allocated ID.
    env.update(JUPYTER_TOKEN=token, JUPYTER_PASSWORD=token)
    body['env'] = env
    return body


def launch_url(pod_id, token):
    import re
    if not re.fullmatch(r'[a-zA-Z0-9]+', pod_id):
        raise ValueError('Invalid pod ID returned by RunPod')
    return f'https://{pod_id}-8888.proxy.runpod.net/lab?token={urllib.parse.quote(token, safe="")}'


def api_request(path, body=None):
    key = os.environ.get('RUNPOD_API_KEY')
    if not key:
        raise ValueError('Bind RUNPOD_API_KEY securely before deploying')
    context = ssl.create_default_context(cafile=os.environ.get('SSL_CERT_FILE'))
    request = urllib.request.Request('https://rest.runpod.io/v1/' + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
        method='POST' if body is not None else 'GET')
    # No retries: an ambiguous failure may already have allocated a charged pod.
    with urllib.request.urlopen(request, context=context, timeout=60) as response:
        return json.load(response)


def template_request(template, request):
    keys = ('imageName', 'containerDiskInGb', 'containerRegistryAuthId',
            'dockerStartCmd', 'dockerEntrypoint', 'ports', 'volumeMountPath')
    body = {key: template[key] for key in keys if key in template}
    body.update(request)
    body['env'] = {**template.get('env', {}), **request.get('env', {})}
    # Materialize settings rather than inherit an old template at allocation time.
    body.pop('templateId', None)
    return body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user-id', required=True, help='Stable private account/user identity')
    parser.add_argument('--request', type=Path, required=True, help='Complete RunPod REST create-pod JSON')
    parser.add_argument('--template-id', help='Read and preserve defaults from your existing RunPod template')
    parser.add_argument('--image', help='Override with the rebuilt v8 image digest')
    parser.add_argument('--store', type=Path, default=Path.home() / '.local/state/comfy-runpod',
                        help='Private persistent storage OUTSIDE all Git repositories')
    parser.add_argument('--deploy', action='store_true', help='Authorize creation of a charged new pod')
    args = parser.parse_args()
    # Reject any Git worktree or repository path, including nested directories.
    store = args.store.expanduser().resolve()
    for parent in (store, *store.parents):
        if (parent / '.git').exists():
            parser.error('Secret storage must be outside Git repositories')
    token, folder = user_token(args.store, args.user_id)
    request = json.loads(args.request.read_text())
    if args.template_id:
        import re
        if not re.fullmatch(r'[a-zA-Z0-9]+', args.template_id):
            parser.error('Invalid template ID')
        request = template_request(api_request('templates/' + args.template_id), request)
        if not args.image:
            parser.error('Template deployments require --image with the rebuilt v8 digest')
    if args.image:
        request['imageName'] = args.image
    body = deployment_request(request, token)
    private_write(folder / 'prepared-request.json', json.dumps(body, indent=2) + '\n')
    print('Prepared deployment saved privately; credential values are hidden.')
    if not args.deploy:
        return
    pod = api_request('pods', body)
    pod_id = pod['id']
    private_write(folder / f'{pod_id}-launch.json', json.dumps({
        'pod_id': pod_id, 'launch_url': launch_url(pod_id, token)}, indent=2) + '\n')
    print(f'Created pod {pod_id}. Private launch file saved in the per-user store. Verification pending.')


if __name__ == '__main__':
    try:
        main()
    except urllib.error.HTTPError as exc:
        raise SystemExit(f'RunPod HTTP {exc.code}; response body withheld. No automatic retry.')
    except (ValueError, OSError, KeyError) as exc:
        # Avoid request/response contents and URL-bearing network diagnostics.
        raise SystemExit(f'Deployment failed ({type(exc).__name__}); check private configuration. No automatic retry.')
