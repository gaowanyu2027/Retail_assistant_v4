// Package health 处理 /api/health（liveness）。
//
// 分工（与 api/main.py 对齐）：
//   - 匿名请求：edge 本地应答固定字节 {"status":"ok"}（Python 挂了也答——这正是
//     故障隔离演示的一部分）；
//   - 带凭据的请求（会话 Cookie / Authorization / ?ticket=）：反代给 Python，
//     拿完整信息（gpu_available / device / uptime_seconds / user / role）。
//
// 凭据判定只影响"要不要多走一跳"：Python 对无效凭据本就按匿名处理，
// 两条路径的回答在匿名语义下逐字相同。
//
// /api/health/ready（真探测 MySQL/Qdrant）**不走本包**，始终反代——
// 就绪语义的真相只能在 Python（真探测在那边）。
package health

import (
	"net/http"
	"strings"

	"retail-assistant/edge/internal/httpx"
)

// cookieName 会话 Cookie 名（api/security.py 的 COOKIE_NAME = "retail_sid"）。
const cookieName = "retail_sid"

// livenessBody 匿名 liveness 应答（FastAPI 的 JSON 分隔符无空格，逐字一致）。
const livenessBody = `{"status":"ok"}`

// HasCredentials 判断请求是否携带任何形式的鉴权凭据。
// 与 Python 侧 user_from_scope 的凭据来源保持一致：Bearer / 会话 Cookie / ticket。
func HasCredentials(r *http.Request) bool {
	if r.Header.Get("Authorization") != "" {
		return true
	}
	for _, part := range strings.Split(r.Header.Get("Cookie"), ";") {
		kv := strings.SplitN(strings.TrimSpace(part), "=", 2)
		if len(kv) == 2 && kv[0] == cookieName && strings.TrimSpace(kv[1]) != "" {
			return true
		}
	}
	if r.URL.Query().Get("ticket") != "" {
		return true
	}
	return false
}

// ServeLiveness 匿名 liveness 应答（GET/HEAD；其余方法对齐 FastAPI 的 405）。
func ServeLiveness(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		w.Header().Set("Allow", "GET, HEAD")
		httpx.WriteString(w, r, http.StatusMethodNotAllowed, "application/json",
			`{"detail":"Method Not Allowed"}`)
		return
	}
	httpx.WriteString(w, r, http.StatusOK, "application/json", livenessBody)
}
