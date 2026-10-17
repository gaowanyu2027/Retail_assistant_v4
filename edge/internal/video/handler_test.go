package video

import (
	"bytes"
	"encoding/binary"
	"net/http"
	"net/http/httptest"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"retail-assistant/edge/internal/auth"
	"retail-assistant/edge/internal/httpx"
)

// ==================== 测试脚手架 ====================

// newTestHandler 起一个"启用状态"的视频数据面，外面套一个只做分发的服务：
// 接管 → Handler 自己答复；没接管 → 回 299 + PROXIED（代表"落回兜底反代"）。
// 返回 (Handler, ws://地址)。
func newTestHandler(t *testing.T, py *fakePython) (*Handler, string) {
	t.Helper()
	backend := py.start()
	h := New(Options{
		Video: true, AuthEnabled: true, BackendURL: backend,
		InternalToken: "k", Verifier: auth.NewVerifier(backend, "k", auth.Observer{}),
	})
	if !h.Enabled() {
		t.Fatal("测试用 Handler 应为启用状态")
	}
	h.Start()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rw := httpx.NewRespWriter(w)
		if _, handled := h.Handle(rw, r); handled {
			return
		}
		w.WriteHeader(299)
		_, _ = w.Write([]byte("PROXIED"))
	}))
	// 一个 Cleanup 里按顺序关：**先关 Handler 再关假 Python**。顺序反了的话，
	// 假 Python 的 Close 会去等上行事件流那条永不结束的请求（死等）。
	t.Cleanup(func() {
		h.Close()
		waitFor(3*time.Second, func() bool { return h.hub.Count() == 0 })
		time.Sleep(60 * time.Millisecond) // 收尾里的"注销观看者"落地：假 Python 一关它就失败
		srv.Close()
	})
	return h, "ws" + strings.TrimPrefix(srv.URL, "http")
}

func cookie(token string) map[string]string {
	return map[string]string{"Cookie": "retail_sid=" + token}
}

// waitFor 轮询等待条件成立（超时返回 false）。
func waitFor(timeout time.Duration, fn func() bool) bool {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if fn() {
			return true
		}
		time.Sleep(5 * time.Millisecond)
	}
	return fn()
}

// connStat 从 /metrics 快照里取某条连接的计数。
func connStat(t *testing.T, h *Handler, viewer string) viewerStats {
	t.Helper()
	snap, ok := h.MetricsSource()().(videoSnapshot)
	if !ok {
		t.Fatal("/metrics 快照类型不符")
	}
	for _, c := range snap.Connections {
		if c.Viewer == viewer {
			return c
		}
	}
	t.Fatalf("快照里没有观看者 %q（现有 %+v）", viewer, snap.Connections)
	return viewerStats{}
}

// ==================== 鉴权决策树 ====================

