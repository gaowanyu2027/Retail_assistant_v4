package video

import (
	"encoding/json"
	"strings"
	"testing"
)

func testClient() *client { return newClient(endpointStream, nil, nil, newStats()) }

func frame(gen, id int) *outFrame {
	return &outFrame{gen: gen, frameID: id, packet: framePacket(id, []byte{0xFF, 0xD8, 0xFF, 0xD9})}
}

func strp(s string) *string { return &s }

// TestOfferFrameDropsOldKeepsNew 背压规格的单元断言：待发帧位容量 1，
// 满则丢旧留新并计数（慢客户端丢的是自己过时的帧）。
func TestOfferFrameDropsOldKeepsNew(t *testing.T) {
	c := testClient()
	for i := 1; i <= 3; i++ {
		c.offerFrame(frame(1, i))
	}
	got := c.takeFrame()
	if got == nil || got.frameID != 3 {
		t.Fatalf("待发帧应为最新一帧（frame_id=3），实得 %+v", got)
	}
	if n := c.framesDropped.Load(); n != 2 {
		t.Errorf("丢帧计数 = %d，期望 2", n)
	}
	if c.takeFrame() != nil {
		t.Error("取走后不该还有待发帧")
	}
}

// TestApplyControlOrderAndSemantics 逐条对应 api/routes/stream.py 的控制事件分支：
// 跳过发起人 → texts 按序 → framed_tail（判据在 clear 复位**之前**求值）→ clear。
func TestApplyControlOrderAndSemantics(t *testing.T) {
	t.Run("顺序与尾部补发", func(t *testing.T) {
		c := testClient()
		c.lastSent = 7 // 已收到过帧
		c.offerFrame(frame(1, 8))
		c.applyControl(ControlEvent{
			Gen: 2, Texts: []string{"t1", "t2"},
			FramedTail: strp("tail"), Clear: true,
		})
		got := c.takeTexts()
		want := []string{"t1", "t2", "tail"}
		if strings.Join(got, "|") != strings.Join(want, "|") {
			t.Errorf("文本序列 = %q，期望 %q", got, want)
		}
		if c.takeFrame() != nil {
			t.Error("clear 应丢弃待发帧")
		}
		if c.lastSent != -1 || c.runGen != -1 {
			t.Errorf("clear 后 lastSent=%d runGen=%d，都应复位为 -1", c.lastSent, c.runGen)
		}
	})

	t.Run("没收到过帧就不补 framed_tail", func(t *testing.T) {
		c := testClient() // lastSent 初始 -1
		c.applyControl(ControlEvent{Texts: []string{"t1"}, FramedTail: strp("tail")})
		got := c.takeTexts()
		if len(got) != 1 || got[0] != "t1" {
			t.Errorf("文本 = %q，期望只有 t1（framed_tail 的判据是 lastSent >= 0）", got)
		}
	})

	t.Run("发起人跳过", func(t *testing.T) {
		c := testClient()
		c.viewerID = "v9"
		c.applyControl(ControlEvent{Except: strp("v9"), Texts: []string{"x"}, Clear: true})
		if got := c.takeTexts(); len(got) != 0 {
			t.Errorf("发起人不该收到广播内容，实得 %q", got)
		}
		if c.lastSent != -1 || c.runGen != -1 {
			t.Error("跳过必须发生在最前面：连 clear 都不该生效")
		}
	})

	t.Run("clear 之外不动 runGen", func(t *testing.T) {
		c := testClient()
		c.runGen = 3
		c.applyControl(ControlEvent{Gen: 3, Texts: []string{"hello"}})
		if c.runGen != 3 {
			t.Errorf("无 clear 的控制事件不该动 runGen，实得 %d", c.runGen)
		}
	})
}

// TestApplyOutcomeKeepsRunGen 动作回执的 clear **只复位帧号、不动 runGen**
// （与迁移前一致：发起人自己不需要重新问候一次）。下一帧仍会因 lastSent<0 触发问候。
func TestApplyOutcomeKeepsRunGen(t *testing.T) {
	c := testClient()
	c.runGen = 5
	c.lastSent = 42
	c.applyOutcome(&ActionOutcome{Gen: 5, Clear: true, Replies: []string{"r1"}})
	if c.lastSent != -1 {
		t.Errorf("回执 clear 后 lastSent=%d，期望 -1", c.lastSent)
	}
	if c.runGen != 5 {
		t.Errorf("回执 clear 不该动 runGen，实得 %d", c.runGen)
	}
	if got := c.takeTexts(); len(got) != 1 || got[0] != "r1" {
		t.Errorf("回执文本 = %q，期望 [r1]", got)
	}
}

// TestPendingTextsBounded 待发文本有上限：超出丢新并计数（不无限增长）。
func TestPendingTextsBounded(t *testing.T) {
	c := testClient()
	for i := 0; i < maxPendingTexts+10; i++ {
		c.enqueueTexts("x")
	}
	if got := len(c.takeTexts()); got != maxPendingTexts {
		t.Errorf("待发文本 %d 条，期望上限 %d", got, maxPendingTexts)
	}
	if got := c.snapshot().TextsDropped; got != 10 {
		t.Errorf("丢弃计数 = %d，期望 10", got)
	}
}

// TestPongFor 是客户端消息的**纯函数**：ts 原样内联（数字/字符串/null 都保真），
// 缺省补 0（夹具 messages.pong_int / pong_float）。
func TestPongFor(t *testing.T) {
	cases := map[string]string{
		`{"type":"ping","ts":1234567890123}`: `{"type":"pong","ts":1234567890123}`,
		`{"type":"ping","ts":1.5}`:           `{"type":"pong","ts":1.5}`,
		`{"type":"ping","ts":"abc"}`:         `{"type":"pong","ts":"abc"}`,
		`{"type":"ping","ts":null}`:          `{"type":"pong","ts":null}`,
		`{"action":"ping"}`:                  `{"type":"pong","ts":0}`,
	}
	for raw, want := range cases {
		_, _, ts, ok := parseAction([]byte(raw))
		if !ok {
			t.Fatalf("%s：应解析为动作", raw)
		}
		if got := pongFor(ts); got != want {
			t.Errorf("%s → %s，期望 %s", raw, got, want)
		}
	}
	if got := pongFor(nil); got != `{"type":"pong","ts":0}` {
		t.Errorf("空 ts → %s", got)
	}
}

// TestParseActionRejectsNonObject 非 JSON 对象一律忽略（迁移前同样是静默忽略，
// 绝不能因此把连接打断）。
func TestParseActionRejectsNonObject(t *testing.T) {
	for _, raw := range []string{"", "null", "[1,2]", `"str"`, "42", "{oops"} {
		if _, _, _, ok := parseAction([]byte(raw)); ok {
			t.Errorf("%q 不该被当作动作", raw)
		}
	}
	action, typ, _, ok := parseAction([]byte(`{"action":"ping","type":"x"}`))
	if !ok || action != "ping" || typ != "x" {
		t.Errorf("解析结果 = (%q,%q,%t)", action, typ, ok)
	}
	// ts 片段必须是**原始字节**（浮点/大整数都不经 Go 的重新序列化）
	_, _, ts, _ := parseAction([]byte(`{"type":"ping","ts":1729000000.25}`))
	if !json.Valid(ts) || string(ts) != "1729000000.25" {
		t.Errorf("ts 原始片段 = %q", ts)
	}
}
