# 服务器上手指南（零基础版）

> 目标：从下单到「能登录、端口能通、Python 环境就绪」，全程约 40 分钟。  
> 前提：你已在腾讯云轻量购买页，选好中国香港地域 + 入门型 90 元/月（2核4G/70GB SSD）。

---

## 📍 当前进度（2026-09-24 13:40 更新）

| 步骤                | 状态                                                                |
| ----------------- | ----------------------------------------------------------------- |
| 1 下单              | ✅ 实例 `acps-legal-hk`（ID `lhins-cbwm3pl5`），到期 **2026-10-22 21:15** |
| 2 拿到公网 IP         | ✅ **`150.109.154.16`**                                            |
| 3 首次登录            | ✅ 已配置 `ssh acps` 一键登录（密钥认证）                                       |
| 4 登录后体检           | ✅ 2核 / 可用内存 3.1Gi + Swap 1.9Gi / 磁盘剩 61G / Ubuntu 24.04.4 LTS     |
| 5 防火墙放行 8443–8447 | ✅ 5 条规则已生效                                                        |
| 6 验证端口公网可达        | ✅ **8443–8447 全部实测返回 HTTPS 200**                                  |
| 7 测网络质量           | ⚠️ 到 ioa.pub 延迟 **189ms**（绕道德国法兰克福），丢包 0% —— 可用但不快，需多时段复测         |
| 8 装 Python 3.14   | ✅ CPython **3.14.7**（`uv` 安装）                                     |
| 9 上传代码 + 重建 venv  | ✅ `~/witrelate/.venv` 已建，`acps_sdk 2.2.0` + `wit 2.1.0` 可导入      |
| 10 离线自检            | ✅ `python -m legal_acps.check_local` 四项全通过                        |
| 11 平台接口探测          | ✅ 平台 CA 与注册服务均可达（见 `docs/acps-account-and-certs.md`）             |
| 12 模型链路实测          | ✅ 真实 DeepSeek 跑通两阶段研判，输出非空                                    |
| 13 www 侧注册（旧路径）     | ⏸️ 3 个 Agent 已由 wit 提交至 `legal-acps-demo` 名下，长期停在 `pending_review`（**比赛不计此平台**） |
| 14 切到比赛指定平台 wt      | ✅ `.env` 置 `ACPS_BACKEND=wt`；`tools.check_backend` **5/5 通过**，`StudentZhao @ wt` 登录成功 |
| 15 wt 侧 Agent 注册    | ✅ 网页手工注册 3 个，**均 `APPROVED` + 有 AIC**（8443 煽动 / 8444 危害 / 8445 主控，endpoint 正确） |
| 16 注册与证书           | ⬜ **当前步骤：选「wit 新建 3 条」还是「沿用已通过的那 3 条」**                        |
| 17 起服务上线           | ⬜ 拿到证书后 `bash scripts/serve.sh start`                              |

**2026-09-24 本轮修掉的两个坑（都会让注册必然失败）**

1. `acps_config.py` 只改了本地、**没同步到服务器**（服务器还是旧版，无 `WT_PRESET`/`backend()`）。
2. `.env` 里**缺 `ACPS_BACKEND=wt`** → 默认走 `ioa_pub`，等于拿 wt 账号连 www，必报 `USER_NOT_FOUND`。

同时把 `.env` 规范化（同名键去重：靠前的 www 凭据保留为注释，wt 凭据生效），
新增 `tools/env_doctor.py`（体检 `.env`）与 `tools/list_agents.py`（列出名下 Agent）。


**本机已建立的文件**

- `C:\Users\Zhao5\.ssh\acps-hk-key.pem` —— 私钥（ASCII 名副本，配置里用的是这个）
- `C:\Users\Zhao5\.ssh\腾讯云__SSH密钥__acps_hk.pem` —— 私钥原件（保留备份）
- `C:\Users\Zhao5\.ssh\config` —— 内含 `Host acps` 配置

**服务器上已有的目录**

- `~/witrelate/` —— 项目主目录（`.venv` 虚拟环境、`legal_acps/` 代码、`tools/` 诊断脚本、`.env` 配置）
- `~/witrelate/.env` —— 运行配置（含 DeepSeek key，权限已设 600）
- `~/tls-test/` —— 临时 HTTPS 自签测试服务，端口验证完可用 `pkill -f 'openssl s_server'` 停掉

