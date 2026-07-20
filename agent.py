import argparse
import csv
import ctypes
import io
import json
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from secure_payload import SecurePayloadError, decrypt_riot_login_request, verify_request_authentication


BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "agent_config.json"
OPEN_TARGETS_PATH = BASE_DIR / "scripts" / "open_targets.json"
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SW_SHOWNORMAL = 1
WAIT_TIMEOUT = 0x00000102
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_RESTORE = 9
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_RETURN = 0x0D
VK_TAB = 0x09
VK_A = 0x41
ULONG_PTR = wintypes.WPARAM
USER32 = ctypes.WinDLL("user32", use_last_error=True)
KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
RIOT_INPUT_LOCK = threading.Lock()
RECENT_LOGIN_LOCK = threading.Lock()
RECENT_LOGIN_SIGNATURES = {}


class MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KeyboardInput(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HardwareInput(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class InputValue(ctypes.Union):
    _fields_ = [("mi", MouseInput), ("ki", KeyboardInput), ("hi", HardwareInput)]


class Input(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", wintypes.DWORD), ("value", InputValue)]


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    allow_reuse_port = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def keyboard_event(virtual_key=0, scan_code=0, flags=0):
    event = Input()
    event.type = INPUT_KEYBOARD
    event.ki = KeyboardInput(virtual_key, scan_code, flags, 0, 0)
    return event


def mouse_event(flags):
    event = Input()
    event.type = INPUT_MOUSE
    event.mi = MouseInput(0, 0, 0, flags, 0, 0)
    return event


def send_input_events(events):
    if not events:
        return
    array = (Input * len(events))(*events)
    sent = USER32.SendInput(len(events), array, ctypes.sizeof(Input))
    if sent != len(events):
        raise ctypes.WinError(ctypes.get_last_error())


def press_virtual_key(virtual_key):
    send_input_events(
        [
            keyboard_event(virtual_key=virtual_key),
            keyboard_event(virtual_key=virtual_key, flags=KEYEVENTF_KEYUP),
        ]
    )


def select_all_text():
    send_input_events(
        [
            keyboard_event(virtual_key=VK_CONTROL),
            keyboard_event(virtual_key=VK_A),
            keyboard_event(virtual_key=VK_A, flags=KEYEVENTF_KEYUP),
            keyboard_event(virtual_key=VK_CONTROL, flags=KEYEVENTF_KEYUP),
        ]
    )


def send_unicode_text(value, delay_seconds):
    encoded = value.encode("utf-16-le")
    for (code_unit,) in struct.iter_unpack("<H", encoded):
        send_input_events(
            [
                keyboard_event(scan_code=code_unit, flags=KEYEVENTF_UNICODE),
                keyboard_event(scan_code=code_unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
            ]
        )
        if delay_seconds:
            time.sleep(delay_seconds)


def get_window_text(hwnd):
    length = USER32.GetWindowTextLengthW(hwnd)
    value = ctypes.create_unicode_buffer(length + 1)
    USER32.GetWindowTextW(hwnd, value, len(value))
    return value.value


def get_window_class(hwnd):
    value = ctypes.create_unicode_buffer(256)
    USER32.GetClassNameW(hwnd, value, len(value))
    return value.value


def get_window_process_path(hwnd):
    process_id = wintypes.DWORD()
    USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    process = KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process_id.value)
    if not process:
        return ""
    try:
        size = wintypes.DWORD(32768)
        value = ctypes.create_unicode_buffer(size.value)
        if not KERNEL32.QueryFullProcessImageNameW(process, 0, value, ctypes.byref(size)):
            return ""
        return value.value
    finally:
        KERNEL32.CloseHandle(process)


def find_riot_window():
    candidates = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def callback(hwnd, _lparam):
        if not USER32.IsWindowVisible(hwnd):
            return True
        title = get_window_text(hwnd)
        class_name = get_window_class(hwnd)
        if title.lower() != "riot client" or class_name != "Chrome_WidgetWin_1":
            return True
        process_path = get_window_process_path(hwnd)
        if Path(process_path).name.lower() != "riot client.exe":
            return True
        candidates.append(
            {
                "hwnd": int(hwnd),
                "title": title,
                "class_name": class_name,
                "process_path": process_path,
            }
        )
        return True

    USER32.EnumWindows(callback, 0)
    return candidates[0] if candidates else None


def wait_for_riot_window(timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() <= deadline:
        window = find_riot_window()
        if window:
            return window
        time.sleep(0.5)
    return None


def wait_for_stable_riot_window(timeout_seconds, stable_seconds):
    deadline = time.monotonic() + timeout_seconds
    stable_hwnd = None
    stable_since = 0
    while time.monotonic() <= deadline:
        window = find_riot_window()
        if window and window["hwnd"] == stable_hwnd:
            if time.monotonic() - stable_since >= stable_seconds:
                return window
        elif window:
            stable_hwnd = window["hwnd"]
            stable_since = time.monotonic()
        else:
            stable_hwnd = None
            stable_since = 0
        time.sleep(0.25)
    return None


def stop_riot_client_processes():
    for process_name in ("Riot Client.exe", "RiotClientServices.exe"):
        subprocess.run(
            ["taskkill.exe", "/f", "/im", process_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    deadline = time.monotonic() + 8
    while time.monotonic() <= deadline:
        if not any_process_running(["Riot Client.exe", "RiotClientServices.exe"]):
            return True
        time.sleep(0.25)
    return False


def activate_window(hwnd):
    if not USER32.IsWindow(hwnd):
        return False
    USER32.ShowWindow(hwnd, SW_RESTORE)
    USER32.BringWindowToTop(hwnd)

    current_thread = KERNEL32.GetCurrentThreadId()
    target_thread = USER32.GetWindowThreadProcessId(hwnd, None)
    foreground = USER32.GetForegroundWindow()
    foreground_thread = USER32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    attached_threads = []
    for thread_id in {target_thread, foreground_thread}:
        if thread_id and thread_id != current_thread:
            if USER32.AttachThreadInput(current_thread, thread_id, True):
                attached_threads.append(thread_id)
    try:
        press_virtual_key(VK_MENU)
        USER32.BringWindowToTop(hwnd)
        USER32.SetWindowPos(
            hwnd,
            ctypes.c_void_p(-1),
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
        )
        USER32.SetForegroundWindow(hwnd)
        USER32.SetActiveWindow(hwnd)
        USER32.SetFocus(hwnd)
        USER32.SetWindowPos(
            hwnd,
            ctypes.c_void_p(-2),
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
        )
    finally:
        for thread_id in attached_threads:
            USER32.AttachThreadInput(current_thread, thread_id, False)

    deadline = time.monotonic() + 3
    while time.monotonic() <= deadline:
        if USER32.GetForegroundWindow() == hwnd:
            return True
        USER32.SetForegroundWindow(hwnd)
        time.sleep(0.1)
    return False


def riot_window_has_foreground(hwnd):
    foreground = USER32.GetForegroundWindow()
    if not foreground:
        return False
    if foreground == hwnd:
        return True
    return Path(get_window_process_path(foreground)).name.lower() == "riot client.exe"


def config_position(config, key, default):
    value = config.get(key, default)
    if not isinstance(value, list) or len(value) != 2:
        return default
    try:
        x_ratio, y_ratio = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return default
    if not 0 <= x_ratio <= 1 or not 0 <= y_ratio <= 1:
        return default
    return x_ratio, y_ratio


def client_screen_point(hwnd, position):
    rect = wintypes.RECT()
    if not USER32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width < 600 or height < 400:
        raise RuntimeError(f"Riot Client window is too small: {width}x{height}")
    point = wintypes.POINT(int(width * position[0]), int(height * position[1]))
    if not USER32.ClientToScreen(hwnd, ctypes.byref(point)):
        raise ctypes.WinError(ctypes.get_last_error())
    return point


def click_screen_point(point):
    if not USER32.SetCursorPos(point.x, point.y):
        raise ctypes.WinError(ctypes.get_last_error())
    send_input_events([mouse_event(MOUSEEVENTF_LEFTDOWN), mouse_event(MOUSEEVENTF_LEFTUP)])


def input_riot_credentials(window, username, password, config):
    hwnd = window["hwnd"]
    activate_window(hwnd)
    if not USER32.IsWindow(hwnd):
        raise RuntimeError("cannot_activate_riot_window")

    username_position = config_position(config, "riot_username_position", [0.13, 0.285])
    delay_seconds = min(0.2, max(0, float(config.get("riot_input_delay_seconds", 0.03))))
    username_point = client_screen_point(hwnd, username_position)
    original_cursor = wintypes.POINT()
    has_cursor = bool(USER32.GetCursorPos(ctypes.byref(original_cursor)))

    try:
        activate_window(hwnd)
        click_screen_point(username_point)
        time.sleep(0.15)
        if not riot_window_has_foreground(hwnd):
            activate_window(hwnd)
            click_screen_point(username_point)
            time.sleep(0.15)
        if not riot_window_has_foreground(hwnd):
            raise RuntimeError("cannot_focus_riot_username")
        select_all_text()
        send_unicode_text(username, delay_seconds)

        if not USER32.IsWindow(hwnd):
            raise RuntimeError("riot_window_recreated")
        if not riot_window_has_foreground(hwnd):
            raise RuntimeError("riot_window_lost_focus_after_username")
        press_virtual_key(VK_TAB)
        time.sleep(0.2)
        if not riot_window_has_foreground(hwnd):
            raise RuntimeError("riot_window_lost_focus_before_password")
        select_all_text()
        send_unicode_text(password, delay_seconds)
        time.sleep(0.15)
        press_virtual_key(VK_RETURN)
    finally:
        if has_cursor:
            USER32.SetCursorPos(original_cursor.x, original_cursor.y)


def remember_request_signature(signature):
    now = time.time()
    with RECENT_LOGIN_LOCK:
        expired = [value for value, created_at in RECENT_LOGIN_SIGNATURES.items() if now - created_at > 180]
        for value in expired:
            RECENT_LOGIN_SIGNATURES.pop(value, None)
        if signature in RECENT_LOGIN_SIGNATURES:
            return False
        RECENT_LOGIN_SIGNATURES[signature] = now
        return True


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


def find_riot_client_executable(config):
    candidates = []
    configured_path = config.get("riot_client_executable")
    if configured_path:
        candidates.append(expand_config_path(configured_path))

    program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    metadata_path = program_data / "Riot Games" / "RiotClientInstalls.json"
    if metadata_path.exists():
        try:
            with metadata_path.open("r", encoding="utf-8-sig") as file:
                metadata = json.load(file)
            for key in ("rc_default", "rc_live"):
                if metadata.get(key):
                    candidates.append(Path(metadata[key]))
            for group_name in ("patchlines", "associated_client"):
                group = metadata.get(group_name, {})
                if isinstance(group, dict):
                    candidates.extend(Path(value) for value in group.values() if isinstance(value, str))
        except (OSError, json.JSONDecodeError):
            pass

    drive_mask = KERNEL32.GetLogicalDrives()
    for index in range(26):
        if drive_mask & (1 << index):
            drive = chr(ord("A") + index)
            candidates.append(Path(f"{drive}:\\Riot Games\\Riot Client\\RiotClientServices.exe"))

    seen = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file() and candidate.name.lower() == "riotclientservices.exe":
            return candidate
    return None


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


def open_path(target_path):
    target_path = Path(target_path)
    suffix = target_path.suffix.lower()
    if suffix == ".lnk":
        candidates, shortcut_details = extract_lnk_exe_candidates(target_path)
        process_names = process_names_from_paths(candidates)
        result = cmd_start_open(target_path)
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
    result = cmd_start_open(target_path)
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


def run_builtin_riot_login(args, config, run_id):
    if len(args) != 2 or not args[0] or not args[1]:
        return 400, {"ok": False, "run_id": run_id, "error": "missing_riot_credentials"}
    username, password = args
    if len(username) > 320 or len(password) > 320:
        return 400, {"ok": False, "run_id": run_id, "error": "riot_credentials_too_long"}

    timeout_seconds = min(120, max(1, int(config.get("riot_window_timeout_seconds", 45))))
    stable_seconds = min(15, max(1, float(config.get("riot_window_ready_delay_seconds", 5))))
    max_attempts = min(5, max(1, int(config.get("riot_login_max_attempts", 3))))
    riot_executable = find_riot_client_executable(config)
    if not riot_executable:
        return 404, {
            "ok": False,
            "run_id": run_id,
            "error": "riot_client_executable_not_found",
            "stderr": "未找到 RiotClientServices.exe，请在 agent_config.json 中配置 riot_client_executable。",
        }

    launch_result = None
    last_error = "riot_window_not_found"
    last_window = None
    with RIOT_INPUT_LOCK:
        window = find_riot_window()
        for attempt in range(1, max_attempts + 1):
            if window:
                window = wait_for_stable_riot_window(stable_seconds + 3, stable_seconds)
            if not window:
                if any_process_running(["RiotClientServices.exe", "Riot Client.exe"]):
                    stop_riot_client_processes()
                    time.sleep(0.5)
                try:
                    launch_result = open_path(riot_executable)
                except OSError as exc:
                    last_error = str(exc)
                    window = None
                    continue
                window = wait_for_stable_riot_window(timeout_seconds, stable_seconds)
            if not window:
                last_error = "riot_window_not_found"
                continue

            last_window = window
            try:
                input_riot_credentials(window, username, password, config)
                break
            except (OSError, RuntimeError, ValueError) as exc:
                last_error = str(exc)
                window = None
                if attempt < max_attempts:
                    stop_riot_client_processes()
                    time.sleep(0.5)
        else:
            foreground = USER32.GetForegroundWindow()
            payload = {
                "ok": False,
                "run_id": run_id,
                "error": last_error,
                "attempts": max_attempts,
                "stderr": "Riot Client 窗口未能保持稳定，已完成自动重启和重新捕获。",
            }
            if foreground:
                payload.update(
                    {
                        "foreground_handle": int(foreground),
                        "foreground_title": get_window_text(foreground),
                        "foreground_class": get_window_class(foreground),
                        "foreground_process": get_window_process_path(foreground),
                    }
                )
            if last_window:
                payload.update(
                    {
                        "window_handle": last_window["hwnd"],
                        "window_title": last_window["title"],
                        "window_class": last_window["class_name"],
                    }
                )
            return 409, payload

    payload = {
        "ok": True,
        "run_id": run_id,
        "login_state": "credentials_submitted",
        "window_handle": window["hwnd"],
        "window_title": window["title"],
        "window_class": window["class_name"],
        "attempts": attempt,
        "window_stable_seconds": stable_seconds,
        "stdout": "已捕获 Riot Client 窗口句柄并提交账号密码。",
    }
    payload["riot_executable"] = str(riot_executable)
    if launch_result:
        payload["launch_state"] = launch_result.get("launch_state", "")
    return 200, payload


class AgentHandler(BaseHTTPRequestHandler):
    server_version = "LanScriptAgent/1.5"

    def do_GET(self):
        if self.path != "/health":
            json_response(self, 404, {"ok": False, "error": "not_found"})
            return
        json_response(self, 200, {"ok": True, "agent": self.server_version})

    def do_POST(self):
        if self.path != "/run":
            json_response(self, 404, {"ok": False, "error": "not_found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 65536:
                raise ValueError("invalid_content_length")
            body = self.rfile.read(length)
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            json_response(self, 400, {"ok": False, "error": "invalid_json"})
            return

        config = load_config()
        script_name = payload.get("script")
        if script_name == "riot_login":
            issued_at = self.headers.get("X-Agent-Timestamp", "")
            signature = self.headers.get("X-Agent-Signature", "")
            try:
                username, password = decrypt_riot_login_request(
                    config.get("token", ""), body, issued_at, signature
                )
            except SecurePayloadError as exc:
                json_response(self, 401, {"ok": False, "error": str(exc)})
                return
            if not remember_request_signature(signature):
                json_response(self, 409, {"ok": False, "error": "replayed_request"})
                return
            args = [username, password]
        else:
            issued_at = self.headers.get("X-Agent-Timestamp", "")
            request_nonce = self.headers.get("X-Agent-Nonce", "")
            signature = self.headers.get("X-Agent-Signature", "")
            if signature:
                try:
                    verify_request_authentication(
                        config.get("token", ""), body, issued_at, request_nonce, signature
                    )
                except SecurePayloadError as exc:
                    json_response(self, 401, {"ok": False, "error": str(exc)})
                    return
                if not remember_request_signature(signature):
                    json_response(self, 409, {"ok": False, "error": "replayed_request"})
                    return
            else:
                token = self.headers.get("X-Agent-Token", "")
                if token != config.get("token"):
                    json_response(self, 401, {"ok": False, "error": "unauthorized"})
                    return
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
        if script.get("builtin") == "riot_login":
            status, result = run_builtin_riot_login(args, config, run_id)
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

    server = ExclusiveThreadingHTTPServer((args.host, args.port), AgentHandler)
    print(f"Agent listening on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
