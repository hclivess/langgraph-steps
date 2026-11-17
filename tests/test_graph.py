"""Run with:  python -m unittest discover tests"""
import json
import unittest
from pathlib import Path

from langgraph.types import Command

from lgsteps import Index, Settings, build_graph

DEMO = Path(__file__).resolve().parent.parent / "demo_data"
MONSTERA = "I just bought a monstera, where exactly should I put it regarding light?"


def run(question, **settings):
    graph = build_graph(Index(DEMO), Settings(**settings))
    config = {"configurable": {"thread_id": "t"}}
    nodes = [name for update in graph.stream({"question": question}, config, stream_mode="updates") for name in update]
    return graph, config, nodes


class GraphTest(unittest.TestCase):
    def test_answers_at_once(self):
        graph, config, nodes = run("which plants tolerate low light?")
        self.assertEqual(nodes, ["retrieve", "grade", "generate"])
        self.assertIn("low light", graph.get_state(config).values["answer"])

    def test_rewrites_and_tries_again(self):
        graph, config, nodes = run(MONSTERA)
        self.assertEqual(nodes, ["retrieve", "grade", "rewrite", "retrieve", "grade", "generate"])
        values = graph.get_state(config).values
        self.assertEqual(values["attempts"], 2)
        self.assertNotEqual(values["query"], values["question"])
        self.assertEqual(len(values["log"]), 6)          # the reducer appended one line per node

    def test_gives_up(self):
        graph, config, nodes = run("how do I change a flat car tyre?")
        self.assertEqual(nodes[-1], "give_up")
        self.assertNotIn("generate", nodes)

    def test_threshold_decides(self):
        question = "when is my sourdough starter ready?"
        self.assertEqual(run(question, threshold=0.3)[2][-1], "give_up")
        self.assertEqual(run(question, threshold=0.25)[2][-1], "generate")

    def test_interrupt_and_resume(self):
        for approved, last in ((True, "generate"), (False, "give_up")):
            graph, config, nodes = run("which plants tolerate low light?", ask_approval=True)
            self.assertEqual(graph.get_state(config).next, ("approve",))
            resumed = [name for update in graph.stream(Command(resume=approved), config, stream_mode="updates")
                       for name in update]
            self.assertEqual(resumed, ["approve", last])
            self.assertEqual(graph.get_state(config).next, ())


class ServerTest(unittest.TestCase):
    def setUp(self):
        try:
            from fastapi.testclient import TestClient
        except Exception as exc:                 # httpx is only needed for this test
            self.skipTest(f"fastapi test client not available: {exc}")
        from lgsteps.server import Session, create_app
        self.client = TestClient(create_app(Session(DEMO)))

    def events(self, url, body):
        response = self.client.post(url, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return [json.loads(line) for line in response.text.splitlines()]

    def test_state_and_page(self):
        state = self.client.get("/api/state").json()
        self.assertIn("grade", state["graph"]["nodes"])
        labels = {e["label"] for e in state["graph"]["edges"] if e["conditional"]}
        self.assertEqual(labels, {"enough", "ask a human", "try again", "out of attempts", "yes", "no"})
        for path in ("/", "/app.js", "/style.css"):
            self.assertEqual(self.client.get(path).status_code, 200)

    def test_run_stream(self):
        events = self.events("/api/run", {"question": MONSTERA})
        started = [e["node"] for e in events if e["step"] == "node_start"]
        self.assertEqual(started, ["retrieve", "grade", "rewrite", "retrieve", "grade", "generate"])
        done = events[-1]
        self.assertEqual(done["step"], "done")
        self.assertIn("Monstera", done["values"]["answer"])
        self.assertEqual(len(done["checkpoints"]), 8)    # the input, then one after every step

    def test_pause_and_resume(self):
        events = self.events("/api/run", {"question": "which plants tolerate low light?", "ask_approval": True})
        self.assertEqual(events[-1]["step"], "paused")
        self.assertTrue(any(e["step"] == "interrupt" for e in events))
        thread = events[0]["thread_id"]
        events = self.events("/api/resume", {"thread_id": thread, "approved": True})
        self.assertEqual(events[-1]["step"], "done")
        self.assertIn("answer", events[-1]["values"])
        again = self.client.post("/api/resume", json={"thread_id": thread, "approved": True})
        self.assertEqual(again.status_code, 400)

    def test_bad_input(self):
        self.assertEqual(self.client.post("/api/run", json={"question": " "}).status_code, 400)
        self.assertEqual(self.client.post("/api/run", json={"question": "x", "threshold": 7}).status_code, 400)
        self.assertEqual(self.client.post("/api/resume", json={"thread_id": "nope"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
