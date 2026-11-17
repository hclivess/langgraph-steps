"""The graph: a small "corrective RAG" loop, written with LangGraph.

    START -> retrieve -> grade -+-> generate -> END          enough good paragraphs
                 ^              +-> approve -> generate       ... and a human must say yes first
                 |              +-> rewrite -+                not enough: change the query, try again
                 +---------------------------+
                                +-> give_up -> END            out of attempts

What makes it a graph and not a chain: the state every node reads and
updates, the conditional edge after `grade`, the loop through `rewrite`, the
pause for a human in `approve`, and a checkpoint saved after every step.
"""
from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .offline import Index, extract_answer, words


class State(TypedDict, total=False):
    question: str                               # what the user asked; never changes
    query: str                                  # what is searched for; `rewrite` changes it
    attempts: int                               # how many searches so far
    documents: list[dict]                       # the last search result
    relevant: list[dict]                        # the documents that passed the grade
    approved: bool                              # the human's answer, if asked
    answer: str
    log: Annotated[list[str], operator.add]     # a reducer: updates are appended, not overwritten


@dataclass
class Settings:
    threshold: float = 0.3          # a paragraph is relevant at this score or more
    max_attempts: int = 3           # searches before giving up
    k: int = 3                      # paragraphs per search
    ask_approval: bool = False      # pause for a human before generating


def build_graph(index: Index, settings: Settings, checkpointer=None):
    def retrieve(state: State) -> dict:
        query = state.get("query", state["question"])
        documents = index.search(query, settings.k)
        best = documents[0]["score"] if documents else 0
        return {"query": query, "documents": documents, "attempts": state.get("attempts", 0) + 1,
                "log": [f"retrieve: searched for \"{query}\", best score {best:.2f}"]}

    def grade(state: State) -> dict:
        relevant = [d for d in state["documents"] if d["score"] >= settings.threshold]
        return {"relevant": relevant,
                "log": [f"grade: {len(relevant)} of {len(state['documents'])} at {settings.threshold:.2f} or more"]}

    def after_grade(state: State) -> str:
        """The conditional edge: reads the state, returns the name of the way to go."""
        if state["relevant"]:
            return "ask a human" if settings.ask_approval else "enough"
        if state["attempts"] < settings.max_attempts:
            return "try again"
        return "out of attempts"

    def rewrite(state: State) -> dict:
        """First rewrite: keep only the words the documents contain.
        Later ones: also drop the word found in the most paragraphs (it says the least)."""
        known = [w for w in dict.fromkeys(words(state["query"])) if w in index.seen_in]     # stemmed, in order
        if state["attempts"] > 1 and len(known) > 1:
            known.remove(max(known, key=lambda w: index.seen_in[w]))
        query = " ".join(known)
        return {"query": query, "log": [f"rewrite: \"{state['query']}\" became \"{query}\""]}

    def approve(state: State) -> dict:
        # interrupt() stops the graph here and saves it.  When the run is resumed with
        # Command(resume=value), this node starts again and interrupt() returns that value.
        approved = interrupt({"question": state["question"],
                              "sources": [f"{d['source']}: {d['heading']}" for d in state["relevant"]]})
        return {"approved": bool(approved), "log": [f"approve: the human said {'yes' if approved else 'no'}"]}

    def after_approve(state: State) -> str:
        return "yes" if state["approved"] else "no"

    def generate(state: State) -> dict:
        answer = extract_answer(state["question"], state["relevant"])
        return {"answer": answer, "log": [f"generate: answered from {len(state['relevant'])} paragraphs"]}

    def give_up(state: State) -> dict:
        reason = "the human rejected the sources" if state.get("approved") is False else \
            f"nothing relevant after {state['attempts']} searches"
        return {"answer": f"No answer: {reason}.", "log": [f"give_up: {reason}"]}

    graph = StateGraph(State)
    for node in (retrieve, grade, rewrite, approve, generate, give_up):
        graph.add_node(node.__name__, node)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "grade")
    graph.add_conditional_edges("grade", after_grade, {"enough": "generate", "ask a human": "approve",
                                                       "try again": "rewrite", "out of attempts": "give_up"})
    graph.add_edge("rewrite", "retrieve")
    graph.add_conditional_edges("approve", after_approve, {"yes": "generate", "no": "give_up"})
    graph.add_edge("generate", END)
    graph.add_edge("give_up", END)
    return graph.compile(checkpointer=checkpointer or InMemorySaver())
