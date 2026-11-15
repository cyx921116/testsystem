# Git 与 GitHub 配置指南

本文以本项目所在的 Windows + Git Bash 环境为例，说明从 Git 初始配置到 GitHub 推送的完整过程。

## 1. 当前环境检查结果

本项目开始配置时的状态如下：

- 已安装 Git：`git version 2.55.0.windows.2`。
- 已安装 Git Credential Manager，可用于保存 GitHub HTTPS 登录凭据。
- 当前项目目录尚未执行 `git init`。
- 尚未配置全局 `user.name`、`user.email` 和 `init.defaultBranch`。
- 尚未安装 GitHub CLI（`gh`）。
- 系统存在 `winget`，可用于安装 GitHub CLI。

Git 提交姓名和邮箱会写入每一条提交记录。不要使用别人的身份，也不要随便填写一个不存在的邮箱。

## 2. 配置 Git 提交身份

### 2.1 全局配置

如果这台电脑主要使用一个 GitHub 账号，可以执行：

```bash
git config --global user.name "你的提交姓名"
git config --global user.email "你的提交邮箱"
git config --global init.defaultBranch main
git config --global core.autocrlf true
```

示例仅演示格式，请不要原样使用示例身份：

```bash
git config --global user.name "zhangsan"
git config --global user.email "zhangsan@example.com"
```

`core.autocrlf true` 是 Windows 常见配置：提交时把文本文件转换为 LF，检出时根据配置转换。项目同时提供 `.gitattributes`，强制 Python、JavaScript、CSS、HTML、Markdown 等源码以 LF 保存，减少跨平台差异。

### 2.2 使用 GitHub noreply 邮箱

如果不想公开私人邮箱，可以在 GitHub 网页进入：

`Settings → Emails → Keep my email addresses private`

然后使用 GitHub 页面显示的 noreply 地址。常见形式为：

```text
ID+用户名@users.noreply.github.com
```

旧账号也可能显示：

```text
用户名@users.noreply.github.com
```

以你自己的 GitHub 页面为准。

### 2.3 只配置当前项目

如果全局已有其他身份，只希望本项目使用 GitHub 身份，在项目根目录执行不带 `--global` 的命令：

```bash
git config user.name "你的提交姓名"
git config user.email "你的提交邮箱"
```

项目级配置保存在 `.git/config`，优先级高于全局配置，不会上传，也不会影响其他项目。

### 2.4 检查配置

```bash
git config --get user.name
git config --get user.email
git config --global --get user.name
git config --global --get user.email
```

注意：`git config --get` 在没有配置时会返回退出码 1，这表示“未找到配置”，不是 Git 安装损坏。

## 3. 选择 GitHub 登录方式

推荐使用 HTTPS + GitHub CLI 或 Git Credential Manager。GitHub 已不接受账号密码直接执行 Git 推送。

### 3.1 方式 A：GitHub CLI（推荐）

安装：

```bash
winget install --id GitHub.cli --exact --source winget
```

安装后关闭并重新打开终端，检查：

```bash
gh --version
```

登录：

```bash
gh auth login --hostname github.com --git-protocol https --web
```

终端会显示一次性验证码并打开浏览器。在浏览器确认登录和授权后，回到终端检查：

```bash
gh auth status
```

让 Git 使用 gh 提供的凭据：

```bash
gh auth setup-git
```

此后 `git push`、`git fetch` 等 HTTPS 操作可以复用登录状态。

如果终端找不到刚安装的 `gh`，先检查默认安装位置是否存在于 PATH：

```text
C:\Program Files\GitHub CLI\gh.exe
```

不要重复安装多个版本；优先修正终端 PATH 或重新打开终端。

### 3.2 方式 B：Git Credential Manager

Git for Windows 通常已经包含 Git Credential Manager。检查：

```bash
git credential-manager --version
git config --get credential.helper
```

第一次执行 `git push` 时，凭据管理器通常会弹出浏览器登录窗口。完成 OAuth 授权后，凭据保存在 Windows 凭据管理器中。

如果 GitHub 要求 Personal Access Token，应在 GitHub 网页创建具有目标仓库 `repo` 权限的 token，并在凭据管理器提示密码时输入 token，而不是 GitHub 账号密码。

安全要求：

- 不要把 token 写进源码、README、脚本、Git 远程地址或聊天记录。
- 不要执行 `https://用户名:token@github.com/...` 形式的远程配置。
- token 泄露后应立即在 GitHub 撤销并重新生成。

### 3.3 方式 C：SSH

如果你更熟悉 SSH，可以生成专用密钥：

```bash
ssh-keygen -t ed25519 -C "你的GitHub邮箱"
```

把生成的**公钥**内容添加到 GitHub：

`Settings → SSH and GPG keys → New SSH key`

公钥一般是 `~/.ssh/id_ed25519.pub`。私钥 `~/.ssh/id_ed25519` 绝不能上传或发给他人。

启动代理并添加密钥：

```bash
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519
ssh -T git@github.com
```

SSH 远程地址格式为：

```text
git@github.com:用户名/仓库名.git
```

如果公司网络封锁 SSH 22 端口，可改用 HTTPS，或为 GitHub 配置 SSH over 443。

## 4. 初始化本地仓库

在项目根目录 `D:\project_demo\Testsystem` 的 Git Bash 中：

```bash
git init -b main
git status
```

旧版 Git 不支持 `git init -b main` 时：

```bash
git init
git branch -M main
```

`.gitignore` 已排除：

