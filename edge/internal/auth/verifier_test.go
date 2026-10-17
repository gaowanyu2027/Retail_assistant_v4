package auth

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// fakeBackend 模拟 Python 内部端点：记录调用次数与最后一次请求，按用户名表回话。
type fakeBackend struct {
	t      *testing.T
	calls  atomic.Int64
	lastMu sync.Mutex
	lastCh string // 最后一次路径
	lastHd string // 最后一次 X-Internal-Token
	lastBd string // 最后一次请求体
	reply  func(w http.ResponseWriter, body string)
}

func newFakeBackend(t *testing.T, reply func(w http.ResponseWriter, body string)) (*fakeBackend, *httptest.Server) {
	t.Helper()
	fb := &fakeBackend{t: t, reply: reply}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		fb.calls.Add(1)
		fb.lastMu.Lock()
		fb.lastCh, fb.lastHd, fb.lastBd = r.URL.Path, r.Header.Get("X-Internal-Token"), string(body)
		fb.lastMu.Unlock()
		fb.reply(w, string(body))
	}))
	t.Cleanup(srv.Close)
	return fb, srv
}

// verdictBackend 只认 "good"（令牌）与 "good-ticket"（票据），其余一律无效
// —— 形状与 Python 内部端点的回答一致。
func verdictBackend(t *testing.T) (*fakeBackend, *httptest.Server) {
	return newFakeBackend(t, func(w http.ResponseWriter, body string) {
		var req map[string]string
		_ = json.Unmarshal([]byte(body), &req)
		w.Header().Set("Content-Type", "application/json")
		if req["token"] == "good" || req["ticket"] == "good-ticket" {
			_, _ = w.Write([]byte(`{"valid":true,"auth_enabled":true,` +
				`"user":{"id":7,"username":"alice","role":"platform"}}`))
			return
		}
		_, _ = w.Write([]byte(`{"valid":false,"user":null,"auth_enabled":true}`))
	})
}

func TestVerifySendsInternalTokenAndPayload(t *testing.T) {
	fb, srv := verdictBackend(t)
	v := NewVerifier(srv.URL, "s3cret", Observer{})

	got := v.Verify("good")
	if !got.Known || !got.Valid || got.User == nil || got.User.Username != "alice" ||
		got.User.Role != "platform" || got.User.ID != 7 {
		t.Fatalf("结论 = %+v，期望有效且带用户三要素", got)
	}
	fb.lastMu.Lock()
	defer fb.lastMu.Unlock()
	if fb.lastCh != "/internal/auth/verify" {
		t.Errorf("调用路径 = %q", fb.lastCh)
	}
	if fb.lastHd != "s3cret" {
		t.Errorf("X-Internal-Token = %q", fb.lastHd)
	}
	if fb.lastBd != `{"token":"good"}` {
		t.Errorf("请求体 = %q", fb.lastBd)
	}
}

func TestVerifyCachesVerdicts(t *testing.T) {
	fb, srv := verdictBackend(t)
	v := NewVerifier(srv.URL, "k", Observer{})
	base := time.Now()
	v.now = func() time.Time { return base }

	if got := v.Verify("good"); !got.Valid {
		t.Fatalf("首次判定应有效：%+v", got)
	}
	if got := v.Verify("good"); !got.Valid {
		t.Fatalf("二次判定应命中缓存且仍有效：%+v", got)
	}
	if n := fb.calls.Load(); n != 1 {
		t.Errorf("同一令牌回源 %d 次，期望 1 次（正缓存 5s）", n)
	}

	// 正缓存到期（5s 后）→ 回源一次
	v.now = func() time.Time { return base.Add(PositiveTTL + time.Millisecond) }
	v.Verify("good")
	if n := fb.calls.Load(); n != 2 {
		t.Errorf("正缓存过期后回源数 = %d，期望 2", n)
	}

	// 负缓存 2s：bad 两次只回源一次，过期后再回源
	v.now = func() time.Time { return base }
	if got := v.Verify("bad"); got.Valid || !got.Known {
		t.Fatalf("无效令牌应得已知的无效结论：%+v", got)
	}
	v.Verify("bad")
	if n := fb.calls.Load(); n != 3 {
		t.Errorf("无效令牌两次调用后回源数 = %d，期望 3", n)
	}
	v.now = func() time.Time { return base.Add(NegativeTTL + time.Millisecond) }
	v.Verify("bad")
	if n := fb.calls.Load(); n != 4 {
		t.Errorf("负缓存过期后回源数 = %d，期望 4", n)
	}
}

