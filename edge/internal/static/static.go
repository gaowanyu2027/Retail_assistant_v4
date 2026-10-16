// Package static 托管前端构建产物（故障隔离的关键：Python 挂了页面仍在）。
//
// 与 api/main.py 的静态托管逐项对齐：
//
//   - "/"：读取 dist/index.html → 在首个 </body> 前注入 vue-fallback 外部脚本
//     → 带 NO_CACHE_HEADERS 返回；每请求复查构建产物完整性（index.html 及其
//     引用的本地资源都在），缺失则显示"前端未构建"提示页（不再回退原生 JS 版）；
//   - /css、/js：StaticFiles 挂载，响应带 Cache-Control: no-store, no-cache,
//     must-revalidate（开发期防浏览器缓存旧样式/脚本）；
//   - /assets：StaticFiles 挂载（Vite 带内容哈希，不设 Cache-Control）；
//   - 静态 404 输出 {"detail":"Not Found"}（对齐 Starlette StaticFiles 的 404 形状）。
package static

import (
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"retail-assistant/edge/internal/httpx"
)

// VueFallbackScript 与 api/main.py 的 VUE_FALLBACK_SCRIPT 逐字一致。
// 必须是同源外部脚本：CSP 为 script-src 'self'，内联想都跑不了（见 main.py 注释）。
const VueFallbackScript = `<script src="/js/vue-fallback.js"></script>`

// unbuiltPage 与 api/main.py 的 _serve_unbuilt 逐字一致。
const unbuiltPage = `<!DOCTYPE html><html><head><meta charset='utf-8'><title>智能零售分析系统</title></head>` +
	`<body style='display:flex;align-items:center;justify-content:center;height:100vh;font-family:sans-serif;color:#333'>` +
	`<div style='text-align:center'><h2>前端未构建</h2>` +
	`<p style='color:#666'>Vue 构建产物缺失/不完整，请执行构建后重启服务。</p>` +
	`<p style='font-size:13px;color:#999'>frontend-vue/dist/index.html</p></div>` +
	`</body></html>`

const notFoundBody = `{"detail":"Not Found"}`
const methodNotAllowedBody = `{"detail":"Method Not Allowed"}`

// refRe 提取 index.html 中的本地资源引用（与 api/main.py 的 _collect_local_refs 同式）。
var refRe = regexp.MustCompile(`(?:src|href)="([^"]+)"`)

// Handler 静态托管。
type Handler struct {
	distDir string
}

func New(distDir string) *Handler { return &Handler{distDir: distDir} }

// ServeIndex 处理 "/"（GET/HEAD；其余方法对齐 FastAPI 的 405）。
func (h *Handler) ServeIndex(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		w.Header().Set("Allow", "GET, HEAD")
		httpx.WriteString(w, r, http.StatusMethodNotAllowed, "application/json", methodNotAllowedBody)
		return
	}
	html, err := h.readIndex()
	if err != nil {
		// 构建产物缺失/不完整：提示页（带 NO_CACHE_HEADERS），语义同 _serve_unbuilt。
		writeHTML(w, r, unbuiltPage, true)
		return
	}
	injected := strings.Replace(html, "</body>", VueFallbackScript+"</body>", 1)
	writeHTML(w, r, injected, true)
}

