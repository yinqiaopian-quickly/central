import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "agent_config.json"
OPEN_TARGETS_PATH = BASE_DIR / "scripts" / "open_targets.json"


def load_config():
    with CONFIG_PATH.open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def json_response(handler, status, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def resolve_command(command):
    resolved = []
    for index, item in enumerate(command):
        if index > 0 and isinstance(item, str) and item.startswith("scripts/"):
            resolved.append(str((BASE_DIR / item).resolve()))
        else:
            resolved.append(item)
    return resolved


def expand_config_path(value):
    expanded = os.path.expandvars(str(value))
    path = Path(expanded)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def candidate_search_roots(config):
    roots = config.get("file_search_roots") or [
        ".",
        "%USERPROFILE%\\Desktop",
        "%USERPROFILE%\\Documents",
        "%USERPROFILE%\\Downloads",
        "%PUBLIC%\\Desktop",
        "%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs",
        "%PROGRAMDATA%\\Microsoft\\Windows\\Start Menu\\Programs",
        "%PROGRAMFILES%",
        "%PROGRAMFILES(X86)%",
    ]
    seen = set()
    for root in roots:
        path = expand_config_path(root)
        key = str(path).lower()
        if key not in seen and path.exists():
            seen.add(key)
            yield path


def normalize_match_text(value):
    return (
        value.lower()
        .replace("：", ":")
        .replace(" ", "")
        .replace("_", "")
        .replace("-", "")
        .replace("·", "")
    )


def path_matches_query(path, query):
    query_norm = normalize_match_text(query)
    name_norm = normalize_match_text(path.name)
    stem_norm = normalize_match_text(path.stem)
    if name_norm == query_norm or stem_norm == query_norm:
        return True
    return query_norm in name_norm or query_norm in stem_norm


def find_target_by_name(filename, config):
    deadline = time.monotonic() + int(config.get("file_search_max_seconds", 45))
    max_scanned_dirs = int(config.get("file_search_max_dirs", 5000))
    scanned_dirs = 0

    for root in candidate_search_roots(config):
        stack = [root]
        while stack:
            if time.monotonic() > deadline:
                return None
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        try:
                            entry_path = Path(entry.path)
                            if entry.is_file() and path_matches_query(entry_path, filename):
                                return entry_path
                            if entry.is_dir(follow_symlinks=False) and path_matches_query(entry_path, filename):
                                return entry_path
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry_path)
                        except OSError:
                            continue
            except OSError:
                continue
            scanned_dirs += 1
            if scanned_dirs > max_scanned_dirs:
                return None
    return None


def find_all_targets_by_name(filename, config):
    deadline = time.monotonic() + int(config.get("file_search_max_seconds", 45))
    max_scanned_dirs = int(config.get("file_search_max_dirs", 5000))
    max_results = int(config.get("file_search_max_results", 20))
    scanned_dirs = 0
    results = []

    for root in candidate_search_roots(config):
        stack = [root]
        while stack:
            if time.monotonic() > deadline or len(results) >= max_results:
                return results
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        try:
                            entry_path = Path(entry.path)
                            if entry.is_file() and path_matches_query(entry_path, filename):
                                results.append(entry_path)
                                if len(results) >= max_results:
                                    return results
                            elif entry.is_dir(follow_symlinks=False):
                                if path_matches_query(entry_path, filename):
                                    results.append(entry_path)
                                    if len(results) >= max_results:
                                        return results
                                stack.append(entry_path)
                        except OSError:
                            continue
            except OSError:
                continue
            scanned_dirs += 1
            if scanned_dirs > max_scanned_dirs:
                return results
    return results


def run_builtin_open_file(script_name, args, config, run_id):
    if not args:
        return 400, {"ok": False, "run_id": run_id, "error": "missing_target_name"}

    if not OPEN_TARGETS_PATH.exists():
        return 500, {"ok": False, "run_id": run_id, "error": f"missing target config: {OPEN_TARGETS_PATH}"}

    with OPEN_TARGETS_PATH.open("r", encoding="utf-8-sig") as file:
        targets = json.load(file)

    target_name = args[0]
    target_value = targets.get(target_name)
    if not target_value:
        return 400, {"ok": False, "run_id": run_id, "error": f"target_not_allowed: {target_name}"}

    exact_path = expand_config_path(target_value)
    target_path = exact_path if exact_path.exists() else find_target_by_name(Path(target_value).name, config)
    if not target_path or not target_path.exists():
        return 404, {
            "ok": False,
            "run_id": run_id,
            "error": f"target_not_found: {target_name}",
            "stdout": f"Exact path: {exact_path}",
        }

    os.startfile(str(target_path))
    return 200, {
        "ok": True,
        "run_id": run_id,
        "returncode": 0,
        "stdout": f"Opened: {target_path}",
        "stderr": "",
    }


