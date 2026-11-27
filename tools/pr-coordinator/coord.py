"""Update the coordinator dashboard state.

coord.py pr NUM STAGE [--machine M] [--note TEXT] [--title T]
coord.py machine NAME STATUS [--os OS] [--task TEXT]
coord.py log TEXT
coord.py headline TEXT
coord.py ask "QUESTION" [--default TEXT]
coord.py answers            # print answered questions
coord.py sync               # refresh PR titles/list from gh
"""

import argparse
import fcntl
import json
import subprocess
import time

from host_activity import update_idle
from runtime import P, gh_argv, load, questions, save, uuid4

HERE = P.data
STATE, QUESTIONS = HERE / "state.json", HERE / "questions.json"


TRACKED_AUTHORS = P.authors


def now():
    return time.strftime("%Y-%m-%d %H:%M")


def derive_machines(s, hosts, busy):
    """Build machine cards from current dashboard work and host probes."""
    certs = s.get("certs", [])
    s["machines"] = []
    for host, spec in hosts.items():
        if not spec.get("dashboard", True):
            continue
        running = [
            c for c in certs if c.get("host") == host and c.get("status") == "running"
        ]
        queued = [
            c for c in certs if c.get("host") == host and c.get("status") == "queued"
        ]
        task = "; ".join(
            f"certifying #{c['pr']} {c['target']} ({c.get('sha')})" for c in running
        )
        if queued:
            task += (
                ("; " if task else "")
                + "queued: "
                + ", ".join(f"#{c['pr']} {c['target']}" for c in queued)
            )
        tests = [
            r
            for r in s.get("runs", [])
            if r.get("host") == host and r.get("status") == "running"
        ]
        if tests:
            task += ("; " if task else "") + "; ".join(
                f"tests for {r.get('label')}" for r in tests
            )
        if not spec.get("ssh"):
            task = "coordinator, dashboard, implementer agents" + (
                "; " + task if task else ""
            )
        reachable = s.get("hosts", {}).get(host, {}).get("reachable")
        if reachable is False:
            status = "offline"
        elif host in busy:
            status = "busy"
        else:
            status = "idle" if reachable else "unknown"
        since = s.get("host_idle", {}).get(host, {}).get("since")
        if status == "idle" and since is not None:
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(since))
            task += (" · " if task else "") + f"idle since {stamp}"
        s["machines"].append(
            {
                "name": spec.get("display", host),
                "os": spec.get("description", spec.get("platform", "")),
                "status": status,
                "task": task,
            }
        )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd")
    p.add_argument("args", nargs="*")
    p.add_argument("--machine")
    p.add_argument("--note")
    p.add_argument("--title", "--pr", dest="title")
    p.add_argument("--os")
    p.add_argument("--task")
    p.add_argument("--default")
    p.add_argument(
        "--owner",
        action="store_true",
        help="coordinator only: queue for the repository owner",
    )
    p.add_argument("--target")
    p.add_argument("--sha")
    p.add_argument(
        "--owner-resume",
        action="store_true",
        help="record a long gap ended by an owner nudge (agent supervisor heartbeat)",
    )
    a = p.parse_args()
    if a.cmd == "agent" and a.args == ["supervisor", "heartbeat"]:
        from events import heartbeat

        heartbeat(owner_resume=a.owner_resume)
        return
    if a.owner_resume:
        p.error("--owner-resume requires agent supervisor heartbeat")
    lock = open(HERE / "state.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    s = load(STATE, {"prs": [], "machines": [], "log": []})
    if a.cmd == "pr":
        num, stage = int(a.args[0]), a.args[1]
        pr = next((x for x in s["prs"] if x["number"] == num), None)
        if pr is None:
            pr = {"number": num}
            s["prs"].append(pr)
        pr["stage"] = stage
        for k in ("machine", "note", "title"):
            if getattr(a, k) is not None:
                pr[k] = getattr(a, k)
        s["prs"].sort(key=lambda x: x["number"])
    elif a.cmd == "machine":
        m = next((x for x in s["machines"] if x["name"] == a.args[0]), None)
        if m is None:
            m = {"name": a.args[0]}
            s["machines"].append(m)
        m["status"] = a.args[1]
        for k in ("os", "task"):
            if getattr(a, k) is not None:
                m[k] = getattr(a, k)
    elif a.cmd == "cert":
        # coord.py cert PR STATUS --target T --sha SHA [--machine HOST]
        certs = s.setdefault("certs", [])
        key = (int(a.args[0]), a.target)
        c = next((x for x in certs if (x["pr"], x["target"]) == key), None)
        if c is None:
            c = {"pr": key[0], "target": a.target}
            certs.append(c)
        c.update(status=a.args[1], sha=a.sha, host=a.machine, at=now())
        if a.task:
            c["pid"] = a.task
        s["certs"] = sorted(certs, key=lambda x: (x["pr"], x["target"]))[-80:]
    elif a.cmd == "agent":
        # coord.py agent NAME STATUS --pr N --task TEXT [--machine WHERE]
        agents = s.setdefault("agents", [])
        g = next((x for x in agents if x["name"] == a.args[0]), None)
        if g is None:
            g = {"name": a.args[0], "started": now()}
            agents.append(g)
        g["status"] = a.args[1]
        g["at"] = now()
        for k, v in (("pr", a.title), ("task", a.task), ("machine", a.machine)):
            if v is not None:
                g[k] = v
        s["agents"] = [
            x for x in agents if x["status"] != "done" or x["at"] >= now()[:10]
        ][-40:]
    elif a.cmd == "run":
        # coord.py run ID STATUS --machine HOST --task CMD --title LABEL
        runs = s.setdefault("runs", [])
        r = next((x for x in runs if x["id"] == a.args[0]), None)
        if r is None:
            r = {"id": a.args[0], "started": now()}
            runs.append(r)
        r.update(status=a.args[1], at=now())
        for k, v in (("host", a.machine), ("cmd", a.task), ("label", a.title)):
            if v is not None:
                r[k] = v
        s["runs"] = runs[-40:]
    elif a.cmd == "log":
        s["log"] = (s.get("log", []) + [[now(), " ".join(a.args)]])[-200:]
    elif a.cmd == "headline":
        s["headline"] = " ".join(a.args)
    elif a.cmd == "ask" and not a.owner:
        # Agents' questions go to the coordinator, who answers or rewrites them for the owner.
        questions(
            lambda rows: (
                rows
                + [
                    {
                        "id": uuid4().hex,
                        "agent": a.machine or "agent",
                        "pr": a.title or "",
                        "question": " ".join(a.args)
                        + (f"  [default: {a.default}]" if a.default else ""),
                    }
                ]
            ),
            agent=True,
        )
        print("sent to the coordinator")
        return
    elif a.cmd == "ask":

        def add(rows):
            qid = max((q["id"] for q in rows), default=0) + 1
            return rows + [
                {
                    "id": qid,
                    "question": " ".join(a.args),
                    "default": a.default,
                    "asked": now(),
                }
            ]

        print(questions(add)[-1]["id"])
        return
    elif a.cmd == "answers":
        for q in load(QUESTIONS, []):
            print(
                f"Q{q['id']} [{q.get('answered_at') or 'open'}] {q['question']}\n   -> {q.get('answer')}"
            )
        return
    elif a.cmd == "sync":
        out = subprocess.run(
            gh_argv(
                "pr",
                "list",
                "--state",
                "open",
                "--limit",
                "100",
                "--json",
                "number,title,author,labels",
            ),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        open_nums = {}
        for pr in json.loads(out):
            if (pr.get("author") or {}).get("login") in TRACKED_AUTHORS or any(
                lab["name"] in (P.priority, P.land_asap) for lab in pr["labels"]
            ):
                open_nums[pr["number"]] = pr["title"]
        for pr in s["prs"]:
            if pr["number"] not in open_nums and pr.get("stage") not in (
                "merged",
                "closed",
            ):
                state = (
                    subprocess.run(
                        gh_argv(
                            "pr",
                            "view",
                            str(pr["number"]),
                            "--json",
                            "state",
                            "--jq",
                            ".state",
                        ),
                        capture_output=True,
                        text=True,
                    )
                    .stdout.strip()
                    .lower()
                )
                pr["stage"] = state or pr.get("stage")
        known = {x["number"] for x in s["prs"]}
        for num, title in open_nums.items():
            if num not in known:
                s["prs"].append({"number": num, "title": title, "stage": "todo"})
            else:
                next(x for x in s["prs"] if x["number"] == num)["title"] = title
        s["prs"].sort(key=lambda x: x["number"])
    derive_machines(s, P.hosts, update_idle(s, P))
    s["updated"] = now()
    save(STATE, s)


if __name__ == "__main__":
    main()
