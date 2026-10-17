package video

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"sync"
	"sync/atomic"
	"time"
)

// 上行订阅的健康参数。**这两个数与 Python 侧的宽限期成对**：
// vision/session.py 的 `_EDGE_GRACE_SECONDS = 10` 意味着"Go 的上行流断了超过
// 10 秒 → Python 判定它登记过的观看者都已失联并逐个注销"，而"最后一个观看者
// 离开即停会话"会把整个视频停掉。所以**断开检测 + 重连必须在这 10 秒内完成**：
//
//	最坏路径 = stallTimeout（判停滞） + retryMax（最后一次退避等待） = 4 + 2 = 6s
//
// 心跳每秒一次（vision_internal.py 的 _HEARTBEAT_SECONDS），停滞 4s 即"连丢 4 拍"，
// 是明确的僵死信号（半开 TCP、中间设备静默丢弃），继续等只会让观看者看定格画面。
const (
	stallTimeout  = 4 * time.Second
	stallCheck    = time.Second
	retryMin      = 300 * time.Millisecond
	retryMax      = 2 * time.Second
	healthyPeriod = 30 * time.Second // 连续在线超过它，重连退避重置为最小
)

// frameMeta kind=1（帧）的 meta。msg 是**下发用的帧元数据 WS 文本**，由 Python
// 组装好（含中文），Go 只决定发不发（节拍见 MetadataInterval）。
type frameMeta struct {
	Gen     int    `json:"gen"`
	FrameID int    `json:"frame_id"`
	Msg     string `json:"msg"`
}

// ControlEvent kind=2（控制）的 meta，字段与 vision/session.py 的 ControlEvent
// 一一对应（`except` 是"跳过这个观看者"，发起人自己已从 HTTP 回执拿到内容）。
type ControlEvent struct {
	Gen        int      `json:"gen"`
	Clear      bool     `json:"clear"`
	Except     *string  `json:"except"`
	FramedTail *string  `json:"framed_tail"`
	Texts      []string `json:"texts"`
}

// IngestSink kind=4 事件的消费者（由 store.Store 实现）。
//
// **必须非阻塞**：它在读取上行流的 goroutine 里被调用，任何等待都会把
// TCP 背压传回 Python，进而拖停视频帧流。store 的入口队列满时丢弃并记账。
type IngestSink interface {
	EnqueueIngest(gen, seq int64, meta []byte)
}

// ingestMeta kind=4 的最小解析：只有 gen/seq 是 Go 分发需要的，其余字段
// 由 sink（store）按 `contracts/ingest_events.json` 解析。
type ingestMeta struct {
	Gen int64 `json:"gen"`
	Seq int64 `json:"seq"`
}

// Upstream 常驻订阅 Python 的上行帧流（`/internal/vision/events`）：连接 → 读信封
// → 分发 → 断了就退避重连。一条 goroutine，没有并发读者。
type Upstream struct {
	api    *API
	hub    *Hub
	stats  *Stats
	ingest IngestSink // nil = 本进程不落库（GO_OWNS_INGEST_WRITES=0）

	kick chan struct{} // 新观看者到来：提前结束退避等待（让"有人在等画面"时重连更快）
	done chan struct{}

	mu      sync.Mutex
	cancel  context.CancelFunc
	started bool
	closed  bool

	noSinkOnce sync.Once
}

func newUpstream(api *API, hub *Hub, stats *Stats, ingest IngestSink) *Upstream {
	return &Upstream{
		api: api, hub: hub, stats: stats, ingest: ingest,
		kick: make(chan struct{}, 1),
		done: make(chan struct{}),
	}
}

// Start 启动常驻订阅（非阻塞）。ctx 一般来自 main，取消即退出。
func (u *Upstream) Start(ctx context.Context) {
	ctx, cancel := context.WithCancel(ctx)
	u.mu.Lock()
	u.cancel, u.started = cancel, true
	u.mu.Unlock()
	go u.run(ctx)
}

// Kick 提示"有新观看者连上了"：若此刻正在退避等待，立刻重连。
func (u *Upstream) Kick() {
	select {
	case u.kick <- struct{}{}:
	default:
	}
}

// Close 停止订阅并断开当前连接（幂等；未 Start 时是空操作）。
func (u *Upstream) Close() {
	u.mu.Lock()
	u.closed = true
	cancel, started := u.cancel, u.started
	u.mu.Unlock()
	if !started {
		return
	}
	cancel()
	<-u.done // 等读取 goroutine 收尾（断开是立即的：取消请求 ctx 即关连接）
}

