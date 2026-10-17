package video

import (
	"bytes"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"os"
	"strconv"
	"testing"
)

// 契约夹具：contracts/ws_messages.json（S0 冻结，由 Python 侧实测导出）。
// 这份用例是"两侧同步"的判据 —— 夹具、Python 实现、Go 实现三者任何一处漂移，
// 这里的断言就会红。
const fixturePath = "../../../contracts/ws_messages.json"

type wsFixtures struct {
	FramePacket struct {
		HeaderBytes int `json:"header_bytes"`
		Examples    []struct {
			FrameID   int    `json:"frame_id"`
			JPEGHex   string `json:"jpeg_hex"`
			PacketHex string `json:"packet_hex"`
		} `json:"examples"`
	} `json:"frame_packet"`
	MetadataSchedule struct {
		Interval int             `json:"interval"`
		Examples map[string]bool `json:"examples"`
	} `json:"metadata_schedule"`
	UpstreamEnvelope struct {
		Magic    string         `json:"magic"`
		Version  int            `json:"version"`
		Kinds    map[string]int `json:"kinds"`
		Examples []struct {
			Name        string `json:"name"`
			Kind        int    `json:"kind"`
			FrameID     int    `json:"frame_id"`
			MetaUTF8    string `json:"meta_utf8"`
			JPEGHex     string `json:"jpeg_hex"`
			EnvelopeHex string `json:"envelope_hex"`
		} `json:"examples"`
	} `json:"upstream_envelope"`
	Messages map[string]string `json:"messages"`
}

func loadFixtures(t *testing.T) *wsFixtures {
	t.Helper()
	raw, err := os.ReadFile(fixturePath)
	if err != nil {
		t.Fatalf("读契约夹具失败（%s）: %v", fixturePath, err)
	}
	var f wsFixtures
	if err := json.Unmarshal(raw, &f); err != nil {
		t.Fatalf("解析契约夹具失败: %v", err)
	}
	return &f
}

// TestFramePacketMatchesFixture 下发二进制帧 = [4B 大端 frame_id][JPEG]。
func TestFramePacketMatchesFixture(t *testing.T) {
	f := loadFixtures(t)
	if f.FramePacket.HeaderBytes != 4 {
		t.Errorf("夹具说帧头 %d 字节，Go 侧按 4 字节实现", f.FramePacket.HeaderBytes)
	}
	for _, ex := range f.FramePacket.Examples {
		jpeg, err := hex.DecodeString(ex.JPEGHex)
		if err != nil {
			t.Fatalf("夹具 jpeg_hex 非法: %v", err)
		}
		got := hex.EncodeToString(framePacket(ex.FrameID, jpeg))
		if got != ex.PacketHex {
			t.Errorf("frame_id=%d：封包 %s，夹具 %s", ex.FrameID, got, ex.PacketHex)
		}
	}
}

// TestMetadataScheduleMatchesFixture 元数据节拍：第 1 帧必发，此后每 interval 帧一次。
func TestMetadataScheduleMatchesFixture(t *testing.T) {
	f := loadFixtures(t)
	if f.MetadataSchedule.Interval != MetadataInterval {
		t.Fatalf("夹具 interval=%d，Go 常量=%d", f.MetadataSchedule.Interval, MetadataInterval)
	}
	for idStr, want := range f.MetadataSchedule.Examples {
		id, err := strconv.Atoi(idStr)
		if err != nil {
			t.Fatalf("夹具键不是帧号: %q", idStr)
		}
		if got := shouldSendMetadata(id, MetadataInterval); got != want {
			t.Errorf("frame_id=%d：应发元数据=%t，夹具=%t", id, got, want)
		}
	}
}

