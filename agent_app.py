import queue
import threading
import tkinter as tk
from http.server import ThreadingHTTPServer
from tkinter import messagebox, ttk

from agent import AgentHandler, CONFIG_PATH


class AgentApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("被控端 Agent")
        self.geometry("620x430")
        self.minsize(560, 380)

        self.server = None
        self.server_thread = None
        self.log_queue = queue.Queue()

        self.host_var = tk.StringVar(value="0.0.0.0")
        self.port_var = tk.StringVar(value="8765")
        self.status_var = tk.StringVar(value="未启动")

        self.create_widgets()
        self.after(150, self.flush_logs)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def create_widgets(self):
        root = ttk.Frame(self, padding=16)
        root.pack(fill=tk.BOTH, expand=True)

        title = ttk.Label(root, text="被控端 Agent", font=("Microsoft YaHei UI", 16, "bold"))
        title.pack(anchor=tk.W)

        subtitle = ttk.Label(root, text="启动后接收主控端请求，只执行 agent_config.json 中登记的白名单脚本。")
        subtitle.pack(anchor=tk.W, pady=(4, 14))

        form = ttk.Frame(root)
        form.pack(fill=tk.X)

        ttk.Label(form, text="监听地址").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(form, textvariable=self.host_var, width=18).grid(row=1, column=0, sticky=tk.EW, padx=(0, 12))
        ttk.Label(form, text="端口").grid(row=0, column=1, sticky=tk.W)
        ttk.Entry(form, textvariable=self.port_var, width=10).grid(row=1, column=1, sticky=tk.W)
        form.columnconfigure(0, weight=1)

        controls = ttk.Frame(root)
        controls.pack(fill=tk.X, pady=14)
        self.start_btn = ttk.Button(controls, text="启动 Agent", command=self.start_server)
        self.start_btn.pack(side=tk.LEFT)
        self.stop_btn = ttk.Button(controls, text="停止", command=self.stop_server, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=8)
        ttk.Label(controls, textvariable=self.status_var).pack(side=tk.LEFT, padx=12)

        ttk.Label(root, text="运行日志").pack(anchor=tk.W)
        self.log_text = tk.Text(root, height=14, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.log_text.configure(state=tk.DISABLED)

    def append_log(self, text):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, text + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def flush_logs(self):
        while True:
            try:
                message = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.append_log(message)
        self.after(150, self.flush_logs)

    def start_server(self):
        if self.server is not None:
            return
        if not CONFIG_PATH.exists():
            messagebox.showerror("缺少配置", f"找不到配置文件：\n{CONFIG_PATH}")
            return
        try:
            port = int(self.port_var.get())
            host = self.host_var.get().strip() or "0.0.0.0"
            self.server = ThreadingHTTPServer((host, port), AgentHandler)
        except Exception as exc:
            self.server = None
            messagebox.showerror("启动失败", str(exc))
            return

        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.status_var.set(f"运行中：{host}:{port}")
        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self.append_log(f"Agent listening on http://{host}:{port}")

    def stop_server(self):
        if self.server is None:
            return
        self.server.shutdown()
        self.server.server_close()
        self.server = None
        self.status_var.set("已停止")
        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)
        self.append_log("Agent stopped")

    def on_close(self):
        if self.server is not None:
            self.stop_server()
        self.destroy()


if __name__ == "__main__":
    AgentApp().mainloop()

