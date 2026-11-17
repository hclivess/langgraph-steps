"""Local web interface:  python -m lgsteps

A FastAPI app, started with uvicorn from inside this module.  It serves the
page in `lgsteps/webui/` (plain JavaScript, no build step) and a small API.

A run answers with newline-delimited JSON: one line per thing LangGraph's own
stream reports (`graph.stream(..., stream_mode=["tasks", "updates", "values"])`):
a node started, a node returned an update, the state after the step, an
interrupt.  Nothing is instrumented by hand.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Callable, Iterator

import uvicorn
from fastapi import Body, FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from .graph import Settings, build_graph
from .offline import Index

WEBUI = Path(__file__).parent / "webui"
_END = object()
MAX_RUNS = 50


class Session:
    """What the server keeps between requests: the index, the checkpoints, the paused runs."""

    def __init__(self, folder: str | Path = "demo_data"):
        self.folder = str(folder)
        self.index = Index(folder)
        self.saver = InMemorySaver()
        self.runs: dict[str, object] = {}       # thread_id -> compiled graph

    def state(self) -> dict:
        drawing = build_graph(self.index, Settings()).get_graph()
        defaults = Settings()
        return {
            "folder": self.folder,
            "documents": self.index.documents,
            "paragraphs": self.index.paragraphs,
            "checkpointer": type(self.saver).__name__,
            "defaults": {"threshold": defaults.threshold, "max_attempts": defaults.max_attempts,
                         "k": defaults.k, "ask_approval": defaults.ask_approval},
            "graph": {"nodes": list(drawing.nodes),
                      "edges": [{"source": e.source, "target": e.target, "label": e.data,
                                 "conditional": e.conditional} for e in drawing.edges]},
        }


def read_settings(body: dict) -> Settings:
    settings = Settings(threshold=float(body.get("threshold", Settings.threshold)),
                        max_attempts=int(body.get("max_attempts", Settings.max_attempts)),
                        k=int(body.get("k", Settings.k)),
                        ask_approval=bool(body.get("ask_approval", False)))
    if not 0 <= settings.threshold <= 1:
        raise ValueError("the threshold must be between 0 and 1")
    if not 1 <= settings.max_attempts <= 10:
        raise ValueError("the number of attempts must be between 1 and 10")
    if not 1 <= settings.k <= 10:
        raise ValueError("k must be between 1 and 10")
    return settings


def checkpoints(app, config: dict) -> list[dict]:
    """The saved checkpoints of a thread, oldest first."""
    out = []
    for snapshot in reversed(list(app.get_state_history(config))):
        values = snapshot.values
        out.append({"step": snapshot.metadata.get("step"), "source": snapshot.metadata.get("source"),
                    "id": snapshot.config["configurable"]["checkpoint_id"], "time": snapshot.created_at,
                    "next": list(snapshot.next), "query": values.get("query"),
                    "attempts": values.get("attempts", 0), "relevant": len(values.get("relevant", [])),
                    "log": len(values.get("log", [])), "answered": "answer" in values})
    return out


def run_events(app, thread_id: str, graph_input) -> Iterator[dict]:
    config = {"configurable": {"thread_id": thread_id}}
    started = time.perf_counter()
    ms = lambda: round((time.perf_counter() - started) * 1000, 1)
    yield {"step": "start", "thread_id": thread_id, "resumed": isinstance(graph_input, Command)}
    for mode, payload in app.stream(graph_input, config, stream_mode=["tasks", "updates", "values"]):
        if "__interrupt__" in payload:          # reported in "updates" and in "values"
            if mode == "updates":
                yield {"step": "interrupt", "value": payload["__interrupt__"][0].value, "ms": ms()}
        elif mode == "tasks" and "input" in payload:
            yield {"step": "node_start", "node": payload["name"], "ms": ms()}
        elif mode == "tasks":
            if payload.get("error"):
                raise RuntimeError(f"{payload['name']}: {payload['error']}")
            yield {"step": "node_end", "node": payload["name"], "ms": ms(),
                   "interrupted": bool(payload.get("interrupts")), "update": payload.get("result") or {}}
        elif mode == "values":
            yield {"step": "state", "values": payload, "ms": ms()}
    snapshot = app.get_state(config)
    yield {"step": "paused" if snapshot.next else "done", "next": list(snapshot.next), "values": snapshot.values,
           "checkpoints": checkpoints(app, config), "ms": ms()}


def _ndjson(make_events: Callable[[], Iterator[dict]]) -> StreamingResponse:
    """Stream one JSON line per event; the work runs in its own thread."""
    events: queue.Queue = queue.Queue()

    def work() -> None:
        try:
            for event in make_events():
                events.put(event)
        except Exception as exc:                # report it in the stream
            events.put({"step": "error", "message": str(exc)})
        finally:
            events.put(_END)

    def lines():
        threading.Thread(target=work, daemon=True).start()
        while (event := events.get()) is not _END:
            yield json.dumps(event, default=str) + "\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})


def create_app(session: Session) -> FastAPI:
    app = FastAPI(title="langgraph-steps", docs_url="/api/docs", redoc_url=None)
    lock = threading.Lock()                     # one run at a time

    def locked(make_events):
        def run():
            with lock:
                yield from make_events()
        return run

    @app.exception_handler(ValueError)
    @app.exception_handler(KeyError)
    async def bad_request(request, exc):
        return JSONResponse({"error": str(exc).strip("'\"")}, status_code=400)

    def page(name: str, media_type: str | None = None):
        return FileResponse(WEBUI / name, media_type=media_type, headers={"Cache-Control": "no-store"})

    @app.get("/", include_in_schema=False)
    def index():
        return page("index.html")

    @app.get("/app.js", include_in_schema=False)
    def app_js():
        return page("app.js", "text/javascript")

    @app.get("/style.css", include_in_schema=False)
    def style_css():
        return page("style.css")

    @app.get("/api/state")
    def get_state():
        return session.state()

    @app.post("/api/run")
    def post_run(body: dict = Body(...)):
        """Start a new thread and run the graph until it ends or pauses."""
        question = str(body.get("question", "")).strip()
        if not question:
            raise ValueError("the question is empty")
        graph = build_graph(session.index, read_settings(body), session.saver)
        thread_id = uuid.uuid4().hex[:8]
        session.runs[thread_id] = graph
        while len(session.runs) > MAX_RUNS:
            session.runs.pop(next(iter(session.runs)))
        return _ndjson(locked(lambda: run_events(graph, thread_id, {"question": question})))

    @app.post("/api/resume")
    def post_resume(body: dict = Body(...)):
        """Continue a paused thread from its checkpoint with the human's answer."""
        thread_id = str(body.get("thread_id", ""))
        if thread_id not in session.runs:
            raise ValueError("this run is not known any more; start a new one")
        graph = session.runs[thread_id]
        if not graph.get_state({"configurable": {"thread_id": thread_id}}).next:
            raise ValueError("this run has already finished")
        return _ndjson(locked(lambda: run_events(graph, thread_id, Command(resume=bool(body.get("approved"))))))

    return app


def serve(session: Session, host: str = "127.0.0.1", port: int = 8767, open_browser: bool = True) -> None:
    """Start uvicorn in this process and (optionally) open the page."""
    url = f"http://{host}:{port}/"
    print(f"langgraph-steps web interface: {url}   (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(create_app(session), host=host, port=port, log_level="warning", ws="none")
