"""The six repeatable production incidents used by the demonstration.

They are data-plane mutations only: they never alter the test suite or execute
user supplied code.  The guardian sees the resulting telemetry, not this
catalog, then creates an RCA and a *gated* remediation PR when appropriate.
"""

from __future__ import annotations


KNOWN_RUNTIME_INCIDENTS = (
    {
        "id": "schema-drift",
        "label": "Schema drift: size renamed to quantity",
        "button": "Schema drift",
        "ops": [{"op": "rename_field", "field": "size", "to": "quantity"}],
    },
    {
        "id": "null-surge",
        "label": "Data quality: null size surge",
        "button": "Null surge",
        "ops": [{"op": "null_field", "field": "size"}],
    },
    {
        "id": "duplicate-storm",
        "label": "Replay: duplicate trade storm",
        "button": "Duplicate storm",
        "ops": [{"op": "duplicate"}],
    },
    {
        "id": "volume-collapse",
        "label": "Upstream stall: volume collapse",
        "button": "Volume collapse",
        "ops": [{"op": "shrink_batch"}],
    },
    {
        "id": "latency-surge",
        "label": "Load pressure: processing latency surge",
        "button": "Latency surge",
        "ops": [{"op": "latency", "ms": 800}],
    },
    {
        "id": "malformed-price",
        "label": "Data quality: malformed price values",
        "button": "Malformed price",
        "ops": [{"op": "corrupt_type", "field": "price", "value": "INVALID"}],
    },
)


def ids() -> tuple[str, ...]:
    """Stable identifiers for validation, tests and dashboard rendering."""
    return tuple(item["id"] for item in KNOWN_RUNTIME_INCIDENTS)
