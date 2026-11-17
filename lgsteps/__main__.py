"""Command line.

    python -m lgsteps                      start the web interface (same as: serve)
    python -m lgsteps serve --port 9000 --no-open --folder my_notes
    python -m lgsteps ask "which plants tolerate low light?"
"""
from __future__ import annotations

import argparse

from .graph import Settings, build_graph
from .offline import Index


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m lgsteps", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    p_serve = sub.add_parser("serve", help="start the web interface (the default)")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8767)
    p_serve.add_argument("--no-open", action="store_true", help="do not open the browser")
    p_serve.add_argument("--folder", default="demo_data", help="folder with .md / .txt files")
    p_ask = sub.add_parser("ask", help="run the graph once in the terminal")
    p_ask.add_argument("question")
    p_ask.add_argument("--folder", default="demo_data")
    p_ask.add_argument("--threshold", type=float, default=Settings.threshold)
    args = parser.parse_args()

    if args.command == "ask":
        graph = build_graph(Index(args.folder), Settings(threshold=args.threshold))
        config = {"configurable": {"thread_id": "cli"}}
        for update in graph.stream({"question": args.question}, config, stream_mode="updates"):
            for node, values in update.items():
                print(f"{node:<9} {values['log'][0].split(': ', 1)[1]}")
        print(f"\n{graph.get_state(config).values['answer']}")
        return

    from .server import Session, serve
    serve(Session(getattr(args, "folder", "demo_data")), getattr(args, "host", "127.0.0.1"),
          getattr(args, "port", 8767), open_browser=not getattr(args, "no_open", False))


if __name__ == "__main__":
    main()
