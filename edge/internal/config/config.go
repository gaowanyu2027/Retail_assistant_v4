// Package config 汇总 edge 的运行配置。
//
// 全部来自环境变量、全部有可用的默认值——镜像不携带配置文件，
// 与项目"容器创建时才读 .env"的既有约定一致（见 AGENTS.md 已知陷阱）。
package config

import (
	"fmt"
	"os"
	"strings"
)

// Config edge 的运行参数。
type Config struct {
	// Listen 监听地址，环境变量 EDGE_LISTEN，默认 ":8000"。
	// 容器里唯一对外端口；本地双进程验证时用 EDGE_LISTEN=:8010 之类避开 Python。
	Listen string
	// BackendURL Python 后端基地址，环境变量 EDGE_BACKEND_URL，
	// 默认 "http://127.0.0.1:8000"（本机直跑 Python 的情况）。
	// compose 覆盖文件里设为 "http://backend:8000"（服务名直连）。
	BackendURL string
	// DistDir 前端构建产物目录，环境变量 EDGE_DIST_DIR，默认 "frontend-vue/dist"。
	// edge 直接托管它（不经过 Python）——Python 挂掉后页面仍可用是 S1 的故障隔离目标。
	DistDir string
	// EdgeAuth 是否启用边缘鉴权早拒，环境变量 EDGE_AUTH（取值 "1"），默认关。
	// 关 = 一切照旧全部反代（回退成本为零）；开 = 无凭据 / 凭据明确无效的
	// `/api/**` 请求在 edge 就拿到与 Python 逐字一致的 401。
	EdgeAuth bool
	// InternalToken 与 Python 共享的内部端点密钥，环境变量 INTERNAL_API_TOKEN。
	// edge 用它调 `/internal/auth/verify`；为空且 EdgeAuth=1 时早拒自动禁用（响亮告警）。
	InternalToken string
	// EdgeVideo 视频数据面是否由 edge 接管，环境变量 EDGE_VIDEO（取值 "1"），默认关。
	// 关 = `/api/ws/**` 照旧反代给 Python（回退成本为零，秒级生效）。
	//
	// **启用前提**：同时 EDGE_AUTH=1 且 INTERNAL_API_TOKEN 非空 —— 接管 WS 要求
	// edge 能拿到权威鉴权结论（`/api/ws/**` 未登录在 Python 侧是 accept 后
	// close(1008)，没有结论就分不清"该拒"与"该接管"）。缺一即整套禁用并告警。
	EdgeVideo bool
}

func Load() Config {
	return Config{
		Listen: envOr("EDGE_LISTEN", ":8000"),
		BackendURL: strings.TrimRight(
			envOr("EDGE_BACKEND_URL", "http://127.0.0.1:8000"), "/"),
		DistDir:       envOr("EDGE_DIST_DIR", "frontend-vue/dist"),
		EdgeAuth:      envOr("EDGE_AUTH", "0") == "1",
		InternalToken: strings.TrimSpace(os.Getenv("INTERNAL_API_TOKEN")),
		EdgeVideo:     envOr("EDGE_VIDEO", "0") == "1",
	}
}

// String 供启动日志使用：**刻意不打印 InternalToken**（密钥不进日志）。
func (c Config) String() string {
	return fmt.Sprintf("listen=%s backend=%s dist=%s edge_auth=%t internal_token_set=%t edge_video=%t",
		c.Listen, c.BackendURL, c.DistDir, c.EdgeAuth, c.InternalToken != "", c.EdgeVideo)
}

func envOr(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}
