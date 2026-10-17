package video

import "sync"

// Hub 上行帧流与客户端集合之间的扇出中枢。**它只保留最新一帧**（给新连接
// 一个起步画面），不做队列：每条连接自己的"待发帧位"已经是容量 1 的最新帧
// 槽位，hub 再排一次队就成了"慢客户端的积压缓冲"，与背压规格相反。
//
// 不变式（背压规格的可验收部分）：
//   - 发布者（上行读取 goroutine）**永不阻塞**：broadcast* 只做"上锁改指针"，
//     不做任何写 socket、不等任何连接；
//   - 慢客户端丢的是自己过时的帧，快客户端一帧不少（客户端各自计
//     frames_dropped，见 client.offerFrame）。
type Hub struct {
	stats *Stats

	mu      sync.Mutex
	clients map[*client]struct{}
	latest  *outFrame
}

func newHub(stats *Stats) *Hub {
	return &Hub{stats: stats, clients: make(map[*client]struct{})}
}

// Add 登记一条连接；如果此刻有正在跑的会话，顺手把最新帧投给它 ——
// 新连接不必等下一帧（12fps 下最多 80ms）才出画面。**不动 hub.latest**：
// 别人正在收的画面不该因为多了一个观看者而复位（见差异清单）。
func (h *Hub) Add(c *client) {
	h.mu.Lock()
	h.clients[c] = struct{}{}
	latest := h.latest
	n := len(h.clients)
	h.mu.Unlock()
	h.stats.setViewers(n)
	if latest != nil {
		c.offerFrame(latest)
	}
}

// Remove 注销连接（幂等）。断开时把自己还留在帧位上的那一帧放掉：
// 客户端对象随后就没人引用，留着只是浪费一份 90KB。
func (h *Hub) Remove(c *client) {
	h.mu.Lock()
	if _, ok := h.clients[c]; !ok {
		h.mu.Unlock()
		return
	}
	delete(h.clients, c)
	n := len(h.clients)
	h.mu.Unlock()
	c.dropPending()
	h.stats.setViewers(n)
}

// publishFrame 发布一帧（帧事件的唯一入口）：先把"最新帧"指针换掉，
// 再锁外逐条投递 —— 慢客户端绝不会拖住这一步。
func (h *Hub) publishFrame(f *outFrame) {
	h.mu.Lock()
	h.latest = f
	clients := h.snapshotLocked()
	h.mu.Unlock()
	for _, c := range clients {
		c.offerFrame(f)
	}
}

// broadcastControl 发布一条控制事件。clear 语义要连 hub 的"最新帧"一起清：
// 会话停了以后新连上来的观看者不该看到一张定格的旧画面。
func (h *Hub) broadcastControl(ctl ControlEvent) {
	h.mu.Lock()
	if ctl.Clear {
		h.latest = nil
	}
	clients := h.snapshotLocked()
	h.mu.Unlock()
	for _, c := range clients {
		c.applyControl(ctl)
	}
}

func (h *Hub) snapshotLocked() []*client {
	out := make([]*client, 0, len(h.clients))
	for c := range h.clients {
		out = append(out, c)
	}
	return out
}

// snapshot 取当前连接表的副本。
func (h *Hub) snapshot() []*client {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.snapshotLocked()
}

// each 遍历当前连接（给 /metrics 与关闭用）。
func (h *Hub) each(fn func(*client)) {
	for _, c := range h.snapshot() {
		fn(c)
	}
}

// Count 当前连接数。
func (h *Hub) Count() int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.clients)
}

// dropPending 放掉待发帧（断开/clear 时）。
func (c *client) dropPending() {
	c.mu.Lock()
	c.pending = nil
	c.mu.Unlock()
}
