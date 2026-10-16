"""Runs the shared repository contract against the in-memory fake."""

import pytest

from fakes import InMemoryRepository
from repository_contract import RepositoryContract


class TestInMemoryRepository(RepositoryContract):
    failures = (ValueError, LookupError)

    @pytest.fixture
    def repo(self):
        return InMemoryRepository()

    def occurrence_rows(self, repo, break_id):
        return [dict(o) for o in repo.occurrences if o["break_id"] == break_id]

    def audit_rows(self, repo):
        return [dict(a) for a in repo.audit]

    def batch_rows(self, repo):
        return [dict(b) for b in repo.batches]

    def run_state(self, repo, run_id):
        run = repo.runs[run_id]
        return run["status"], run["finished_at"]
