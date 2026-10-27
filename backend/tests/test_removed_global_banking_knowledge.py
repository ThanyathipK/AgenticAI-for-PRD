"""Regression guard for removal of the cross-project knowledge feature."""

from app.main import app


def test_global_banking_knowledge_api_is_not_registered():
    paths = {path for route in app.routes if (path := getattr(route, "path", ""))}

    assert not any(path.startswith("/api/banking-knowledge") for path in paths)
