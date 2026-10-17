package auth

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sync"
	"time"
)

// User 判定结果里的用户三要素（Python 内部端点只回这三个字段，最小暴露面）。
type User struct {
	ID       int64  `json:"id"`
	Username string `json:"username"`
	Role     string `json:"role"`
}

// Verdict 一次判定的结论。
type Verdict struct {
	Valid bool  `json:"valid"`
	User  *User `json:"user"`
	// AuthEnabled：Python 侧是否开着鉴权（内部端点回的同名字段）。关掉鉴权时
	// 中间件直接放行（不解析登录态），此时 `valid:false` 不表示"该拒绝"——
	// 它只表示"没有登录态"，所以**只有这一位为真时结论才算权威**（见 fetch）。
	AuthEnabled bool `json:"auth_enabled"`
	// Known：是否拿到了**权威结论**。false = 回源没成功（超时/网络错/非 200/解析失败）
	// 或 Python 侧根本没开鉴权。调用方必须按"放行、交 Python 判定"处理 ——
	// 早拒只做减法，不做加法。
	Known bool `json:"-"`
}

const (
	// PositiveTTL 有效结论的缓存时长。会话令牌有滑动续期（Python 侧 60s 写限流），
	// 5s 意味着一个活跃会话最多每 5s 回源一次，而在被登出/吊销后最多 5s 内仍被放行 ——
	// 这 5s 的窗口由 Python 的每请求鉴权兜底（edge 放行 ≠ 放行）。
	PositiveTTL = 5 * time.Second
	// NegativeTTL 无效结论的缓存时长，刻意更短：令牌由服务端签发，正常客户端不会
	// 反复送无效令牌；短缓存既挡掉风暴，也让"刚登录"不会因旧负结论被误拒。
	NegativeTTL = 2 * time.Second
	// defaultMaxEntries 缓存条数上限。无效令牌可以无限多（攻击者构造），
	// 必须有界：满了先清过期项，仍满则随机淘汰约四分之一。
	defaultMaxEntries = 4096
	// defaultMaxInflight 同时在途的回源校验数上限。超限时不再发起新校验，
	// 直接按"未知"放行 —— 别的请求占着名额时，这个请求退化成本次迁移前的行为
	// （照旧交给 Python），而不是排队等待。
	defaultMaxInflight = 32
	// verifyTimeout 单次回源校验的超时。这是"提前判"的优化：若 Python 一秒内
	// 答不上来，说明这次卸载不划算，直接放行让它自己判（Python 那边本来也要处理）。
	verifyTimeout = 1 * time.Second
	// maxBodyBytes 响应体读取上限（内部端点只回一个小 JSON）。
	maxBodyBytes = 64 << 10
)

// Observer 与 /metrics 的接线（字段可为 nil）。
type Observer struct {
	// OnVerify 成功拿到权威结论（缓存未命中）。
	OnVerify func()
	// OnCacheHit 命中本地 TTL 缓存。
	OnCacheHit func()
	// OnVerifyError 未拿到结论（错误/超时/非 200）。
	OnVerifyError func()
}

func (o Observer) verify() {
	if o.OnVerify != nil {
		o.OnVerify()
	}
}

func (o Observer) cacheHit() {
	if o.OnCacheHit != nil {
		o.OnCacheHit()
	}
}

func (o Observer) verifyError() {
	if o.OnVerifyError != nil {
		o.OnVerifyError()
	}
}

type cacheEntry struct {
	verdict Verdict
	expires time.Time
}

// call 同一令牌的并发校验：只发一次回源请求，其余等结论（单飞）。
type call struct {
	done    chan struct{}
	verdict Verdict
}

// Verifier 会话判定器：TTL 缓存 + 单飞 + 有界容量，回源调 Python 内部端点。
type Verifier struct {
	backend string
	token   string
	client  *http.Client
	obs     Observer

	maxEntries  int
	maxInflight int
	now         func() time.Time

	sem chan struct{} // 在途回源校验的名额

	mu       sync.Mutex
	cache    map[string]cacheEntry
	inflight map[string]*call
}

// NewVerifier 构造判定器。backend 为 Python 基地址（不含尾斜杠），
// internalToken 为共享密钥（为空时调用方不该启用早拒，见 Gate）。
func NewVerifier(backend, internalToken string, obs Observer) *Verifier {
	return &Verifier{
		backend: backend,
		token:   internalToken,
		client: &http.Client{
			Timeout: verifyTimeout,
			Transport: &http.Transport{
				Proxy:               nil, // 后端在容器网络/本机回环，绝不走 HTTP_PROXY
				MaxIdleConns:        8,
				MaxIdleConnsPerHost: 8,
				IdleConnTimeout:     90 * time.Second,
			},
		},
		obs:         obs,
		maxEntries:  defaultMaxEntries,
		maxInflight: defaultMaxInflight,
		now:         time.Now,
		sem:         make(chan struct{}, defaultMaxInflight),
		cache:       make(map[string]cacheEntry),
		inflight:    make(map[string]*call),
	}
}

