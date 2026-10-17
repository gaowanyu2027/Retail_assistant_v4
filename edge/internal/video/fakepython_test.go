package video

import (
	"encoding/binary"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// fakePython 模拟 Python 侧：四个视频内部端点 + 两个鉴权内部端点。
// 记录所有调用，供用例断言"Go 到底做了什么"（先测量、再断言）。
type fakePython struct {
	t *testing.T

	mu        sync.Mutex
	attached  []string          // attach 的 "viewer=username" 记录
	attachSeq map[string]int    // 每个用户名已分配过的 viewer id 数
	detached  []string          // detach 的 viewer id
	actions   []json.RawMessage // 转发的原始动作消息（保序）
	uploads   [][]byte          // 收到的本机摄像头帧
	verifies  int               // /internal/auth/verify 调用次数
	tickets   int               // /internal/auth/consume-ticket 调用次数

	verifyFail   atomic.Bool   // 真时 verify 一律 500（模拟"拿不准"）
	blockUploads chan struct{} // 非 nil 时 client-frame 阻塞到它关闭
	events       chan []byte   // 上行信封字节（关闭即断开）
	eventConns   atomic.Int64  // events 被连接的次数
}

func newFakePython(t *testing.T) *fakePython {
	return &fakePython{
		t: t, events: make(chan []byte, 256), attachSeq: map[string]int{},
	}
}

// eventsCh / swapEvents 取、换当前的上行通道。**必须经锁**：模拟"上行断了"
// 是先换新通道、再关旧通道，而旧连接的处理 goroutine 可能正好在读这个字段。
func (f *fakePython) eventsCh() chan []byte {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.events
}

func (f *fakePython) swapEvents(ch chan []byte) {
	f.mu.Lock()
	f.events = ch
	f.mu.Unlock()
}

// start 起服务并返回基地址。t.Cleanup 里关闭。
func (f *fakePython) start() string {
	srv := httptest.NewServer(http.HandlerFunc(f.serve))
	f.t.Cleanup(srv.Close)
	return srv.URL
}

func (f *fakePython) serve(w http.ResponseWriter, r *http.Request) {
	switch r.URL.Path {
	case "/internal/auth/verify":
		var req map[string]string
		_ = json.NewDecoder(r.Body).Decode(&req)
		f.mu.Lock()
		f.verifies++
		f.mu.Unlock()
		if f.verifyFail.Load() {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		writeVerdict(w, userFor(req["token"]))
	case "/internal/auth/consume-ticket":
		var req map[string]string
		_ = json.NewDecoder(r.Body).Decode(&req)
		f.mu.Lock()
		f.tickets++
		f.mu.Unlock()
		writeVerdict(w, userFor(req["ticket"]))
	case "/internal/vision/viewer":
		var req struct {
			Op       string `json:"op"`
			Viewer   string `json:"viewer"`
			Username string `json:"username"`
			FrameID  *int   `json:"frame_id"`
		}
		_ = json.NewDecoder(r.Body).Decode(&req)
		if req.Op == "detach" {
			f.mu.Lock()
			f.detached = append(f.detached, req.Viewer)
			f.mu.Unlock()
			writeJSON(w, map[string]any{"gen": 1, "replies": []string{}})
			return
		}
		viewer := req.Viewer
		if viewer == "" {
			// 模拟 Python 分配 id：同一用户开第二路连接时 id 必须互异
			// （否则 /metrics 里两条连接会撞成同一条，测试也就分辨不出来了）。
			f.mu.Lock()
			f.attachSeq[req.Username]++
			n := f.attachSeq[req.Username]
			f.mu.Unlock()
			viewer = "v" + req.Username
			if n > 1 {
				viewer += strconv.Itoa(n)
			}
		}
		f.mu.Lock()
		f.attached = append(f.attached, viewer+"="+req.Username)
		f.mu.Unlock()
		replies := []string{}
		if req.FrameID != nil {
			replies = []string{"greet"} // "视频源已开始输出画面"的替身（可观察）
		}
		writeJSON(w, map[string]any{"viewer": viewer, "gen": 1, "replies": replies})
	case "/internal/vision/action":
		var req struct {
			Message json.RawMessage `json:"message"`
		}
		_ = json.NewDecoder(r.Body).Decode(&req)
		f.mu.Lock()
		f.actions = append(f.actions, req.Message)
		f.mu.Unlock()
		writeJSON(w, ActionOutcome{Gen: 1, Clear: true, Replies: []string{"ack"},
			FramedTail: strp("tail")})
	case "/internal/vision/client-frame":
		body, _ := io.ReadAll(r.Body)
		// 先记录、再受闸门阻塞：模拟"Python 手上这一帧还没处理完"，
		// 而请求仍悬在空中（HTTP 没回）。Go 侧的在途语义据此可判定。
		f.mu.Lock()
		f.uploads = append(f.uploads, body)
		f.mu.Unlock()
		if gate := f.uploadGate(); gate != nil {
			<-gate
		}
		w.WriteHeader(http.StatusNoContent)
	case "/internal/vision/events":
		f.eventConns.Add(1)
		ch := f.eventsCh() // 取一次：换通道只影响**下一次**连接
		w.Header().Set("Content-Type", "application/octet-stream")
		w.WriteHeader(http.StatusOK)
		flusher, _ := w.(http.Flusher)
		for {
			select {
			case <-r.Context().Done():
				return
			case b, ok := <-ch:
				if !ok {
					return
				}
				if _, err := w.Write(b); err != nil {
					return
				}
				if flusher != nil {
					flusher.Flush()
				}
			}
		}
	default:
		http.NotFound(w, r)
	}
}

func (f *fakePython) uploadGate() chan struct{} {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.blockUploads
}

func (f *fakePython) setUploadGate(g chan struct{}) {
	f.mu.Lock()
	f.blockUploads = g
	f.mu.Unlock()
}

// userFor 令牌表：good → alice，good-ticket → bob（票据路线用不同的人，便于区分）。
func userFor(token string) *userRow {
	switch token {
	case "good":
		return &userRow{valid: true, id: 7, username: "alice", role: "platform"}
	case "good-ticket":
		return &userRow{valid: true, id: 8, username: "bob", role: "viewer"}
	default:
		return &userRow{}
	}
}

type userRow struct {
	valid    bool
	id       int
	username string
	role     string
}

func writeVerdict(w http.ResponseWriter, u *userRow) {
	out := map[string]any{"valid": u.valid, "auth_enabled": true, "user": nil}
	if u.valid {
		out["user"] = map[string]any{"id": u.id, "username": u.username, "role": u.role}
	}
	writeJSON(w, out)
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

// 断言助手（都在锁内取快照，避免与请求 goroutine 竞争）。

func (f *fakePython) attachRecords() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.attached...)
}

func (f *fakePython) detachRecords() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.detached...)
}

