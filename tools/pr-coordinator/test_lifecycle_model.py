"""Exercise dashboard timing from representative API/coordinator records."""

from datetime import datetime, timezone
from typing import Any

from lifecycle_model import build, summarize

EPOCH = 1795500000
LINK = "https://github.com/example/project/actions/runs/1"


def iso(delta: int) -> str:
    return datetime.fromtimestamp(EPOCH + delta, timezone.utc).isoformat()


def test_queue_exits_and_parallel_jobs_are_counted_once() -> None:
    def event(kind: str, at: int, **fields: Any) -> dict[str, Any]:
        return {"__typename": kind, "createdAt": iso(at), **fields}

    pr = {
        "createdAt": iso(0),
        "mergedAt": iso(70),
        "isDraft": False,
        "title": "Example",
        "headRefOid": "abc",
        "events": [
            event("ReadyForReviewEvent", 5),
            event("AddedToMergeQueueEvent", 40),
            event("RemovedFromMergeQueueEvent", 45, reason="failed_checks"),
            event("AddedToMergeQueueEvent", 50),
            event("RemovedFromMergeQueueEvent", 70, reason="merged"),
        ],
    }
    hosted = {
        "id": 1,
        "created_at": iso(10),
        "head_sha": "abc",
        "name": "ready checks",
        "html_url": LINK,
        "run_attempt": 2,
        "status": "completed",
        "conclusion": "success",
        "updated_at": iso(40),
    }
    checks = {
        **hosted,
        "id": 2,
        "created_at": iso(52),
        "run_started_at": iso(54),
        "updated_at": iso(68),
        "run_attempt": 1,
    }
    jobs = [
        {
            "id": 11,
            "name": "ubuntu / test",
            "created_at": iso(10),
            "started_at": iso(14),
            "completed_at": iso(39),
            "conclusion": "success",
            "html_url": LINK + "/job/11",
            "run_attempt": 2,
        },
        {
            "id": 12,
            "name": "macos / test",
            "created_at": iso(10),
            "started_at": iso(20),
            "completed_at": iso(40),
            "conclusion": "success",
            "html_url": LINK + "/job/12",
            "run_attempt": 2,
        },
    ]
    result = build(
        1,
        pr,
        [hosted],
        {1: jobs},
        {},
        {},
        [checks],
        {"log": [], "certs": []},
        None,
        None,
        EPOCH + 100,
    )
    stats = summarize([result])
    phases = {p["phase"]: p for p in stats["phases"]}
    assert phases["hosted"] == {
        "phase": "hosted",
        "prs": 1,
        "median": 30,
        "p90": 30,
        "waiting": 4,
        "running": 26,
        "unknown": 0,
    }
    assert phases["queue"]["waiting"] == 11
    assert phases["queue"]["running"] == 14
    assert (
        sum(p["waiting"] + p["running"] + p["unknown"] for p in phases.values()) == 70
    )
    assert stats["waste"] == {"queue_removals": 1, "reruns": 1}
    assert any(
        s["status"] == "failed" and "failed_checks" in s["label"]
        for s in result["segments"]
    )
    assert any(
        s["lane"] == "macOS" and s["url"].endswith("/job/12") for s in result["jobs"]
    )


def test_certification_without_start_is_not_elapsed_run_time() -> None:
    pr = {
        "createdAt": iso(0),
        "mergedAt": None,
        "isDraft": True,
        "title": "Example",
        "headRefOid": "new",
        "events": [
            {
                "__typename": "HeadRefForcePushedEvent",
                "createdAt": iso(15),
                "afterCommit": {"oid": "new"},
            }
        ],
    }
    statuses = {
        "old": [
            {
                "context": "local-ci/macos-arm64",
                "state": "failure",
                "created_at": iso(10),
                "target_url": None,
            }
        ]
    }
    result = build(
        1,
        pr,
        [],
        {},
        {},
        statuses,
        [],
        {"log": [], "certs": []},
        None,
        None,
        EPOCH + 20,
    )
    cert = next(s for s in result["segments"] if s["phase"] == "cert")
    assert cert["start"] == cert["end"] == EPOCH + 10
    assert cert["status"] == "failed"
    assert result["waste"][0]["kind"] == "superseded_certs"