// Verify 判定一枚会话令牌。带 TTL 缓存（正 5s / 负 2s）与同令牌单飞。
func (v *Verifier) Verify(token string) Verdict {
	if token == "" {
		return Verdict{Known: true} // 空令牌无需回源：明确无效
	}
	now := v.now()

	v.mu.Lock()
	if e, ok := v.cache[token]; ok && now.Before(e.expires) {
		v.mu.Unlock()
		v.obs.cacheHit()
		return e.verdict
	}
	if c, ok := v.inflight[token]; ok {
		v.mu.Unlock()
		<-c.done
		return c.verdict
	}
	c := &call{done: make(chan struct{})}
	v.inflight[token] = c
	v.mu.Unlock()

	verdict := v.fetch("verify", "token", token) // 网络调用不持锁

	v.mu.Lock()
	c.verdict = verdict
	delete(v.inflight, token)
	if verdict.Known {
		v.storeLocked(token, verdict, now)
	}
	v.mu.Unlock()
	close(c.done)
	return verdict
}

// ConsumeTicket 消费一次性票据。**刻意不缓存**：票据是用后即焚的，
// 缓存任何结论都会把"一次"变成"一段时间"。
//
// 当前早拒不使用它（带 `?ticket=` 的请求直接放行，交 Python 消费）；
// 这是 S3 视频数据面的接口：届时由 Go 持有 WS 连接，握手前需要先验证票据。
func (v *Verifier) ConsumeTicket(ticket string) Verdict {
	if ticket == "" {
		return Verdict{Known: true}
	}
	return v.fetch("consume-ticket", "ticket", ticket)
}

// fetch 回源调 Python 内部端点；任何失败都返回 Known=false（调用方放行）。
func (v *Verifier) fetch(action, field, value string) Verdict {
	select {
	case v.sem <- struct{}{}:
		defer func() { <-v.sem }()
	default:
		// 在途校验已达上限：不再排队，按"未知"处理（放行给 Python）。
		v.obs.verifyError()
		return Verdict{}
	}

	payload, err := json.Marshal(map[string]string{field: value})
	if err != nil {
		v.obs.verifyError()
		return Verdict{}
	}
	url := fmt.Sprintf("%s/internal/auth/%s", v.backend, action)
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(payload))
	if err != nil {
		v.obs.verifyError()
		return Verdict{}
	}
	req.Header.Set("Content-Type", "application/json")
	// 密钥只在此处出现：这是 edge → Python 的内网调用，与客户端请求互不相干
	// （客户端伪造的同名头在反代层被剥离，见 internal/proxy）。
	req.Header.Set("X-Internal-Token", v.token)

	resp, err := v.client.Do(req)
	if err != nil {
		v.obs.verifyError()
		return Verdict{}
	}
	defer func() {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, maxBodyBytes))
		resp.Body.Close()
	}()
	if resp.StatusCode != http.StatusOK {
		// 404 = 未配置密钥 / Python 版本还没有内部端点；5xx = Python 侧异常。
		// 一律按"未知"放行：功能半上线时退化为迁移前的行为，而不是把用户挡在门外。
		v.obs.verifyError()
		return Verdict{}
	}
	var out Verdict
	if err := json.NewDecoder(io.LimitReader(resp.Body, maxBodyBytes)).Decode(&out); err != nil {
		v.obs.verifyError()
		return Verdict{}
	}
	// 只有 Python 自报"鉴权开着"时结论才权威：AUTH_ENABLED=0 时中间件直接放行，
	// `valid:false` 只是"没有登录态"，按它拒绝会与直连行为不一致（旧版 Python
	// 没有这个字段，同样落到这里 → 退化为放行，不会误拒）。
	if !out.AuthEnabled {
		v.obs.verifyError()
		return Verdict{}
	}
	out.Known = true
	if !out.Valid {
		out.User = nil // 无效结论不带用户信息（防止半可信数据被下游误用）
	}
	v.obs.verify()
	return out
}

// storeLocked 写入缓存（调用方持锁）。
func (v *Verifier) storeLocked(token string, verdict Verdict, now time.Time) {
	ttl := NegativeTTL
	if verdict.Valid {
		ttl = PositiveTTL
	}
	if len(v.cache) >= v.maxEntries {
		v.evictLocked(now)
	}
	v.cache[token] = cacheEntry{verdict: verdict, expires: now.Add(ttl)}
}

// evictLocked 先清过期项，仍满则随机淘汰约四分之一（map 迭代顺序随机）。
func (v *Verifier) evictLocked(now time.Time) {
	for k, e := range v.cache {
		if !now.Before(e.expires) {
			delete(v.cache, k)
		}
	}
	target := v.maxEntries * 3 / 4
	for k := range v.cache {
		if len(v.cache) <= target {
			return
		}
		delete(v.cache, k)
	}
}
