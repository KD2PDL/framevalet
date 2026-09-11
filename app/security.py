"""Same-origin browser writes and a streaming request-size ceiling."""
from urllib.parse import urlsplit
from fastapi import HTTPException
from starlette.formparsers import MultiPartException
from starlette.responses import JSONResponse


def same_origin(origin, scheme, host):
    if not origin:
        return False
    try:
        p = urlsplit(origin)
        expected = urlsplit(f"{scheme}://{host}")
        return (p.scheme, p.hostname, p.port or (443 if p.scheme == 'https' else 80)) == (
            expected.scheme, expected.hostname,
            expected.port or (443 if expected.scheme == 'https' else 80)) and not p.username
    except ValueError:
        return False


class BodyTooLarge(HTTPException, MultiPartException):
    def __init__(self):
        HTTPException.__init__(self, 413, 'request too large (max 160 MB)')
        self.message = self.detail  # multipart parser closes its temporary files


class RequestGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope['headers'])
        if scope['method'] not in ('GET', 'HEAD', 'OPTIONS'):
            origin = headers.get(b'origin', headers.get(b'referer', b'')).decode()
            if not same_origin(origin, scope['scheme'], headers.get(b'host', b'').decode()):
                return await JSONResponse({'detail': 'same-origin request required'}, 403)(scope, receive, send)
        limit = 160_000_000
        try:
            length = int(headers.get(b'content-length', b'0'))
        except ValueError:
            length = limit + 1
        if length > limit:
            return await JSONResponse({'detail': 'request too large'}, 413)(scope, receive, send)
        count = 0
        async def bounded_receive():
            nonlocal count
            message = await receive()
            count += len(message.get('body', b''))
            if count > limit:
                raise BodyTooLarge()
            return message
        await self.app(scope, bounded_receive, send)