**测得的关键网络数据（用于日后判断是否换线路）**

| 目标                                        | 延迟    | 丢包 | 备注                |
| ----------------------------------------- | ----- | -- | ----------------- |
| [www.ioa.pub（境内）](http://www.ioa.pub（境内）) | 189ms | 0% | 绕道法兰克福，直连本应约 40ms |
| api.deepseek.com                          | 1.2ms | 0% | 接入点在香港本地，极快       |

---

## 开始之前：先搞懂 3 个概念

**1. 你买到的是什么**

一台放在香港机房里的电脑，7×24 开机，有一个独立的公网 IP（比如 `43.128.1.2`）。你不在它面前，所以只能通过网络操作它。它跑的是 Linux（Ubuntu），没有图形界面，全靠敲命令。

**2. 你怎么操作它——SSH**

SSH 就是一个加密的远程命令行。你在自己 Windows 的终端里敲一条 `ssh` 命令（用你的私钥文件认证），你的终端就变成了那台香港服务器的终端。你敲的每条命令，都是在服务器上执行的，不是在你电脑上。

关掉窗口 = 断开连接，但**服务器上的程序不会停**（这和我们后面要做的"常驻 Agent"有关）。

**3. 端口是什么**

端口是同一台机器上区分不同服务的编号。就像一栋楼的门牌号：IP 是楼，端口是房间号。

- `22` 端口 = SSH，你登录用
- `80` / `443` = 网站常用的 HTTP / HTTPS
- `8443`–`8447` = 你的 5 个 Agent 要住的房间

**关键点**：腾讯云默认只开少数几个门（22/80/443/3389/ICMP）。你要用的 8443–8447 是**锁着的**，必须去控制台手动开。这是这个项目最常见的坑。

---

## 第 1 步：下单（购买页怎么填）

回到你的购买页，逐项核对：

| 字段           | 填什么                                         |
| ------------ | ------------------------------------------- |
| 地域           | 中国香港（不动）                                    |
| 套餐类型         | 中国香港入门型（不动）                                 |
| 套餐规格         | 90 元/月 · 2核4G · 70GB SSD · 30Mbps · 2TB（不动） |
| 创建方式         | **基于操作系统镜像**（必须改，不能是"应用镜像"）                 |
| 镜像           | **Ubuntu 24.04 LTS 64bit**                  |
| 登录方式         | **SSH 密钥登录**（已改 ✓）                          |
| 密钥对          | 选中你创建好的密钥对（没有就先创建，见下）                       |
| 服务器名称        | `acps-legal-hk`                             |
| 一键配置域名解析     | 不勾                                          |
| 10GB 共享存储特惠包 | 不勾                                          |
| 协议           | 勾选（三项都要）                                    |
| 时长           | 1 个月                                        |
| 数量           | 1                                           |
| 自动续费         | 不勾                                          |

### 🔑 用密钥登录前，必须先拿到私钥文件

选了"SSH 密钥登录"，页面上会出现**选择密钥对**的下拉框。

**如果还没有密钥对**，点旁边的"新建密钥对"：

1. 输入名字，比如 `acps-hk-key`
2. 确定 → **浏览器会立即自动下载一个 `.pem` 私钥文件**
3. ⚠️ **腾讯云只给这一次下载机会，之后永远无法再获取**

**私钥存哪里**：移到 `C:\Users\Zhao5\.ssh\` 下（没有该目录就新建），改名 `acps-hk-key.pem`。

存好之后再回到购买页选中它，然后点**立即购买**，付款 90 元。

> 🔴 私钥文件 = 你服务器的唯一钥匙。丢了只能去控制台重新绑定密钥对（需要关机）或重置密码。**不要放桌面，不要发微信，不要传网盘。**

**密钥登录和密码登录的区别**：没有密码了。SSH 拿你的**私钥**去和服务器上的**公钥**配对，配对成功放行。好处是不会被暴力破解（密码会被扫，密钥不会），代价是私钥文件必须保管好。

---

## 第 2 步：拿到公网 IP

付款后：

1. 进入 **腾讯云控制台** → 左侧 **轻量应用服务器**
2. 找到你刚买的实例，状态先是"创建中"，等 1–2 分钟变成"运行中"
3. 列表里能看到它的 **公网 IP**，形如 `43.128.1.2`。**把这个 IP 记下来**，后面全程要用

> 如果状态卡在"创建中"超过 5 分钟，刷新页面或联系客服。

---

## 第 3 步：第一次登录

**打开 Windows 的 PowerShell**（按 `Win` 键，输入 `powershell`，回车）。

敲下面这行，把 `43.128.1.2` 换成你的真实 IP：

```powershell
ssh -i "C:\Users\Zhao5\.ssh\acps-hk-key.pem" ubuntu@43.128.1.2
```

把路径和 IP 都换成你自己的。**用密钥登录不需要输密码**，直接进。

**第一次连接会看到这样的提示：**

```
The authenticity of host '43.128.1.2' can't be established.
ED25519 key fingerprint is SHA256:xxxxxxxxxxxxxxxx.
Are you sure you want to continue connecting (yes/no/[fingerprint])?
```

这是正常的（第一次认识这台机器）。**输入 `yes`，回车。**

**不会再提示输密码了。**&#x76F4;接看到下面这样的欢迎信息就是成功：

```
Welcome to Ubuntu 24.04.1 LTS (GNU/Linux 6.8.0-45-generic x86_64)
...
ubuntu@acps-legal-hk:~$
```

### 嫌命令太长？配一个快捷方式（可选，但强烈建议）

每次都敲那一长串路径很烦。配置一次，以后只敲 `ssh acps`：

**在你自己电脑的 PowerShell 里**执行：

```powershell
notepad $HOME\.ssh\config
```

记事本会问"文件不存在，是否新建"→ 点**是**。然后粘贴以下内容并保存：

```
Host acps
    HostName 43.128.1.2
    User ubuntu
    IdentityFile C:\Users\Zhao5\.ssh\acps-hk-key.pem
```

（`HostName` 换成你的真实 IP）

**以后登录就只需要**：

```powershell
ssh acps
```

---

### 如果遇到权限报错

Windows 上有时会报：

```
Permissions for 'acps-hk-key.pem' are too open.
```

这是说私钥文件权限太宽松，SSH 拒绝使用。**在 PowerShell 里执行**（把用户名换成你的）：

```powershell
icacls "C:\Users\Zhao5\.ssh\acps-hk-key.pem" /inheritance:r
icacls "C:\Users\Zhao5\.ssh\acps-hk-key.pem" /grant:r "$env:USERNAME:R"
```

然后重新 `ssh` 即可。

> 🔴 如果提示 `Connection timed out`：说明 22 端口不通。确认 IP 没敲错、实例状态是"运行中"，再去控制台防火墙检查 22 端口。  
> 🔴 如果提示 `Permission denied (publickey)`：多半是私钥路径写错、或选错了密钥对。确认 `-i` 后面的路径真实存在。

---

## 第 4 步：登录后先做 3 个体检

逐条敲，看看服务器是不是健康的：

```bash
whoami
```

应该输出 `ubuntu`。

```bash
free -h
```

应该看到 `Mem:` 那一行总量约 `3.7Gi`（标称 4GB，系统占用一部分）。**记住这个数字**，后面跑 Agent 要靠它判断内存够不够。

```bash
df -h /
```

看 `/` 那一行的容量约 `69G`，已用 2G 左右。

```bash
nproc && lsb_release -a
```

应该输出 `2` 和 `Ubuntu 24.04.x LTS`。

> 💡 `sudo` 是什么：`ubuntu` 是普通用户，装软件等操作要在命令前加 `sudo` 提权。腾讯云给你的这个用户有 sudo 权限，放心用。

---

## 第 5 步：控制台放行端口（最关键的一步）

**这一步不做，后面所有东西都白搭。**

1. 回到腾讯云控制台 → 轻量应用服务器 → 点进你的实例
2. 上方标签选 **防火墙**
3. 点 **添加规则**
4. 按下表**一条一条加**（共 5 条）：

| 应用类型 | 协议  | 端口   | 来源     | 策略 |
| ---- | --- | ---- | ------ | -- |
| 自定义  | TCP | 8443 | 全部IPv4 | 允许 |
| 自定义  | TCP | 8444 | 全部IPv4 | 允许 |
| 自定义  | TCP | 8445 | 全部IPv4 | 允许 |
| 自定义  | TCP | 8446 | 全部IPv4 | 允许 |
| 自定义  | TCP | 8447 | 全部IPv4 | 允许 |

顺手确认一下 **22 端口是放行的**（默认有）。

> 为什么要 5 个端口：一个主控 Agent + 4 个专用 Agent，每个住一个"房间"。目标 5 个 Agent 就能全部装下。

---

## 第 6 步：验证端口真的通了

**在你的服务器上**（刚才那个 SSH 窗口）敲：

```bash
mkdir -p ~/tls-test && cd ~/tls-test
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj "/CN=test.local"
```

这会生成一个临时自签证书（用于测试，不是最终要用的）。

然后启动一个临时 HTTPS 服务：

```bash
openssl s_server -accept 8443 -cert cert.pem -key key.pem -www
```

**这个命令会"占住"窗口**（不返回提示符），说明服务正在跑。**不要关这个窗口。**

**现在另开一个 PowerShell 窗口**（再按一次 `Win` 开 PowerShell），敲：

```powershell
Test-NetConnection 43.128.1.2 -Port 8443
```

把 IP 换成你的。**看到 `TcpTestSucceeded : True` 就成功了。**

或者更直观——**用浏览器访问**：

```
https://43.128.1.2:8443
```

会弹出"证书不受信任"的警告（自签证书必然如此）：

- Chrome：点 **高级** → **继续前往 43.128.1.2（不安全）**
- Edge：点 **详细信息** → **转到此网页**

看到一条灰色的测试页面（写着 `s_server` 之类），就说明**端口、防火墙、HTTPS 三件事全部打通了**。

**验证完，回到服务器窗口按 `Ctrl + C` 停掉测试服务。**

---

## 第 7 步：测网络质量（这是你要紧盯的指标）

香港入门型不保证跨境线路质量，所以必须实测。在服务器上敲：

```bash
sudo apt update && sudo apt install -y mtr-tiny
mtr -rw -4 -c 20 www.ioa.pub
```

**怎么读结果**：

看最后一行 `Loss%`（丢包率）和 `Avg`（平均延迟）：

| 结果                  | 判断                |
| ------------------- | ----------------- |
| 丢包 0%，延迟 30–80ms    | 良好，放心用            |
| 丢包 0–2%，延迟 80–150ms | 可接受，JSON 通信没问题    |
| 丢包 > 5%，或延迟 > 200ms | 线路有问题，考虑换锐驰型或境内节点 |

**建议早中晚各测一次**（尤其晚 20:00–23:00），连续测几天。把结果记下来，一个月后就有数据支撑要不要换线路了。

同理测一下模型 API 的连通性（先换成你打算用的 API 域名）：

```bash
mtr -rw -4 -c 20 api.deepseek.com
```

---

## 第 8 步：装 Python 3.14 环境

Ubuntu 24.04 自带 Python 3.12，但你要 3.14。用 `uv` 装最省事：

```bash
sudo apt update && sudo apt install -y curl git build-essential
```

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```bash
source $HOME/.local/bin/env
```

```bash
uv python install 3.14
```

验证：

```bash
uv python list
```

应该能看到 `cpython-3.14.x-linux-x86_64-gnu` 这一行。

---

## 第 9 步：上传项目代码

**在你自己电脑上新开一个 PowerShell**（不是 SSH 窗口），敲：

```powershell
ssh acps "mkdir -p ~/witrelate/legal_acps"
```

```powershell
scp "C:\Users\Zhao5\Desktop\Mycode\witrelate\legal_acps\*.py" acps:~/witrelate/legal_acps/
```



```powershell
scp "C:\Users\Zhao5\Desktop\Mycode\witrelate\*.whl" acps:~/witrelate/
```

（如果你跳过了上面配 `acps` 快捷方式那步，就把 `acps` 换成 `-i "C:\Users\Zhao5\.ssh\acps-hk-key.pem" ubuntu@43.128.1.2`）

**用密钥登录，这几条命令都不需要输密码。**

> ⚠️ **不要传 `.venv` 目录**——你本地那个是 Windows 的，拿到 Linux 上用不了，必须在服务器上重建。

**回到服务器窗口**，建虚拟环境并安装依赖：

```bash
cd ~/witrelate
uv venv --python 3.14 .venv
source .venv/bin/activate
uv pip install acps_sdk-2.2.0-py3-none-any.whl wit_framework-2.1.0-cp312.cp313.cp314-none-any.whl
```

`wit_framework` 的文件名里带 `cp312.cp313.cp314`，说明它支持 Python 3.14，没问题。

验证装好了：

```bash
python -c "import acps_sdk, wit; print('OK')"
```

输出 `OK` 就说明环境完全就绪。

---

## 日常操作速查

| 想做什么         | 命令                                      |
| ------------ | --------------------------------------- |
| 登录服务器        | `ssh acps`（或 `ssh -i 私钥路径 ubuntu@IP`）   |
| 退出登录         | `exit`                                  |
| 看内存          | `free -h`                               |
| 看磁盘          | `df -h`                                 |
| 看正在跑的程序      | `ps aux \| grep python`                 |
| 看端口被谁占用      | `ss -tlnp`                              |
| 看某个程序的日志     | `tail -f ~/日志文件.log`（`Ctrl+C` 退出）       |
| 后台跑程序（关窗口不停） | `nohup python xxx.py > run.log 2>&1 &`  |
| 停掉后台程序       | `pkill -f xxx.py`                       |
| 激活虚拟环境       | `source ~/witrelate/.venv/bin/activate` |

---

## 常见报错对照

| 报错                                                | 原因              | 怎么办                                      |
| ------------------------------------------------- | --------------- | ---------------------------------------- |
| `Connection timed out`                            | 端口不通            | 检查 IP、实例状态、控制台防火墙是否放行该端口                 |
| `Connection refused`                              | 连上了机器但端口没服务     | 程序没启动，或监听在 `127.0.0.1` 而非 `0.0.0.0`      |
| `Permission denied (publickey)`                   | 私钥路径错、或密钥对不匹配   | 确认 `-i` 后的路径真实存在；确认购买时选的密钥对就是这把私钥        |
| `Permissions for 'xxx.pem' are too open`          | Windows 下私钥权限过宽 | 见第 3 步的 `icacls` 修复命令                    |
| `WARNING: UNPROTECTED PRIVATE KEY FILE!`          | 同上              | 同上                                       |
| `Bad permissions` / 私钥格式错误                        | 文件损坏或格式不对       | 重新从腾讯云下载 `.pem`（若仍持有）；或控制台重新绑定密钥对        |
| `WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED` | 服务器重装过，指纹变了     | 本地执行 `ssh-keygen -R 43.128.1.2`（换成你的 IP） |
| `sudo: command not found`                         | 少打了 sudo        | 命令前加 `sudo`                              |
| `No space left on device`                         | 磁盘满             | `df -h` 看哪满了，清 pip 缓存：`uv cache clean`   |

---

## 做完这些之后

到这里，你已经完成了：

- ✅ 服务器可登录
- ✅ 8443–8447 端口从公网可达（实测验证过）
- ✅ 网络质量有实测数据
- ✅ Python 3.14 + acps_sdk + wit 环境就绪

**接下来要做的**（详见 `docs/acps-account-and-certs.md`）：

1. 把 ioa.pub 的账号密码填进 `~/witrelate/.env` 的 `ACPS_USERNAME` / `ACPS_PASSWORD`
2. 跑 `enroll()`，把 3 个角色注册到平台（提交后平台会审核，wit 自动轮询）
3. 起 3 个常驻进程

**更正**：本文件早先版本在这里列了「实名审核」和「申请 3 个 HTTPS 证书」两项，
经查源码与实测，**证书这一项是错的**——证书由平台 CA 在 `enroll()` 时自动签发，
你不需要申请任何证书，也不需要域名。详见 `docs/acps-account-and-certs.md`。
