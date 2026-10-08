import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth, cloudflare, config, db as dbm, logbuf, maint, routes, sources, worker, ws

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
config.ensure_dirs()
logbuf.install()


def create_app() -> FastAPI:
    config.ensure_dirs()
    maint.apply_pending_restore()
    dbm.init()
    db = dbm.connect()
    logbuf.set_level(config.get(db, "log_level"))
    # headless bootstrap: ADMIN_USER/ADMIN_PASSWORD env creates the first admin
    if config.BOOTSTRAP_ADMIN_USER and config.BOOTSTRAP_ADMIN_PASSWORD:
        if not db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            auth.create_user(db, config.BOOTSTRAP_ADMIN_USER,
                             config.BOOTSTRAP_ADMIN_PASSWORD,
                             role="admin", can_delete_any=True)
    # TV_* env seeds the first TV on an empty install; TV_TOKEN carries a paired
    # connection to a new machine without redoing the Allow-on-the-remote ceremony.
    if config.SEED_TV_HOST and not db.execute("SELECT 1 FROM tvs LIMIT 1").fetchone():
        cur = db.execute(
            "INSERT INTO tvs(name, host, mac, client_name, created) VALUES(?,?,?,?,?)",
            (config.SEED_TV_NAME, config.SEED_TV_HOST, config.SEED_TV_MAC,
             config.SEED_TV_CLIENT, dbm.now()))
        db.commit()
        if config.SEED_TV_TOKEN:
            config.token_path(cur.lastrowid).write_text(config.SEED_TV_TOKEN)
    db.close()

    app = FastAPI(title="framevalet", docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(routes.router)
    app.include_router(sources.router)
    app.include_router(ws.router)
    from .security import RequestGuard
    app.add_middleware(RequestGuard)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"),
              name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "frame-ancestors 'none'; object-src 'none'; base-uri 'self'")
        if request.cookies.get(auth.COOKIE) or request.url.path in ("/login", "/setup") \
           or "cf-access-jwt-assertion" in request.headers:
            resp.headers["Cache-Control"] = "no-store"   # keep Cloudflare's edge cache out
        return resp

    @app.exception_handler(307)
    async def redirect_handler(request: Request, exc):
        return RedirectResponse(exc.headers["Location"], 307)

    @app.on_event("startup")
    async def startup():
        ws.init(asyncio.get_running_loop())
        asyncio.create_task(worker.run())
        db = dbm.connect()
        try:
            token = config.get(db, "cf_tunnel_token")
            if token and config.get(db, "cf_tunnel_autostart") == "true":
                cloudflare.tunnel.start(token)
        finally:
            db.close()

    @app.on_event("shutdown")
    async def shutdown():
        cloudflare.tunnel.stop()

    return app


app = create_app()


def run():
    import uvicorn
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    run()
