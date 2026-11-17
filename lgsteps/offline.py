"""Offline stand-ins, so the demo runs with no API key and no model download.

The graph's nodes need three abilities a real application gets from models:
search (an embedding model), rewriting a question, and writing an answer (a
chat model).  Here each is a small rule you can read in a minute.  They are
good enough to *see* the graph work, not to ship.
"""
from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import InMemoryVectorStore

_TOKEN = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset("""
a an and are as at be but by do does for from has have how i if in into is it its
my of on or our so that the their there these they this to was we what when where
which who why will with you your
""".split())


def stem(word: str) -> str:
    """Crude: watering -> water, plants -> plant.  Enough for a demo."""
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def words(text: str) -> list[str]:
    return [stem(w) for w in _TOKEN.findall(text.lower()) if w not in STOPWORDS]


class HashEmbeddings(Embeddings):
    """Bag-of-words embedding via the "hashing trick": each word is hashed to
    one of `dim` buckets, the vector counts hits per bucket and is normalised.
    It captures word overlap only, no meaning."""

    def __init__(self, dim: int = 512):
        self.dim = dim

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for word in words(text):
                vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dim] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


class Index:
    """The documents of a folder, cut into paragraphs, in a vector store."""

    def __init__(self, folder: str | Path):
        self.store = InMemoryVectorStore(HashEmbeddings())
        self.seen_in: dict[str, int] = {}       # word -> number of paragraphs it occurs in
        self.documents = 0
        paragraphs = []
        for path in sorted(Path(folder).glob("*")):
            if path.suffix.lower() not in (".md", ".txt"):
                continue
            self.documents += 1
            title = heading = path.stem.replace("_", " ").title()
            for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8")):
                block = block.strip()
                if not block:
                    continue
                if block.startswith("#"):       # a heading travels with the paragraphs under it
                    heading = block.lstrip("# ").strip()
                    continue
                paragraphs.append(Document(page_content=block, metadata={
                    "source": path.name, "heading": heading if heading == title else f"{title}: {heading}"}))
        if not paragraphs:
            raise ValueError(f"no .md or .txt files with text in {folder}")
        # A paragraph is embedded together with its heading; the text itself stays in the metadata.
        entries = [Document(page_content=f"{p.metadata['heading']}\n{p.page_content}",
                            metadata={**p.metadata, "text": p.page_content}) for p in paragraphs]
        for entry in entries:
            for word in set(words(entry.page_content)):
                self.seen_in[word] = self.seen_in.get(word, 0) + 1
        self.store.add_documents(entries)
        self.paragraphs = len(entries)

    def search(self, query: str, k: int) -> list[dict]:
        if not words(query):
            return []
        hits = self.store.similarity_search_with_score(query, k=k)
        return [{"source": doc.metadata["source"], "heading": doc.metadata["heading"],
                 "text": doc.metadata["text"], "score": round(float(score), 4)}
                for doc, score in hits if score > 0]


_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


def extract_answer(question: str, documents: list[dict], sentences: int = 2) -> str:
    """Not a language model: quotes the sentences that share the rarest words
    with the question, each with its [n] source number."""
    wanted = set(words(question))
    pool = [(number, sentence.strip())
            for number, doc in enumerate(documents, 1)
            for sentence in _SENTENCE.split(doc["text"]) if sentence.strip()]
    seen_in = {w: sum(1 for _, s in pool if w in words(s)) for w in wanted}
    scored = []
    for number, sentence in pool:
        score = sum(1 / seen_in[w] for w in wanted & set(words(sentence)))
        if score:
            scored.append((score, -len(scored), f"{sentence} [{number}]"))
    if not scored:
        return "I don't know: the documents have nothing on this."
    best = sorted(scored, reverse=True)[:sentences]
    return " ".join(text for _, _, text in sorted(best, key=lambda item: -item[1]))