// TestHandlerDecisionTree 逐条对照 api/security.py 的 `user_from_scope` 顺序：
// 会话凭据 → `?ticket=`（用后即焚）→ `?token=`（政策在 Python）→ 1008。
func TestHandlerDecisionTree(t *testing.T) {
	const (
		taken = "taken"
		deny  = "deny"
		proxy = "proxy"
	)
	cases := []struct {
		name     string
		path     string
		headers  map[string]string
		verifier bool // verify 端点失败（模拟"拿不准"）
		want     string
		wantUser string
		tickets  int // 期望 consume-ticket 的调用次数（票据用后即焚，一次都不能白烧）
	}{
		{name: "匿名升级→1008", path: pathStream, want: deny},
		{name: "有效 Cookie→接管", path: pathStream,
			headers: cookie("good"), want: taken, wantUser: "alice"},
		{name: "无效 Cookie→1008", path: pathStream,
			headers: cookie("bad"), want: deny},
		{name: "垃圾 Cookie 不挡有效 Bearer", path: pathStream,
			headers: map[string]string{"Cookie": "retail_sid=bad",
				"Authorization": "Bearer good"},
			want: taken, wantUser: "alice"},
		{name: "有效票据→接管", path: pathStream + "?ticket=good-ticket",
			want: taken, wantUser: "bob", tickets: 1},
		{name: "无效票据→1008", path: pathStream + "?ticket=bad",
			want: deny, tickets: 1},
		{name: "有有效 Cookie 时不消费票据", path: pathStream + "?ticket=good-ticket",
			headers: cookie("good"), want: taken, wantUser: "alice"},
		{name: "?token= 一律反代（政策在 Python）", path: pathStream + "?token=xyz",
			want: proxy},
		{name: "回源失败→反代（宁可多判一次，不可误杀）", path: pathStream,
			headers: cookie("good"), verifier: true, want: proxy},
		{name: "本机摄像头端点同样按此判定", path: pathClient,
			headers: cookie("good"), want: taken, wantUser: "alice"},
		{name: "本机摄像头匿名→1008", path: pathClient, want: deny},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			py := newFakePython(t)
			if tc.verifier {
				py.verifyFail.Store(true)
			}
			_, wsBase := newTestHandler(t, py)

			hs := rawHandshake(t, wsBase+tc.path, tc.headers)
			switch tc.want {
			case proxy:
				if hs.status != 299 || string(hs.body) != "PROXIED" {
					t.Fatalf("期望落回反代（299/PROXIED），实得 %d/%q", hs.status, hs.body)
				}
				if n := len(py.attachRecords()); n != 0 {
					t.Errorf("反代路径不该在数据面登记观看者：%v", py.attachRecords())
				}
			case deny:
				if hs.status != 101 {
					t.Fatalf("期望先握手成功再关闭（101），实得 %d", hs.status)
				}
				c := &rawWS{t: t, conn: hs.conn, br: hs.br}
				c.expectClose(1008) // 与 Python 一致：accept 后 close(1008)，无 reason
				if n := len(py.attachRecords()); n != 0 {
					t.Errorf("被拒的连接不该登记观看者：%v", py.attachRecords())
				}
			case taken:
				if hs.status != 101 {
					t.Fatalf("期望接管（101），实得 %d（body=%q）", hs.status, hs.body)
				}
				py.waitAttach("v"+tc.wantUser+"="+tc.wantUser, 2*time.Second)
				c := &rawWS{t: t, conn: hs.conn, br: hs.br}
				if code, isClose, _ := c.tryClose(200 * time.Millisecond); isClose {
					t.Fatalf("接管后不该立刻关闭，却收到 close(%d)", code)
				}
			}
			if got := py.ticketCalls(); got != tc.tickets {
				t.Errorf("consume-ticket 调用 %d 次，期望 %d 次", got, tc.tickets)
			}
		})
	}
}

// TestHandlerIgnoresNonUpgradeRequests 同一个路径上的普通 HTTP 请求语义不变
// （一律交回反代；route_parity 差异为 0 由构造保证）。
func TestHandlerIgnoresNonUpgradeRequests(t *testing.T) {
	py := newFakePython(t)
	h, wsBase := newTestHandler(t, py)
	srvURL := "http" + strings.TrimPrefix(wsBase, "ws")

	for _, path := range []string{pathStream, pathClient} {
		resp, err := http.Get(srvURL + path)
		if err != nil {
			t.Fatalf("GET %s 失败: %v", path, err)
		}
		body := make([]byte, 16)
		n, _ := resp.Body.Read(body)
		_ = resp.Body.Close()
		if resp.StatusCode != 299 || !strings.HasPrefix(string(body[:n]), "PROXIED") {
			t.Errorf("GET %s：期望落回反代，实得 %d/%q", path, resp.StatusCode, body[:n])
		}
	}
	if n := h.hub.Count(); n != 0 {
		t.Errorf("普通请求不该产生视频连接，实得 %d", n)
	}

	// 别的路径的升级请求也不归它管
	hs := rawHandshake(t, wsBase+"/api/ws/other", cookie("good"))
	if hs.status != 299 {
		t.Errorf("/api/ws/other 的升级请求应落回反代，实得 %d", hs.status)
	}
}

// ==================== 上行 → 客户端 ====================

