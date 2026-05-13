"""MHM Public Results Server."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import server.database as db
from shared.scoring import rank_results

API_KEY = os.environ.get("MHM_API_KEY", "changeme")

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="MHM Public Results", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, course_id: Optional[int] = None):
    all_results = db.get_public_results()
    courses_seen: dict[int, str] = {}
    for r in all_results:
        courses_seen[r.course.id] = r.course.name

    if course_id:
        filtered = [r for r in all_results if r.course.id == course_id]
    else:
        filtered = all_results

    ranked = rank_results(filtered)
    return templates.TemplateResponse("results.html", {
        "request": request,
        "ranked": ranked,
        "courses": courses_seen,
        "selected_course_id": course_id,
        "total": len(all_results),
    })


@app.get("/api/results")
async def api_results():
    all_results = db.get_public_results()
    ranked = rank_results(all_results)
    return JSONResponse([
        {
            "rank": rank,
            "racer": r.racer.name,
            "team": r.racer.team,
            "course": r.course.name,
            "total_points": r.total_points,
            "elapsed_seconds": r.elapsed_seconds,
            "status": r.entry.status.value,
        }
        for rank, r in ranked
    ])


@app.post("/api/sync")
async def sync(request: Request, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")
    payload = await request.json()
    acked = db.apply_sync(payload)
    return JSONResponse(acked)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server.app:app", host="0.0.0.0", port=8000, reload=False)
