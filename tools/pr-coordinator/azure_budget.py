"""Advisory estimate for subscriptions with a fixed monthly Azure credit."""

import argparse
import json
import math
import subprocess
import sys
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from runtime import P, Project, load, save

Settings = tuple[str, float, float]


def settings(project: Project) -> Settings | None:
    try:
        subscription = project.get("azure_budget", "subscription")
    except AttributeError:
        raise ValueError("azure_budget must be a TOML table") from None
    if subscription is None:
        return None
    try:
        subscription = str(UUID(subscription))
    except (AttributeError, TypeError, ValueError):
        raise ValueError(
            "azure_budget.subscription must be an Azure subscription UUID"
        ) from None
    amounts = []
    for key in ("monthly_credit_usd", "expected_burn_usd_per_day"):
        value = project.get("azure_budget", key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0.01:
            raise ValueError(f"azure_budget.{key} must be at least $0.01")
        amounts.append(value)
    return subscription, amounts[0], amounts[1]


def azure_request(
    subscription: str, endpoint: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    args = [
        "az",
        "rest",
        "--url",
        f"https://management.azure.com/subscriptions/{subscription}/providers/{endpoint}",
        "--subscription",
        subscription,
        "--output",
        "json",
        "--only-show-errors",
    ]
    if body is not None:
        args += ["--method", "post", "--body", json.dumps(body)]
    response = subprocess.run(
        args,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    result = json.loads(response.stdout)
    if not isinstance(result, dict):
        raise ValueError("Azure did not return a JSON object")
    return result


def refresh(project: Project, config: Settings, now: datetime) -> None:
    subscription, grant, _ = config
    periods = azure_request(
        subscription,
        "Microsoft.Billing/billingPeriods?api-version=2018-03-01-preview&$top=2",
    )
    for item in periods["value"]:
        props = item["properties"]
        start = date.fromisoformat(props["billingPeriodStartDate"])
        end = date.fromisoformat(props["billingPeriodEndDate"])
        if start <= now.date() <= end:
            break
    else:
        raise ValueError("Azure did not report a billing period covering today")
    result = azure_request(
        subscription,
        "Microsoft.CostManagement/query?api-version=2023-03-01",
        {
            "type": "ActualCost",
            "timeframe": "Custom",
            "timePeriod": {
                "from": datetime.combine(start, time.min, UTC).isoformat(),
                "to": now.isoformat(),
            },
            "dataset": {
                "granularity": "None",
                "aggregation": {"totalCost": {"name": "PreTaxCost", "function": "Sum"}},
            },
        },
    )["properties"]
    columns = [column["name"] for column in result["columns"]]
    rows = result["rows"]
    if len(rows) != 1 or rows[0][columns.index("Currency")] != "USD":
        raise ValueError("Azure did not report one current-period USD cost total")
    cost = rows[0][columns.index("PreTaxCost")]
    if type(cost) not in (int, float) or not math.isfinite(cost):
        raise ValueError("Azure returned an invalid period cost")
    # The billing period end is inclusive.
    snapshot = {
        "subscription": subscription,
        "monthly_credit_usd": grant,
        "remaining_usd": round(min(grant, max(0, grant - cost)), 2),
        "reset_date": (end + timedelta(days=1)).isoformat(),
        "fetched_at": now.isoformat(),
    }
    save(project.data / "azure-budget.json", snapshot)


def status(project: Project, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    try:
        config = settings(project)
        if config is None:
            return {"status": "disabled"}
        subscription, grant, burn = config
        snapshot = load(project.data / "azure-budget.json", {})
        if (
            snapshot.get("subscription") != subscription
            or snapshot.get("monthly_credit_usd") != grant
        ):
            return {"status": "missing"}
        age = now - datetime.fromisoformat(snapshot["fetched_at"])
        days = (date.fromisoformat(snapshot["reset_date"]) - now.date()).days
        remaining = snapshot["remaining_usd"]
        if type(remaining) not in (int, float) or not 0 <= remaining <= grant:
            return {"status": "missing"}
        if not timedelta(0) <= age <= timedelta(days=1) or days <= 0:
            return {"status": "stale"}
    except (AttributeError, KeyError, TypeError, ValueError):
        return {"status": "unavailable"}
    return {
        "status": "estimated",
        "remaining_usd": remaining,
        "reset_date": snapshot["reset_date"],
        "days_to_reset": days,
        "fetched_at": snapshot["fetched_at"],
        "use_it_score": remaining / days / burn,
    }


def summary(value: dict[str, Any]) -> str:
    if value["status"] != "estimated":
        return f"Azure credit estimate: {value['status']}"
    days = value["days_to_reset"]
    return (
        f"Azure: estimated ${value['remaining_usd']:,.2f} credit left, "
        f"resets in {days} {'day' if days == 1 else 'days'} "
        f"(use-it score {value['use_it_score']:.2f}×)"
    )


def suggestion(project: Project, *, now: datetime | None = None) -> str:
    value = status(project, now=now)
    if value["status"] == "estimated" and value["use_it_score"] >= 1:
        return (
            f"{summary(value)} — consider approved short cloud work "
            "(prefer Spot; verify cleanup)."
        )
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="query Azure Billing and Cost Management for the configured subscription",
    )
    parser.add_argument(
        "--json", action="store_true", help="output the cached advisory as JSON"
    )
    args = parser.parse_args()
    now = datetime.now(UTC)
    if args.refresh:
        try:
            config = settings(P)
            if config is None:
                raise ValueError("configure [azure_budget] before refreshing")
            refresh(P, config, now)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            print(
                "azure-budget: Azure request failed; check az login, billing access and offer support; cache unchanged",
                file=sys.stderr,
            )
            return 1
        except (OSError, KeyError, IndexError, TypeError, ValueError) as exc:
            print(
                f"azure-budget: refresh failed; cache unchanged: {exc}", file=sys.stderr
            )
            return 1
    value = status(P, now=now)
    print(json.dumps(value) if args.json else summary(value))
    return 0