// TestUpstreamFramesAndControlReachClient 信封 → WS 的完整路径：
// 换代问候 → 元数据节拍 → 二进制帧 → 控制文本（顺序与迁移前逐条一致）。
func TestUpstreamFramesAndControlReachClient(t *testing.T) {
	py := newFakePython(t)
	h, wsBase := newTestHandler(t, py)
	c := dialRaw(t, wsBase+pathStream, dialOpts{headers: cookie("good")})
	py.waitAttach("valice=alice", 2*time.Second)

	// 第 1 帧：先"视频源已开始输出画面"问候，再元数据（frame_id=1 必发），最后二进制
	py.events <- frameEnvelope(1, 1, "M1", []byte{0xAA, 0xBB})
	expectText(t, c, "greet")
	expectText(t, c, "M1")
	expectBinary(t, c, 1, []byte{0xAA, 0xBB})

	// 第 2 帧：既不发问候也不发元数据（2 % 5 != 0），只发二进制
	py.events <- frameEnvelope(1, 2, "M2", []byte{0xCC})
	expectBinary(t, c, 2, []byte{0xCC})

	// 控制事件：texts 按序，随后是 framed_tail（此时已收到过帧）
	py.events <- controlEnvelope(1, false, "", "TAIL", "T1", "T2")
	expectText(t, c, "T1")
	expectText(t, c, "T2")
	expectText(t, c, "TAIL")

	// 心跳：不产生任何下发消息
	py.events <- heartbeatEnvelope()
	if op, p, err := c.tryReadFrame(250 * time.Millisecond); err == nil {
		t.Fatalf("心跳不该产生消息，却收到 opcode=%#x payload=%q", op, p)
	}

	// 第 5 帧：元数据节拍命中
	py.events <- frameEnvelope(1, 5, "M5", []byte{0xEE})
	expectText(t, c, "M5")
	expectBinary(t, c, 5, []byte{0xEE})

	snap := h.MetricsSource()().(videoSnapshot)
	if !snap.Upstream || snap.Frames != 3 || snap.Heartbeats != 1 {
		t.Errorf("上行计数不符：%+v", snap)
	}
}

// TestPingAnsweredByEdgeLocally ping 就地应答（客户端消息的纯函数），
// 不转发给 Python —— Python 侧不该出现任何动作调用。
func TestPingAnsweredByEdgeLocally(t *testing.T) {
	py := newFakePython(t)
	_, wsBase := newTestHandler(t, py)
	c := dialRaw(t, wsBase+pathStream, dialOpts{headers: cookie("good")})
	py.waitAttach("valice=alice", 2*time.Second)

	c.writeText(`{"type":"ping","ts":1729000000.25}`)
	expectText(t, c, `{"type":"pong","ts":1729000000.25}`)
	if n := len(py.actionRecords()); n != 0 {
		t.Errorf("ping 不该转发给 Python，实际转发 %d 条", n)
	}
}

// TestActionForwardedVerbatimAndOutcomeApplied 动作原样转发；回执按
// replies → framed_tail（判据在 clear 之前）→ clear 的语义下发。
func TestActionForwardedVerbatimAndOutcomeApplied(t *testing.T) {
	py := newFakePython(t)
	_, wsBase := newTestHandler(t, py)
	c := dialRaw(t, wsBase+pathStream, dialOpts{headers: cookie("good")})
	py.waitAttach("valice=alice", 2*time.Second)

	raw := `{"action":"stop","ts":123}`
	c.writeText(raw)
	// 此时还没收到过帧：framed_tail **不补发**（判据 lastSent >= 0），只有 replies
	expectText(t, c, "ack")
	if op, p, err := c.tryReadFrame(200 * time.Millisecond); err == nil {
		t.Fatalf("未收到过帧就不该补 framed_tail，却收到 opcode=%#x payload=%q", op, p)
	}
	acts := py.actionRecords()
	if len(acts) != 1 || string(acts[0]) != raw {
		t.Errorf("转发的动作 = %q，期望原样 %q", acts, raw)
	}

	// 回执 clear 只复位帧号、不动换代：下一帧命中 lastSent < 0，仍要问候
	py.events <- frameEnvelope(1, 9, "M9", []byte{0x01})
	expectText(t, c, "greet")
	expectBinary(t, c, 9, []byte{0x01})
}

