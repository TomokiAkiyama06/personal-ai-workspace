"""Importable retriever factories used by the benchmark CLI tests."""


class FixedRetriever:
    def __init__(self, ids):
        self.ids = ids

    def retrieve(self, query_text, requester_principals, k):
        return self.ids


def make_retriever():
    return FixedRetriever(["mem1"])


def make_malformed_retriever():
    return FixedRetriever([5])