def run_builtin_open_file_search(args, config, run_id):
    if not args:
        return 400, {"ok": False, "run_id": run_id, "error": "missing_file_name"}

    filename = args[0].strip()
    if not filename:
        return 400, {"ok": False, "run_id": run_id, "error": "missing_file_name"}
    if filename != Path(filename).name or any(sep in filename for sep in ["/", "\\", ":"]):
        return 400, {
            "ok": False,
            "run_id": run_id,
            "error": "file_name_only",
            "stderr": "Please enter a file name only, not a full path.",
        }

    target_path = find_target_by_name(filename, config)
    if not target_path or not target_path.exists():
        candidates = find_all_targets_by_name(filename, config)
        return 404, {
            "ok": False,
            "run_id": run_id,
            "error": f"file_not_found: {filename}",
            "stdout": (
                "Search roots: "
                + "; ".join(str(path) for path in candidate_search_roots(config))
                + ("\nCandidates:\n" + "\n".join(str(path) for path in candidates) if candidates else "")
            ),
        }

    os.startfile(str(target_path))
    return 200, {
        "ok": True,
        "run_id": run_id,
        "returncode": 0,
        "stdout": f"Opened: {target_path}",
        "stderr": "",
    }


class AgentHandler(BaseHTTPRequestHandler):
    server_version = "LanScriptAgent/1.0"

    def do_GET(self):
        if self.path != "/health":
            json_response(self, 404, {"ok": False, "error": "not_found"})
            return
        json_response(self, 200, {"ok": True, "agent": self.server_version})

    def do_POST(self):
        if self.path != "/run":
            json_response(self, 404, {"ok": False, "error": "not_found"})
            return

        config = load_config()
        token = self.headers.get("X-Agent-Token", "")
        if token != config.get("token"):
            json_response(self, 401, {"ok": False, "error": "unauthorized"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            payload = json.loads(body or "{}")
        except (ValueError, json.JSONDecodeError):
            json_response(self, 400, {"ok": False, "error": "invalid_json"})
            return

        script_name = payload.get("script")
        args = payload.get("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            json_response(self, 400, {"ok": False, "error": "args_must_be_string_array"})
            return

        scripts = config.get("scripts", {})
        script = scripts.get(script_name)
        if not script:
            json_response(self, 400, {"ok": False, "error": "script_not_allowed"})
            return

        run_id = str(uuid.uuid4())
        if script.get("builtin") == "open_file":
            status, result = run_builtin_open_file(script_name, args, config, run_id)
            json_response(self, status, result)
            return
        if script.get("builtin") == "open_file_search":
            status, result = run_builtin_open_file_search(args, config, run_id)
            json_response(self, status, result)
            return

        command = resolve_command(script["command"]) + args
        timeout = int(script.get("timeout_seconds", config.get("default_timeout_seconds", 120)))

        try:
            completed = subprocess.run(
                command,
                cwd=str(BASE_DIR),
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
            )
            json_response(
                self,
                200,
                {
                    "ok": completed.returncode == 0,
                    "run_id": run_id,
                    "returncode": completed.returncode,
                    "stdout": completed.stdout[-8000:],
                    "stderr": completed.stderr[-8000:],
                },
            )
        except subprocess.TimeoutExpired as exc:
            json_response(
                self,
                408,
                {
                    "ok": False,
                    "run_id": run_id,
                    "error": "timeout",
                    "stdout": (exc.stdout or "")[-8000:],
                    "stderr": (exc.stderr or "")[-8000:],
                },
            )
        except OSError as exc:
            json_response(self, 500, {"ok": False, "run_id": run_id, "error": str(exc)})

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))


def main():
    parser = argparse.ArgumentParser(description="LAN script execution agent")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    if not CONFIG_PATH.exists():
        raise SystemExit(f"Missing config: {CONFIG_PATH}")

    server = ThreadingHTTPServer((args.host, args.port), AgentHandler)
    print(f"Agent listening on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