// TestClientEndpointReadyAndUploadMerge 本机摄像头端点：连接后首条是 ready；
// 上行做**最新帧合并**（在途时只覆盖，不排队），断开时向 Python 注销。
func TestClientEndpointReadyAndUploadMerge(t *testing.T) {
	py := newFakePython(t)
	h, wsBase := newTestHandler(t, py)

	gate := make(chan struct{})
	var once sync.Once
	release := func() { once.Do(func() { close(gate) }) }
	defer release() // 用例中途失败也要把卡住的请求放掉，否则清理时死等
	py.setUploadGate(gate)

	c := dialRaw(t, wsBase+pathClient, dialOpts{headers: cookie("good")})
	expectText(t, c, readyMessage)
	py.waitAttach("valice=alice", 2*time.Second)

	c.writeBinary([]byte("frame-1"))
	if !waitFor(2*time.Second, func() bool { return len(py.uploadRecords()) == 1 }) {
		t.Fatalf("第一帧没有到 Python：%d 帧", len(py.uploadRecords()))
	}
	// 第一帧还卡在 Python 手里时再来两帧：合并槽只留最新的一帧
	c.writeBinary([]byte("frame-2"))
	c.writeBinary([]byte("frame-3"))
	time.Sleep(50 * time.Millisecond)
	release()
	if !waitFor(3*time.Second, func() bool { return len(py.uploadRecords()) == 2 }) {
		t.Fatalf("合并后应只再发一帧，实际 %d 帧", len(py.uploadRecords()))
	}
	ups := py.uploadRecords()
	if !bytes.Equal(ups[0], []byte("frame-1")) || !bytes.Equal(ups[1], []byte("frame-3")) {
		t.Errorf("上行序列 = %q，期望 [frame-1 frame-3]（中间那帧该被合并掉）", ups)
	}
	if st := connStat(t, h, "valice"); st.FramesDropped == 0 {
		t.Error("被合并掉的帧应计入 frames_dropped")
	}

	// 断开：向 Python 注销观看者（"最后一个观看者离开即停会话"的输入）
	_ = c.conn.Close()
	if !waitFor(3*time.Second, func() bool { return len(py.detachRecords()) == 1 }) {
		t.Errorf("断开后应注销观看者，实际 %v", py.detachRecords())
	}
}

// ==================== 扇出公平性（S3 验收判据） ====================

