# 局域网文件搜索打开工具

这是一个局域网内的主控/被控工具。被控电脑运行 Agent，主控电脑可以勾选一台或多台被控电脑，按文件名或程序名让被控电脑在本机搜索并打开。

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

## 防火墙

如果主控端连不上被控端，在被控电脑管理员 PowerShell 中放行端口：

```powershell
New-NetFirewallRule -DisplayName "LAN Agent 8765" -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow
```

## 重新打包

修改代码后执行：

```powershell
python build_exe.py
```

打包结果会输出到 `release/`，并可重新压缩为 `release.zip`。
