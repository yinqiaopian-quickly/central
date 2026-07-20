# 局域网文件搜索打开工具

这是一个局域网内的主控/被控工具。被控电脑运行 Agent，主控电脑可以勾选一台或多台被控电脑，按文件名或程序名让被控电脑在本机搜索并打开。

完整操作步骤和故障排查请查看 `使用教程.txt`。

## 文件说明

- `主控端.exe`：运行在主控电脑上。
- `被控端Agent.exe`：运行在被控电脑上。
- `agent_config.json`：Token、搜索范围和允许指令配置。
- `hosts.txt`：主控端保存的被控主机列表。
- `release.zip`：可直接复制分发的打包文件。

## 使用方式

1. 把 `release.zip` 解压到主控电脑和被控电脑。
2. 在每台被控电脑上运行 `被控端Agent.exe`。
3. 被控端监听地址保持 `0.0.0.0`，端口保持 `8765`，点击“启动 Agent”。
4. 在主控电脑上运行 `主控端.exe`。
5. 添加被控主机，例如 `192.168.1.20:8765`。
6. 勾选要操作的主机。
7. 输入文件名称或程序名称，例如 `README.md` 或 `倚天2：觉醒`。
8. 点击“搜索并打开”。

## Riot Client 登录

1. 在主控端主机列表中选中一个 IP。
2. 在“当前主机 Riot 登录账号”中手动填写账号和密码，点击“应用”。
3. 对每个需要操作的 IP 分别设置账号和密码；密码只保存在主控端本次运行的内存中。
4. 勾选需要操作的主机，点击“启动并登录 Riot”。
5. 被控端会启动 Riot Client，捕获登录窗口句柄，并在当前桌面中输入对应账号和密码。

执行登录时，被控电脑必须保持用户桌面已登录且未锁定。遇到验证码或二次验证时需要人工处理。
被控端会等待同一个 Riot Client 窗口句柄连续稳定 5 秒后再输入；期间句柄变化会重新计时。如果服务存在但登录窗口已退出，被控端会自动重启 Riot Client 并重新捕获稳定窗口，最多尝试 3 次。

## 搜索范围

默认会在被控电脑的这些位置搜索：

```text
程序所在目录
当前用户桌面
当前用户文档
当前用户下载
公共桌面
当前用户开始菜单
所有用户开始菜单
Program Files
Program Files (x86)
```

如果需要扩大搜索范围，修改被控电脑的 `agent_config.json`：

```json
"file_search_roots": [
  ".",
  "C:\\",
  "D:\\"
]
```

修改后需要重启 `被控端Agent.exe`。

## 安全说明

主控端不能直接传完整路径执行任意程序，只能传文件名或程序名。被控端只会在 `agent_config.json` 配置的搜索范围内查找并打开匹配项。

首次使用建议把 `agent_config.json` 里的 `token` 改成随机长字符串，并确保主控端和被控端使用同一个 Token。

Riot 登录请求使用 Token 派生的 AES-GCM 密钥加密，并通过 HMAC 校验。账号密码不会写入 `hosts.txt`，也不会输出到运行日志。请勿继续使用默认 Token。

## 防火墙

如果主控端连不上被控端，在被控电脑管理员 PowerShell 中放行端口：

```powershell
New-NetFirewallRule -DisplayName "LAN Agent 8765" -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow
```

## 重新打包

修改代码后执行：

```powershell
python -m pip install -r requirements.txt
python build_exe.py
```

打包结果会输出到 `release/`，并可重新压缩为 `release.zip`。
