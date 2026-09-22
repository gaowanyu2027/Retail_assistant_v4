# 部署到云服务器（阿里云 ECS · 2C4G · Ubuntu 22.04）

> 目标：把本项目的 **Docker 化部署**在一台阿里云 ECS 上跑起来，得到一个可点开的 **Live Demo** 地址。
> 本地（Windows 开发机）已用同一套 `docker compose` 验证过，这里记录的是"搬到 Linux 服务器"要额外处理的 6 件事。
>
> 本文所有命令都在**服务器上**执行（除注明"本机"的地方）。

## 0. 目标环境（本次实测的实例）

| 项 | 值 |
|---|---|
| 实例 | 阿里云 ECS `ecs.e-c1m2.large` · 2 vCPU / 4 GiB · Ubuntu 22.04 |
| 系统盘 | ESSD Entry 40 GiB |
| 公网 | 弹性公网 IP（EIP）**10 Mbps 峰值 · 按使用流量计费** |
| 访问方式 | **不绑域名** → `http://<公网IP>:8000`（大陆节点用非标端口 + IP，无需 ICP 备案） |

> **为什么不绑域名**：中国大陆节点上，**域名 + 80/443** 必须 ICP 备案；
> 用 **IP + 8000** 不触发备案要求。想要域名 + HTTPS，请改用**香港/海外节点**（免备案）。

## 1. 部署前：安全组与两个"不要"

- **安全组只放行 `22` + `8000`**；`3306` / `6333` **绝不开公网**（compose 里它们本来就没有对外映射）。
- **不要装"宝塔面板"**（控制台会推荐）：多一层 Web 面板 + 默认弱口令风险，且对 Docker 部署没有任何帮助。
- **不要用"节省停机"停止实例**（会释放公网 IP）。用 EIP 后 IP 才固定；演示期结束记得**先解绑、再释放 EIP**，否则会持续收闲置费。

## 2. 装 Docker + 镜像加速（国内拉 Docker Hub 会很慢）

```bash
# Docker 官方脚本 + 阿里云镜像（--mirror Aliyun 让它走国内源）
curl -fsSL https://get.docker.com | sudo sh -s -- --mirror Aliyun
sudo systemctl enable --now docker

# 配镜像加速器（地址在：阿里云控制台 → 容器镜像服务 → 镜像工具 → 镜像加速器）
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{ "registry-mirrors": ["https://<你的ID>.mirror.aliyuncs.com"] }
EOF
sudo systemctl restart docker

docker --version && docker compose version
```

## 3. 加 2G swap（**重要**）

