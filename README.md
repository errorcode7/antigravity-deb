# Google Antigravity & Antigravity IDE .deb 自动化打包发布工作流

本项目实现了从 [https://antigravity.google/releases](https://antigravity.google/releases) 自动解析 **Antigravity 2.0** 与 **Antigravity IDE** 的 Linux 版本压缩包下载地址、自动化下载、重构并打包为标准 Debian (`.deb`) 安装包，并通过 **GitHub Actions** 进行矩阵构建与自动发布 Release。

---

## 🌟 核心特性

- 🔍 **双源解析策略**：
  - 首选调用官方 Cloud Run Auto-Updater API 端点：
    - Antigravity 2.0: `https://antigravity-hub-auto-updater-974169037036.us-central1.run.app/releases`
    - Antigravity IDE: `https://antigravity-ide-auto-updater-974169037036.us-central1.run.app/releases`
  - 备用解析 `https://antigravity.google/releases` 页面内置的 `data-static-versions` / `data-fallback-ide` 属性与 Astro 前端脚本。
- 📦 **标准 Debian 规范打包**：
  - 应用目录置于 `/opt/antigravity` 与 `/opt/antigravity-ide`。
  - 命令行入口脚本安装至 `/usr/bin/antigravity`、`/usr/bin/antigravity-ide`（支持快捷别名 `agy-ide`）。
  - 自动集成桌面启动器文件（`.desktop`）与高分辨率应用图标（`/usr/share/pixmaps`、`/usr/share/icons/hicolor/...`）。
  - 自动设置 Electron `chrome-sandbox` 的 SUID 权限（`chmod 4755`），避免非 root 用户运行崩溃。
  - 规范声明依赖项（`libgtk-3-0`、`libnss3`、`libasound2 | libasound2t64` 等），支持通过 `apt` 自动补齐依赖。
  - 包含 `postinst` 与 `postrm` 维护脚本，安装/卸载时自动更新系统图标与桌面缓存。
- ⚡ **多架构支持**：
  - 同时支持 `amd64` (x86_64) 与 `arm64` (AArch64)。
- 🚀 **GitHub Actions CI/CD 流水线**：
  - 支持 **手动触发（workflow_dispatch）**：可自定义选择打包产品、架构、指定版本或最新版、是否创建 GitHub Release。
  - 支持 **定时检测（schedule）**：每日定时检查官方是否有新版本，检测到更新时自动打包发布。
  - **多任务并行矩阵构建**：不同产品和架构在独立的 GitHub Runner 上并发下载和打包。
  - 自动生成 `SHA256SUMS.txt` 校验文件与美观的 Markdown 发版日志。

---

## 📂 项目结构

```
.
├── .github/
│   └── workflows/
│       └── build-deb.yml       # GitHub Actions 自动化打包与发布工作流
├── build_deb.py                # 核心解析、下载、重构与打包工具（纯 Python 标准库，零外部依赖）
├── .gitignore
└── README.md                   # 使用与部署文档
```

---

## 🛠️ 本地运行指南

`build_deb.py` 采用纯 Python 3 标准库编写，环境只需安装系统自带的 `dpkg-deb` 即可。

### 1. 查看上游可用版本
```bash
python3 build_deb.py --list
```

### 2. 查看解析出的下载地址（不下载）
```bash
python3 build_deb.py --only-resolve --product both --arch both
```

输出示例：
```
============================================================
Resolving antigravity (target version: latest)...
Resolved: Version 2.12.2, Execution ID 6298742303883264

--- Target: antigravity 2.12.2 (amd64) ---
Archive URL: https://storage.googleapis.com/antigravity-public/antigravity-hub/2.12.2-6298742303883264/linux-x64/Antigravity.tar.gz

--- Target: antigravity 2.12.2 (arm64) ---
Archive URL: https://storage.googleapis.com/antigravity-public/antigravity-hub/2.12.2-6298742303883264/linux-arm/Antigravity.tar.gz

============================================================
Resolving antigravity-ide (target version: latest)...
Resolved: Version 2.5.5, Execution ID 4923483625488384

--- Target: antigravity-ide 2.5.5 (amd64) ---
Archive URL: https://edgedl.me.gvt1.com/edgedl/release2/j0qc3/antigravity/stable/2.5.5-4923483625488384/linux-x64/Antigravity%20IDE.tar.gz

--- Target: antigravity-ide 2.5.5 (arm64) ---
Archive URL: https://edgedl.me.gvt1.com/edgedl/release2/j0qc3/antigravity/stable/2.5.5-4923483625488384/linux-arm/Antigravity%20IDE.tar.gz
```

### 3. 一键下载并构建指定架构的 deb 包
```bash
# 构建两者 x64 最新版本并输出至 ./dist 目录
python3 build_deb.py --product both --arch amd64 --out-dir ./dist

# 或仅构建 Antigravity 2.0
python3 build_deb.py --product antigravity --arch amd64 --out-dir ./dist

# 或仅构建 Antigravity IDE
python3 build_deb.py --product antigravity-ide --arch amd64 --out-dir ./dist
```

---

## 🤖 部署到 GitHub Actions

### 1. 推送代码到你的 GitHub 仓库
```bash
git init
git add .
git commit -m "feat: add Antigravity deb repackage pipeline & GitHub Actions workflow"
git remote add origin git@github.com:<your-user>/<your-repo>.git
git branch -M main
git push -u origin main
```

### 2. 检查仓库权限
为了让 GitHub Actions 能够发布 Release 并上传 deb 文件，请确保仓库配置了写权限：
1. 访问仓库的 **Settings** -> **Actions** -> **General**。
2. 找到 **Workflow permissions**，勾选 **Read and write permissions** 并保存。

### 3. 手动触发构建
1. 在 GitHub 仓库页面点击 **Actions** 标签页。
2. 在左侧选择 **Build & Release Antigravity Debian Packages** 工作流。
3. 点击右侧 **Run workflow** 下拉菜单：
   - **Product to package**: 选择 `both`、`antigravity` 或 `antigravity-ide`。
   - **Target architecture**: 选择 `both`、`amd64` 或 `arm64`。
   - **Antigravity 2.0 version**: 填入特定版本（如 `2.12.2`）或保持 `latest`。
   - **Antigravity IDE version**: 填入特定版本（如 `2.5.5`）或保持 `latest`。
   - **Publish as GitHub Release**: 勾选以发布正式 Release。
4. 点击绿色 **Run workflow** 按钮启动构建。

### 4. 获取输出产物
构建成功后：
- 在 **Releases** 页面中将自动创建对应的 Release 标签（例如 `v2.12.2-2.5.5`），包含：
  - `antigravity_2.12.2_amd64.deb`
  - `antigravity_2.12.2_arm64.deb`
  - `antigravity-ide_2.5.5_amd64.deb`
  - `antigravity-ide_2.5.5_arm64.deb`
  - `SHA256SUMS.txt` 校验文件
- 在 Action 运行记录的 **Artifacts** 区域也可以直接下载生成的 deb 安装包。

---

## 📥 安装与使用 deb 软件包

在 Ubuntu / Debian / Deepin / UOS 等 Linux 系统中：

```bash
# 推荐使用 apt 安装（可自动解析并安装缺少的系统依赖）：
sudo apt update
sudo apt install ./antigravity_2.12.2_amd64.deb
sudo apt install ./antigravity-ide_2.5.5_amd64.deb

# 或者使用 dpkg 安装（若提示缺少依赖，可运行 sudo apt install -f 修复）：
sudo dpkg -i antigravity_2.12.2_amd64.deb
sudo apt install -f -y
```

### 启动应用
- **图形界面**：在桌面应用菜单搜索 `Antigravity` 或 `Antigravity IDE` 点击启动。
- **命令行**：
  - 启动 Antigravity 2.0：直接在终端输入 `antigravity`。
  - 启动 Antigravity IDE：在终端输入 `antigravity-ide` 或 `agy-ide`，支持传参（例如 `antigravity-ide /path/to/project`）。