// TestFanoutFairness 注入一条"连上但不读"的慢连接：
// 快连接必须一帧不少（frames_dropped = 0、帧号连续），慢连接丢的是自己过时的帧，
// 恢复读取后拿到的是**最新帧**而不是积压。
//
// "慢到开始丢帧"这一步是**实测**出来的（一直发到它真的丢），不靠猜环回缓冲多大：
// 实测本机 Windows 环回能一次吞下 2MB 的写，写死帧数会变成"看机器脸色"。
func TestFanoutFairness(t *testing.T) {
	py := newFakePython(t)
	h, wsBase := newTestHandler(t, py)
	fast := dialRaw(t, wsBase+pathStream, dialOpts{headers: cookie("good")})
	// 慢连接走票据路线（bob）：同一用户连两路也会被分到不同 viewer id，
	// 但用**不同身份**就不必依赖分配顺序 —— 快/慢在 /metrics 里一眼可分。
	slow := dialRaw(t, wsBase+pathStream+"?ticket=good-ticket", dialOpts{})
	slow.shrinkBuffers() // 能小则小（尽量早地进入背压），真正的判据在下面的实测
	py.waitAttach("valice=alice", 2*time.Second)
	py.waitAttach("vbob=bob", 2*time.Second)

	bins := make(chan int, 512)
	go func() {
		defer close(bins)
		for {
			op, p, err := fast.tryReadFrame(3 * time.Second)
			if err != nil {
				return
			}
			if op == wsBinary && len(p) >= 4 {
				bins <- int(binary.BigEndian.Uint32(p[:4]))
			}
		}
	}()

	const (
		per      = 256 << 10 // 每帧 256KB
		maxFrame = 240       // 上限 60MB：够任何机器的环回缓冲填满
		paceDur  = 15 * time.Millisecond
	)
	payload := bytes.Repeat([]byte{0xAB}, per)
	published := 0
	for i := 1; i <= maxFrame; i++ {
		h.hub.publishFrame(&outFrame{
			gen: 1, frameID: i, packet: framePacket(i, payload),
			meta: `{"frame_id":` + strconv.Itoa(i) + `}`,
		})
		published = i
		time.Sleep(paceDur) // 给快连接留出写+读的时间：它该一帧不漏
		if connStat(t, h, "vbob").FramesDropped > 0 {
			break // 慢连接的写者已被背压卡住，开始丢帧
		}
	}
	if st := connStat(t, h, "vbob"); st.FramesDropped == 0 {
		t.Fatalf("发了 %d 帧（%.1f MB）慢连接仍未丢帧：背压没生效，或本机环回缓冲大得不现实",
			published, float64(published*per)/(1<<20))
	}

	// 快连接：一帧不少、帧号连续 —— 这就是"慢连接不拖累别人"
	var got []int
	timeout := time.After(15 * time.Second)
	for len(got) < published {
		select {
		case id, ok := <-bins:
			if !ok {
				t.Fatalf("快连接中断，仅收到 %v", got)
			}
			got = append(got, id)
		case <-timeout:
			t.Fatalf("快连接收帧超时，仅收到 %d/%d 帧：%v", len(got), published, got)
		}
	}
	for i, id := range got {
		if id != i+1 {
			t.Fatalf("快连接帧号不连续：第 %d 个是 %d（应为 %d），全部 %v", i, id, i+1, got)
		}
	}
	if st := connStat(t, h, "valice"); st.FramesDropped != 0 {
		t.Errorf("快连接丢帧 %d 次，期望 0（慢连接不该拖累它）", st.FramesDropped)
	}

	// 慢连接开始读：应迅速追到最新帧，而不是把积压逐张补完
	var slowIDs []int
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		op, p, err := slow.tryReadFrame(time.Second)
		if err != nil {
			break
		}
		if op == wsBinary && len(p) >= 4 {
			id := int(binary.BigEndian.Uint32(p[:4]))
			slowIDs = append(slowIDs, id)
			if id == published {
				break
			}
		}
	}
	if len(slowIDs) == 0 || slowIDs[len(slowIDs)-1] != published {
		t.Fatalf("慢连接恢复后应收到最新帧 %d，实际 %v", published, slowIDs)
	}
	if len(slowIDs) > 3 {
		t.Errorf("慢连接补了 %d 帧积压（%v），期望直接跳到最新帧", len(slowIDs), slowIDs)
	}
}

// TestPeerDisconnectLeavesOtherViewersStreaming 双观看者断开其一：
// 另一条连接持续收帧、不被牵连；注销只发生在那一条上。
func TestPeerDisconnectLeavesOtherViewersStreaming(t *testing.T) {
	py := newFakePython(t)
	h, wsBase := newTestHandler(t, py)
	keep := dialRaw(t, wsBase+pathStream, dialOpts{headers: cookie("good")})
	drop := dialRaw(t, wsBase+pathStream+"?ticket=good-ticket", dialOpts{})
	py.waitAttach("valice=alice", 2*time.Second)
	py.waitAttach("vbob=bob", 2*time.Second)

	h.hub.publishFrame(&outFrame{gen: 1, frameID: 1, packet: framePacket(1, []byte{0x01}), meta: "M1"})
	expectText(t, keep, "greet")
	expectText(t, keep, "M1")
	expectBinary(t, keep, 1, []byte{0x01})

	// bob 断开：只有他被注销
	_ = drop.conn.Close()
	if !waitFor(3*time.Second, func() bool {
		for _, v := range py.detachRecords() {
			if v == "vbob" {
				return true
			}
		}
		return false
	}) {
		t.Fatalf("断开的那条应向 Python 注销，实际 %v", py.detachRecords())
	}
	for _, v := range py.detachRecords() {
		if v == "valice" {
			t.Fatalf("另一条连接不该被注销：%v", py.detachRecords())
		}
	}

	// 留下的一条照常收帧（不中断、不重发问候）
	h.hub.publishFrame(&outFrame{gen: 1, frameID: 2, packet: framePacket(2, []byte{0x02}), meta: "M2"})
	expectBinary(t, keep, 2, []byte{0x02})
	h.hub.publishFrame(&outFrame{gen: 1, frameID: 3, packet: framePacket(3, []byte{0x03}), meta: "M3"})
	expectBinary(t, keep, 3, []byte{0x03})
}

