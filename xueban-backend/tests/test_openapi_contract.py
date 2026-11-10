"""OpenAPI generation contract tests."""

from __future__ import annotations

from collections import Counter
import warnings

from xueban.server import app


def test_openapi_operation_ids_are_unique_without_duplicate_warnings():
    """Every generated client operation must have a stable, unique identifier."""
    previous_schema = app.openapi_schema
    app.openapi_schema = None
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            schema = app.openapi()
    finally:
        app.openapi_schema = previous_schema

    duplicate_warnings = [
        str(item.message)
        for item in caught
        if "Duplicate Operation ID" in str(item.message)
    ]
    assert duplicate_warnings == []

    operation_ids = [
        operation["operationId"]
        for path_item in schema["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]
    duplicates = [
        operation_id
        for operation_id, count in Counter(operation_ids).items()
        if count > 1
    ]
    assert duplicates == []
