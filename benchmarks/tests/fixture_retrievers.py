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


def make_object_without_retrieve():
    return object()


def make_wrong_signature_retriever():
    class WrongSignature:
        def retrieve(self, only_one):
            return []

    return WrongSignature()


def make_failing_retriever():
    raise RuntimeError("model init failed: token=SECRET-TOKEN")


def make_string_retriever():
    return FixedRetriever("mem1")
