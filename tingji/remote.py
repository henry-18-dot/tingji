"""Trust only the configured Tailscale Serve origin and the owner's injected identity."""
import json
import re
from urllib.parse import urlsplit

from . import storage


def configuration():
    path = storage.DATA / 'remote.json'
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        origin = str(data.get('url', '')).rstrip('/')
        parsed = urlsplit(origin)
        if (parsed.scheme != 'https' or not re.fullmatch(r'[a-z0-9-]+\.[a-z0-9-]+\.ts\.net', parsed.netloc)
                or parsed.path or not data.get('ownerLogin') or '\n' in data['ownerLogin']):
            return {}
        return {'url': origin, 'host': parsed.netloc, 'ownerLogin': data['ownerLogin']}
    except (ValueError, OSError, TypeError):
        return {}


def allowed_request(headers, port):
    host = headers.get('Host', '')
    local = {f'127.0.0.1:{port}', f'localhost:{port}'}
    identity = headers.get('Tailscale-User-Login', '')
    forwarded = headers.get('X-Forwarded-For') or headers.get('X-Forwarded-Proto')
    config = configuration()
    # Never classify a reverse-proxied request as a trusted local browser because
    # its proxy rewrites Host to localhost. Missing identity must fail closed.
    if identity or forwarded or host not in local:
        if not config or host != config['host'] or identity != config['ownerLogin']:
            raise PermissionError('只有已配置的本人私有网络账号可以访问听记。')
        origin = config['url']
        is_local = False
    else:
        origin = 'http://' + host
        is_local = True
    supplied_origin = headers.get('Origin')
    if supplied_origin and supplied_origin != origin:
        raise PermissionError('不接受其他网站发起的请求。')
    return is_local


def public_status():
    config = configuration()
    return {'configured': bool(config), 'url': config.get('url', ''), 'kind': 'tailscale',
            'account': config.get('ownerLogin', '')}
