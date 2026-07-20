import json
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from controller import BASE_DIR, run_on_host


HOSTS_PATH = BASE_DIR / "hosts.txt"
AGENT_CONFIG_PATH = BASE_DIR / "agent_config.json"
APP_VERSION = "2026.07.20-4"


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


class ControllerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"主控端 {APP_VERSION}")
        self.geometry("940x640")
        self.minsize(820, 560)

        self.token_var = tk.StringVar()
        self.file_name_var = tk.StringVar()
        self.host_input_var = tk.StringVar()
        self.interval_var = tk.StringVar(value="5")
        self.timeout_var = tk.StringVar(value="180")
        self.summary_var = tk.StringVar(value="等待执行")
        self.host_selected = {}

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
        ttk.Label(left, text="只填写文件名或程序名，例如 README.md、倚天2：觉醒").pack(anchor=tk.W, pady=(0, 10))

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

        ttk.Label(left, text="勾选要操作的主机，单击或按空格切换").pack(anchor=tk.W)
        table_wrap = ttk.Frame(left)
        table_wrap.pack(fill=tk.BOTH, expand=True, pady=(4, 8))
        self.host_tree = ttk.Treeview(
            table_wrap,
            columns=("selected", "host"),
            show="headings",
            selectmode="browse",
            height=10,
        )
        self.host_tree.heading("selected", text="选择")
        self.host_tree.heading("host", text="主机")
        self.host_tree.column("selected", width=54, anchor=tk.CENTER, stretch=False)
        self.host_tree.column("host", width=250, anchor=tk.W)
        self.host_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(table_wrap, orient=tk.VERTICAL, command=self.host_tree.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.host_tree.configure(yscrollcommand=scrollbar.set)
        self.host_tree.bind("<ButtonRelease-1>", self.on_host_click)
        self.host_tree.bind("<space>", lambda _event: self.toggle_selected_host())

        host_buttons = ttk.Frame(left)
        host_buttons.pack(fill=tk.X)
        ttk.Button(host_buttons, text="全选", command=self.select_all_hosts).pack(side=tk.LEFT)
        ttk.Button(host_buttons, text="反选", command=self.invert_hosts).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(host_buttons, text="删除", command=self.delete_selected_host).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(host_buttons, text="保存", command=self.save_hosts).pack(side=tk.LEFT, padx=(8, 0))
        self.run_btn = ttk.Button(host_buttons, text="逐台搜索并打开", command=self.run_task)
        self.run_btn.pack(side=tk.RIGHT)

        ttk.Label(right, textvariable=self.summary_var).pack(anchor=tk.W)
        self.result_text = tk.Text(right, wrap=tk.WORD)
        self.result_text.pack(fill=tk.BOTH, expand=True, pady=(6, 0))

    def load_state(self):
        config = read_json(AGENT_CONFIG_PATH, {})
        self.token_var.set(config.get("token", ""))
        self.set_hosts(read_hosts(), selected=True)
        self.summary_var.set("配置已加载")

    def set_hosts(self, hosts, selected):
        self.host_tree.delete(*self.host_tree.get_children())
        self.host_selected = {}
        for host in hosts:
            self.host_selected[host] = selected
            self.host_tree.insert("", tk.END, iid=host, values=(self.selected_text(host), host))

    def selected_text(self, host):
        return "是" if self.host_selected.get(host) else "否"

    def refresh_host_row(self, host):
        if self.host_tree.exists(host):
            self.host_tree.item(host, values=(self.selected_text(host), host))

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
            self.host_tree.insert("", tk.END, iid=host, values=(self.selected_text(host), host))
            self.summary_var.set(f"已添加主机：{host}")
        self.host_input_var.set("")
        self.save_hosts(show_status=False)

    def current_host(self):
        selection = self.host_tree.selection()
        return selection[0] if selection else ""

    def on_host_click(self, event):
        row_id = self.host_tree.identify_row(event.y)
        if not row_id:
            return
        self.host_tree.selection_set(row_id)
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
        self.result_text.delete("1.0", tk.END)
        self.summary_var.set(f"正在逐台执行，共 {len(hosts)} 台...")
        thread = threading.Thread(
            target=self.run_worker,
            args=(hosts, self.token_var.get(), filename, interval, timeout),
            daemon=True,
        )
        thread.start()

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
        if payload.get("target_process_names"):
            lines.append("目标进程: " + ", ".join(payload["target_process_names"]))
        if payload.get("target_candidates"):
            lines.append("快捷方式目标: " + " | ".join(payload["target_candidates"]))
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
        if fail_count:
            failed = [
                self.format_result_block(host, status, payload)
                for host, status, payload in results
                if not self.is_confirmed_open(payload)
            ]
            messagebox.showerror("执行失败", "\n\n".join(failed[:5]))
        else:
            messagebox.showinfo("执行完成", f"成功打开 {ok_count} 台。")


if __name__ == "__main__":
    ControllerApp().mainloop()
