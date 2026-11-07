#!/usr/bin/env python3
"""Throwaway helper for .github/workflows/shots-32211.yml (KiroCrew PR #32211).

Runs ONLY on a GitHub-hosted ubuntu-latest runner. It never changes a kernel
setting itself: the workflow does that with `sudo sysctl`, and this helper only
reads the result, boots and stops isolated gateways, reads gateway APIs, and
writes evidence into measurements.json.

Subcommands:
  host-state  read the userns knobs and try unshare(CLONE_NEWUSER) in a child
  start       boot `python -m kiro_crew gateway` on a throwaway KIROCREW_HOME
  api         GET one dashboard API path with the gateway's session cookie
  wait-setup  poll the prerequisite API until initial_setup_complete is true
  stop        stop a gateway this helper started
  doctor      run `python -m kiro_crew doctor` against a KIROCREW_HOME
  log-lines   copy the gateway log lines that mention the cap into measurements
  scrub       replace the sign-in token and cookie in log files
  run-info    record the commit, ref, run URL and runner image
  note        store a JSON value (how a piece of state was seeded)
  verdict     fail when any recorded check failed

Every result lands in measurements.json under a dotted key, and every pass/fail
lands in its "checks" list, so the last step can fail the job on any of them
while each earlier step still uploads its evidence.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import platform
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

USERNS_KNOBS = {
    "user.max_user_namespaces": "/proc/sys/user/max_user_namespaces",
    "kernel.apparmor_restrict_unprivileged_userns": "/proc/sys/kernel/apparmor_restrict_unprivileged_userns",
    "kernel.apparmor_restrict_unprivileged_unconfined": "/proc/sys/kernel/apparmor_restrict_unprivileged_unconfined",
    "kernel.unprivileged_userns_clone": "/proc/sys/kernel/unprivileged_userns_clone",
    "apparmor.enabled": "/sys/module/apparmor/parameters/enabled",
}

# Run in a child interpreter so this helper itself never enters a namespace.
UNSHARE_CHILD = r"""
import errno, json, os
try:
    os.unshare(os.CLONE_NEWUSER)
    print(json.dumps({"ok": True, "errno": 0, "errno_name": ""}))
except OSError as exc:
    print(json.dumps({"ok": False, "errno": exc.errno,
                      "errno_name": errno.errorcode.get(exc.errno, "?"),
                      "strerror": exc.strerror}))
