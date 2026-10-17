package report

import (
	"context"
	"sync"

	"retail-assistant/edge/internal/wsutil"
)

// maxPendingReports 每条连接待发汇报的队列上限。
//
// 汇报是低频消息（分钟级），16 条已经意味着"这条连接早就不健康了"。
// 满时**丢最旧留最新**：订阅者最需要的是最近一条运营汇报，而不是积压的旧摘要。
const maxPendingReports = 16

// client 一个订阅连接。读侧只做存活处理（ping/pong/close 由 wsutil 内部消化），
// 写侧是唯一的 socket 写者。
type client struct {
	conn  *wsutil.Conn
	stats *Stats

	mu    sync.Mutex
	queue [][]byte
	wake  chan struct{} // 容量 1 的"有活干"门铃

	cancel context.CancelFunc // 连接级取消（由 Handler 设置）
	loops  sync.WaitGroup     // 本连接的读/写循环（收尾时等它们退出）
}

func newClient(conn *wsutil.Conn, stats *Stats) *client {
	return &client{
		conn:  conn,
		stats: stats,
		wake:  make(chan struct{}, 1),
	}
}

// shutdown 由 Handler.Close 调用：取消并关闭连接（幂等；teardown 随后照常收尾）。
func (c *client) shutdown() {
	if c.cancel != nil {
		c.cancel()
	}
	_ = c.conn.Close()
}

// offer 投递一条汇报（生产者侧，**永不阻塞**）。队列满时丢最旧的一条。
// 返回值只表示"消息已进入队列"，不表示对端已经收到。
func (c *client) offer(msg []byte) bool {
	c.mu.Lock()
	if len(c.queue) >= maxPendingReports {
		c.queue = c.queue[1:]
		c.stats.incDropped()
	}
	c.queue = append(c.queue, msg)
	c.mu.Unlock()

	select {
	case c.wake <- struct{}{}:
	default:
	}
	return true
}

// take 取队首的一条汇报（消费者侧）。
func (c *client) take() []byte {
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.queue) == 0 {
		return nil
	}
	msg := c.queue[0]
	c.queue = c.queue[1:]
	return msg
}

// writeLoop 唯一的 socket 写者。文本消息按入队顺序逐条写出；
// 写失败（含 wsutil 的 15s 写超时）即结束连接。
func (c *client) writeLoop(ctx context.Context) {
	for {
		if msg := c.take(); msg != nil {
			if err := c.conn.WriteText(string(msg)); err != nil {
				return
			}
			c.stats.incDelivered()
			continue
		}
		select {
		case <-ctx.Done():
			return
		case <-c.wake:
		}
	}
}

// readLoop 读侧：汇报通道是单向的，入站文本/二进制一律忽略。
// 必须持续读——否则客户端发来的 close/pong 得不到处理，TCP 背压也会堆在服务端。
func (c *client) readLoop(ctx context.Context) {
	for {
		if _, _, err := c.conn.ReadMessage(); err != nil {
			return
		}
	}
}
