import asyncio
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth, config, db as dbm, routes, worker

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")


def create_app() -> FastAPI:
    dbm.init()
    # TV_TOKEN env: lets a paired connection move to a new machine without
    # redoing the Allow-on-the-remote ceremony (pairs with TV_CLIENT_NAME).
    tok = os.environ.get("TV_TOKEN", "").strip()
    if tok and (not config.TOKEN_PATH.exists()
                or config.TOKEN_PATH.read_text().strip() != tok):
        config.TOKEN_PATH.write_text(tok)
    # headless bootstrap: ADMIN_USER/ADMIN_PASSWORD env creates the first admin
    if config.BOOTSTRAP_ADMIN_USER and config.BOOTSTRAP_ADMIN_PASSWORD:
        db = dbm.connect()
        if not db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            auth.create_user(db, config.BOOTSTRAP_ADMIN_USER,
                             config.BOOTSTRAP_ADMIN_PASSWORD,
                             role="admin", can_delete_any=True)
        db.close()

    app = FastAPI(title="framevalet", docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(routes.router)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"),
              name="static")

    @app.exception_handler(307)
    async def redirect_handler(request: Request, exc):
        return RedirectResponse(exc.headers["Location"], 307)

    @app.on_event("startup")
    async def startup():
        asyncio.create_task(worker.run())

    return app


app = create_app()


def run():
    import uvicorn
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    run()
