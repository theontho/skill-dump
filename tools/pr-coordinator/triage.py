"""Per-PR landing state for the open owner backlog (JSON lines or table)."""

import json
import subprocess
import sys

from runtime import P, gh_argv

Q = """
query($n:Int!){repository(owner:"__OWNER__",name:"__REPO__"){pullRequest(number:$n){
 number title isDraft author{login} mergeable mergeStateStatus headRefName headRefOid baseRefOid
 autoMergeRequest{enabledAt} mergeQueueEntry{state position}
 reviewThreads(first:100){nodes{isResolved}}
 reviews(last:20){nodes{author{login} state submittedAt commit{oid}}}
 labels(first:20){nodes{name}}
 closingIssuesReferences(first:3){nodes{number labels(first:20){nodes{name}}}}
 commits(last:1){nodes{commit{statusCheckRollup{state contexts(first:100){nodes{
   ... on StatusContext{context state}
   ... on CheckRun{name conclusion status}}}}}}}
}}}""".replace("__OWNER__", P.repo_owner).replace("__REPO__", P.repo_name)


def gh(*args):
    return subprocess.run(
        gh_argv(*args), capture_output=True, text=True, check=True
    ).stdout


def info(n):
    d = json.loads(gh("api", "graphql", "-f", f"query={Q}", "-F", f"n={n}"))["data"][
        "repository"
    ]["pullRequest"]
    head = d["headRefOid"]
    unresolved = sum(1 for t in d["reviewThreads"]["nodes"] if not t["isResolved"])
    cop = [
        r
        for r in d["reviews"]["nodes"]
        if r["author"] and "copilot" in r["author"]["login"].lower()
    ]
    cop_head = any(r["commit"] and r["commit"]["oid"] == head for r in cop)
    roll = d["commits"]["nodes"][0]["commit"]["statusCheckRollup"]
    ctx = roll["contexts"]["nodes"] if roll else []
    local = {
        c["context"]: c["state"]
        for c in ctx
        if c.get("context", "").startswith(P.cert_prefix)
    }
    failing = sorted(
        {
            c.get("name") or c.get("context")
            for c in ctx
            if c.get("conclusion") in ("FAILURE", "TIMED_OUT")
            or c.get("state") in ("FAILURE", "ERROR")
            and not (c.get("context") or "").startswith(P.cert_prefix)
        }
    )
    machines = sorted(
        {
            lab["name"]
            for i in d["closingIssuesReferences"]["nodes"]
            for lab in i["labels"]["nodes"]
            if lab["name"].startswith(P.get("labels", "machine_prefix", "machine-"))
        }
    )
    return {
        "n": n,
        "author": (d.get("author") or {}).get("login"),
        "title": d["title"][:60],
        "draft": d["isDraft"],
        "merge": d["mergeStateStatus"],
        "mergeable": d["mergeable"],
        "head": head[:9],
        "branch": d["headRefName"],
        "unresolved": unresolved,
        "copilot_on_head": cop_head,
        "copilot_reviews": len(cop),
        "local": local,
        "failing": failing,
        "rollup": roll["state"] if roll else None,
        "auto": bool(d["autoMergeRequest"]),
        "queue": d["mergeQueueEntry"]["state"] if d["mergeQueueEntry"] else None,
        "machine": machines,
        "labels": [lab["name"] for lab in d["labels"]["nodes"]],
        "issues": [i["number"] for i in d["closingIssuesReferences"]["nodes"]],
    }


def main():
    nums = [int(x) for x in sys.argv[1:]] or [
        p["number"]
        for p in json.loads(
            gh(
                "pr",
                "list",
                "--state",
                "open",
                "--limit",
                "100",
                "--json",
                "number,author,labels",
            )
        )
        if (p.get("author") or {}).get("login") in P.authors
        or any(lab["name"] in (P.priority, P.land_asap) for lab in p.get("labels", []))
    ]
    for n in sorted(nums):
        print(json.dumps(info(n)))


if __name__ == "__main__":
    main()