func TestVerifySingleFlight(t *testing.T) {
	release := make(chan struct{})
	fb, srv := newFakeBackend(t, func(w http.ResponseWriter, _ string) {
		<-release // 卡住第一次回源，逼并发请求汇入同一次调用
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"valid":true,"auth_enabled":true,` +
			`"user":{"id":1,"username":"u","role":"viewer"}}`))
	})
	v := NewVerifier(srv.URL, "k", Observer{})

	const n = 16
	var wg sync.WaitGroup
	results := make([]Verdict, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			results[i] = v.Verify("same")
		}(i)
	}
	time.Sleep(50 * time.Millisecond) // 让其余 goroutine 都进入等待
	close(release)
	wg.Wait()

	if got := fb.calls.Load(); got != 1 {
		t.Errorf("并发 %d 次同令牌回源 %d 次，期望 1 次（单飞）", n, got)
	}
	for i, r := range results {
		if !r.Known || !r.Valid {
			t.Errorf("第 %d 个结果 = %+v，期望与首个一致", i, r)
		}
	}
}

func TestVerifyFailureIsUnknownNotDeny(t *testing.T) {
	cases := map[string]func(w http.ResponseWriter, _ string){
		"端点缺失(404)":  func(w http.ResponseWriter, _ string) { w.WriteHeader(404) },
		"服务端异常(500)": func(w http.ResponseWriter, _ string) { w.WriteHeader(500) },
		"响应不是 JSON": func(w http.ResponseWriter, _ string) {
			_, _ = w.Write([]byte("<html>oops</html>"))
		},
	}
	for name, reply := range cases {
		_, srv := newFakeBackend(t, reply)
		v := NewVerifier(srv.URL, "k", Observer{})
		got := v.Verify("whatever")
		if got.Known {
			t.Errorf("%s：应得 Known=false（放行交 Python），实得 %+v", name, got)
		}
	}

	// 连不上（端口悬空）：同样必须是"未知"而不是拒绝
	v := NewVerifier("http://127.0.0.1:1", "k", Observer{})
	if got := v.Verify("whatever"); got.Known {
		t.Errorf("连不上时应得 Known=false，实得 %+v", got)
	}

	// 失败结论不缓存：恢复后立刻就能拿到权威结论
	var fail atomic.Bool
	fail.Store(true)
	fb, srv := newFakeBackend(t, func(w http.ResponseWriter, _ string) {
		if fail.Load() {
			w.WriteHeader(500)
			return
		}
		_, _ = w.Write([]byte(`{"valid":true,"auth_enabled":true,` +
			`"user":{"id":1,"username":"u","role":"root"}}`))
	})
	v2 := NewVerifier(srv.URL, "k", Observer{})
	if got := v2.Verify("t"); got.Known {
		t.Fatalf("第一次应失败：%+v", got)
	}
	fail.Store(false)
	if got := v2.Verify("t"); !got.Known || !got.Valid {
		t.Errorf("上游恢复后应立即得到权威结论：%+v（回源 %d 次）", got, fb.calls.Load())
	}
}

// TestAuthDisabledIsUnknownNotDeny AUTH_ENABLED=0 的 Python 对谁都放行，
// `valid:false` 只说明"没有登录态"。edge 若照字面拒绝，就会出现
// "直连 200、经 edge 401"的分裂 —— 必须按"未知"处理（放行给 Python）。
func TestAuthDisabledIsUnknownNotDeny(t *testing.T) {
	_, srv := newFakeBackend(t, func(w http.ResponseWriter, _ string) {
		_, _ = w.Write([]byte(`{"valid":false,"user":null,"auth_enabled":false}`))
	})
	v := NewVerifier(srv.URL, "k", Observer{})
	if got := v.Verify("any"); got.Known {
		t.Fatalf("鉴权关着时应得 Known=false，实得 %+v", got)
	}
	// 结论不入缓存（不权威），且 gate 必须放行
	if _, ok := v.cache["any"]; ok {
		t.Errorf("非权威结论不该进缓存")
	}

	// 旧版 Python（没有这个字段）走同一条路：宁可退化为"全部反代"，不可误拒
	_, srv2 := newFakeBackend(t, func(w http.ResponseWriter, _ string) {
		_, _ = w.Write([]byte(`{"valid":false,"user":null}`))
	})
	v2 := NewVerifier(srv2.URL, "k", Observer{})
	if got := v2.Verify("any"); got.Known {
		t.Fatalf("无 auth_enabled 字段时应得 Known=false，实得 %+v", got)
	}
}

func TestVerifySaturatedFailsOpen(t *testing.T) {
	fb, srv := verdictBackend(t)
	v := NewVerifier(srv.URL, "k", Observer{})
	v.sem = make(chan struct{}, 1)
	v.sem <- struct{}{} // 占满名额

	got := v.Verify("good")
	if got.Known {
		t.Errorf("名额饱和时应按未知放行，实得 %+v", got)
	}
	if n := fb.calls.Load(); n != 0 {
		t.Errorf("名额饱和时不该发起回源，实得 %d 次", n)
	}
}

func TestCacheIsBounded(t *testing.T) {
	fb, srv := verdictBackend(t)
	v := NewVerifier(srv.URL, "k", Observer{})
	v.maxEntries = 8
	base := time.Now()
	v.now = func() time.Time { return base }

	for i := 0; i < 50; i++ {
		v.Verify(strings.Repeat("x", i+1)) // 每个令牌都无效，逐条落缓存
	}
	v.mu.Lock()
	size := len(v.cache)
	v.mu.Unlock()
	if size > 8 {
		t.Errorf("缓存 %d 条，超过上限 8", size)
	}
	if fb.calls.Load() == 0 {
		t.Error("应当发生过回源")
	}
}

func TestConsumeTicketNeverCached(t *testing.T) {
	fb, srv := verdictBackend(t)
	v := NewVerifier(srv.URL, "k", Observer{})

	first := v.ConsumeTicket("good-ticket")
	if !first.Known || !first.Valid {
		t.Fatalf("首次消费应有效：%+v", first)
	}
	// 票据一次性：即便上游仍答"有效"，也不得缓存 —— 再调必须再回源
	v.ConsumeTicket("good-ticket")
	if n := fb.calls.Load(); n != 2 {
		t.Errorf("票据消费回源 %d 次，期望 2（不缓存）", n)
	}
	fb.lastMu.Lock()
	defer fb.lastMu.Unlock()
	if fb.lastCh != "/internal/auth/consume-ticket" {
		t.Errorf("调用路径 = %q", fb.lastCh)
	}
	if fb.lastBd != `{"ticket":"good-ticket"}` {
		t.Errorf("请求体 = %q", fb.lastBd)
	}
}

func TestEmptyTokenIsKnownInvalid(t *testing.T) {
	v := NewVerifier("http://127.0.0.1:1", "k", Observer{})
	for _, got := range []Verdict{v.Verify(""), v.ConsumeTicket("")} {
		if !got.Known || got.Valid {
			t.Errorf("空凭据应得「已知且无效」：%+v", got)
		}
	}
}
