// Package routing 决定每个请求由 edge 本地处理还是兜底反代给 Python。
//
// 白名单刻意保持最小（与迁移方案一致）：
//
//	"/"                         首页（注入 vue-fallback 脚本后返回构建产物）
//	/css/**、/js/**、/assets/**  静态资源（故障隔离：Python 挂了页面仍在）
//	/api/health                 liveness；匿名本地应答，带凭据时反代（拿完整信息）
//	/api/frontend/fallback      前端加载失败上报（本地记录，Python 挂了也能收到）
//	/metrics                    内网指标（Python 侧不存在该路径，仅私网可访问）
//	/internal/**                本地 404（Python 的内部端点路径：公网请求不出网；
//	                            Python 侧另有共享密钥 + 对端 IP 三道门禁，互不依赖）
//
// 其余一切——包括 21 个 include_in_schema=False 的兼容别名、/docs、
// /openapi.json、/api/health/ready（真探测必须打 Python）——一律兜底反代。
// 兜底是**零枚举**的：Python 侧新增/改名路由，edge 无需同步修改。
//
// 唯一在归类之前被截走的是两条视频 WS（/api/ws/stream、/api/ws/client）：
// EDGE_VIDEO=1 且拿得到权威鉴权结论时由 video.Handler 接管；拿不准或没开，
// 它原样落回这里 —— 仍是 Proxy，走兜底反代（归类结果不变，语义不变）。
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
	// Internal /internal/**：本进程服务的内部端点（Python 用它给 edge 做鉴权判定）。
	// edge 一律本地 404 —— 公网请求根本到不了这些端点（Python 侧另有独立的三道门禁）。
	Internal
	// Video 视频 WS：由 edge 的数据面接管（EDGE_VIDEO=1）。**只能由
	// video.Handler.Handle 判定后指派**，Classify 不会返回它 —— 同一个路径上的
	// 普通 HTTP 请求与"未接管（交回反代）"的 WS 请求都还是 Proxy。
	Video
	// Report 主动汇报 WS：由 edge 的 report.Handler 接管（EDGE_REPORT=1）。
	// 同样只能由 Handler 指派 —— 未接管时仍是 Proxy。
	Report
)

// AllKinds 全部处理方式（配置遍历 / 指标初始化用，顺序稳定）。
var AllKinds = [...]Kind{Proxy, Index, Static, Health, Fallback, Metrics, Internal, Video, Report}

var kindNames = [...]string{"proxy", "index", "static", "health", "fallback", "metrics", "internal", "video", "report"}

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
	case strings.HasPrefix(path, "/internal/"):
		return Internal
	case strings.HasPrefix(path, "/css/"),
		strings.HasPrefix(path, "/js/"),
		strings.HasPrefix(path, "/assets/"):
		return Static
	default:
		return Proxy
	}
}
