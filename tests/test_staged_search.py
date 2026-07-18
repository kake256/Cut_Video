import unittest
from unittest.mock import patch

from moment_retrieval.search_service import EvidenceSpan, SearchHit, SEMANTIC_KIND
from moment_retrieval.staged_search import (
    SearchRequestRegistry,
    StagedSearchCoordinator,
)


class SearchRequestRegistryTest(unittest.TestCase):
    def test_new_request_supersedes_only_the_same_session(self):
        registry = SearchRequestRegistry()
        first = registry.begin("session-a")
        other = registry.begin("session-b")
        second = registry.begin("session-a")

        self.assertFalse(registry.is_current("session-a", first))
        self.assertTrue(registry.is_current("session-a", second))
        self.assertTrue(registry.is_current("session-b", other))

    def test_expired_request_is_not_current(self):
        with patch("moment_retrieval.staged_search.time.monotonic") as clock:
            clock.return_value = 10.0
            registry = SearchRequestRegistry(ttl_sec=5)
            request_id = registry.begin(None)
            clock.return_value = 16.0

            self.assertFalse(registry.is_current(None, request_id))

    def test_coordinator_yields_text_then_semantic_and_closes_each_connection(self):
        connections = []
        text_hit = SearchHit(
            "text-1", "video-1", "text", EvidenceSpan(0, 1000),
            0, 1000, "text result",
        )
        semantic_hit = SearchHit(
            "semantic-1", "video-1", SEMANTIC_KIND, EvidenceSpan(2000, 3000),
            2000, 3000, "semantic result", semantic_score=0.9,
        )

        class Connection:
            closed = False

            def close(self):
                self.closed = True

        class Service:
            semantic_error = None

            def search_text_stage(self, *_args, **_kwargs):
                return [text_hit], "publication-1"

            def search_semantic_stage(self, *_args, **_kwargs):
                return [semantic_hit]

        def connect():
            connection = Connection()
            connections.append(connection)
            return connection

        stages = list(StagedSearchCoordinator().search(
            "query",
            session_id="session",
            connection_factory=connect,
            service_factory=lambda _connection: Service(),
            public_video_id="video-1",
            text_limit=5,
            semantic_limit=5,
            min_score=0.55,
        ))

        self.assertEqual([[hit.hit_id for hit in stage.hits] for stage in stages], [
            ["text-1"], ["text-1", "semantic-1"],
        ])
        self.assertFalse(stages[0].complete)
        self.assertTrue(stages[1].complete)
        self.assertTrue(all(connection.closed for connection in connections))

    def test_coordinator_discards_a_superseded_request_before_first_yield(self):
        registry = SearchRequestRegistry()

        class Connection:
            def close(self):
                pass

        class Service:
            semantic_error = None

            def search_text_stage(self, *_args, **_kwargs):
                registry.begin("session")
                return [], "publication-1"

        stages = list(StagedSearchCoordinator(registry=registry).search(
            "old query",
            session_id="session",
            connection_factory=Connection,
            service_factory=lambda _connection: Service(),
            public_video_id=None,
            text_limit=5,
            semantic_limit=5,
            min_score=0.55,
        ))

        self.assertEqual(stages, [])

    def test_coordinator_skips_semantic_work_when_superseded_after_text_yield(self):
        registry = SearchRequestRegistry()
        semantic_calls = []
        connections = []

        class Connection:
            def close(self):
                pass

        class Service:
            semantic_error = None

            def search_text_stage(self, *_args, **_kwargs):
                return [], "publication-1"

            def search_semantic_stage(self, *_args, **_kwargs):
                semantic_calls.append(True)
                return []

        def connect():
            connections.append(True)
            return Connection()

        generator = StagedSearchCoordinator(registry=registry).search(
            "old query",
            session_id="session",
            connection_factory=connect,
            service_factory=lambda _connection: Service(),
            public_video_id=None,
            text_limit=5,
            semantic_limit=5,
            min_score=0.55,
        )
        next(generator)
        registry.begin("session")

        self.assertEqual(list(generator), [])
        self.assertEqual(semantic_calls, [])
        self.assertEqual(len(connections), 1)


if __name__ == "__main__":
    unittest.main()