// TestControlTextBeatsQueuedFrame 待发文本**严格优先**于待发帧：
// 先实测把写者逼到"卡在一帧的写里"的状态，再让下一帧躺进待发帧位、最后排入
// 控制文本 —— 复原出写者一抬头就该看见的顺序：…frame:k → STOP → frame:k+1。
func TestControlTextBeatsQueuedFrame(t *testing.T) {
	py := newFakePython(t)
	h, wsBase := newTestHandler(t, py)
	c := dialRaw(t, wsBase+pathStream, dialOpts{headers: cookie("good")})
	py.waitAttach("valice=alice", 2*time.Second)
	c.shrinkBuffers() // 能小则小；真正的判据是下面"写者卡住"的实测

	const (
		per      = 256 << 10
		maxFrame = 240
	)
	payload := bytes.Repeat([]byte{0x5A}, per)

	// ① 一直发到写者卡住（frames_sent 追不上发布的帧号）
	stuck := 0
	for i := 1; i <= maxFrame; i++ {
		h.hub.publishFrame(&outFrame{gen: 1, frameID: i, packet: framePacket(i, payload), meta: "M"})
		if !waitFor(400*time.Millisecond, func() bool {
			return connStat(t, h, "valice").FramesSent >= int64(i)
		}) {
			stuck = i
			break
		}
	}
	if stuck == 0 {
		t.Fatalf("发了 %d 帧（%.1f MB）也没把写者卡住：本机环回缓冲大得不现实，换台机器跑这条",
			maxFrame, float64(maxFrame*per)/(1<<20))
	}
	if stuck == maxFrame {
		t.Fatalf("第 %d 帧正好是上限，状态不明", stuck)
	}
	t.Logf("写者卡在第 %d 帧上（此前 %d 帧已写出）", stuck, stuck-1)

	// ② 下一帧进待发帧位，再排入控制文本：此刻"帧已在位、文本后到"
	h.hub.publishFrame(&outFrame{gen: 1, frameID: stuck + 1, packet: framePacket(stuck+1, payload), meta: "M"})
	time.Sleep(100 * time.Millisecond)
	h.hub.broadcastControl(ControlEvent{Gen: 1, Texts: []string{"STOP"}})

	// ③ 开始读：写者先写完卡住的那帧，然后必须先发 STOP、再取待发帧
	var order []string
	deadline := time.Now().Add(20 * time.Second)
	for time.Now().Before(deadline) {
		op, p, err := c.tryReadFrame(5 * time.Second)
		if err != nil {
			break
		}
		switch {
		case op == wsText:
			order = append(order, "text:"+string(p))
		case op == wsBinary && len(p) >= 4:
			order = append(order, "frame:"+strconv.Itoa(int(binary.BigEndian.Uint32(p[:4]))))
		default:
			order = append(order, "other")
		}
		if len(order) > 0 && order[len(order)-1] == "frame:"+strconv.Itoa(stuck+1) {
			break
		}
	}
	seq := strings.Join(order, " ")
	want := []string{"frame:" + strconv.Itoa(stuck), "text:STOP", "frame:" + strconv.Itoa(stuck+1)}
	for _, w := range want {
		if !strings.Contains(seq, w) {
			t.Fatalf("读取序列缺 %q：%q", w, seq)
		}
	}
	if !(strings.Index(seq, want[0]) < strings.Index(seq, want[1]) &&
		strings.Index(seq, want[1]) < strings.Index(seq, want[2])) {
		t.Errorf("顺序应为 %s → %s → %s，实得 %q", want[0], want[1], want[2], seq)
	}
}

// ==================== 无泄漏 ====================