// run 重连主循环。**永远不退出**（除非 ctx 结束）：上行断了就是"暂时没有画面"，
// 观看者的 WS 不该因此被牵连断开 —— 画面恢复后他们继续收帧。
func (u *Upstream) run(ctx context.Context) {
	defer close(u.done)
	backoff := retryMin
	for ctx.Err() == nil {
		start := time.Now()
		err := u.session(ctx)
		if ctx.Err() != nil {
			return
		}
		u.stats.upstreamDown()
		if time.Since(start) >= healthyPeriod {
			backoff = retryMin // 稳定在线后断开：从头退避，别一上来就等 2 秒
		}
		u.stats.reconnect()
		slog.Warn("上行帧流断开，准备重连",
			"err", err.Error(), "在线", time.Since(start).Round(time.Millisecond).String(),
			"退避", backoff.String())
		select {
		case <-ctx.Done():
			return
		case <-u.kick:
			backoff = retryMin
			continue // 有人正等着画面：立刻重连，不等待
		case <-time.After(backoff):
		}
		if backoff < retryMax {
			backoff *= 2
			if backoff > retryMax {
				backoff = retryMax
			}
		}
	}
}

// session 一次订阅的生命周期：连上 → 逐条读信封分发，直到出错或停滞。
func (u *Upstream) session(ctx context.Context) error {
	// 本次订阅的子 ctx：看门狗靠取消它来掐断一次卡死的读取。
	sessCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	body, err := u.api.Events(sessCtx)
	if err != nil {
		return err
	}
	defer body.Close()

	u.stats.upstreamUp()
	slog.Info("上行帧流已连接")
	last := &atomic.Int64{}
	last.Store(time.Now().UnixNano())
	u.stats.heartbeatAt(last.Load())
	go u.watchdog(sessCtx, cancel, last)

	for {
		env, err := readEnvelope(body)
		if err != nil {
			return err
		}
		last.Store(time.Now().UnixNano())
		u.stats.heartbeatAt(last.Load())
		u.dispatch(env)
	}
}

// watchdog 停滞看门狗：心跳断了 stallTimeout 就掐掉这次订阅（触发重连）。
//
// 没有它的话，一条半开连接会静静地挂在那里：观看者看到的是定格的最后一帧，
// 而 Python 那边 10 秒后就把观众全注销了。
func (u *Upstream) watchdog(ctx context.Context, cancel context.CancelFunc, last *atomic.Int64) {
	t := time.NewTicker(stallCheck)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			if time.Since(time.Unix(0, last.Load())) > stallTimeout {
				slog.Warn("上行帧流停滞，主动断开重连", "停滞上限", stallTimeout.String())
				cancel()
				return
			}
		}
	}
}

// dispatch 分发一条信封。**解析失败只丢这一条**（信封自带长度，不会失步），
// 连接照常：为一条坏 meta 掐断整条视频流不划算。
func (u *Upstream) dispatch(env *envelope) {
	switch env.kind {
	case envelopeKindFrame:
		var m frameMeta
		if err := json.Unmarshal(env.meta, &m); err != nil {
			u.stats.decodeError()
			slog.Warn("帧信封 meta 解析失败，已丢弃该帧", "err", err.Error())
			return
		}
		u.stats.frame()
		u.hub.publishFrame(&outFrame{
			gen:     m.Gen,
			frameID: m.FrameID,
			packet:  framePacket(m.FrameID, env.jpeg),
			meta:    m.Msg,
		})
	case envelopeKindControl:
		var ctl ControlEvent
		if err := json.Unmarshal(env.meta, &ctl); err != nil {
			u.stats.decodeError()
			slog.Warn("控制信封 meta 解析失败，已丢弃该事件", "err", err.Error())
			return
		}
		u.stats.control()
		u.hub.broadcastControl(ctl)
	case envelopeKindHeartbeat:
		u.stats.heartbeat()
	case envelopeKindIngest:
		var m ingestMeta
		if err := json.Unmarshal(env.meta, &m); err != nil || m.Gen <= 0 || m.Seq <= 0 {
			u.stats.decodeError()
			slog.Warn("入库信封 meta 不合法，已丢弃该事件", "err", err)
			return
		}
		u.stats.ingestEvent()
		if u.ingest == nil {
			// Python 侧开了 GO_OWNS_INGEST_WRITES 而 edge 没开：数据无人落库。
			// 每条都打日志会刷屏，只响亮地提醒一次；计数在 /metrics 里持续可见。
			u.stats.ingestDropped()
			u.noSinkOnce.Do(func() {
				slog.Error("收到入库事件但本进程未开启 GO_OWNS_INGEST_WRITES=1，数据不会落库")
			})
			return
		}
		u.ingest.EnqueueIngest(m.Gen, m.Seq, env.meta)
	default:
		u.stats.decodeError()
		slog.Warn("未知信封类型，已忽略", "kind", env.kind)
	}
}

// String 便于日志（信封内容不进日志：可能含帧元数据）。
func (e *envelope) String() string {
	return fmt.Sprintf("kind=%d frame_id=%d meta=%dB jpeg=%dB",
		e.kind, e.frameID, len(e.meta), len(e.jpeg))
}