// TestEnvelopeConstantsMatchFixture 信封常量（magic/版本/kind 编号）与夹具一致。
func TestEnvelopeConstantsMatchFixture(t *testing.T) {
	f := loadFixtures(t)
	if f.UpstreamEnvelope.Magic != envelopeMagic {
		t.Errorf("magic=%q，夹具=%q", envelopeMagic, f.UpstreamEnvelope.Magic)
	}
	if f.UpstreamEnvelope.Version != envelopeVersion {
		t.Errorf("版本=%d，夹具=%d", envelopeVersion, f.UpstreamEnvelope.Version)
	}
	kinds := map[string]byte{
		"frame": envelopeKindFrame, "control": envelopeKindControl, "heartbeat": envelopeKindHeartbeat,
		"ingest": envelopeKindIngest,
	}
	for name, got := range kinds {
		if want := f.UpstreamEnvelope.Kinds[name]; int(got) != want {
			t.Errorf("kind(%s)=%d，夹具=%d", name, got, want)
		}
	}
}

// TestEnvelopeRoundTripMatchesFixture 逐字节构造夹具里的信封，再解析回来 ——
// 这一条同时钉住 Python 侧的 `_envelope()`（按夹具的 layout 构造）与 Go 侧的
// readEnvelope：任何一侧改了字段次序/端序，都会在这里红。
func TestEnvelopeRoundTripMatchesFixture(t *testing.T) {
	f := loadFixtures(t)
	for _, ex := range f.UpstreamEnvelope.Examples {
		jpeg, err := hex.DecodeString(ex.JPEGHex)
		if err != nil {
			t.Fatalf("%s：jpeg_hex 非法: %v", ex.Name, err)
		}
		// 与 vision_internal.py 的 _envelope() 逐字段对应
		built := make([]byte, 0, envelopeHeaderBytes+len(ex.MetaUTF8)+len(jpeg))
		built = append(built, envelopeMagic...)
		built = append(built, envelopeVersion, byte(ex.Kind))
		built = binary.BigEndian.AppendUint32(built, uint32(ex.FrameID))
		built = binary.BigEndian.AppendUint32(built, uint32(len(ex.MetaUTF8)))
		built = binary.BigEndian.AppendUint32(built, uint32(len(jpeg)))
		built = append(built, ex.MetaUTF8...)
		built = append(built, jpeg...)

		if got := hex.EncodeToString(built); got != ex.EnvelopeHex {
			t.Errorf("%s：构造出的信封与夹具不符\n实际 %s\n夹具 %s", ex.Name, got, ex.EnvelopeHex)
			continue
		}
		env, err := readEnvelope(bytes.NewReader(built))
		if err != nil {
			t.Errorf("%s：解析失败: %v", ex.Name, err)
			continue
		}
		if int(env.kind) != ex.Kind || env.frameID != ex.FrameID {
			t.Errorf("%s：解析得到 kind=%d frame_id=%d，期望 %d/%d",
				ex.Name, env.kind, env.frameID, ex.Kind, ex.FrameID)
		}
		if string(env.meta) != ex.MetaUTF8 {
			t.Errorf("%s：meta 不符\n实际 %q\n期望 %q", ex.Name, env.meta, ex.MetaUTF8)
		}
		if hex.EncodeToString(env.jpeg) != ex.JPEGHex {
			t.Errorf("%s：jpeg 不符", ex.Name)
		}
	}
}

// TestGeneratedMessagesMatchFixture Go 自行合成的两条消息（ready / pong）
// 与夹具逐字节一致 —— 其余一切下发文本都由 Python 组装，Go 不合成。
func TestGeneratedMessagesMatchFixture(t *testing.T) {
	f := loadFixtures(t)
	if got := readyMessage; got != f.Messages["ready"] {
		t.Errorf("ready=%q，夹具=%q", got, f.Messages["ready"])
	}
	cases := []struct {
		raw string
		key string
	}{
		{`{"type":"ping"}`, "pong_int"},                      // 缺 ts → 0
		{`{"type":"ping","ts":0}`, "pong_int"},               // 显式 0
		{`{"type":"ping","ts":1729000000.25}`, "pong_float"}, // 浮点 ts 原样回
	}
	for _, c := range cases {
		_, _, ts, ok := parseAction([]byte(c.raw))
		if !ok {
			t.Fatalf("%s：消息不是 JSON 对象", c.raw)
		}
		if got := pongFor(ts); got != f.Messages[c.key] {
			t.Errorf("%s → %q，夹具 %s=%q", c.raw, got, c.key, f.Messages[c.key])
		}
	}
}
