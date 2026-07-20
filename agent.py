import argparse
import csv
import ctypes
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "agent_config.json"
OPEN_TARGETS_PATH = BASE_DIR / "scripts" / "open_targets.json"
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SW_SHOWNORMAL = 1
WAIT_TIMEOUT = 0x00000102
STILL_ACTIVE = 259


class ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("fMask", ctypes.c_ulong),
        ("hwnd", ctypes.c_void_p),
        ("lpVerb", ctypes.c_wchar_p),
        ("lpFile", ctypes.c_wchar_p),
        ("lpParameters", ctypes.c_wchar_p),
        ("lpDirectory", ctypes.c_wchar_p),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.c_void_p),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.c_wchar_p),
        ("hkeyClass", ctypes.c_void_p),
        ("dwHotKey", ctypes.c_ulong),
        ("hIcon", ctypes.c_void_p),
        ("hProcess", ctypes.c_void_p),
    ]


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
        .replace("\uff1a", ":")
        .replace(" ", "")
        .replace("_", "")
        .replace("-", "")
        .replace("\u00b7", "")
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


def unique_values(values):
    seen = set()
    result = []
    for value in values:
        key = value.lower()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def resolve_shortcut_with_cscript(shortcut_path):
    script = (
        'Set shell = CreateObject("WScript.Shell")\n'
        'Set link = shell.CreateShortcut(WScript.Arguments(0))\n'
        'WScript.Echo "TargetPath=" & link.TargetPath\n'
        'WScript.Echo "Arguments=" & link.Arguments\n'
        'WScript.Echo "WorkingDirectory=" & link.WorkingDirectory\n'
    )
    script_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".vbs", delete=False, encoding="ascii") as file:
            file.write(script)
            script_path = file.name
        output = subprocess.check_output(
            ["cscript.exe", "//nologo", script_path, str(shortcut_path)],
            stderr=subprocess.STDOUT,
            timeout=8,
        )
        text = output.decode("mbcs", errors="ignore")
        details = {}
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                details[key] = value.strip()
        return details
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ShortcutError": str(exc)}
    finally:
        if script_path:
            try:
                os.unlink(script_path)
            except OSError:
                pass


def extract_exe_paths_from_text(value):
    if not value:
        return []
    return [match.strip().strip('"') for match in re.findall(r"[A-Za-z]:\\[^\x00\r\n\"]+?\.exe", value, flags=re.IGNORECASE)]


def extract_lnk_exe_candidates(shortcut_path):
    data = Path(shortcut_path).read_bytes()
    details = resolve_shortcut_with_cscript(shortcut_path)
    candidates = []
    if details.get("TargetPath"):
        candidates.append(details["TargetPath"])
    candidates.extend(extract_exe_paths_from_text(details.get("Arguments", "")))
    patterns = [
        (data, rb"[A-Za-z]:\\[^\x00\r\n\"]+?\.exe"),
        (data.decode("utf-16le", errors="ignore"), r"[A-Za-z]:\\[^\x00\r\n\"]+?\.exe"),
    ]
    for source, pattern in patterns:
        for match in re.findall(pattern, source, flags=re.IGNORECASE):
            if isinstance(match, bytes):
                try:
                    value = match.decode("mbcs", errors="ignore")
                except LookupError:
                    value = match.decode(errors="ignore")
            else:
                value = match
            value = value.strip().strip('"')
            if value:
                candidates.append(value)
    return unique_values(candidates), details


def running_process_names():
    try:
        output = subprocess.check_output(
            ["tasklist", "/fo", "csv", "/nh"],
            stderr=subprocess.DEVNULL,
            text=True,
            errors="ignore",
        )
    except (OSError, subprocess.SubprocessError):
        return set()

    names = set()
    for row in csv.reader(io.StringIO(output)):
        if row:
            names.add(row[0].lower())
    return names


def process_names_from_paths(paths):
    return unique_values([Path(path).name for path in paths if Path(path).suffix.lower() == ".exe"])


def any_process_running(process_names):
    if not process_names:
        return False
    running = running_process_names()
    return any(name.lower() in running for name in process_names)


def wait_for_process(process_names, timeout_seconds=8):
    if not process_names:
        return False
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() <= deadline:
        if any_process_running(process_names):
            return True
        time.sleep(0.5)
    return False


def shell_execute_open(target_path, wait_ms=2500, confirm_process_names=None):
    target_path = Path(target_path)
    confirm_process_names = confirm_process_names or []
    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(ShellExecuteInfo)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.hwnd = None
    info.lpVerb = "open"
    info.lpFile = str(target_path)
    info.lpParameters = None
    info.lpDirectory = str(target_path.parent)
    info.nShow = SW_SHOWNORMAL

    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        raise ctypes.WinError()

    h_process = info.hProcess
    if not h_process:
        if wait_for_process(confirm_process_names):
            return {
                "accepted": True,
                "confirmed_open": True,
                "launch_state": "target_process_running_without_handle",
            }
        return {
            "accepted": True,
            "confirmed_open": False,
            "launch_state": "sent_without_process_handle",
        }

    try:
        wait_result = ctypes.windll.kernel32.WaitForSingleObject(h_process, wait_ms)
        exit_code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h_process, ctypes.byref(exit_code))
        if wait_result == WAIT_TIMEOUT or exit_code.value == STILL_ACTIVE:
            return {
                "accepted": True,
                "confirmed_open": True,
                "launch_state": "process_running",
            }
        if exit_code.value == 0 and wait_for_process(confirm_process_names):
            return {
                "accepted": True,
                "confirmed_open": True,
                "launch_state": "target_process_running_after_shell_exit",
            }
        return {
            "accepted": True,
            "confirmed_open": False,
            "launch_state": f"process_exited:{exit_code.value}",
        }
    finally:
        ctypes.windll.kernel32.CloseHandle(h_process)


