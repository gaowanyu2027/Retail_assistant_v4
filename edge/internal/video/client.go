package video

import (
	"context"
	"encoding/json"
	"log/slog"
	"sync"
	"sync/atomic"

	"retail-assistant/edge/internal/wsutil"
)

// maxPendingTexts 待发文本队列上限。控制消息量级是"每次动作一两条"，
// 64 已经是"这条连接早就不健康了"的量；真到上限就丢新的并计数。
const maxPendingTexts = 64

// outFrame 一帧待发内容。**hub 组装一次**（含 [4B frame_id][JPEG] 的封包），
// 全体观看者共享同一份只读字节 —— 扇出不复制像素。
type outFrame struct {
	gen     int
	frameID int
	packet  []byte
	meta    string // 帧元数据文本（发不发按节拍，见 shouldSendMetadata）
}

// client 一个观看者的连接状态（连接即会话）。范本见 vision/session.py 的 Viewer
// 与 api/routes/stream.py 的每条连接局部量（last_sent / running_gen）：
//
//	mu 保护"待发帧位 + 待发文本 + 已发帧号 + 所属代"，生产者在 hub 侧
//	（上行订阅 goroutine）、消费者是本连接的写者 goroutine。**锁内绝不写 socket**：
//	写者先取走内容、放锁，再写 —— 否则慢客户端会把 hub 一并拖住。
type client struct {
	endpoint string // "stream" / "client"
	conn     *wsutil.Conn
	api      *API
	stats    *Stats

	mu           sync.Mutex
	viewerID     string
	username     string
	pending      *outFrame
	texts        []string
	lastSent     int // 本连接已推送的最大帧号（-1 = 还没收到过帧）
	runGen       int // 已发过"视频源已开始输出画面"的那一代
	textsDropped int64

	wake chan struct{} // 容量 1 的"有活干"门铃

	// /api/ws/client（浏览器本机摄像头）的上行合并槽：在途时只覆盖，不排队
	// —— 等价迁移前的 `deque(maxlen=3)` 语义（旧帧本来也没人看）。
	upMu   sync.Mutex
	upload []byte
	upWake chan struct{}

	cancel context.CancelFunc // 连接级取消（由 Handler 设置）

	framesSent    atomic.Int64
	framesDropped atomic.Int64
	lastFrameID   atomic.Int64

	loops sync.WaitGroup // 本连接的读/写循环（收尾时等它们退出）
}

func newClient(endpoint string, conn *wsutil.Conn, api *API, stats *Stats) *client {
	return &client{
		endpoint: endpoint, conn: conn, api: api, stats: stats,
		lastSent: -1, runGen: -1,
		wake:   make(chan struct{}, 1),
		upWake: make(chan struct{}, 1),
	}
}

// shutdown 由 Handler.Close 调用：取消并关闭连接（幂等；teardown 随后照常收尾）。
func (c *client) shutdown() {
	c.cancel()
	_ = c.conn.Close()
}

// ==================== 生产者侧（hub / 上行订阅） ====================

// offerFrame 投递一帧：待发帧位**只有一个** —— 满则丢旧留新。
// 这是背压规格的核心（契约里写死的部分）：慢客户端丢的是自己过时的帧，
// 既不会阻塞发布者，也不会让快客户端少收一帧。
func (c *client) offerFrame(f *outFrame) {
	c.mu.Lock()
	if c.pending != nil {
		c.framesDropped.Add(1)
	}
	c.pending = f
	c.wakeLocked()
	c.mu.Unlock()
}

// applyControl 应用一条控制事件，逐条对应 api/routes/stream.py 的处理顺序：
// 跳过发起人 → texts 按序 → framed_tail（判据在 clear 复位**之前**求值）→ clear。
func (c *client) applyControl(ctl ControlEvent) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if ctl.Except != nil && *ctl.Except == c.viewerID {
		return // 发起人自己已从 HTTP 回执里拿到，不重复发
	}
	c.appendTextsLocked(ctl.Texts...)
	if ctl.FramedTail != nil && c.lastSent >= 0 {
		c.appendTextsLocked(*ctl.FramedTail)
	}
	if ctl.Clear {
		c.pending = nil // 此前待发的帧作废
		c.lastSent = -1
		c.runGen = -1 // 与迁移前一致：控制事件 clear 会连"已问候过的代"一并复位
	}
	c.wakeLocked()
}

