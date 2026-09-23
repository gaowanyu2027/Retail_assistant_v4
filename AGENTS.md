# AGENTS.md — 本仓库的协作约定

> 给 AI 编码助手（DSH / Claude Code 等）与人类贡献者共用。**目的**：把"评测门禁、提交与文档约定、
> 已知陷阱"写进仓库，让助手**开箱即守**，减少返工。只写**事实与命令**，不写愿望。
> 机器相关的补充放在 `AGENTS.local.md`（不入库）。

## 0. 一句话

线下门店的智能零售 AI Agent：CV 感知（YOLO + ByteTrack）→ 结构化数据 → LangGraph Agent 归因问答 → 网页 + 微信小程序双端。

## 1. 三条硬规则（违反即返工）

1. **先测量、再断言**：判断"配置是否生效 / 功能是否正常"必须给出**可复现的测量**——
   `docker compose config`、`docker compose exec … env`、`getBoundingClientRect()`、原始日志行、测试输出。
   **不要用文本 grep 去推断运行时行为**（本项目历史上因此误判过两次）。
2. **每个改动都要能验证**：跑下面的门禁命令；新增行为**同时加测试**（离线优先）。
3. **文档同步**：改动写进 `改进记录.md`（做了什么 / 为什么 / 怎么验证）；
   **部署相关的问题写进 `部署流程.md`**。

## 2. 门禁命令（改完必跑）

| 目的 | 命令 |
|---|---|
| 单测（离线，秒级） | `python tests/run_tests.py` |
| Agent 评测门禁（需 MySQL + LLM Key） | `python evals/run_evals.py` |
| 前端构建（改动 `frontend-vue/` 或 `public/` 后必跑） | `cd frontend-vue && npm run build` |
| 本地起服务 | `python run.py`（<http://127.0.0.1:8000>） |
| Compose 配置校验 | `docker compose config --quiet` |

## 3. 提交与协作约定

- **提交消息**：中性、对外可读，只描述"做了什么"；不写内部叙事（"此前…/纠正…/教训…"），
  不出现与个人事务相关的字眼（完整禁用清单在项目外的仓库外笔记里，不放仓库）。
- **不要执行 `git push`**：提交可以由助手完成，**推送由仓库所有者自己执行**。
- 大文件、仓库外文件、`AGENTS.local.md` 不进仓库（见 `.gitignore`）。

## 4. 已知陷阱（踩过的，别重犯）

| 现象 | 真相 |
|---|---|
| 改了 `.env` 不生效 | 容器**创建时**才读入 → 必须 `docker compose up -d`（必要时 `--force-recreate`） |
| 改了 `frontend-vue/public/` 下的 js/css 不生效 | 那些文件**不进 bundle**、由构建原样拷贝 → 必须重新 `npm run build` |
| `config/settings.py` 里某项配了却不生效 | 该文件存在**硬编码**项（`VIDEO_FPS` 曾写死 30）→ 新增配置务必读环境变量并写清默认值 |
| 手机宽度下视频画面"消失" | 栅格隐式列 + 图表固定宽度把视频列挤成 0×0，画布尺寸归零（详见 `改进记录.md` 第 31 条）→ 用 `getBoundingClientRect()` 判定，别猜 |
| 构建突然要十几分钟 | 改动 Dockerfile 里**安装步骤那条 `RUN` 的文本**（或传 `--build-arg`）会使 pip 层缓存失效 |
| 服务器上 `git pull` 失败 | 该 ECS **访问不了 GitHub** → 部署走"本机打包 → `scp` → 解包 → 重建"，文档里不要写 `git pull` |
| 脚本遍历文件名时漏掉中文名文件 | `git ls-files` 默认把非 ASCII 名转成八进制转义 → 用 `git -c core.quotepath=false ls-files` |

## 5. 权限与数据隔离（改相关代码前必读）

- **角色三档**：`root` / `platform` / `viewer`，权限矩阵在 `api/security.py`，路由用 `require_perm(...)` 强制；
- **隔离**：会话、问答历史、长期记忆、Qdrant payload 均按 `owner`（`auth_context.py` 的 ContextVar）；
  新增任何读写路径都必须带 owner，并考虑"无上下文时"的安全默认值；
- **前端置灰只是体验**：真正的强制在服务端；新增写接口必须挂权限（有守卫测试盯着）。

## 6. 给 AI 助手的输出约定（人类同样受益）

- 给用户的**命令必须单独成块、块内只有命令**：用户会整段复制粘贴到终端，混入说明文字会被当成命令执行；
- 报告改动时**给证据**（命令输出、测试结果），不要只给结论；
- 不确定时先说"我需要这条测量数据"，**不要先给根因**（本项目的两次误判都源于此）。
