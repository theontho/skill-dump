"""Optional Chromium/CDP smoke test against an existing populated dashboard."""

import argparse
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path


def poll(read):
    for _ in range(100):
        try:
            if value := read():
                return value
        except OSError:
            pass
        time.sleep(0.1)
    raise AssertionError("timed out waiting for browser or page")


def check(browser, base):
    import websocket  # Optional smoke-test client, not required by pr-coord.

    def tabs():
        with urllib.request.urlopen(
            f"http://127.0.0.1:{browser}/json", timeout=1
        ) as response:
            return json.load(response)

    socket = websocket.create_connection(
        next(
            tab["webSocketDebuggerUrl"] for tab in poll(tabs) if tab["type"] == "page"
        ),
        timeout=30,
        suppress_origin=True,
    )
    index = 0
    errors = []

    def call(method, **params):
        nonlocal index
        index += 1
        socket.send(json.dumps(dict(id=index, method=method, params=params)))
        while True:
            result = json.loads(socket.recv())
            if result.get("method") == "Runtime.exceptionThrown":
                errors.append(result["params"])
            if result.get("id") == index:
                assert "error" not in result, result
                return result["result"]

    def js(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        assert "exceptionDetails" not in result, result
        return result["result"].get("value")

    def navigate(path, selector):
        call("Page.navigate", url=base + path)
        poll(
            lambda: js(
                f"location.pathname === {json.dumps(path)} && document.querySelector({json.dumps(selector)}) !== null"
            )
        )
        assert js(
            "performance.getEntriesByType('resource').every(r => r.name.startsWith(location.origin))"
        )

    def act(selector, kind):
        point = js(f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            el.scrollIntoView({{block:"center",inline:"center"}});
            const r = el.getBoundingClientRect();
            return [r.left + r.width / 2, r.top + r.height / 2];
        }})()""")
        for event in (
            ("mouseMoved",) if kind == "hover" else ("mousePressed", "mouseReleased")
        ):
            call(
                "Input.dispatchMouseEvent",
                type=event,
                x=point[0],
                y=point[1],
                **({} if kind == "hover" else {"button": "left", "clickCount": 1}),
            )

    try:
        call("Page.enable")
        call("Runtime.enable")
        navigate("/", "#pg .node")
        act("#pg .node", "hover")
        assert js("document.querySelector('#pgd').innerText.includes('GitHub')")
        graph = js("document.querySelectorAll('#pg .node').length")
        headings = js(
            "Array.from(document.querySelectorAll('h2'), x => x.textContent.split(' (')[0])"
        )

        navigate("/lifecycle", ".pr-strip .strip-piece")
        assert js("!document.querySelector('#detailedTraces').open")
        assert js("""(() => {
            const piece = [...document.querySelectorAll('.pr-strip .strip-piece:not(.merged)')]
                .find(el => el.getBoundingClientRect().width > 12);
            piece.dataset.smokeTarget = 'yes'; return true;
        })()""")
        detail = js("""(() => {
            document.querySelector('[data-smoke-target]').dispatchEvent(new PointerEvent('pointerenter'));
            return document.querySelector('#tip:not([hidden])')?.innerText;
        })()""")
        assert detail and "Elapsed time" in detail, (detail, errors)
        act("#detailedTraces > summary", "click")
        poll(lambda: js("document.querySelectorAll('.prrow').length"))
        flow = js("document.querySelectorAll('.flow-card').length")

        navigate("/tests", "#results .test-id")
        stages = js(
            "Array.from(document.querySelectorAll('.stage'), x => x.querySelector('summary').innerText)"
        )
        count = js("document.querySelector('#count').textContent")
        first = js("document.querySelector('#results .test-id').textContent")
        js("document.querySelector('.stage summary').click()")
        assert js("document.querySelector('.stage').open")
        js("document.querySelector('#next').click()")
        assert js("document.querySelector('#count').textContent") != count
        js("""(() => {
            const input = document.querySelector('#search'); input.value = 'unmatchable-8fa9ce2fa34';
            input.dispatchEvent(new Event('input'));
        })()""")
        assert js("document.querySelector('#count').textContent") == "0 results"
        call(
            "Emulation.setDeviceMetricsOverride",
            width=395,
            height=850,
            deviceScaleFactor=1,
            mobile=True,
        )
        assert js("document.documentElement.scrollWidth <= innerWidth")
        assert not errors, errors
        return {
            "graph_nodes": graph,
            "dashboard_headings": headings,
            "lifecycle_cards": flow,
            "test_stages": len(stages),
            "test_count": count,
            "first_test": first,
        }
    finally:
        socket.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    parser.add_argument(
        "--no-sandbox",
        action="store_true",
        help="use only inside an isolated test namespace",
    )
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    profile = args.out.parent / f"chromium-{os.getpid()}"
    argv = [
        "chromium",
        "--headless=new",
        "--remote-debugging-port=0",
        "--window-size=1600,1100",
        "--disable-background-networking",
        f"--user-data-dir={profile}",
        "about:blank",
    ]
    if args.no_sandbox:
        argv.insert(1, "--no-sandbox")
    with (args.out.parent / "chromium.log").open("ab") as log:
        proc = subprocess.Popen(
            argv,
            stdout=log,
            stderr=log,
            env={**os.environ, "TMPDIR": str(args.out.parent.resolve())},
        )
        try:
            port = int(
                poll(lambda: (profile / "DevToolsActivePort").read_text().splitlines())[
                    0
                ]
            )
            result = check(port, args.base.rstrip("/"))
            args.out.write_text(json.dumps(result, indent=2) + "\n")
            if args.compare:
                live = json.loads(args.compare.read_text())
                # The live graph is continuously rebuilt. Compare layout and inventory, not the live PR count.
                for key in set(live) - {"graph_nodes"}:
                    assert live[key] == result[key], (key, live[key], result[key])
            print(
                f"CDP smoke passed for {args.base}: {result['graph_nodes']} graph nodes, {result['test_count']}."
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    main()
