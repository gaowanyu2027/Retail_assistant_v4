// Package video 是视频数据面的 Go 侧：连接、会话仲裁、扇出、丢帧、协议。
//
// 分工（三条铁律）：**Go 不碰像素**（采集/解码/推理/标注/JPEG 编码在 Python），
// **Python 不碰连接**（公网 WS 由 Go 持有，Python 只监听内网），
// **安全关键代码不复制**（本机摄像头归属判定、源守卫、票据消费都在 Python）。
//
// 因此这里只做五件事：
//
//  1. WS 接入与鉴权决策（cookie/bearer 会话、`?ticket=` 一次性票据）；
//  2. 观看者登记（`/internal/vision/viewer`）与会话动作转发（`/internal/vision/action`）；
//  3. 订阅上行帧流（`/internal/vision/events`）并**扇出**给各客户端；
//  4. 背压：每客户端一个写者 + 一个"待发帧位"（满则丢旧留新）——慢客户端
//     既不拖慢别人，也不把 TCP 背压传回 Python；
//  5. 本机摄像头帧的上行合并（`/internal/vision/client-frame`）。
//
// 所有下发**文本**（含中文与任意 JSON 键序）都由 Python 组装，Go 逐字转发；
// Go 只自行合成两条：`{"event":"ready"}`（/api/ws/client 连接后首条）与 pong
// （ping 应答，是客户端消息的纯函数）。这两条的字节形状在
// contracts/ws_messages.json 里有 golden，见 envelope_test.go / handler_test.go。
package video

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
)

// 上行帧流的信封常量。**与 api/routes/vision_internal.py 成对**，
// 改动必须两侧同步（contracts/ws_messages.json 的 upstream_envelope 是判据）。
const (
	envelopeMagic         = "DSHF"
	envelopeVersion       = 1
	envelopeHeaderBytes   = 18
	envelopeKindFrame     = 1
	envelopeKindControl   = 2
	envelopeKindHeartbeat = 3
	// envelopeKindIngest 仅内网（S4）：Python 把三张视频派生表的待写行发来，
	// 由 store.Store 落库。**永不下发浏览器**，ws_protocol 里没有对应消息。
	envelopeKindIngest = 4
)

// 单段上限：meta 是一小段 JSON，jpeg 是 1280×720 q70（实测 ~90KB）。
// 给足余量即可 —— 真超出说明对面不是我们的实现，直接断开重连。
const (
	maxMetaBytes = 1 << 20  // 1MiB
	maxJPEGBytes = 12 << 20 // 12MiB
)

var errBadEnvelope = errors.New("video: 上行信封格式不合法")

// envelope 一条已解析的信封。
type envelope struct {
	kind    byte
	frameID int
	meta    []byte
	jpeg    []byte
}

// readEnvelope 读一条信封：magic(4B)|ver(1B)|kind(1B)|frame_id(4B 大端)|
// metaLen(4B 大端)|jpegLen(4B 大端)|meta(UTF-8 JSON)|jpeg(原始字节)。
//
// 顺序**不可颠倒**：长度字段先于变长段，才能一次性把变长段读全（io.ReadFull）。
func readEnvelope(r io.Reader) (*envelope, error) {
	var hdr [envelopeHeaderBytes]byte
	if _, err := io.ReadFull(r, hdr[:]); err != nil {
		return nil, err
	}
	if string(hdr[0:4]) != envelopeMagic {
		return nil, fmt.Errorf("%w: magic=%q", errBadEnvelope, hdr[0:4])
	}
	if hdr[4] != envelopeVersion {
		return nil, fmt.Errorf("%w: 版本 %d", errBadEnvelope, hdr[4])
	}
	kind := hdr[5]
	frameID := int(binary.BigEndian.Uint32(hdr[6:10]))
	metaLen := int(binary.BigEndian.Uint32(hdr[10:14]))
	jpegLen := int(binary.BigEndian.Uint32(hdr[14:18]))
	if metaLen > maxMetaBytes || jpegLen > maxJPEGBytes {
		return nil, fmt.Errorf("%w: metaLen=%d jpegLen=%d", errBadEnvelope, metaLen, jpegLen)
	}

	env := &envelope{kind: kind, frameID: frameID}
	if metaLen > 0 {
		env.meta = make([]byte, metaLen)
		if _, err := io.ReadFull(r, env.meta); err != nil {
			return nil, err
		}
	}
	if jpegLen > 0 {
		env.jpeg = make([]byte, jpegLen)
		if _, err := io.ReadFull(r, env.jpeg); err != nil {
			return nil, err
		}
	}
	return env, nil
}

// framePacket 组装下发给浏览器的二进制帧：`[4B 大端 frame_id][JPEG]`。
// 与 ws_protocol.py 的 frame_packet 逐字节一致（夹具 frame_packet.examples）；
// 一帧只组装一次，所有观看者共享同一份只读字节。
func framePacket(frameID int, jpeg []byte) []byte {
	out := make([]byte, 4+len(jpeg))
	binary.BigEndian.PutUint32(out, uint32(frameID))
	copy(out[4:], jpeg)
	return out
}

// MetadataInterval 与 config/settings.py 的 WS_METADATA_INTERVAL 必须一致：
// 帧元数据的下发节拍由 Go 决定（Python 一律把 meta 放进信封，发不发看这里）。
// 判据是 contracts/ws_messages.json 的 metadata_schedule —— 夹具与这份常量
// 任何一侧变了，go test ./internal/video 都会红。
const MetadataInterval = 5

// shouldSendMetadata 第 1 帧必发（首帧="源真的出画面了"的唯一可信证据），
// 此后每 interval 帧一次。与 ws_protocol.py 的同名函数同规则。
func shouldSendMetadata(frameID, interval int) bool {
	if interval <= 0 {
		interval = MetadataInterval
	}
	return frameID == 1 || frameID%interval == 0
}