func (f *fakePython) actionRecords() []json.RawMessage {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]json.RawMessage(nil), f.actions...)
}

func (f *fakePython) uploadRecords() [][]byte {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([][]byte(nil), f.uploads...)
}

func (f *fakePython) ticketCalls() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.tickets
}

func (f *fakePython) verifyCalls() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.verifies
}

// waitAttach 等到出现某条 attach 记录（超时即 Fatal）。
func (f *fakePython) waitAttach(want string, timeout time.Duration) {
	f.t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		for _, rec := range f.attachRecords() {
			if rec == want {
				return
			}
		}
		time.Sleep(5 * time.Millisecond)
	}
	f.t.Fatalf("等待 attach %q 超时，实际记录 %v", want, f.attachRecords())
}

// envelopeBytes 按 Python 的 `_envelope()` 组装一条信封（测试内独立一份，
// 与 contract_test.go 的逐字节断言互为佐证）。
// 布局：magic(4) | ver(1) | kind(1) | frame_id(4) | metaLen(4) | jpegLen(4) | meta | jpeg
func envelopeBytes(kind byte, frameID int, meta string, jpeg []byte) []byte {
	out := []byte(envelopeMagic)
	out = append(out, envelopeVersion, kind)
	out = binary.BigEndian.AppendUint32(out, uint32(frameID))
	out = binary.BigEndian.AppendUint32(out, uint32(len(meta)))
	out = binary.BigEndian.AppendUint32(out, uint32(len(jpeg)))
	out = append(out, meta...)
	return append(out, jpeg...)
}

// frameEnvelope 组装 kind=1（帧）信封：meta.msg 是下发给浏览器的帧元数据文本。
func frameEnvelope(gen, frameID int, msg string, jpeg []byte) []byte {
	meta := `{"gen":` + strconv.Itoa(gen) + `,"frame_id":` + strconv.Itoa(frameID) +
		`,"msg":` + quoteJSON(msg) + `}`
	return envelopeBytes(envelopeKindFrame, frameID, meta, jpeg)
}

// controlEnvelope 组装 kind=2（控制）信封（except / framedTail 传空串即 null）。
func controlEnvelope(gen int, clear bool, except, framedTail string, texts ...string) []byte {
	meta := `{"gen":` + strconv.Itoa(gen) + `,"clear":` + boolStr(clear) +
		`,"except":` + optStr(except) + `,"framed_tail":` + optStr(framedTail) +
		`,"texts":[` + joinQuoted(texts) + `]}`
	return envelopeBytes(envelopeKindControl, 0, meta, nil)
}

// heartbeatEnvelope 组装 kind=3（心跳）信封（meta 与 jpeg 皆空）。
func heartbeatEnvelope() []byte {
	return envelopeBytes(envelopeKindHeartbeat, 0, "", nil)
}

// ingestEnvelope 组装 kind=4（入库）信封；extra 是除 gen/seq 外的 JSON 字段
// （如 `"alerts":[...]`），传空串则只有 gen/seq。
func ingestEnvelope(gen, seq int, extra string) []byte {
	meta := `{"gen":` + strconv.Itoa(gen) + `,"seq":` + strconv.Itoa(seq)
	if extra != "" {
		meta += "," + extra
	}
	meta += "}"
	return envelopeBytes(envelopeKindIngest, 0, meta, nil)
}

func boolStr(b bool) string {
	if b {
		return "true"
	}
	return "false"
}

func optStr(s string) string {
	if s == "" {
		return "null"
	}
	return quoteJSON(s)
}

func quoteJSON(s string) string {
	b, _ := json.Marshal(s)
	return string(b)
}

func joinQuoted(items []string) string {
	parts := make([]string, 0, len(items))
	for _, s := range items {
		parts = append(parts, quoteJSON(s))
	}
	return strings.Join(parts, ",")
}