- `.venv/`：Python 虚拟环境，不上传。
- `.idea/`、`.vscode/`：编辑器本地配置。
- `data/`、`*.db*`：生产数据库及 SQLite 临时文件。
- `.env`、`.env.*`：密钥和环境配置。
- `__pycache__/`、`*.py[cod]`：Python 缓存。
- `dist/`：构建产物。
- 日志、系统缩略图等本地文件。

应上传的依赖锁文件是 `uv.lock`，它能帮助其他电脑还原相同依赖版本。

提交前检查：

```bash
git status --short
git add .gitignore .gitattributes pyproject.toml uv.lock .python-version README.md docs src
git status --short
```

确认列表中没有数据库、虚拟环境、token 或生产 SN 数据后再提交：

```bash
git commit -m "feat: 初始化生产测试系统"
```

查看提交：

```bash
git log --oneline --decorate -5
```

## 5. 创建 GitHub 仓库并推送

### 5.1 使用 gh 新建私有仓库

```bash
gh repo create Testsystem --private --source=. --remote=origin --push
```

新建公开仓库：

```bash
gh repo create Testsystem --public --source=. --remote=origin --push
```

该命令会依次创建远程仓库、设置 `origin` 并推送当前分支。执行前必须已经存在本地提交。

如果只想创建仓库和远程地址，稍后手动推送：

```bash
gh repo create Testsystem --private --source=. --remote=origin
git push -u origin main
```

### 5.2 在 GitHub 网页新建仓库

1. 打开 <https://github.com/new>。
2. 填写仓库名，例如 `Testsystem`。
3. 选择 Private 或 Public。
4. 如果本地已有提交，不要勾选“Add a README file”。
5. 不要同时选择网页提供的 `.gitignore` 或 License，以免产生无关的初始提交。
6. 创建后复制 HTTPS 地址。

在本地执行：

```bash
git remote add origin https://github.com/你的用户名/Testsystem.git
git branch -M main
git push -u origin main
```

如果远程地址配置错误：

```bash
git remote set-url origin https://github.com/你的用户名/正确仓库名.git
```

### 5.3 上传到已有空仓库

已有空仓库同样只需设置远程地址并推送：

```bash
git remote add origin https://github.com/你的用户名/已有仓库名.git
git push -u origin main
```

如果本地已经存在 `origin`，使用 `git remote set-url`，不要重复 `add`。

### 5.4 远程仓库已经有提交

如果网页仓库已有 README、License 或其他提交，推送可能被拒绝。不要直接使用 `git push --force` 覆盖。

推荐先确认远程内容：

```bash
git fetch origin
git log --oneline --graph --all --decorate
```

如果确实需要合并两个无共同历史的项目：

```bash
git pull --rebase origin main --allow-unrelated-histories
```

解决可能的冲突并验证项目后再推送。若远程有重要内容，先备份或新建分支。

## 6. 推送后的验证

```bash
git status
git remote -v
git branch -vv
git log --oneline --decorate -3
```

正常状态应满足：

- 工作区显示 `nothing to commit, working tree clean`。
- `origin` 指向你的 GitHub 仓库。
- 本地 `main` 跟踪 `origin/main`。
- GitHub 网页能看到 `src`、`docs`、`pyproject.toml`、`uv.lock` 等文件。
- GitHub 网页看不到 `.venv`、`data`、`.idea` 和本地数据库。

也可以在另一目录验证克隆：

```bash
git clone https://github.com/你的用户名/Testsystem.git
cd Testsystem
uv sync
uv run testsystem
```

## 7. 日常提交和更新

```bash
git status
git add -p
git commit -m "fix: 修正串口接收超时处理"
git push
```

`git add -p` 会分块展示修改，适合提交前审查。简单修改也可以使用 `git add 文件名`。

拉取远程更新：

```bash
git pull --rebase origin main
```

本地有未提交修改时，先提交或明确使用 stash，避免拉取过程覆盖工作：

```bash
git stash push -m "尚未完成的修改"
git pull --rebase origin main
git stash pop
```

## 8. 常见问题

### 8.1 `Author identity unknown`

缺少提交身份：

```bash
git config user.name "你的提交姓名"
git config user.email "你的提交邮箱"
```

然后重新执行 `git commit`。

### 8.2 `Authentication failed`

依次检查：

```bash
gh auth status
git config --get credential.helper
git remote -v
```

如果使用 gh，可重新执行 `gh auth login` 和 `gh auth setup-git`。如果 Windows 保存了错误账号，可在“Windows 凭据管理器 → Windows 凭据”中删除对应的 GitHub 凭据，再重新登录。

### 8.3 `Repository not found`

常见原因：

- 仓库名或用户名拼写错误。
- 当前登录账号没有私有仓库权限。
- 远程地址指向了其他账号。
- token 没有目标仓库权限。

### 8.4 `non-fast-forward` 或 `fetch first`

远程有本地没有的新提交。先执行：

```bash
git pull --rebase origin main
```

不要为图方便直接强推，强推可能覆盖协作者提交。

### 8.5 数据库被意外加入暂存区

如果尚未提交：

```bash
git restore --staged data/testsystem.db
```

然后把 `data/` 保留在 `.gitignore` 中。如果数据库或密钥已经进入历史提交，仅删除文件不足以清除历史，应立即轮换密钥，并使用专门的历史清理流程。

## 9. 本项目上传边界

推荐上传：源码、静态页面、项目文档、依赖声明和锁文件。

不推荐上传：

- 真实产品 SN 和测试报告。
- MES 地址中包含的内网域名、账号、密码或 token。
- MQTT 密码、硬件许可证、J-Link 安装目录。
- SQLite 数据库、虚拟环境和编辑器缓存。

演示配置中的 `127.0.0.1`、示例 SN 和仿真数据可以保留。现场部署参数应通过运行时数据库或受控环境变量配置。