// applyOutcome 应用一次动作回执（只发给发起人自己）。顺序与迁移前一致：
// replies → framed_tail（判据在 clear 之前）→ clear。
func (c *client) applyOutcome(o *ActionOutcome) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.appendTextsLocked(o.Replies...)
	if o.FramedTail != nil && c.lastSent >= 0 {
		c.appendTextsLocked(*o.FramedTail)
	}
	if o.Clear {
		c.pending = nil
		c.lastSent = -1
	}
	c.wakeLocked()
}

// enqueueTexts 追加文本（不经 clear 语义，如 pong 应答）。
func (c *client) enqueueTexts(texts ...string) {
	if len(texts) == 0 {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.appendTextsLocked(texts...)
	c.wakeLocked()
}

func (c *client) appendTextsLocked(texts ...string) {
	for _, t := range texts {
		if len(c.texts) >= maxPendingTexts {
			c.textsDropped++
			continue
		}
		c.texts = append(c.texts, t)
	}
}

// wakeLocked 按门铃（非阻塞）：写者可能正在写上一批内容，醒来后自会取空。
func (c *client) wakeLocked() {
	select {
	case c.wake <- struct{}{}:
	default:
	}
}

// ==================== 消费者侧（本连接的写者 goroutine） ====================

// writeLoop 唯一的 socket 写者。顺序规则（**文本严格优先于帧**）：
// 先把待发文本清空，再取待发帧 —— 这样"视频流已停止"这类控制消息
// 永远不会被队列里的一帧插到前面（帧的旧版本已经在 clear 时丢掉了）。
func (c *client) writeLoop(ctx context.Context) {
	for {
		if ctx.Err() != nil {
			return
		}
		if texts := c.takeTexts(); len(texts) > 0 {
			for _, t := range texts {
				if err := c.conn.WriteText(t); err != nil {
					return
				}
			}
			continue
		}
		if f := c.takeFrame(); f != nil {
			if !c.deliverFrame(ctx, f) {
				return
			}
			continue
		}
		select {
		case <-ctx.Done():
			return
		case <-c.wake:
		}
	}
}

func (c *client) deliverFrame(ctx context.Context, f *outFrame) bool {
	// 换代判据与迁移前逐字一致：`gen != running_gen or last_sent < 0`。
	// 后半个条件在"动作回执带 clear"后仍会命中（那一路只复位 last_sent、
	// 不动 running_gen），所以两处都得看。
	c.mu.Lock()
	greet := f.gen != c.runGen || c.lastSent < 0
	c.runGen = f.gen
	c.mu.Unlock()
	if greet {
		// 向 Python 取"视频源已开始输出画面"问候（attach 幂等）。
		for _, t := range c.syncViewer(ctx, f.frameID) {
			if err := c.conn.WriteText(t); err != nil {
				return false
			}
		}
	}
	if shouldSendMetadata(f.frameID, MetadataInterval) {
		if err := c.conn.WriteText(f.meta); err != nil {
			return false
		}
	}
	if err := c.conn.WriteBinary(f.packet); err != nil {
		return false
	}
	c.framesSent.Add(1)
	c.lastFrameID.Store(int64(f.frameID))
	c.mu.Lock()
	c.lastSent = f.frameID
	c.mu.Unlock()
	return true
}

// syncViewer 登记（幂等）并取问候。失败返回 nil（不阻塞帧：视频照常走，
// 只是这条连接错过了那句状态文本）。
func (c *client) syncViewer(ctx context.Context, frameID int) []string {
	c.mu.Lock()
	viewerID, username := c.viewerID, c.username
	c.mu.Unlock()
	sync, err := c.api.Attach(ctx, viewerID, username, &frameID)
	if err != nil {
		slog.Warn("观看者登记失败", "endpoint", c.endpoint, "err", err.Error())
		return nil
	}
	c.mu.Lock()
	c.viewerID = sync.Viewer
	c.mu.Unlock()
	return sync.Replies
}

// readLoop /api/ws/stream 的读侧：客户端动作。ping 就地应答（纯函数，
// 不必绕 Python 一圈），其余原文转发给 Python —— **Go 不解释业务动作**。
//
// Add/Done 一律由启动方（handler.serveStream）记账，本函数自己不碰 loops：
// 两边都记就是负数计数（panic）。下面的 readUploads 同理。
func (c *client) readLoop(ctx context.Context) {
	for {
		op, payload, err := c.conn.ReadMessage()
		if err != nil {
			return
		}
		if op != wsutil.TextMessage {
			continue // 上行只认文本动作；二进制帧忽略（见差异清单）
		}
		action, typ, ts, ok := parseAction(payload)
		if !ok {
			continue // 非 JSON 对象：迁移前同样是静默忽略
		}
		if action == "ping" || typ == "ping" {
			c.enqueueTexts(pongFor(ts))
			continue
		}
		c.mu.Lock()
		viewerID, username := c.viewerID, c.username
		c.mu.Unlock()
		out, err := c.api.Action(ctx, viewerID, username, payload)
		if err != nil {
			slog.Warn("动作转发失败", "endpoint", c.endpoint, "err", err.Error())
			continue
		}
		c.applyOutcome(out)
	}
}

// readUploads /api/ws/client 的读侧：只认二进制帧（浏览器本机摄像头的原始 JPEG）。
// 文本帧忽略 —— 这一路没有动作协议（与迁移前的差异见交付文档清单）。
func (c *client) readUploads(ctx context.Context) {
	for {
		op, payload, err := c.conn.ReadMessage()
		if err != nil {
			return
		}
		if op != wsutil.BinaryMessage {
			continue
		}
		c.offerUpload(payload)
	}
}

// offerUpload 投进合并槽：**在途时只覆盖**（在途的那帧已经在发给 Python 的路上，
// 攒下来的旧帧没有意义）。丢弃计入 framesDropped（这条连接的 endpoint=client，
// 指标里与观看端的丢帧不会混）。
func (c *client) offerUpload(jpeg []byte) {
	c.upMu.Lock()
	if c.upload != nil {
		c.framesDropped.Add(1)
	}
	c.upload = jpeg
	c.upMu.Unlock()
	select {
	case c.upWake <- struct{}{}:
	default:
	}
}

func (c *client) takeUpload() []byte {
	c.upMu.Lock()
	defer c.upMu.Unlock()
	j := c.upload
	c.upload = nil
	return j
}

// pumpUploads 上行泵：把合并槽里的最新帧转发给 Python（归属判定在那边）。
// 单条 POST 最多 5s（frameTimeout），超时即丢这一帧继续 —— 绝不排队堆积。
func (c *client) pumpUploads(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case <-c.upWake:
		}
		jpeg := c.takeUpload()
		if jpeg == nil {
			continue
		}
		c.mu.Lock()
		username := c.username
		c.mu.Unlock()
		if err := c.api.ClientFrame(ctx, username, jpeg); err != nil {
			slog.Warn("本机摄像头帧转发失败", "err", err.Error())
			continue
		}
		c.framesSent.Add(1)
	}
}

