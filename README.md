# Google Antigravity, Antigravity IDE & CLI .deb 自动化打包发布工作流

本项目实现了从官方 API 自动获取 **Antigravity 2.0**（桌面平台）、**Antigravity IDE**（编程编辑器）和 **Antigravity CLI**（命令行终端工具 `agy`）的最新版本。**具备智能版本更新检测机制：仅在上游发布新版本时才执行下载与打包；若版本未更新，则自动跳过所有下载与构建流程。**

---

## 🌟 核心特性

- 🎯 **最新版本智能追踪与增量构建**：
  - 默认仅抓取最新发布（Latest）。
  - **增量检测**：通过比对 `versions.json` 或 GitHub Release 标签版本。
  - **若版本未更新**：立刻退出流程，**不发起任何大文件下载、不解析压缩包**，极大节约带宽与 GitHub Actions 运行额度。
  - **若任一产品更新**：仅对更新的产品拉取新版本、打包并发布 Release。
  - **支持强制构建 (`--force`)**：需要重新打包时可直接绕过版本检查。
- 🔍 **官方多源 API 实时解析**：
  - Antigravity 2.0: `https://antigravity-hub-auto-updater-974169037036.us-central1.run.app/releases`
  - Antigravity IDE: `https://antigravity-ide-auto-updater-974169037036.us-central1.run.app/releases`
  - Antigravity CLI: `https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/`
- 📦 **标准 Debian 规范打包（三者零冲突，支持完全共存）**：
  - **`antigravity`**：应用置于 `/opt/antigravity`，启动命令 `/usr/bin/antigravity`，集成桌面快捷方式与高分图标。
  - **`antigravity-ide`**：应用置于 `/opt/antigravity-ide`，启动命令 `/usr/bin/antigravity-ide`（别名 `agy-ide`）。
  - **`antigravity-cli`**：二进制直接安装至 `/usr/bin/agy`（软链 `antigravity-cli`），全局可用，轻量免额外依赖。
  - 自动修复 Electron `chrome-sandbox` 提权权限（`chmod 4755`）。
  - 完整声明依赖项（`libpango-1.0-0`、`libgtk-3-0`、`libnss3` 等），支持通过 `apt` 自动补齐。
- ⚡ **多架构支持**：
  - 同时支持 `amd64` (x86_64) 与 `arm64` (AArch64)。
- 🚀 **GitHub Actions CI/CD 流水线**：
  - **定时自动检测 (`schedule`)**：每日定时检查官方版本，有更新才触发矩阵**并发下载与打包**（多架构、多产品独立 runner 并发执行）。
  - **手动触发 (`workflow_dispatch`)**：支持勾选 `force_build` 强制重构指定产品或架构。
  - 自动生成 `SHA256SUMS.txt` 校验清单并发布 GitHub Release。

---

## 📂 项目结构

```
.
├── .github/
│   └── workflows/
│       └── build-deb.yml       # GitHub Actions 自动化检测、并发打包与 Release 发布流水线
├── build_deb.py                # 核心解析、更新检测、下载与打包工具（纯 Python 标准库，零外部依赖）
├── versions.json               # 本地/仓库跟踪的已构建版本号记录
├── .gitignore
└── README.md                   # 使用与部署文档
```

---

## 🛠️ 本地运行指南

### 1. 检查最新版本与更新状态（不下载）
```bash
python3 build_deb.py --check-update
```
- 若已是最新版：
  ```
  [*] Checking for updates against recorded versions:
      - antigravity     : upstream=2.12.2   recorded=2.12.2   -> Up to date (2.12.2)
      - antigravity-ide : upstream=2.5.5    recorded=2.5.5    -> Up to date (2.5.5)
  [i] All target products are already up-to-date. No new versions found. Exiting.
  ```
- 若有新版，会自动列出有更新的产品并开始处理。

### 2. 查看所有上游历史版本列表
```bash
python3 build_deb.py --list
```

### 3. 查看当前最新版本的下载 URL（只解析不下载）
```bash
python3 build_deb.py --only-resolve --product both --arch both
```

### 4. 强制重新下载并打包最新版本
```bash
# 无论本地是否已有记录，强制构建 amd64 包至 ./dist
python3 build_deb.py --product both --arch amd64 --force --out-dir ./dist
```

打包成功后会自动更新 `versions.json` 文件。

---

## 🤖 部署到 GitHub Actions

### 1. 推送代码到你的 GitHub 仓库
```bash
cd /tmp/agy
git remote add origin git@github.com:<你的用户名>/<你的仓库名>.git
git push -u origin main
```

### 2. 检查仓库权限
为了让 GitHub Actions 能够自动提交更新后的 `versions.json` 以及发布 Release，请开启写权限：
1. 打开 GitHub 仓库页面 -> **Settings** -> **Actions** -> **General**。
2. 滚动到 **Workflow permissions**，勾选 **Read and write permissions** 并保存。

### 3. 运行效果
- **首次运行 / 手动触发**：
  - 在 **Actions** 页面点击 **Build & Release Antigravity Debian Packages** -> **Run workflow**。
  - 流水线自动解析并构建最新版安装包，发布 Release（如 `v2.12.2-2.5.5`），并将版本号写入 `versions.json` 自动回推到仓库。
- **每日定时运行 (Cron)**：
  - 每天 UTC 02:00 自动运行。
  - 若官方没有发布新版，工作流在第一个任务运行 10 秒后识别到 `No updates found` 并直接结束，**完全不会触发下载与构建**。
  - 若官方发布了新版本（如 Antigravity 升级为 2.12.3），流水线立刻自动并发构建新版并生成新 Release！

---

## 📥 安装生成的 deb 安装包

在 Ubuntu / Debian / Deepin / UOS 等系统中：

```bash
# 使用 apt 本地安装（自动解决依赖）：
sudo apt update
sudo apt install ./antigravity_2.12.2_amd64.deb
sudo apt install ./antigravity-ide_2.5.5_amd64.deb
sudo apt install ./antigravity-cli_1.1.27_amd64.deb

# 启动应用：
antigravity       # 启动 Antigravity 2.0 桌面端
antigravity-ide   # 启动 Antigravity IDE (也可以使用别名 agy-ide)
agy               # 启动 Antigravity CLI 命令行终端（也可使用别名 antigravity-cli）
```
