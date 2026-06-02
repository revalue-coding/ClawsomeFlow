"""Round-trip tests for the task description ↔ output-summary codec.

The codec spans three concerns and is critical to keep in sync:

1. ``merge_description`` / ``split_description`` raw helpers.
2. ``FlowTask`` Pydantic model:
   * ``model_validator`` folds the helper field INTO ``description``.
   * ``field_serializer`` re-splits on the way out — including
     **nested** serialisation under :class:`FlowSpec` (this is what
     guarantees the front-end gets two clean fields back via the
     REST GET endpoints).
3. The dispatch prompt always sees the canonical merged form (because
   ``RunController`` uses ``Flow.parsed_spec()`` which re-validates).
"""

from __future__ import annotations

import pytest

from app.models import (
    OUTPUT_SUMMARY_MARKER,
    AgentKind,
    FlowAgent,
    FlowSpec,
    FlowTask,
    merge_description,
    split_description,
)


# ── raw helpers ───────────────────────────────────────────────────────


@pytest.mark.parametrize("body,req,expected", [
    ("do X", "report N", f"do X\n\n{OUTPUT_SUMMARY_MARKER}\nreport N"),
    ("do X", None, "do X"),
    ("do X", "", "do X"),
    ("", "report N", f"{OUTPUT_SUMMARY_MARKER}\nreport N"),
    ("", None, ""),
    ("multi\nline body", "  trim me  ",
     f"multi\nline body\n\n{OUTPUT_SUMMARY_MARKER}\ntrim me"),
])
def test_merge_examples(body: str, req: str | None, expected: str) -> None:
    assert merge_description(body, req) == expected


def test_merge_strips_trailing_whitespace() -> None:
    out = merge_description("body\n\n   ", "req")
    assert out == f"body\n\n{OUTPUT_SUMMARY_MARKER}\nreq"


@pytest.mark.parametrize("text,expected_body,expected_req", [
    (f"do X\n\n{OUTPUT_SUMMARY_MARKER}\nreport N", "do X", "report N"),
    ("just description", "just description", None),
    ("", "", None),
    (f"{OUTPUT_SUMMARY_MARKER}\nonly req", "", "only req"),
    # Marker absent → entire text is body, no req.
    ("body without marker", "body without marker", None),
    # Multiple markers — only first counts.
    (f"a\n{OUTPUT_SUMMARY_MARKER}\nfirst\n{OUTPUT_SUMMARY_MARKER}\nsecond",
     "a", f"first\n{OUTPUT_SUMMARY_MARKER}\nsecond"),
])
def test_split_examples(
    text: str, expected_body: str, expected_req: str | None,
) -> None:
    assert split_description(text) == (expected_body, expected_req)


def test_round_trip_property() -> None:
    samples = [
        ("body", "req"),
        ("multi\nline", "multi\nline req"),
        ("body only", None),
        ("", "req only"),
        ("", None),
    ]
    for b, r in samples:
        merged = merge_description(b, r)
        out_body, out_req = split_description(merged)
        assert out_body == b.rstrip()
        # ``r`` is normalised to ``None`` when None or whitespace-only.
        expected_req = (r.strip() if r is not None else None) or None
        assert out_req == expected_req


# ── FlowTask model ────────────────────────────────────────────────────


def test_flowtask_merges_helper_into_description() -> None:
    t = FlowTask(
        id="t", owner_agent_id="a", subject="x",
        description="do X", output_summary_requirement="report N",
    )
    assert t.description == f"do X\n\n{OUTPUT_SUMMARY_MARKER}\nreport N"
    # Helper is cleared internally to keep `description` the single truth.
    assert t.output_summary_requirement is None


def test_flowtask_idempotent_on_re_validation() -> None:
    t1 = FlowTask(
        id="t", owner_agent_id="a", subject="x",
        description="do X", output_summary_requirement="report N",
    )
    # Re-parse from its dump — the resulting task should be identical.
    t2 = FlowTask.model_validate(t1.model_dump())
    assert t2.description == t1.description
    assert t2.output_summary_requirement is None


def test_flowtask_dump_splits_back_snake() -> None:
    t = FlowTask(
        id="t", owner_agent_id="a", subject="x",
        description="do X", output_summary_requirement="report N",
    )
    data = t.model_dump(by_alias=False)
    assert data["description"] == "do X"
    assert data["output_summary_requirement"] == "report N"


def test_flowtask_dump_splits_back_camel() -> None:
    t = FlowTask(
        id="t", owner_agent_id="a", subject="x",
        description="do X", output_summary_requirement="report N",
    )
    data = t.model_dump(by_alias=True)
    assert data["description"] == "do X"
    assert data["outputSummaryRequirement"] == "report N"


def test_flowtask_no_summary_dumps_none() -> None:
    t = FlowTask(
        id="t", owner_agent_id="a", subject="x", description="do X",
    )
    data = t.model_dump(by_alias=True)
    assert data["description"] == "do X"
    assert data["outputSummaryRequirement"] is None


def test_flowspec_nested_serialisation_splits_per_task() -> None:
    """Critical: Pydantic v2 nested serialisation of a FlowSpec must
    invoke the FlowTask field_serializers on every task (not just the
    top-level model). If this regresses, the REST GET endpoints would
    leak the marker into the front-end."""
    spec = FlowSpec(
        agents=[FlowAgent(id="a", name="A", kind=AgentKind.openclaw, is_leader=True)],
        tasks=[
            FlowTask(
                id="t1", owner_agent_id="a", subject="s",
                description="body1", output_summary_requirement="req1",
            ),
            FlowTask(
                id="t2", owner_agent_id="a", subject="s",
                description="body2", output_summary_requirement=None,
            ),
        ],
    )
    out = spec.model_dump(by_alias=True, mode="json")
    t1, t2 = out["tasks"]
    assert t1["description"] == "body1"
    assert t1["outputSummaryRequirement"] == "req1"
    assert t2["description"] == "body2"
    assert t2["outputSummaryRequirement"] is None


def test_flowspec_round_trip_preserves_canonical_in_memory_form() -> None:
    """After dump → re-validate, in-memory ``task.description`` is the
    merged canonical form again — which is what the dispatch prompt
    renderer needs to see."""
    spec = FlowSpec(
        agents=[FlowAgent(id="a", name="A", kind=AgentKind.openclaw, is_leader=True)],
        tasks=[FlowTask(
            id="t", owner_agent_id="a", subject="x",
            description="do X", output_summary_requirement="report N",
        )],
    )
    spec2 = FlowSpec.model_validate(spec.model_dump(by_alias=True, mode="json"))
    assert spec2.tasks[0].description == \
        f"do X\n\n{OUTPUT_SUMMARY_MARKER}\nreport N"
