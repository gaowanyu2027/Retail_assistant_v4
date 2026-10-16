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
}

func Load() Config {
	return Config{
		Listen: envOr("EDGE_LISTEN", ":8000"),
		BackendURL: strings.TrimRight(
			envOr("EDGE_BACKEND_URL", "http://127.0.0.1:8000"), "/"),
		DistDir: envOr("EDGE_DIST_DIR", "frontend-vue/dist"),
	}
}

func (c Config) String() string {
	return fmt.Sprintf("listen=%s backend=%s dist=%s", c.Listen, c.BackendURL, c.DistDir)
}

func envOr(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}