4 GiB 内存要同时跑 `mysql + qdrant + redis + backend(torch)`，加上 YOLO 推理的峰值，
不加 swap 容易被 OOM Killer 干掉（表现为容器莫名重启）。

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h     # 确认 Swap 一行有 2.0Gi
```

## 4. 拉代码

```bash
sudo mkdir -p /opt && cd /opt
sudo git clone https://github.com/gaowanyu2027/Retail_assistant_v4.git retail-assistant
cd retail-assistant
sudo mkdir -p data/sources
```

## 5. ⚠️ 精简 `docker-compose.yml` 的三处挂载（省 9.3GB 上传）

`docker-compose.yml` 用 bind mount 把一批**被 gitignore 的大文件**挂进容器。
服务器上 `git clone` 完**一个都没有**，而 **Docker 遇到不存在的挂载路径会自己建空目录** →
容器照样起来，但**表情/语音/演示视频静默失效**（不报错，很难查）。

| 挂载 | 体积 | 服务器上怎么办 |
|---|---|---|
| `./yolo26n.pt` | 5.3MB | **必须上传**（行人检测/热度，核心功能） |
| `./best.pt` + `./mobilenetv3_fer_best.pth` | 5.9 + 16MB | **建议上传**（顾客表情分析） |
| `./data` | — | 随代码目录创建（认证库 + 演示视频） |
| `./mmpose/demo/resources/demo.mp4` | — | **注释掉**（只是演示素材） |
| `./all_models` | **9.2GB** | **注释掉**（语音唤醒/ASR 模型，家里上行传不完） |
| `./sherpa-onnx-kws-zipformer-.../` | 37MB | **注释掉**（同上） |

```bash
sudo nano docker-compose.yml
# 把下面三行前面加 # ：
#   - ./mmpose/demo/resources/demo.mp4:/app/mmpose/demo/resources/demo.mp4:ro
#   - ./all_models:/app/all_models:ro
#   - ./sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01:/app/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01:ro
```

> 放弃的是"语音唤醒 / 语音识别"两个附加功能，**不影响**问答、热度、告警、表情、看板、评测这些主线能力。

## 6. 上传必需的 4 个文件（**在本机**执行）

```powershell
# 本机 PowerShell（Windows 自带 scp）
scp yolo26n.pt best.pt mobilenetv3_fer_best.pth root@<公网IP>:/opt/retail-assistant/
scp data\sources\demo_loop.mp4 root@<公网IP>:/opt/retail-assistant/data/sources/
```
（也可以用 ECS 控制台「远程连接 → 文件上传」，或 Workbench）

## 7. 配 `.env`（`mysql_root` 是**必填**，缺了 compose 直接报错）

```bash
cd /opt/retail-assistant
sudo cp .env.example .env && sudo nano .env
```

至少要设这几项：

```ini
mysql_root=<强口令>              # 必填！compose 里是 ${mysql_root:?...}
DEEPSEEK_API_KEY=<你的 DeepSeek Key>  # 不填 → LLM 走模板降级，问答质量下降
AUTH_ROOT_PASSWORD=<强口令>       # 公网可访问，绝不能用默认打印的随机口令
AUTH_COOKIE_SECURE=0             # 走 http（没配 HTTPS）
VIDEO_FPS=12                     # 2 核 CPU：本地 30fps 是强 CPU 的成绩，线上降到 12
```

```bash
sudo chmod 600 .env
```

## 8. ⚠️ `data/` 权限（否则登录 500）

容器以 **`appuser`(uid 1000)** 运行（`Dockerfile` 的 `ARG APP_UID=1000`，注释里就写着
"Linux 下要求宿主目录属主与 APP_UID 一致"）。bind mount 会**覆盖**镜像里的 chown →
宿主机 `./data` 属主必须是 1000，否则 `auth.db` 建不出来 → **登录直接 500**。

```bash
sudo chown -R 1000:1000 /opt/retail-assistant/data
```

## 9. 构建并启动

Dockerfile 的构建参数已经参数化，国内建议显式指定（默认已是清华 pip 源）：

```bash
sudo docker compose build --build-arg USE_LOCK=1 \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  --build-arg PYTORCH_CPU_INDEX=https://download.pytorch.org/whl/cpu
sudo docker compose up -d
```

- `USE_LOCK=1` → 用 `requirements.lock.txt`（123 包，锁版可复现，也就是容器里实测过的那套）；
- 首次构建 **5-15 分钟**（要下 torch CPU 轮子 + 前端 `npm ci && npm run build`）；
- 若卡在 torch 下载，可把 `PYTORCH_CPU_INDEX` 换成国内镜像站（如清华/阿里云的 pytorch-wheels 镜像），
  **以镜像站当前可用为准**，不要照抄未验证的地址。

## 10. 验证

```bash
sudo docker compose ps                          # 4 个服务都应 Up
curl -fsS http://127.0.0.1:8000/api/health/ready
# 期望：{"status":"ok","critical":{"mysql":true},"degraded":[],...}
sudo docker compose logs -f backend | head -50   # 看首启日志（会打印 root 账号/口令提示）
```

浏览器打开 **`http://<公网IP>:8000`** → 用 `root` + 你设的 `AUTH_ROOT_PASSWORD` 登录。

然后建议：在「用户管理」里建一个**只读 demo 账号**（platform 角色）给外部使用者用，别把你的 root 口令给对方。

## 11. 上公网必须做的四道防护

1. **强口令**：`AUTH_ROOT_PASSWORD` 别用默认的；给外部使用者单独的只读账号；
2. **DeepSeek 设用量上限与预算告警**（控制台）—— 公网可访问的问答接口会被爬，这是**真金白银**的风险；
3. **限流**：登录限流项目里已有；要再加一层就上 nginx `limit_req`（可选）；
4. **不要接真实摄像头**：演示用 `data/sources/demo_loop.mp4` 循环素材；`config/cameras.yaml`
   里那台 RTSP 保持注释状态。

## 12. 回滚与运维