// ==================== 小工具 ====================

func (c *client) viewer() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.viewerID
}

func (c *client) setViewer(id, username string) {
	c.mu.Lock()
	c.viewerID, c.username = id, username
	c.mu.Unlock()
}

func (c *client) takeTexts() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.texts) == 0 {
		return nil
	}
	out := c.texts
	c.texts = nil
	return out
}

func (c *client) takeFrame() *outFrame {
	c.mu.Lock()
	defer c.mu.Unlock()
	f := c.pending
	c.pending = nil
	return f
}

// parseAction 取出路由判据。返回原始 ts 片段：pong 要**逐字**回给客户端
// （Python 侧是 dumps_message(pong_message(msg.get("ts", 0)))）。
func parseAction(msg []byte) (action, typ string, ts json.RawMessage, ok bool) {
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(msg, &obj); err != nil || obj == nil {
		return "", "", nil, false
	}
	_ = json.Unmarshal(obj["action"], &action)
	_ = json.Unmarshal(obj["type"], &typ)
	return action, typ, obj["ts"], true
}

// pongFor 组装 ping 应答。与 ws_protocol.py 的 pong_message 逐字节一致：
//
//	{"type":"pong","ts":<ts>}      缺 ts 时补 0
//
// ts 直接内联客户端给的原始 JSON 片段 —— 数字（Date.now() 是整数）、
// null、字符串都保真；唯一与 Python 有别的是非规范数字写法（如 1e3），
// 语义相同、字节不同，真实客户端不会这么发。
func pongFor(ts json.RawMessage) string {
	if len(ts) == 0 {
		ts = json.RawMessage("0")
	}
	return `{"type":"pong","ts":` + string(ts) + `}`
}

// readyMessage `/api/ws/client` 连接后的首条消息（Go 自行合成的两条之一，
// 与 Python 的 send_json({"event":"ready"}) 逐字节一致）。
const readyMessage = `{"event":"ready"}`
