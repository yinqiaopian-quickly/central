import json
import ipaddress
import queue
import socket
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import messagebox, ttk

from controller import BASE_DIR, run_on_host, run_riot_login_on_host


HOSTS_PATH = BASE_DIR / "hosts.txt"
AGENT_CONFIG_PATH = BASE_DIR / "agent_config.json"
APP_VERSION = "2026.07.20-21"


def read_json(path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def read_hosts():
    if not HOSTS_PATH.exists():
        return []
    hosts = []
    seen = set()
    with HOSTS_PATH.open("r", encoding="utf-8-sig") as file:
        for line in file:
            value = line.strip()
            if value and not value.startswith("#") and value not in seen:
                hosts.append(value)
                seen.add(value)
    return hosts


def normalize_host(value):
    value = value.strip()
    for prefix in ("http://", "https://"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
    value = value.split("/", 1)[0]
    if not value:
        return ""
    return value if ":" in value else f"{value}:8765"


def get_local_lan_cidr():
    ip = ""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.2)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
    except OSError:
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except OSError:
            ip = ""
    if not ip or ip.startswith("127."):
        return "192.168.1.0/24"
    parts = ip.split(".")
    if len(parts) == 4:
        return ".".join(parts[:3]) + ".0/24"
    return "192.168.1.0/24"


def check_agent_health(ip, port, timeout):
    host = f"{ip}:{port}"
    request = urllib.request.Request(f"http://{host}/health", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("ok"):
            return host, True, payload
        return host, False, payload
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return host, False, {"error": str(exc)}


class ControllerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"主控端 {APP_VERSION}")
        self.geometry("1080x780")
        self.minsize(980, 700)

        self.token_var = tk.StringVar()
        self.file_name_var = tk.StringVar(value="Riot Client")
        self.host_input_var = tk.StringVar()
        self.riot_username_var = tk.StringVar()
        self.riot_password_var = tk.StringVar()
        self.scan_cidr_var = tk.StringVar(value=get_local_lan_cidr())
        self.scan_port_var = tk.StringVar(value="8765")
        self.scan_timeout_var = tk.StringVar(value="0.6")
        self.interval_var = tk.StringVar(value="5")
        self.timeout_var = tk.StringVar(value="180")
        self.summary_var = tk.StringVar(value="等待执行")
        self.host_selected = {}
        self.host_credentials = {}
        self.discovered_hosts = []
        self.scan_event_queue = queue.Queue()

        self.create_widgets()
        self.load_state()

    def create_widgets(self):
        root = ttk.Frame(self, padding=16)
        root.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(root)
        header.pack(fill=tk.X)
        ttk.Label(header, text=f"主控端 {APP_VERSION}", font=("Microsoft YaHei UI", 16, "bold")).pack(side=tk.LEFT)
        ttk.Button(header, text="刷新配置", command=self.load_state).pack(side=tk.RIGHT)

        body = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, pady=14)

        left = ttk.Frame(body, padding=(0, 0, 12, 0))
        right = ttk.Frame(body)
        body.add(left, weight=2)
        body.add(right, weight=3)

        ttk.Label(left, text="Token").pack(anchor=tk.W)
        ttk.Entry(left, textvariable=self.token_var, show="*").pack(fill=tk.X, pady=(4, 10))

        ttk.Label(left, text="文件名称").pack(anchor=tk.W)
        ttk.Entry(left, textvariable=self.file_name_var).pack(fill=tk.X, pady=(4, 4))
        ttk.Label(left, text="只填写文件名或程序名").pack(anchor=tk.W, pady=(0, 10))

        grid = ttk.Frame(left)
        grid.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(grid, text="启动间隔秒数").grid(row=0, column=0, sticky=tk.W)
        ttk.Label(grid, text="单台超时秒数").grid(row=0, column=1, sticky=tk.W, padx=(10, 0))
        ttk.Entry(grid, textvariable=self.interval_var, width=10).grid(row=1, column=0, sticky=tk.EW, pady=(4, 0))
        ttk.Entry(grid, textvariable=self.timeout_var, width=10).grid(row=1, column=1, sticky=tk.EW, padx=(10, 0), pady=(4, 0))
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        ttk.Label(left, text="添加被控主机").pack(anchor=tk.W)
        add_row = ttk.Frame(left)
        add_row.pack(fill=tk.X, pady=(4, 8))
        host_entry = ttk.Entry(add_row, textvariable=self.host_input_var)
        host_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        host_entry.bind("<Return>", lambda _event: self.add_host())
        ttk.Button(add_row, text="添加", command=self.add_host).pack(side=tk.LEFT, padx=(8, 0))

        scan_box = ttk.LabelFrame(left, text="自动检索被控端")
        scan_box.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(scan_box, text="扫描网段").grid(row=0, column=0, sticky=tk.W, padx=(8, 0), pady=(8, 0))
        ttk.Label(scan_box, text="端口").grid(row=0, column=1, sticky=tk.W, padx=(8, 0), pady=(8, 0))
        ttk.Label(scan_box, text="超时秒").grid(row=0, column=2, sticky=tk.W, padx=(8, 0), pady=(8, 0))
        ttk.Entry(scan_box, textvariable=self.scan_cidr_var, width=18).grid(row=1, column=0, sticky=tk.EW, padx=(8, 0), pady=(4, 8))
        ttk.Entry(scan_box, textvariable=self.scan_port_var, width=7).grid(row=1, column=1, sticky=tk.EW, padx=(8, 0), pady=(4, 8))
        ttk.Entry(scan_box, textvariable=self.scan_timeout_var, width=7).grid(row=1, column=2, sticky=tk.EW, padx=(8, 0), pady=(4, 8))
        self.scan_btn = ttk.Button(scan_box, text="自动检索", command=self.scan_agents)
        self.scan_btn.grid(row=1, column=3, sticky=tk.EW, padx=8, pady=(4, 8))
        self.add_scan_btn = ttk.Button(scan_box, text="添加扫描结果", command=self.add_discovered_hosts)
        self.add_scan_btn.grid(row=2, column=0, columnspan=4, sticky=tk.EW, padx=8, pady=(0, 8))
        scan_box.columnconfigure(0, weight=1)

        credential_box = ttk.LabelFrame(left, text="当前主机 Riot 登录账号")
        credential_box.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(credential_box, text="账号").grid(row=0, column=0, sticky=tk.W, padx=(8, 0), pady=(8, 0))
        ttk.Label(credential_box, text="密码").grid(row=0, column=1, sticky=tk.W, padx=(8, 0), pady=(8, 0))
        ttk.Entry(credential_box, textvariable=self.riot_username_var).grid(
            row=1, column=0, sticky=tk.EW, padx=(8, 0), pady=(4, 8)
        )
        ttk.Entry(credential_box, textvariable=self.riot_password_var, show="*").grid(
            row=1, column=1, sticky=tk.EW, padx=(8, 0), pady=(4, 8)
        )
        ttk.Button(credential_box, text="应用", command=self.apply_current_credentials).grid(
            row=1, column=2, sticky=tk.EW, padx=8, pady=(4, 8)
        )
        ttk.Button(credential_box, text="清除", command=self.clear_current_credentials).grid(
            row=1, column=3, sticky=tk.EW, padx=(0, 8), pady=(4, 8)
        )
        credential_box.columnconfigure(0, weight=1)
        credential_box.columnconfigure(1, weight=1)

        ttk.Label(left, text="在选择列单击或按空格切换要操作的主机").pack(anchor=tk.W)
        table_wrap = ttk.Frame(left)
        table_wrap.pack(fill=tk.BOTH, expand=True, pady=(4, 8))
        self.host_tree = ttk.Treeview(
            table_wrap,
            columns=("selected", "host", "account", "password"),
            show="headings",
            selectmode="browse",
            height=4,
        )
        self.host_tree.heading("selected", text="选择")
        self.host_tree.heading("host", text="主机")
        self.host_tree.heading("account", text="Riot账号")
        self.host_tree.heading("password", text="密码")
        self.host_tree.column("selected", width=46, anchor=tk.CENTER, stretch=False)
        self.host_tree.column("host", width=142, anchor=tk.W)
        self.host_tree.column("account", width=110, anchor=tk.W)
        self.host_tree.column("password", width=62, anchor=tk.CENTER, stretch=False)
        self.host_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(table_wrap, orient=tk.VERTICAL, command=self.host_tree.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.host_tree.configure(yscrollcommand=scrollbar.set)
        self.host_tree.bind("<ButtonRelease-1>", self.on_host_click)
        self.host_tree.bind("<space>", lambda _event: self.toggle_selected_host())
        self.host_tree.bind("<<TreeviewSelect>>", self.on_host_selection_changed)

        host_buttons = ttk.Frame(left)
        host_buttons.pack(fill=tk.X)
        ttk.Button(host_buttons, text="全选", command=self.select_all_hosts).pack(side=tk.LEFT)
        ttk.Button(host_buttons, text="反选", command=self.invert_hosts).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(host_buttons, text="删除", command=self.delete_selected_host).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(host_buttons, text="清空", command=self.clear_hosts).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(host_buttons, text="保存", command=self.save_hosts).pack(side=tk.LEFT, padx=(8, 0))

        action_buttons = ttk.Frame(left)
        action_buttons.pack(fill=tk.X, pady=(8, 0))
        self.run_btn = ttk.Button(action_buttons, text="逐台搜索并打开", command=self.run_task)
        self.run_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.riot_login_btn = ttk.Button(action_buttons, text="启动并登录 Riot", command=self.run_riot_login_task)
        self.riot_login_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))

        ttk.Label(right, textvariable=self.summary_var).pack(anchor=tk.W)
        self.result_text = tk.Text(right, wrap=tk.WORD)
        self.result_text.pack(fill=tk.BOTH, expand=True, pady=(6, 0))

    def load_state(self):
        config = read_json(AGENT_CONFIG_PATH, {})
        self.token_var.set(config.get("token", ""))
        self.set_hosts(read_hosts(), selected=True)
        self.summary_var.set("配置已加载")

    def set_hosts(self, hosts, selected):
        existing_credentials = self.host_credentials
        self.host_tree.delete(*self.host_tree.get_children())
        self.host_selected = {}
        self.host_credentials = {}
        for host in hosts:
            self.host_selected[host] = selected
            if host in existing_credentials:
                self.host_credentials[host] = existing_credentials[host]
            self.host_tree.insert("", tk.END, iid=host, values=self.host_row_values(host))

    def selected_text(self, host):
        return "是" if self.host_selected.get(host) else "否"

    def host_row_values(self, host):
        credentials = self.host_credentials.get(host, {})
        username = credentials.get("username", "")
        password_status = "已填写" if credentials.get("password") else "未填写"
        return self.selected_text(host), host, username, password_status

    def refresh_host_row(self, host):
        if self.host_tree.exists(host):
            self.host_tree.item(host, values=self.host_row_values(host))

    def on_host_selection_changed(self, _event=None):
        host = self.current_host()
        credentials = self.host_credentials.get(host, {})
        self.riot_username_var.set(credentials.get("username", ""))
        self.riot_password_var.set(credentials.get("password", ""))

    def apply_current_credentials(self):
        host = self.current_host()
        if not host:
            messagebox.showwarning("未选择主机", "请先在主机列表中选中一个 IP。")
            return
        username = self.riot_username_var.get().strip()
        password = self.riot_password_var.get()
        if not username or not password:
            messagebox.showwarning("账号不完整", "请填写 Riot 账号和密码。")
            return
        self.host_credentials[host] = {"username": username, "password": password}
        self.refresh_host_row(host)
        self.summary_var.set(f"已为 {host} 设置 Riot 登录账号；密码仅保存在本次运行内")

    def clear_current_credentials(self):
        host = self.current_host()
        if not host:
            messagebox.showwarning("未选择主机", "请先在主机列表中选中一个 IP。")
            return
        self.host_credentials.pop(host, None)
        self.riot_username_var.set("")
        self.riot_password_var.set("")
        self.refresh_host_row(host)
        self.summary_var.set(f"已清除 {host} 的 Riot 登录账号")

    def add_host(self):
        host = normalize_host(self.host_input_var.get())
        if not host:
            return
        if host in self.host_selected:
            self.host_selected[host] = True
            self.refresh_host_row(host)
            self.summary_var.set("主机已存在，已勾选")
        else:
            self.host_selected[host] = True
            self.host_tree.insert("", tk.END, iid=host, values=self.host_row_values(host))
            self.summary_var.set(f"已添加主机：{host}")
        self.host_input_var.set("")
        self.save_hosts(show_status=False)

    def add_or_select_host(self, host):
        host = normalize_host(host)
        if not host:
            return False
        is_new = host not in self.host_selected
        self.host_selected[host] = True
        if is_new:
            self.host_tree.insert("", tk.END, iid=host, values=self.host_row_values(host))
        else:
            self.refresh_host_row(host)
        return is_new

    def add_discovered_hosts(self):
        if not self.discovered_hosts:
            messagebox.showinfo("没有扫描结果", "请先点击自动检索，扫描到被控端后再添加。")
            return
        added = 0
        for host in self.discovered_hosts:
            if self.add_or_select_host(host):
                added += 1
        self.save_hosts(show_status=False)
        self.summary_var.set(f"已添加扫描结果 {len(self.discovered_hosts)} 台，新增 {added} 台")
        messagebox.showinfo("添加完成", f"已添加并勾选 {len(self.discovered_hosts)} 台扫描结果，新增 {added} 台。")

    def clear_hosts(self):
        if not self.host_selected:
            self.summary_var.set("主机列表已经为空")
            return
        if not messagebox.askyesno("确认清空", "确定清空现有已添加的主机列表吗？"):
            return
        self.host_tree.delete(*self.host_tree.get_children())
        self.host_selected = {}
        self.host_credentials = {}
        self.riot_username_var.set("")
        self.riot_password_var.set("")
        self.save_hosts(show_status=False)
        self.summary_var.set("已清空主机列表")

    def scan_agents(self):
        try:
            network = ipaddress.ip_network(self.scan_cidr_var.get().strip(), strict=False)
            port = int(self.scan_port_var.get().strip())
            timeout = max(0.1, float(self.scan_timeout_var.get().strip()))
        except ValueError:
            messagebox.showwarning("参数错误", "扫描网段、端口或超时格式不正确，例如 192.168.1.0/24、8765、0.6。")
            return

        addresses = list(network.hosts())
        if not addresses:
            messagebox.showwarning("网段错误", "这个网段没有可扫描的主机地址。")
            return
        if len(addresses) > 4096:
            messagebox.showwarning("网段过大", "请使用较小网段，例如 192.168.1.0/24。")
            return

        self.scan_btn.configure(state=tk.DISABLED)
        self.discovered_hosts = []
        while True:
            try:
                self.scan_event_queue.get_nowait()
            except queue.Empty:
                break
        self.summary_var.set(f"正在扫描 {network}，共 {len(addresses)} 个地址...")
        self.append_result(f"开始自动检索被控端：{network} 端口 {port}")
        self.append_result("扫描结果会先输出在这里，点击“添加扫描结果”后才会加入主机列表。")
        thread = threading.Thread(target=self.scan_worker, args=(addresses, port, timeout), daemon=True)
        thread.start()
        self.after(50, self.poll_scan_events)

    def scan_worker(self, addresses, port, timeout):
        found = []
        scanned = 0
        max_workers = min(128, max(8, len(addresses)))
        try:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(check_agent_health, str(ip), port, timeout) for ip in addresses]
                for future in as_completed(futures):
                    host, ok, payload = future.result()
                    scanned += 1
                    if ok:
                        found.append(host)
                        agent = payload.get("agent", "Agent")
                        self.scan_event_queue.put(("found", host, agent))
                    if scanned % 32 == 0 or scanned == len(addresses):
                        self.scan_event_queue.put(("progress", scanned, len(addresses), len(found)))
        except Exception as exc:
            self.scan_event_queue.put(("error", str(exc)))
            return
        self.scan_event_queue.put(("finished", found, len(addresses)))

    def poll_scan_events(self):
        finished = False
        while True:
            try:
                event = self.scan_event_queue.get_nowait()
            except queue.Empty:
                break

            event_type = event[0]
            if event_type == "found":
                self.on_agent_found(event[1], event[2])
            elif event_type == "progress":
                self.summary_var.set(f"扫描中 {event[1]}/{event[2]}，发现 {event[3]} 台")
            elif event_type == "finished":
                self.finish_scan(event[1], event[2])
                finished = True
            elif event_type == "error":
                self.scan_btn.configure(state=tk.NORMAL)
                self.summary_var.set("扫描失败")
                self.append_result(f"[扫描失败] {event[1]}")
                messagebox.showerror("扫描失败", event[1])
                finished = True

        if not finished:
            self.after(50, self.poll_scan_events)

    def on_agent_found(self, host, agent):
        if host not in self.discovered_hosts:
            self.discovered_hosts.append(host)
        status = "已在主机列表" if host in self.host_selected else "待添加"
        self.append_result(f"[发现] {host} {agent}（{status}）")

    def finish_scan(self, found, total):
        self.scan_btn.configure(state=tk.NORMAL)
        self.discovered_hosts = list(dict.fromkeys(found))
        self.summary_var.set(f"扫描完成，共扫描 {total} 个地址，发现 {len(found)} 台")
        if found:
            messagebox.showinfo("扫描完成", f"发现 {len(found)} 台被控端。确认后可点击“添加扫描结果”加入主机列表。")
        else:
            messagebox.showinfo("扫描完成", "没有发现被控端，请检查网段、端口、防火墙和虚拟机网络模式。")

    def current_host(self):
        selection = self.host_tree.selection()
        return selection[0] if selection else ""

    def on_host_click(self, event):
        row_id = self.host_tree.identify_row(event.y)
        if not row_id:
            return
        self.host_tree.selection_set(row_id)
        if self.host_tree.identify_column(event.x) == "#1":
            self.host_selected[row_id] = not self.host_selected.get(row_id, False)
            self.refresh_host_row(row_id)

    def toggle_selected_host(self):
        host = self.current_host()
        if not host:
            return
        self.host_selected[host] = not self.host_selected.get(host, False)
        self.refresh_host_row(host)

    def select_all_hosts(self):
        for host in list(self.host_selected):
            self.host_selected[host] = True
            self.refresh_host_row(host)
        self.summary_var.set(f"已全选 {len(self.host_selected)} 台主机")

    def invert_hosts(self):
        for host in list(self.host_selected):
            self.host_selected[host] = not self.host_selected.get(host, False)
            self.refresh_host_row(host)
        self.summary_var.set("已反选")

    def delete_selected_host(self):
        host = self.current_host()
        if not host:
            messagebox.showwarning("未选择主机", "请先在列表中选中一台主机。")
            return
        self.host_tree.delete(host)
        self.host_selected.pop(host, None)
        self.host_credentials.pop(host, None)
        self.riot_username_var.set("")
        self.riot_password_var.set("")
        self.save_hosts(show_status=False)
        self.summary_var.set(f"已删除主机：{host}")

    def all_hosts(self):
        return list(self.host_selected.keys())

    def selected_hosts(self):
        return [host for host, selected in self.host_selected.items() if selected]

    def save_hosts(self, show_status=True):
        hosts = self.all_hosts()
        HOSTS_PATH.write_text("\n".join(hosts) + ("\n" if hosts else ""), encoding="utf-8")
        if show_status:
            self.summary_var.set(f"已保存 {len(hosts)} 台主机")

    def append_result(self, text):
        self.result_text.insert(tk.END, text + "\n")
        self.result_text.see(tk.END)

    def run_task(self):
        hosts = self.selected_hosts()
        if not hosts:
            messagebox.showwarning("未勾选主机", "请至少勾选一台被控主机。")
            return
        if not self.token_var.get():
            messagebox.showwarning("缺少 Token", "请输入 Token。")
            return

        filename = self.file_name_var.get().strip()
        if not filename:
            messagebox.showwarning("缺少文件名称", "请输入文件名称，例如 README.md。")
            return

        try:
            interval = max(0, float(self.interval_var.get()))
            timeout = max(5, int(self.timeout_var.get()))
        except ValueError:
            messagebox.showwarning("参数错误", "启动间隔和超时必须是数字。")
            return

        self.save_hosts(show_status=False)
        self.run_btn.configure(state=tk.DISABLED)
        self.riot_login_btn.configure(state=tk.DISABLED)
        self.result_text.delete("1.0", tk.END)
        self.summary_var.set(f"正在逐台执行，共 {len(hosts)} 台...")
        thread = threading.Thread(
            target=self.run_worker,
            args=(hosts, self.token_var.get(), filename, interval, timeout),
            daemon=True,
        )
        thread.start()

    def run_riot_login_task(self):
        hosts = self.selected_hosts()
        if not hosts:
            messagebox.showwarning("未勾选主机", "请至少勾选一台被控主机。")
            return
        if not self.token_var.get():
            messagebox.showwarning("缺少 Token", "请输入 Token。")
            return

        missing = [host for host in hosts if not self.host_credentials.get(host, {}).get("password")]
        if missing:
            messagebox.showwarning(
                "缺少 Riot 账号",
                "请先为这些主机手动填写并应用 Riot 账号和密码：\n" + "\n".join(missing[:10]),
            )
            return

        try:
            interval = max(0, float(self.interval_var.get()))
            timeout = max(5, int(self.timeout_var.get()))
        except ValueError:
            messagebox.showwarning("参数错误", "启动间隔和超时必须是数字。")
            return

        credentials = {
            host: {
                "username": self.host_credentials[host]["username"],
                "password": self.host_credentials[host]["password"],
            }
            for host in hosts
        }
        self.save_hosts(show_status=False)
        self.run_btn.configure(state=tk.DISABLED)
        self.riot_login_btn.configure(state=tk.DISABLED)
        self.result_text.delete("1.0", tk.END)
        self.summary_var.set(f"正在逐台启动并登录 Riot，共 {len(hosts)} 台...")
        thread = threading.Thread(
            target=self.riot_login_worker,
            args=(hosts, self.token_var.get(), credentials, interval, timeout),
            daemon=True,
        )
        thread.start()

    def riot_login_worker(self, hosts, token, credentials, interval, timeout):
        results = []
        total = len(hosts)
        for index, host in enumerate(hosts, start=1):
            self.after(0, self.append_result, f"正在执行第 {index}/{total} 台：{host}")
            try:
                account = credentials[host]
                host, status, payload = run_riot_login_on_host(
                    host,
                    token,
                    account["username"],
                    account["password"],
                    timeout,
                )
            except Exception as exc:
                status = 0
                payload = {"ok": False, "error": f"主控端异常: {exc}"}
            results.append((host, status, payload))
            self.after(0, self.render_one_result, host, status, payload)
            if index < total and interval > 0:
                self.after(0, self.append_result, f"等待 {interval:g} 秒后执行下一台...\n")
                time.sleep(interval)
        ok_count = sum(1 for _host, _status, payload in results if payload.get("ok"))
        self.after(0, self.finish_riot_login, results, ok_count)

    def run_worker(self, hosts, token, filename, interval, timeout):
        results = []
        total = len(hosts)
        for index, host in enumerate(hosts, start=1):
            self.after(0, self.append_result, f"正在执行第 {index}/{total} 台：{host}")
            try:
                host, status, payload = run_on_host(host, token, "open_file_search", [filename], timeout)
            except Exception as exc:
                status = 0
                payload = {"ok": False, "error": f"主控端异常: {exc}"}
            results.append((host, status, payload))
            self.after(0, self.render_one_result, host, status, payload)
            if index < total and interval > 0:
                self.after(0, self.append_result, f"等待 {interval:g} 秒后执行下一台...\n")
                time.sleep(interval)
        ok_count = sum(1 for _host, _status, payload in results if self.is_confirmed_open(payload))
        self.after(0, self.finish_run, results, ok_count)

    def is_confirmed_open(self, payload):
        if "confirmed_open" in payload:
            return bool(payload.get("confirmed_open"))
        return bool(payload.get("ok"))

    def format_result_block(self, host, status, payload):
        ok = "OK" if self.is_confirmed_open(payload) else "FAIL"
        lines = [f"[{ok}] {host} HTTP={status}"]
        if payload.get("launch_state"):
            lines.append(f"启动状态: {payload['launch_state']}")
        if payload.get("login_state"):
            lines.append(f"登录状态: {payload['login_state']}")
        if payload.get("attempts"):
            lines.append(f"尝试次数: {payload['attempts']}")
        if payload.get("window_stable_seconds"):
            lines.append(f"窗口稳定等待: {payload['window_stable_seconds']:g} 秒")
        if payload.get("window_handle"):
            lines.append(f"窗口句柄: {payload['window_handle']}")
        if payload.get("window_title"):
            lines.append(f"窗口标题: {payload['window_title']}")
        if payload.get("window_class"):
            lines.append(f"窗口类名: {payload['window_class']}")
        if payload.get("foreground_handle"):
            lines.append(f"失败时前台句柄: {payload['foreground_handle']}")
        if payload.get("foreground_title"):
            lines.append(f"失败时前台标题: {payload['foreground_title']}")
        if payload.get("foreground_class"):
            lines.append(f"失败时前台类名: {payload['foreground_class']}")
        if payload.get("foreground_process"):
            lines.append(f"失败时前台程序: {payload['foreground_process']}")
        if payload.get("riot_executable"):
            lines.append(f"Riot程序: {payload['riot_executable']}")
        if payload.get("cmd"):
            lines.append(f"执行命令: {payload['cmd']}")
        if payload.get("cmd_script"):
            lines.append(f"CMD脚本: {payload['cmd_script']}")
        if payload.get("target_process_names"):
            lines.append("目标进程: " + ", ".join(payload["target_process_names"]))
        if payload.get("shortcut_target"):
            lines.append(f"快捷方式 TargetPath: {payload['shortcut_target']}")
        if payload.get("shortcut_arguments"):
            lines.append(f"快捷方式参数: {payload['shortcut_arguments']}")
        if payload.get("target_candidates"):
            lines.append("快捷方式目标: " + " | ".join(payload["target_candidates"]))
        if payload.get("shortcut_error"):
            lines.append(f"快捷方式解析错误: {payload['shortcut_error']}")
        if payload.get("stdout"):
            lines.extend(["STDOUT:", payload["stdout"].rstrip()])
        if payload.get("stderr"):
            lines.extend(["STDERR:", payload["stderr"].rstrip()])
        if payload.get("error"):
            lines.append(f"ERROR: {payload['error']}")
        return "\n".join(lines)

    def render_one_result(self, host, status, payload):
        self.append_result(self.format_result_block(host, status, payload))
        self.append_result("")

    def finish_run(self, results, ok_count):
        total = len(results)
        fail_count = total - ok_count
        self.summary_var.set(f"完成 {total} 台，成功 {ok_count} 台，失败 {total - ok_count} 台")
        self.run_btn.configure(state=tk.NORMAL)
        self.riot_login_btn.configure(state=tk.NORMAL)
        if fail_count:
            failed = [
                self.format_result_block(host, status, payload)
                for host, status, payload in results
                if not self.is_confirmed_open(payload)
            ]
            messagebox.showerror("执行失败", "\n\n".join(failed[:5]))
        else:
            messagebox.showinfo("执行完成", f"成功打开 {ok_count} 台。")

    def finish_riot_login(self, results, ok_count):
        total = len(results)
        fail_count = total - ok_count
        self.summary_var.set(f"Riot 登录提交完成 {total} 台，成功 {ok_count} 台，失败 {fail_count} 台")
        self.run_btn.configure(state=tk.NORMAL)
        self.riot_login_btn.configure(state=tk.NORMAL)
        if fail_count:
            failed = [
                self.format_result_block(host, status, payload)
                for host, status, payload in results
                if not payload.get("ok")
            ]
            messagebox.showerror("Riot 登录失败", "\n\n".join(failed[:5]))
        else:
            messagebox.showinfo("Riot 登录已提交", f"已向 {ok_count} 台电脑的 Riot 登录窗口输入账号密码。")


if __name__ == "__main__":
    ControllerApp().mainloop()