// TestNoGoroutineLeak 连接/断开 200 次后 goroutine 数回到基线附近，且
// 前后两半不增长（真正的泄漏会线性累积）。
func TestNoGoroutineLeak(t *testing.T) {
	py := newFakePython(t)
	h, wsBase := newTestHandler(t, py)

	cycle := func(n int) {
		for i := 0; i < n; i++ {
			c := dialRaw(t, wsBase+pathStream, dialOpts{headers: cookie("good")})
			_ = c.conn.Close() // 立刻断开：处理侧走读错误 → 收尾
		}
	}
	settle := func() int {
		prev := runtime.NumGoroutine()
		for i := 0; i < 100; i++ {
			time.Sleep(50 * time.Millisecond)
			n := runtime.NumGoroutine()
			if n >= prev { // 不再下降即视为已稳定
				return n
			}
			prev = n
		}
		return prev
	}

	cycle(5) // 预热：让内部 HTTP 连接池与各常驻 goroutine 先建立起来
	base := settle()
	cycle(100)
	first := settle()
	cycle(100)
	second := settle()

	t.Logf("goroutine：基线 %d → 100 次后 %d → 200 次后 %d", base, first, second)
	if first > base+10 || second > base+10 {
		t.Errorf("goroutine 未回基线：%d → %d → %d", base, first, second)
	}
	if second-first > 3 {
		t.Errorf("后 100 次仍在增长（%d → %d），疑似泄漏", first, second)
	}
	if n := h.hub.Count(); n != 0 {
		t.Errorf("全部断开后仍登记着 %d 条连接", n)
	}
	if snap := h.MetricsSource()().(videoSnapshot); snap.Viewers != 0 {
		t.Errorf("/metrics 里仍显示 %d 个观看者", snap.Viewers)
	}
	if !waitFor(5*time.Second, func() bool { return len(py.detachRecords()) >= 200 }) {
		t.Errorf("注销次数 %d，期望 ≥ 200（每次断开都要向 Python 注销）", len(py.detachRecords()))
	}
}

// ==================== 上行重连 ====================

// TestUpstreamReconnectsAndKeepsViewer 上行流断开后自动重连，**观看者的连接不受牵连**：
// 重连期间不断开、恢复后继续收帧。
func TestUpstreamReconnectsAndKeepsViewer(t *testing.T) {
	py := newFakePython(t)
	h, wsBase := newTestHandler(t, py)
	c := dialRaw(t, wsBase+pathStream, dialOpts{headers: cookie("good")})
	py.waitAttach("valice=alice", 2*time.Second)

	py.events <- frameEnvelope(1, 1, "M1", []byte{0x01})
	expectText(t, c, "greet")
	expectText(t, c, "M1")
	expectBinary(t, c, 1, []byte{0x01})

	// 掐断上行：先换上新通道（只影响**下一次**连接），再关旧通道让当前这次订阅结束
	old := py.eventsCh()
	py.swapEvents(make(chan []byte, 256))
	close(old)
	if !waitFor(5*time.Second, func() bool { return py.eventConns.Load() >= 2 }) {
		t.Fatalf("上行流断开后未重连，连接次数 %d", py.eventConns.Load())
	}

	// 观看者连接没被牵连：重连后继续收帧（换代问候只在 gen 变化时发）
	py.events <- frameEnvelope(1, 2, "M2", []byte{0x02})
	expectBinary(t, c, 2, []byte{0x02})

	snap := h.MetricsSource()().(videoSnapshot)
	if snap.Reconnects == 0 {
		t.Error("重连次数应 > 0")
	}
	if !snap.Upstream {
		t.Error("重连后上行应标记为已连接")
	}
}

// ==================== 小工具 ====================

func expectText(t *testing.T, c *rawWS, want string) {
	t.Helper()
	op, payload := c.readFrame(3 * time.Second)
	if op != wsText {
		t.Fatalf("期望文本 %q，得到 opcode=%#x payload=%q", want, op, payload)
	}
	if string(payload) != want {
		t.Fatalf("文本 = %q，期望 %q", payload, want)
	}
}

func expectBinary(t *testing.T, c *rawWS, wantID int, wantJPEG []byte) {
	t.Helper()
	op, payload := c.readFrame(3 * time.Second)
	if op != wsBinary {
		t.Fatalf("期望二进制帧 %d，得到 opcode=%#x payload=%q", wantID, op, payload)
	}
	if len(payload) < 4 {
		t.Fatalf("二进制帧太短：%q", payload)
	}
	if got := int(binary.BigEndian.Uint32(payload[:4])); got != wantID {
		t.Fatalf("帧号 = %d，期望 %d", got, wantID)
	}
	if !bytes.Equal(payload[4:], wantJPEG) {
		t.Fatalf("帧内容 = %q，期望 %q", payload[4:], wantJPEG)
	}
}
