from typing import Any

from lifecycle_flow import overview, ready_signals, strip


def test_stage_strip_revisits_queue_without_double_counting() -> None:
    pr = {
        "first": 0,
        "segments": [
            {"phase": "hosted", "start": 12, "end": 23},
            {"phase": "checks", "start": 15, "end": 25},
            {"phase": "queue", "start": 26, "end": 28},
            {"phase": "refresh", "start": 28, "end": 31},
            {"phase": "queue", "start": 31, "end": 40},
        ],
    }
    stages = strip(pr, 40, [10])
    assert [(s["stage"], s["start"], s["end"]) for s in stages] == [
        ("dev", 0, 10),
        ("unrecorded", 10, 12),
        ("hosted", 12, 23),
        ("checks", 23, 25),
        ("unrecorded", 25, 26),
        ("queue", 26, 28),
        ("refresh", 28, 31),
        ("queue", 31, 40),
    ]


def test_ready_total_uses_passing_review_if_it_preceded_ready() -> None:
    raw = {
        "createdAt": "2026-11-24T00:00:00Z",
        "isDraft": False,
        "events": [
            {"__typename": "ReadyForReviewEvent", "createdAt": "2026-11-24T03:00:00Z"},
            {
                "__typename": "PullRequestReview",
                "createdAt": "2026-11-24T01:00:00Z",
                "submittedAt": "2026-11-24T02:00:00Z",
                "state": "COMMENTED",
            },
            {
                "__typename": "PullRequestReview",
                "createdAt": "2026-11-24T02:00:00Z",
                "submittedAt": "2026-11-24T02:30:00Z",
                "state": "APPROVED",
            },
        ],
    }
    ready, qualifying = ready_signals({"first": 1795478400}, raw, 1795492800)
    assert len(ready) == 1
    assert qualifying == ready[0] - 30 * 60


def test_overview_orders_open_prs_by_stage_and_merged_prs_by_finish() -> None:
    def pr(number: int, phase: str, finished: int | None) -> dict[str, Any]:
        return {
            "number": number,
            "title": "Test",
            "url": "",
            "first": 1,
            "merged": finished,
            "first_source": "PR opened",
            "segments": [
                {"phase": phase, "start": 2, "end": finished, "activity": "waiting"}
            ],
            "waste": [],
        }

    result = overview(
        {
            "generated": 30,
            "prs": [
                pr(1, "queue", 15),
                pr(2, "queue", None),
                pr(3, "cert", None),
                pr(4, "queue", 20),
            ],
        },
        {},
        {},
    )
    assert [row["number"] for row in result["rows"]] == [3, 2, 4, 1]
    assert result["full"] == {"n": 2, "median": 16, "p90": 19}
    assert result["stages"]["merged"] == {"n": 2, "median": 0, "p90": 0}


def test_flow_stage_uses_observed_duration_even_if_strip_attributes_overlap() -> None:
    pr = {
        "number": 1,
        "title": "Test",
        "url": "",
        "first": 1,
        "merged": 30,
        "first_source": "PR opened",
        "segments": [
            {"phase": "hosted", "start": 5, "end": 21, "activity": "running"},
            {"phase": "hosted", "start": 6, "end": 28, "activity": "running"},
            {"phase": "lane", "start": 10, "end": 20, "activity": "waiting"},
        ],
        "waste": [],
    }
    result = overview({"generated": 30, "prs": [pr]}, {}, {})
    assert result["stages"]["hosted"]["median"] == 23
    assert result["stages"]["lane"]["median"] == 10
    assert {s["stage"] for s in result["rows"][0]["strip"]} == {"hosted", "dev"}