def cmd_start_open(target_path):
    target_path = Path(target_path)
    cwd = target_path if target_path.is_dir() else target_path.parent
    batch_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".bat", delete=False, encoding="mbcs") as file:
            file.write("@echo off\n")
            file.write(f'cd /d "{cwd}"\n')
            file.write(f'start "" "{target_path}"\n')
            file.write("exit /b %ERRORLEVEL%\n")
            batch_path = file.name
        process = subprocess.Popen(
            ["cmd.exe", "/d", "/c", batch_path],
            cwd=str(cwd),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        time.sleep(0.7)
        returncode = process.poll()
        accepted = returncode is None or returncode == 0
        return {
            "accepted": accepted,
            "confirmed_open": accepted,
            "launch_state": "cmd_started" if returncode is None else f"cmd_start_returned:{returncode}",
            "cmd": f'cmd.exe /d /c "{batch_path}"',
            "cmd_script": f'cd /d "{cwd}" && start "" "{target_path}"',
            "cmd_stdout": "",
            "cmd_stderr": "",
        }
    finally:
        if batch_path:
            try:
                os.unlink(batch_path)
            except OSError:
                pass


def simulated_explorer_activate(target_path):
    target_path = Path(target_path)
    script = (
        'Set shell = CreateObject("WScript.Shell")\n'
        f'shell.Run "explorer.exe /select,""{target_path}""", 1, False\n'
        "WScript.Sleep 1600\n"
        'shell.SendKeys "{ENTER}"\n'
        "WScript.Sleep 300\n"
    )
    script_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".vbs", delete=False, encoding="mbcs") as file:
            file.write(script)
            script_path = file.name
        completed = subprocess.run(
            ["cscript.exe", "//nologo", script_path],
            cwd=str(target_path.parent if target_path.parent.exists() else BASE_DIR),
            capture_output=True,
            text=True,
            shell=False,
            timeout=8,
        )
        accepted = completed.returncode == 0
        return {
            "accepted": accepted,
            "confirmed_open": accepted,
            "launch_state": f"simulated_enter_returned:{completed.returncode}",
            "activation_method": "explorer_select_enter",
            "activation_script": f'explorer.exe /select,"{target_path}" -> SendKeys ENTER',
            "cmd_stdout": completed.stdout.strip(),
            "cmd_stderr": completed.stderr.strip(),
        }
    finally:
        if script_path:
            try:
                os.unlink(script_path)
            except OSError:
                pass


def open_path(target_path):
    target_path = Path(target_path)
    suffix = target_path.suffix.lower()
    if suffix == ".lnk":
        candidates, shortcut_details = extract_lnk_exe_candidates(target_path)
        process_names = process_names_from_paths(candidates)
        result = simulated_explorer_activate(target_path)
        if candidates:
            result["target_candidates"] = candidates[:5]
        if process_names:
            result["target_process_names"] = process_names
        if shortcut_details.get("TargetPath"):
            result["shortcut_target"] = shortcut_details["TargetPath"]
        if shortcut_details.get("Arguments"):
            result["shortcut_arguments"] = shortcut_details["Arguments"]
        if shortcut_details.get("WorkingDirectory"):
            result["shortcut_working_directory"] = shortcut_details["WorkingDirectory"]
        if shortcut_details.get("ShortcutError"):
            result["shortcut_error"] = shortcut_details["ShortcutError"]
        return result
    result = simulated_explorer_activate(target_path)
    if suffix == ".exe":
        result["target_process_names"] = [target_path.name]
    return result


def build_open_response(run_id, target_path, launch_result):
    confirmed = bool(launch_result.get("confirmed_open"))
    stdout_lines = [f"{'CMD open accepted' if confirmed else 'CMD open failed'}: {target_path}"]
    if launch_result.get("cmd_stdout"):
        stdout_lines.extend(["CMD STDOUT:", launch_result["cmd_stdout"]])
    payload = {
        "ok": confirmed,
        "accepted": bool(launch_result.get("accepted")),
        "confirmed_open": confirmed,
        "launch_state": launch_result.get("launch_state", ""),
        "cmd": launch_result.get("cmd", ""),
        "cmd_script": launch_result.get("cmd_script", ""),
        "activation_method": launch_result.get("activation_method", ""),
        "activation_script": launch_result.get("activation_script", ""),
        "run_id": run_id,
        "returncode": 0 if confirmed else 1,
        "stdout": "\n".join(stdout_lines),
        "stderr": launch_result.get("cmd_stderr", ""),
    }
    for key in (
        "target_candidates",
        "target_process_names",
        "shortcut_target",
        "shortcut_arguments",
        "shortcut_working_directory",
        "shortcut_error",
    ):
        if launch_result.get(key):
            payload[key] = launch_result[key]
    return payload


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

    try:
        launch_result = open_path(target_path)
    except OSError as exc:
        return 500, {
            "ok": False,
            "run_id": run_id,
            "error": str(exc),
            "stdout": f"Target: {target_path}",
        }
    return 200, build_open_response(run_id, target_path, launch_result)


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

    try:
        launch_result = open_path(target_path)
    except OSError as exc:
        return 500, {
            "ok": False,
            "run_id": run_id,
            "error": str(exc),
            "stdout": f"Target: {target_path}",
        }
    return 200, build_open_response(run_id, target_path, launch_result)


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
