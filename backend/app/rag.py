"""Retrieval over the merchant help center (RAG).

A TF-IDF vector index with cosine similarity, built in pure Python so the
demo has no heavy dependencies and works offline. Hinglish and Hindi words
are expanded to English synonyms before search. The interface (search ->
ranked docs with scores) is the same one a dense-embedding vector store
would expose, so it can be swapped for one without touching the agents.
"""
from __future__ import annotations

import math
from collections import Counter

from .config import LEXICON, SEED
from .text import tokens

STOP = set(LEXICON["stopwords"])
SYN = LEXICON["rag_synonyms"]


def _terms(text: str) -> list[str]:
    out = []
    for t in tokens(text):
        if t in STOP:
            continue
        out.append(t)
        if t in SYN:
            out.extend(SYN[t].split())
    # crude stemming so "settled"/"settlement" and "refunds"/"refund" meet
    return [t[:-1] if len(t) > 4 and t.endswith("s") and t.isascii() else t for t in out]


class HelpCenterIndex:
    def __init__(self, docs: list[dict]):
        self.docs = docs
        tf = [Counter(_terms(d["title"] + " " + d["title"] + " " + d["body"])) for d in docs]
        df = Counter(term for counts in tf for term in counts)
        n = len(docs)
        self.idf = {term: math.log((1 + n) / (1 + c)) + 1 for term, c in df.items()}
        self.vecs = [self._weigh(counts) for counts in tf]

    def _weigh(self, counts: Counter) -> dict[str, float]:
        vec = {t: (1 + math.log(c)) * self.idf.get(t, 0.0) for t, c in counts.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {t: v / norm for t, v in vec.items()}

    def search(self, query: str, k: int = 3) -> list[dict]:
        q = self._weigh(Counter(_terms(query)))
        scored = []
        for doc, vec in zip(self.docs, self.vecs):
            score = sum(w * vec.get(t, 0.0) for t, w in q.items())
            scored.append((score, doc))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {"id": d["id"], "title": d["title"], "score": round(s, 3), "snippet": d["body"][:180], "short": d["short"]}
            for s, d in scored[:k] if s > 0
        ]


index = HelpCenterIndex(SEED["kb"])
