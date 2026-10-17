package video

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"
)

// 内部端点路径（与 api/routes/vision_internal.py 一一对应）。
const (
	pathEvents      = "/internal/vision/events"
	pathAction      = "/internal/vision/action"
	pathViewer      = "/internal/vision/viewer"
	pathClientFrame = "/internal/vision/client-frame"
)

const (
	// apiTimeout 普通内部调用的整体超时（登记/注销/动作）。动作可能触发
	// 打开视频源、文件 stat 等，留得宽一些；超时即失败，连接继续保持
	// （下一次动作会重新走一遍）。
	apiTimeout = 30 * time.Second
	// frameTimeout 本机摄像头推帧的单次超时。这条路径每秒十几帧，
	// 慢一点就该丢掉这一帧（下一帧马上到），绝不排队堆积。
	frameTimeout = 5 * time.Second
	// maxRespBytes 内部端点响应体上限（都只是小 JSON）。
	maxRespBytes = 1 << 20
)

// API 调用 Python 内部端点。所有请求都带共享密钥 X-Internal-Token；
// Python 侧另有三道门禁（密钥 + 对端地址 + 路径），两者互不依赖。
type API struct {
	base  string
	token string
	http  *http.Client
}

// NewAPI 构造客户端。base 为 Python 基地址（不含尾斜杠）。
func NewAPI(base, token string) *API {
	return &API{
		base:  base,
		token: token,
		http: &http.Client{
			Transport: &http.Transport{
				Proxy:               nil, // 后端在内网/环回，绝不走 HTTP_PROXY
				MaxIdleConns:        16,
				MaxIdleConnsPerHost: 16,
				IdleConnTimeout:     90 * time.Second,
			},
			// 刻意不设客户端整体 Timeout：上行事件流是长连接，
			// 每次调用各自用 ctx 控时（见 apiTimeout / frameTimeout）。
		},
	}
}

// ViewerSync `/internal/vision/viewer` 的 attach 回执。
type ViewerSync struct {
	Viewer  string   `json:"viewer"`
	Gen     int      `json:"gen"`
	Replies []string `json:"replies"`
}

// ActionOutcome `/internal/vision/action` 的回执（语义见 vision/session.py）。
type ActionOutcome struct {
	Gen        int      `json:"gen"`
	Replaced   bool     `json:"replaced"`
	Clear      bool     `json:"clear"`
	Replies    []string `json:"replies"`
	FramedTail *string  `json:"framed_tail"`
}

// Attach 观看者登记（幂等：带同一个 viewer 再调只刷新，不会重复登记），
// 顺带取"已在运行"的问候（frameID 传 nil 表示"还不知道要发第几帧"）。
func (a *API) Attach(ctx context.Context, viewerID, username string, frameID *int) (*ViewerSync, error) {
	body, err := json.Marshal(map[string]any{
		"op": "attach", "viewer": viewerID, "username": username, "frame_id": frameID,
	})
	if err != nil {
		return nil, err
	}
	var out ViewerSync
	if err := a.postJSON(ctx, pathViewer, body, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// Detach 观看者注销。Python 侧"最后一个观看者离开才停会话"。
func (a *API) Detach(ctx context.Context, viewerID string) error {
	if viewerID == "" {
		return nil // 从没登记成功过（Python 不在场）：无需注销
	}
	body, err := json.Marshal(map[string]any{"op": "detach", "viewer": viewerID})
	if err != nil {
		return err
	}
	return a.postJSON(ctx, pathViewer, body, nil)
}

// Action 转发一条客户端动作，返回发给**发起人**的回执。
// msg 用原始 JSON 字节透传：Go 不改写客户端消息（键序、数字格式都保持原样）。
func (a *API) Action(ctx context.Context, viewerID, username string, msg json.RawMessage) (*ActionOutcome, error) {
	body, err := json.Marshal(map[string]any{
		"viewer": viewerID, "username": username, "message": msg,
	})
	if err != nil {
		return nil, err
	}
	var out ActionOutcome
	if err := a.postJSON(ctx, pathAction, body, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// ClientFrame 转发一帧浏览器本机摄像头画面（原始 JPEG 字节体）。
// **归属判定在 Python**（与 /ws/client 直连路径共用同一段逻辑），这里不复刻。
func (a *API) ClientFrame(ctx context.Context, username string, jpeg []byte) error {
	ctx, cancel := context.WithTimeout(ctx, frameTimeout)
	defer cancel()
	u := a.base + pathClientFrame + "?username=" + url.QueryEscape(username)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u, bytes.NewReader(jpeg))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/octet-stream")
	req.Header.Set("X-Internal-Token", a.token)
	resp, err := a.http.Do(req)
	if err != nil {
		return err
	}
	defer drain(resp)
	if resp.StatusCode != http.StatusNoContent && resp.StatusCode != http.StatusOK {
		return fmt.Errorf("client-frame 返回 %d", resp.StatusCode)
	}
	return nil
}

// Events 订阅上行帧流：返回的 ReadCloser 由调用方按信封逐条读取（readEnvelope）。
// 没有整体超时（长连接）；ctx 取消即断开（Go 侧靠它做停滞看门狗与关闭回收）。
func (a *API) Events(ctx context.Context) (io.ReadCloser, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, a.base+pathEvents, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-Internal-Token", a.token)
	resp, err := a.http.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		drain(resp)
		return nil, fmt.Errorf("events 返回 %d", resp.StatusCode)
	}
	return resp.Body, nil
}

func (a *API) postJSON(ctx context.Context, path string, body []byte, out any) error {
	ctx, cancel := context.WithTimeout(ctx, apiTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, a.base+path, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Internal-Token", a.token)
	resp, err := a.http.Do(req)
	if err != nil {
		return err
	}
	defer drain(resp)
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("%s 返回 %d", path, resp.StatusCode)
	}
	if out == nil {
		return nil
	}
	return json.NewDecoder(io.LimitReader(resp.Body, maxRespBytes)).Decode(out)
}

// drain 读完并关闭响应体：让连接能被复用（不读完的连接会被直接丢弃）。
func drain(resp *http.Response) {
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, maxRespBytes))
	_ = resp.Body.Close()
}