// writeHTML 输出 HTML 响应；noCache=true 时附带 NO_CACHE_HEADERS
// （与 api/main.py 首页/提示页的头部逐字一致）。
func writeHTML(w http.ResponseWriter, r *http.Request, body string, noCache bool) {
	if noCache {
		h := w.Header()
		h.Set("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
		h.Set("Pragma", "no-cache")
		h.Set("Expires", "0")
	}
	httpx.WriteString(w, r, http.StatusOK, "text/html; charset=utf-8", body)
}

// readIndex 读取 index.html 并复查其引用的本地资源都在（对齐 _vue_dist_available）。
func (h *Handler) readIndex() (string, error) {
	b, err := os.ReadFile(filepath.Join(h.distDir, "index.html"))
	if err != nil {
		return "", err
	}
	html := string(b)
	for _, ref := range collectLocalRefs(html) {
		rel := strings.TrimLeft(ref, "/")
		if rel == "" {
			continue
		}
		if _, err := os.Stat(filepath.Join(h.distDir, filepath.FromSlash(rel))); err != nil {
			return "", fmt.Errorf("index.html 引用的资源缺失: %s", rel)
		}
	}
	return html, nil
}

// collectLocalRefs 与 api/main.py 的 _collect_local_refs 同语义：
// 过滤 http(s)/data/// 开头的引用，去掉 # 与 ? 后缀。
func collectLocalRefs(html string) []string {
	var refs []string
	for _, m := range refRe.FindAllStringSubmatch(html, -1) {
		ref := m[1]
		if ref == "" ||
			strings.HasPrefix(ref, "http://") ||
			strings.HasPrefix(ref, "https://") ||
			strings.HasPrefix(ref, "data:") ||
			strings.HasPrefix(ref, "//") {
			continue
		}
		if i := strings.IndexAny(ref, "#?"); i >= 0 {
			ref = ref[:i]
		}
		refs = append(refs, ref)
	}
	return refs
}

// ServeStatic 处理 /css/**、/js/**、/assets/**（GET/HEAD；其余方法对齐 StaticFiles 的 405）。
func (h *Handler) ServeStatic(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		// Starlette StaticFiles 的 405 不带 Allow 头，这里保持一致。
		httpx.WriteString(w, r, http.StatusMethodNotAllowed, "application/json", methodNotAllowedBody)
		return
	}
	mount, sub := splitMount(r.URL.Path)
	if mount == "" {
		notFound(w, r)
		return
	}
	// 路径卫生：出现 .. / . / 空段一律 404（Python 侧 StaticFiles 同样拒绝越界路径，
	// 且 /js/a//b 这类路径在两边都不存在）。正常路径不含这些段。
	for _, seg := range strings.Split(sub, "/") {
		if seg == "" || seg == "." || seg == ".." {
			notFound(w, r)
			return
		}
	}
	full := filepath.Join(h.distDir, mount, filepath.FromSlash(sub))
	fi, err := os.Stat(full)
	if err != nil || fi.IsDir() {
		notFound(w, r)
		return
	}
	f, err := os.Open(full)
	if err != nil {
		notFound(w, r)
		return
	}
	defer f.Close()

	// /css、/js 的 no-cache 与 api/main.py 的 no_cache_static_files 中间件一致
	// （覆盖语义，且只有 Cache-Control 一个头——不带 Pragma/Expires）。
	if mount == "css" || mount == "js" {
		w.Header().Set("Cache-Control", "no-store, no-cache, must-revalidate")
	}
	w.Header().Set("Content-Type", withCharset(contentTypeFor(filepath.Ext(full))))
	http.ServeContent(w, r, filepath.Base(full), fi.ModTime(), f)
}

// splitMount 返回（挂载名, 子路径）；不属于任何挂载时 mount 为空。
func splitMount(p string) (string, string) {
	for _, m := range []string{"css", "js", "assets"} {
		if strings.HasPrefix(p, "/"+m+"/") {
			return m, strings.TrimPrefix(p, "/"+m+"/")
		}
	}
	return "", ""
}

func notFound(w http.ResponseWriter, r *http.Request) {
	httpx.WriteString(w, r, http.StatusNotFound, "application/json", notFoundBody)
}

// withCharset 复刻 Starlette Response.init_headers 的字符集补全：
// `text/*` 类型统一补 `; charset=utf-8`（已含 charset= 的除外，大小写不敏感）。
// 这是实测比对出来的差异 —— route_parity 首轮 10 条差异全在这里：
// 后端经 StaticFiles/FileResponse 出的 text/css、text/javascript 都带 charset。
func withCharset(contentType string) string {
	if strings.HasPrefix(contentType, "text/") &&
		!strings.Contains(strings.ToLower(contentType), "charset=") {
		return contentType + "; charset=utf-8"
	}
	return contentType
}

// contentTypeFor 复刻 Python mimetypes.guess_type 对常见扩展名的取值
// （.js → text/javascript、.css → text/css；text/* 的 charset 后缀由
// withCharset 按 Starlette 规则补，Go 内置 mime 包的表与 Python 不一致，不能直接用）。
func contentTypeFor(ext string) string {
	switch strings.ToLower(ext) {
	case ".js", ".mjs":
		return "text/javascript"
	case ".css":
		return "text/css"
	case ".html", ".htm":
		return "text/html"
	case ".json", ".map":
		return "application/json"
	case ".svg":
		return "image/svg+xml"
	case ".png":
		return "image/png"
	case ".jpg", ".jpeg":
		return "image/jpeg"
	case ".gif":
		return "image/gif"
	case ".webp":
		return "image/webp"
	case ".ico":
		return "image/vnd.microsoft.icon"
	case ".woff":
		return "font/woff"
	case ".woff2":
		return "font/woff2"
	case ".ttf":
		return "font/ttf"
	case ".txt":
		return "text/plain"
	default:
		return "application/octet-stream"
	}
}