```bash
sudo docker compose down                    # 停服（数据在命名卷 + ./data，不丢）
sudo docker compose up -d --build           # 重新构建启动
sudo docker compose logs --tail=100 backend # 看日志
```

- **数据**：`mysql8_volume` / `qdrant-docker` / `redis_data` 是命名卷；应用侧 `./data` 是 bind mount
  （含 `auth.db`）。`down` 不加 `-v` 都不会删数据；
- **换版本**：`git pull && docker compose up -d --build`；
- **费用**：实例免费试用期到 **2026-12-02**（之后按小时计费）；EIP 单独出账单；
  流量按 GB 计费（免费额度 20GB）→ 演示时把 `VIDEO_FPS` 调低、别长时间挂着看视频。

## 13. 下一步：CD（自动部署）

手动跑通之后，`.github/workflows/deploy.yml` 就能"一键更新线上版本"。
**注意方向**：实测这台阿里云 ECS **访问不了 GitHub**（`git clone` 报
`GnuTLS recv error (-110): The TLS connection was non-properly terminated.`），
所以**不能让服务器 `git pull`**，要反过来 —— **GitHub 的 runner 打包后 scp 推到服务器**：

```
GitHub Actions（runner 上有代码）──scp 源码包──> 服务器 /tmp ──SSH 触发──> docker compose up -d --build
                                                              └─ 健康检查失败 → 回滚上一个镜像标签
```

仓库里已放好 `.github/workflows/deploy.yml`（默认**手动触发**；改成 push 自动部署的开关在文件注释里）。
需要配 3 个 Secrets：`DEPLOY_HOST` / `DEPLOY_USER` / `DEPLOY_SSH_KEY`
（部署**专用**密钥，别复用个人密钥；对应公钥加到服务器上部署账号的 `authorized_keys`）。

**安全要点**：服务器上建**单独的部署账号**（只加 `docker` 组、不用 root）、禁密码登录、`PermitRootLogin no`；
`.env` 不进仓库（本来就没进，见 `.gitignore`）。首次部署前手动跑过一遍 `docker compose up -d --build` 最稳。

### 13.1 版本保留与回滚（任意往期）

部署时服务器会维护一份"版本仓库"：

| 路径 / 标签 | 内容 |
|---|---|
| `/opt/releases/<sha8>.tar.gz` | 每个部署过的**源码包**（保留最近 **3** 个，旧的自动删） |
| `/opt/releases/CURRENT` / `PREVIOUS` | 当前 / 上一个版本的 sha8（供"回退一步"） |
| `retail-assistant:sha-<sha8>` | 每个版本的**镜像**（同样保留最近 3 个） |
| `retail-assistant:v4` | 正在运行的镜像 |

**回滚不需要重新构建**（2 核机器一次构建 ~20 分钟）：旧镜像重新 tag 成 `v4` + 旧源码包解包回去 +
`docker compose up -d --force-recreate backend`，**秒级生效**。

```text
Actions → Run workflow
  rollback_to = 59cc78c     # 回滚到指定版本（sha 前缀）
  rollback_to = previous    # 回退一步
  rollback_to = （留空）     # 正常部署当前 main
```

**部署失败会自动回滚**：健康检查（`/api/health/ready`）+ **首页引用了前端产物**（`assets/index-`）两项都通过才算成功；
任一失败 → 打日志 → 自动切回 `PREVIOUS` 并以失败退出。

⚠ **数据不参与回滚**：`data/`（auth.db）与 MySQL/Qdrant/Redis 卷原地继续用。若某版本改过 schema，
回滚镜像可能与新数据不兼容 —— **回滚前先备份 `data/`**。

## 附：本次踩过的坑（给未来的自己）

1. **compose 挂载被 gitignore 的大文件** → 服务器上文件不存在，Docker 建空目录，功能**静默失效**（第 5 节）；
2. **bind mount 覆盖镜像 chown** → `./data` 属主必须是 uid 1000，否则登录 500（第 8 节）；
3. **4G 内存跑 torch + 三个数据库** → 不加 swap 会被 OOM（第 3 节）；
4. **国内拉 Docker Hub / torch** → 配镜像加速器 + 构建参数（第 2、9 节）；
5. **大陆节点 + 域名 = 必须备案** → 走 `IP:8000`，或用香港节点（第 0 节）；
6. **"节省停机"会释放公网 IP** → 转 EIP 固定地址，不用时先解绑再释放（第 1 节）。
