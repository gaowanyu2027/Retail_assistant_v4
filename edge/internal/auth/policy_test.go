package auth

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"testing"
)

// fixturePath 指向 Python 侧实测导出的鉴权策略夹具。
const fixturePath = "../../../contracts/auth_policy.json"

type authPolicy struct {
	PublicPaths  []string `json:"public_paths"`
	CookieName   string   `json:"cookie_name"`
	BearerPrefix string   `json:"bearer_prefix"`
	Unauthorized struct {
		Status      int    `json:"status"`
		ContentType string `json:"content_type"`
		Body        string `json:"body"`
	} `json:"unauthorized"`
	NotFound struct {
		Status      int    `json:"status"`
		ContentType string `json:"content_type"`
		Body        string `json:"body"`
	} `json:"not_found"`
}

func loadPolicy(t *testing.T) authPolicy {
	t.Helper()
	raw, err := os.ReadFile(filepath.Clean(fixturePath))
	if err != nil {
		if os.Getenv("EDGE_TEST_ALLOW_MISSING_FIXTURES") == "1" {
			t.Skipf("夹具缺失（已放行）：%v", err)
		}
		t.Fatalf("读不到夹具 %s：%v（夹具由 `python contracts/export_fixtures.py` 导出；"+
			"确要跳过设 EDGE_TEST_ALLOW_MISSING_FIXTURES=1）", fixturePath, err)
	}
	var p authPolicy
	if err := json.Unmarshal(raw, &p); err != nil {
		t.Fatalf("夹具解析失败：%v", err)
	}
	return p
}

// TestPolicyMatchesPythonFixture 把 Go 侧的策略常量钉在 Python 实测导出的夹具上：
// 任一侧改了对外契约而另一侧没跟上，这里就红。
func TestPolicyMatchesPythonFixture(t *testing.T) {
	p := loadPolicy(t)

	if CookieName != p.CookieName {
		t.Errorf("CookieName = %q，夹具为 %q", CookieName, p.CookieName)
	}
	if BearerPrefix != p.BearerPrefix {
		t.Errorf("BearerPrefix = %q，夹具为 %q", BearerPrefix, p.BearerPrefix)
	}
	if UnauthorizedStatus != p.Unauthorized.Status {
		t.Errorf("UnauthorizedStatus = %d，夹具为 %d", UnauthorizedStatus, p.Unauthorized.Status)
	}
	if UnauthorizedCT != p.Unauthorized.ContentType {
		t.Errorf("UnauthorizedCT = %q，夹具为 %q", UnauthorizedCT, p.Unauthorized.ContentType)
	}
	if UnauthorizedBody != p.Unauthorized.Body {
		t.Errorf("UnauthorizedBody = %q，夹具为 %q", UnauthorizedBody, p.Unauthorized.Body)
	}
	if NotFoundStatus != p.NotFound.Status || NotFoundCT != p.NotFound.ContentType ||
		NotFoundBody != p.NotFound.Body {
		t.Errorf("404 形状与夹具不一致：Go(%d,%q,%q) 夹具(%d,%q,%q)",
			NotFoundStatus, NotFoundCT, NotFoundBody,
			p.NotFound.Status, p.NotFound.ContentType, p.NotFound.Body)
	}

	// 免登录白名单：逐条比 NeedsAuth，并确认没有多余的白名单项（夹具里没有的路径必须需登录）。
	allowed := map[string]bool{}
	for _, path := range p.PublicPaths {
		allowed[path] = true
		if NeedsAuth(path) {
			t.Errorf("夹具中的免登录路径 %q 在 Go 侧被判为需登录", path)
		}
	}
	if len(publicPaths) != len(allowed) {
		t.Errorf("Go 侧白名单 %d 项，夹具 %d 项：%v vs %v",
			len(publicPaths), len(allowed), publicPaths, allowed)
	}
}

func TestNeedsAuth(t *testing.T) {
	cases := []struct {
		path string
		want bool
	}{
		{"/api/auth/login", false},
		{"/api/auth/logout", false},
		{"/api/health", false},
		{"/api/health/ready", false},
		{"/api/frontend/fallback", false},
		{"/api/queries", true},
		{"/api/", true},
		{"/api", false},                  // 无尾斜杠：不是 /api/ 前缀，照旧交 Python
		{"/api/healthz", true},           // 前缀白名单必须精确匹配
		{"/internal/auth/verify", false}, // 不走早拒（本地 404）
		{"/docs", false},                 // 文档前缀刻意不在 edge 复刻
		{"/", false},
		{"/assets/index.js", false},
	}
	for _, c := range cases {
		if got := NeedsAuth(c.path); got != c.want {
			t.Errorf("NeedsAuth(%q) = %v，期望 %v", c.path, got, c.want)
		}
	}
}

func TestCredentialsPriorityAndParsing(t *testing.T) {
	h := http.Header{}
	h.Set("Authorization", "Bearer   tok-bear  ")
	h.Set("Cookie", "other=1; retail_sid=tok-cookie; retail_sid=ignored")
	got := Credentials(h)
	want := []Credential{{"bearer", "tok-bear"}, {"cookie", "tok-cookie"}}
	if len(got) != len(want) {
		t.Fatalf("Credentials = %v，期望 %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("第 %d 枚 = %v，期望 %v", i, got[i], want[i])
		}
	}

	// 小写前缀同样识别；空令牌不算候选。
	h2 := http.Header{}
	h2.Set("Authorization", "bearer")
	if cs := Credentials(h2); len(cs) != 0 {
		t.Errorf("裸前缀不该产生候选：%v", cs)
	}
	h3 := http.Header{}
	h3.Set("Authorization", "basic abc")
	h3.Set("Cookie", "retail_sid=   ")
	if cs := Credentials(h3); len(cs) != 0 {
		t.Errorf("非 Bearer 与空 Cookie 值都不该产生候选：%v", cs)
	}
}

func TestIsWebSocketUpgrade(t *testing.T) {
	mk := func(conn, upgrade string) *http.Request {
		r := &http.Request{Header: http.Header{}}
		if conn != "" {
			r.Header.Set("Connection", conn)
		}
		if upgrade != "" {
			r.Header.Set("Upgrade", upgrade)
		}
		return r
	}
	for _, c := range []struct {
		conn, upgrade string
		want          bool
	}{
		{"Upgrade", "websocket", true},
		{"keep-alive, Upgrade", "WebSocket", true},
		{"keep-alive", "websocket", false},
		{"Upgrade", "h2c", false},
		{"", "", false},
	} {
		if got := IsWebSocketUpgrade(mk(c.conn, c.upgrade)); got != c.want {
			t.Errorf("IsWebSocketUpgrade(%q,%q) = %v，期望 %v", c.conn, c.upgrade, got, c.want)
		}
	}
}
