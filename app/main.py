import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth, config, db as dbm, logbuf, routes, sources, worker, ws

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
logbuf.install()


def create_app() -> FastAPI:
    dbm.init()
    db = dbm.connect()
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
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"),
              name="static")

    @app.exception_handler(307)
    async def redirect_handler(request: Request, exc):
        return RedirectResponse(exc.headers["Location"], 307)

    @app.on_event("startup")
    async def startup():
        ws.init(asyncio.get_running_loop())
        asyncio.create_task(worker.run())

    return app


app = create_app()


def run():
    import uvicorn
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    run()
