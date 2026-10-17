package report

import "sync"

// Hub 订阅连接集合与扇出中枢。**只登记连接，不保存历史消息**：
// 汇报是"推送时点在线的订阅者"语义，后连上来的客户端不该收到旧汇报
// （与迁移前的 Python 实现一致——它也只遍历当时的 `_report_clients`）。
type Hub struct {
	stats *Stats

	mu      sync.Mutex
	clients map[*client]struct{}
}

func newHub(stats *Stats) *Hub {
	return &Hub{stats: stats, clients: make(map[*client]struct{})}
}

// Add 登记一条连接。
func (h *Hub) Add(c *client) {
	h.mu.Lock()
	h.clients[c] = struct{}{}
	n := len(h.clients)
	h.mu.Unlock()
	h.stats.setClients(n)
}

// Remove 注销一条连接（幂等）。
func (h *Hub) Remove(c *client) {
	h.mu.Lock()
	if _, ok := h.clients[c]; !ok {
		h.mu.Unlock()
		return
	}
	delete(h.clients, c)
	n := len(h.clients)
	h.mu.Unlock()
	h.stats.setClients(n)
}

// broadcast 把同一条消息投给当前所有连接，返回投递到的连接数。
// 锁外逐条投递；单条连接满队列时由它自己丢旧留新，**绝不阻塞发布者**。
func (h *Hub) broadcast(msg []byte) int {
	clients := h.snapshot()
	for _, c := range clients {
		c.offer(msg)
	}
	return len(clients)
}

// snapshot 取当前连接表的副本。
func (h *Hub) snapshot() []*client {
	h.mu.Lock()
	defer h.mu.Unlock()
	out := make([]*client, 0, len(h.clients))
	for c := range h.clients {
		out = append(out, c)
	}
	return out
}

// each 遍历当前连接（给关闭用）。
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
