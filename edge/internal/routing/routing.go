// Package routing 决定每个请求由 edge 本地处理还是兜底反代给 Python。
//
// 白名单刻意保持最小（与迁移方案一致）：
//
//	"/"                         首页（注入 vue-fallback 脚本后返回构建产物）
//	/css/**、/js/**、/assets/**  静态资源（故障隔离：Python 挂了页面仍在）
//	/api/health                 liveness；匿名本地应答，带凭据时反代（拿完整信息）
//	/api/frontend/fallback      前端加载失败上报（本地记录，Python 挂了也能收到）
//	/metrics                    内网指标（Python 侧不存在该路径，仅私网可访问）
//
// 其余一切——包括 21 个 include_in_schema=False 的兼容别名、/docs、
// /openapi.json、/api/health/ready（真探测必须打 Python）——一律兜底反代。
// 兜底是**零枚举**的：Python 侧新增/改名路由，edge 无需同步修改。
package routing

import "strings"

// Kind 请求的处理方式。
type Kind int

const (
	// Proxy 兜底反代给 Python。
	Proxy Kind = iota
	// Index 首页 "/"。
	Index
	// Static /css、/js、/assets 挂载下的静态文件。
	Static
	// Health /api/health（liveness）。
	Health
	// Fallback /api/frontend/fallback。
	Fallback
	// Metrics /metrics（内网）。
	Metrics
)

// AllKinds 全部处理方式（配置遍历 / 指标初始化用，顺序稳定）。
var AllKinds = [...]Kind{Proxy, Index, Static, Health, Fallback, Metrics}

var kindNames = [...]string{"proxy", "index", "static", "health", "fallback", "metrics"}

func (k Kind) String() string {
	if int(k) < 0 || int(k) >= len(kindNames) {
		return "unknown"
	}
	return kindNames[k]
}

// Classify 按请求路径归类。只做精确匹配与前缀匹配，不做任何路径清洗——
// 清洗/规范化（如 /js 无斜杠、双斜杠）一概交回 Python 处理（与直连语义一致）。
func Classify(path string) Kind {
	switch {
	case path == "/":
		return Index
	case path == "/api/health":
		return Health
	case path == "/api/frontend/fallback":
		return Fallback
	case path == "/metrics":
		return Metrics
	case strings.HasPrefix(path, "/css/"),
		strings.HasPrefix(path, "/js/"),
		strings.HasPrefix(path, "/assets/"):
		return Static
	default:
		return Proxy
	}
}
