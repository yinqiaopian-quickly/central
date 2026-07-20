import argparse
import json
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def load_hosts(path):
    hosts = []
    with path.open("r", encoding="utf-8-sig") as file:
        for line in file:
            value = line.strip()
            if value and not value.startswith("#"):
                hosts.append(value)
    return hosts


def run_on_host(host, token, script, script_args, timeout):
    url = f"http://{host}/run"
    body = json.dumps({"script": script, "args": script_args}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Agent-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return host, response.status, payload
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except json.JSONDecodeError:
            payload = {"ok": False, "error": exc.reason}
        return host, exc.code, payload
    except Exception as exc:
        return host, 0, {"ok": False, "error": str(exc)}


def main():
    parser = argparse.ArgumentParser(description="Run an allowed script on many LAN agents")
    parser.add_argument("--hosts", default=str(BASE_DIR / "hosts.txt"))
    parser.add_argument("--script", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--arg", action="append", default=[], help="Pass one argument; repeat for multiple args")
    parser.add_argument("--parallel", type=int, default=16)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    hosts = load_hosts(Path(args.hosts))
    if not hosts:
        raise SystemExit("No hosts found.")

    with ThreadPoolExecutor(max_workers=args.parallel) as executor:
        futures = [
            executor.submit(run_on_host, host, args.token, args.script, args.arg, args.timeout)
            for host in hosts
        ]
        for future in as_completed(futures):
            host, status, payload = future.result()
            ok = "OK" if payload.get("ok") else "FAIL"
            print(f"[{ok}] {host} HTTP={status}")
            if payload.get("stdout"):
                print("STDOUT:")
                print(payload["stdout"].rstrip())
            if payload.get("stderr"):
                print("STDERR:")
                print(payload["stderr"].rstrip())
            if payload.get("error"):
                print("ERROR:", payload["error"])
            print()


if __name__ == "__main__":
    main()