"""

READY_PREFIX = "KIROCREW_READY:"


# ── measurements.json ────────────────────────────────────────────────────────


def load(path: Path) -> dict:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"schema": 1, "pr": 32211, "checks": []}


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def store(path: Path, key: str, value: object) -> None:
    data = load(path)
    node = data
    parts = key.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    save(path, data)


def check(path: Path, name: str, ok: bool, detail: object) -> bool:
    data = load(path)
    data.setdefault("checks", []).append({"name": name, "ok": bool(ok), "detail": detail})
    save(path, data)
    print(f"{'PASS' if ok else 'FAIL'} {name}: {json.dumps(detail)[:400]}")
    return bool(ok)


# ── host state ───────────────────────────────────────────────────────────────


def read_knob(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="ascii").strip()
    except OSError as exc:
        return f"<unreadable: {exc.strerror}>"


def cmd_host_state(args: argparse.Namespace) -> int:
    state = {
        "kernel": platform.release(),
        "knobs": {name: read_knob(path) for name, path in USERNS_KNOBS.items()},
    }
    child = subprocess.run(
        [sys.executable, "-I", "-c", UNSHARE_CHILD],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    try:
        state["unshare_newuser"] = json.loads(child.stdout.strip() or "{}")
    except json.JSONDecodeError:
        state["unshare_newuser"] = {"raw": child.stdout, "stderr": child.stderr}
    print(json.dumps(state, indent=2))
    store(args.measurements, f"host.{args.label}", state)
    if not args.expect_cap_zero:
        return 0
    cap = state["knobs"]["user.max_user_namespaces"]
    probe = state["unshare_newuser"]
    ok = cap == "0" and probe.get("errno") == errno.ENOSPC
    check(
        args.measurements,
        "host: user.max_user_namespaces is 0 and unshare(CLONE_NEWUSER) fails ENOSPC",
        ok,
        {"max_user_namespaces": cap, "unshare_newuser": probe},
    )
    return 0 if ok else 1


# ── gateway lifecycle ────────────────────────────────────────────────────────


def gateway_env(home: Path, kiro_bin: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "KIROCREW_HOME": str(home),
            # kiro-cli's own home, so nothing reads or writes the runner's ~/.kiro.
            "KIRO_HOME": str(home / "kiro"),
            # The packaged fake ACP backend stands in for kiro-cli, as the repo's
            # own E2E and GUI-test boots do. It answers --version and whoami.
            "KIROCREW_KIRO_BIN": kiro_bin,
            "KIROCREW_SKIP_MODEL_DOWNLOAD": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


def fake_backend_path() -> str:
    from kiro_crew.testing import fake_acp_backend

    return str(fake_acp_backend.__file__)


def mask(value: str) -> None:
    if value and os.environ.get("GITHUB_ACTIONS"):
        print(f"::add-mask::{value}")


def exchange_cookie(base_url: str, token: str) -> str:
    """Trade the one-time link token for the session cookie, as boot.sh does."""
    opener = urllib.request.OpenerDirector()
    opener.add_handler(urllib.request.HTTPHandler())

    def cookie_from(headers) -> str:
        for key, value in headers:
            if key.lower() == "set-cookie" and value.startswith("mc_token_"):
                return value.split(";", 1)[0].strip()
        return ""

    try:
        with opener.open(f"{base_url}/?token={token}", timeout=20) as resp:
            return cookie_from(resp.getheaders())
    except urllib.error.HTTPError as exc:
        return cookie_from(exc.headers.items())


def cmd_start(args: argparse.Namespace) -> int:
    home = args.home
    home.mkdir(parents=True, exist_ok=True)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.log_dir / f"gateway-{args.name}.out.log"
    err_path = args.log_dir / f"gateway-{args.name}.err.log"
    argv = [
        sys.executable,
        "-m",
        "kiro_crew",
        "gateway",
        "--port",
        "auto",
        "--no-open",
        "--json-ready",
        "--no-crons",
    ]
    # No --test-mode (it injects a ready prerequisite state) and no --approval
    # yolo: the real probe and the default approval policy run.
    with open(out_path, "ab") as out, open(err_path, "ab") as err:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            env=gateway_env(home, fake_backend_path()),
            start_new_session=True,
        )
    ready = None
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        for log in (out_path, err_path):
            for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith(READY_PREFIX):
                    ready = json.loads(line[len(READY_PREFIX):])
                    break
            if ready:
                break
        if ready:
            break
        time.sleep(1)
    if not ready:
        print(f"::error::gateway {args.name} did not print {READY_PREFIX} (exit {proc.poll()})")
        tail = err_path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        print("\n".join(tail))
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        return 1
    token = str(ready["token"])
    mask(token)
    base_url = f"http://127.0.0.1:{int(ready['port'])}"
    cookie = exchange_cookie(base_url, token)
    mask(cookie.split("=", 1)[-1] if cookie else "")
    state = {
        "name": args.name,
        "pid": proc.pid,
        "base_url": base_url,
        "token": token,
        "cookie": cookie,
        "home": str(home),
        "logs": [str(out_path), str(err_path)],
    }
    old = os.umask(0o077)
    try:
        args.state.write_text(json.dumps(state), encoding="utf-8")
    finally:
        os.umask(old)
    print(f"gateway {args.name} ready at {base_url} (pid {proc.pid}, cookie {'yes' if cookie else 'NO'})")
    return 0 if cookie else 1


def read_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def api_get(state: dict, path: str, timeout: float = 120) -> tuple[int, object]:
    # Only the HTTP handler: a 4xx/5xx comes back as a response with its body
    # (the 503 sandbox refusal is evidence, not an error), and nothing redirects.
    opener = urllib.request.OpenerDirector()
    opener.add_handler(urllib.request.HTTPHandler())
    req = urllib.request.Request(f"{state['base_url']}{path}", headers={"Cookie": state["cookie"]})
    try:
        with opener.open(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    text = raw.decode("utf-8", errors="replace")
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, text[:2000]


def cmd_api(args: argparse.Namespace) -> int:
    state = read_state(args.state)
    status, body = api_get(state, args.path)
    record = {"request": f"GET {args.path}", "status": status, "body": body}
    print(json.dumps(record, indent=2)[:4000])
    store(args.measurements, args.key, record)
    ok = True
    for expectation in args.expect or []:
        field, _, wanted = expectation.partition("=")
        if field == "status":
            actual: object = status
            wanted_value: object = int(wanted)
        else:
            actual = body.get(field) if isinstance(body, dict) else None
            wanted_value = json.loads(wanted)
        ok &= check(
            args.measurements,
            f"{args.key}: {field} == {wanted}",
            actual == wanted_value,
            {"actual": actual},
        )
    for field, needle in (pair.partition("~")[::2] for pair in args.contains or []):
        actual = body.get(field) if isinstance(body, dict) else None
        ok &= check(
            args.measurements,
            f"{args.key}: {field} contains {needle!r}",
            isinstance(actual, str) and needle in actual,
            {"actual": actual},
        )
    return 0 if ok else 1


def cmd_wait_setup(args: argparse.Namespace) -> int:
    state = read_state(args.state)
    deadline = time.monotonic() + args.timeout
    attempts = []
    while True:
        # explicit = the Check again probe: it always runs --version and whoami.
        status, body = api_get(state, "/api/kiro-prerequisite?refresh=explicit")
        summary = {"status": status}
        if isinstance(body, dict):
            for field in (
                "installed",
                "authenticated",
                "ready",
                "initial_setup_complete",
                "sandbox_unavailable",
                "sandbox_failure_kind",
                "sandbox_remedy",
                "sandbox_detail",
                "probe_error",
            ):
                summary[field] = body.get(field)
        attempts.append(summary)
        done = isinstance(body, dict) and body.get("initial_setup_complete") is True
        if done or time.monotonic() > deadline:
            break
        time.sleep(5)
    marker = Path(state["home"]) / ".kiro_cli_setup_complete"
    store(
        args.measurements,
        args.key,
        {
            "how": "gateway's own first-run probe (GET /api/kiro-prerequisite?refresh=explicit) "
            "on a host whose sandbox works; whoami succeeding writes the setup marker",
            "attempts": attempts,
            "marker_written_by_gateway": marker.is_file(),
        },
    )
    ok = check(
        args.measurements,
        f"{args.key}: the gateway reports first-run setup complete",
        done,
        {**attempts[-1], "marker_written_by_gateway": marker.is_file()},
    )
    return 0 if ok else 1


def cmd_note(args: argparse.Namespace) -> int:
    store(args.measurements, args.key, json.loads(args.json))
    return 0


def cmd_run_info(args: argparse.Namespace) -> int:
    env = os.environ
    store(
        args.measurements,
        "run",
        {
            "head_sha": env.get("GITHUB_SHA", ""),
            "ref": env.get("GITHUB_REF", ""),
            "run_url": "{}/{}/actions/runs/{}".format(
                env.get("GITHUB_SERVER_URL", ""), env.get("GITHUB_REPOSITORY", ""), env.get("GITHUB_RUN_ID", "")
            ),
            "runner_image": f"{env.get('ImageOS', '')} {env.get('ImageVersion', '')}".strip(),
            "python": sys.version.split()[0],
        },
    )
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    if not args.state.is_file():
        print(f"no state file {args.state}; nothing to stop")
        return 0
    pid = int(read_state(args.state)["pid"])
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return 0
    for _ in range(40):
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            print(f"gateway pid {pid} stopped")
            return 0
        time.sleep(0.5)
    os.killpg(pid, signal.SIGKILL)
    print(f"gateway pid {pid} killed after SIGTERM timeout")
    return 0


# ── doctor and logs ──────────────────────────────────────────────────────────


def cmd_doctor(args: argparse.Namespace) -> int:
    env = gateway_env(args.home, fake_backend_path())
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "kiro_crew", "doctor"],
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
            check=False,
        )
        output, code = proc.stdout + proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        output, code = f"{exc.stdout or ''}{exc.stderr or ''}\n<timed out after 300s>", None
    args.out.write_text(output, encoding="utf-8")
    backend_lines = [
        line.rstrip()
        for line in output.splitlines()
        if "backend:" in line or "max_user_namespaces" in line or "ENOSPC" in line
    ]
    store(
        args.measurements,
        "doctor",
        {"command": "python -m kiro_crew doctor", "exit_code": code, "backend_lines": backend_lines},
    )
    print("\n".join(backend_lines))
    check(
        args.measurements,
        "doctor: names user namespaces disabled (user.max_user_namespaces=0), never 'transiently'",
        "user.max_user_namespaces=0" in output and "transiently" not in output,
        backend_lines[:6],
    )
    return 0


def cmd_log_lines(args: argparse.Namespace) -> int:
    state = read_state(args.state)
    lines = []
    # The captured stdout/stderr, plus any log file the gateway wrote in its own
    # home: only matching lines are copied, and those files are never uploaded.
    home_logs = [p for p in Path(state["home"]).rglob("*.log") if p.is_file() and p.stat().st_size < 50_000_000]
    for log in [*state["logs"], *home_logs]:
        for line in Path(log).read_text(encoding="utf-8", errors="replace").splitlines():
            if "max_user_namespaces" in line or "ENOSPC" in line or "sandbox refused" in line:
                line = line.replace(state["token"], "[REDACTED-TOKEN]")
                if state.get("cookie"):
                    line = line.replace(state["cookie"].split("=", 1)[-1], "[REDACTED-COOKIE]")
                lines.append(line[:600])
    store(args.measurements, args.key, lines[:40])
    print("\n".join(lines[:40]))
    check(
        args.measurements,
        f"{args.key}: gateway log names user.max_user_namespaces=0",
        any("user.max_user_namespaces=0" in line for line in lines),
        lines[:3],
    )
    return 0


def cmd_scrub(args: argparse.Namespace) -> int:
    secrets: list[tuple[str, str]] = []
    for state_path in args.state:
        if not state_path.is_file():
            continue
        state = read_state(state_path)
        secrets.append((state["token"], "[REDACTED-TOKEN]"))
        if state.get("cookie"):
            secrets.append((state["cookie"].split("=", 1)[-1], "[REDACTED-COOKIE]"))
    for path in args.files:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for secret, label in secrets:
            if secret:
                text = text.replace(secret, label)
        path.write_text(text, encoding="utf-8")
        print(f"scrubbed {path}")
    return 0


def cmd_verdict(args: argparse.Namespace) -> int:
    data = load(args.measurements)
    checks = data.get("checks", [])
    failed = [c["name"] for c in checks if not c["ok"]]
    missing = [name for name in args.require or [] if not any(c["name"].startswith(name) for c in checks)]
    for name in failed:
        print(f"::error::check failed: {name}")
    for name in missing:
        print(f"::error::check never ran: {name}")
    print(f"{len(checks)} checks, {len(failed)} failed, {len(missing)} missing")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        rows = ["## shots-32211 checks", "", "| | check |", "|---|---|"]
        rows += [f"| {'✅' if c['ok'] else '❌'} | {c['name']} |" for c in checks]
        rows += [f"| ❌ | never ran: {name} |" for name in missing]
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("\n".join(rows) + "\n")
    return 1 if failed or missing or not checks else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--measurements", type=Path, required=True)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("host-state")
    p.add_argument("--label", required=True)
    p.add_argument("--expect-cap-zero", action="store_true")
    p.set_defaults(func=cmd_host_state)

    p = sub.add_parser("start")
    p.add_argument("--name", required=True)
    p.add_argument("--home", type=Path, required=True)
    p.add_argument("--log-dir", type=Path, required=True)
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--timeout", type=float, default=300)
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("api")
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--expect", action="append", help="field=<json> or status=<int>")
    p.add_argument("--contains", action="append", help="field~substring")
    p.set_defaults(func=cmd_api)

    p = sub.add_parser("wait-setup")
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--timeout", type=float, default=120)
    p.set_defaults(func=cmd_wait_setup)

    p = sub.add_parser("stop")
    p.add_argument("--state", type=Path, required=True)
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("doctor")
    p.add_argument("--home", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("log-lines")
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--key", required=True)
    p.set_defaults(func=cmd_log_lines)

    p = sub.add_parser("scrub")
    p.add_argument("--state", type=Path, action="append", required=True)
    p.add_argument("files", type=Path, nargs="*")
    p.set_defaults(func=cmd_scrub)

    p = sub.add_parser("run-info")
    p.set_defaults(func=cmd_run_info)

    p = sub.add_parser("note")
    p.add_argument("--key", required=True)
    p.add_argument("--json", required=True)
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("verdict")
    p.add_argument("--require", action="append", help="a check-name prefix that must exist")
    p.set_defaults(func=cmd_verdict)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
