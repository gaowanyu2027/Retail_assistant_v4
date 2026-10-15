"""WS 协议契约测试（混合架构迁移 · S0 冻结）。

三类断言：
  1. golden：对照 contracts/ws_messages.json 逐字比对 ws_protocol.py 的输出；
  2. 字节等价：新协议（内部直存 JPEG 字节）的下行字节 == 迁移前 base64 往返的产出
     —— 证明"去 base64"对客户端零感知（这是 S0 改动的核心安全性证据）；
  3. 静态守卫（AST）：stream.py 不再持有 base64 字符串帧、帧发送必经 frame_packet，
     任何人回退这套做法都会在这里红。

依赖约定：只依赖 stdlib + ws_protocol（其本体只依赖 config.settings）。
CI 单测 job 的最小依赖集里没有 cv2，故本文件**不得** import stream.py
（只可读它的源码做静态检查）。
"""
import ast
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import ws_protocol as wsp  # noqa: E402

FIXTURE_PATH = PROJECT_ROOT / "contracts" / "ws_messages.json"


def _fixture() -> dict:
    # 每次读盘：run_tests.py 在同一进程里跑全部用例，避免"改了夹具还是旧值"的假绿
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_frame_packet_matches_fixtures():
    fx = _fixture()["frame_packet"]
    assert fx["header_bytes"] == wsp.FRAME_HEADER_BYTES == 4
    assert fx["byte_order"] == "big-endian"
    for ex in fx["examples"]:
        got = wsp.frame_packet(ex["frame_id"], bytes.fromhex(ex["jpeg_hex"]))
        assert got.hex() == ex["packet_hex"], ex


def test_frame_packet_matches_legacy_base64_roundtrip():
    """字节等价：迁移前的 base64 往返与现在的直存字节，下发内容完全一致。"""
    jpeg = bytes(range(256)) * 3          # 覆盖全部字节值
    for frame_id in (1, 617, 100000):
        legacy = int(frame_id).to_bytes(4, "big") + base64.b64decode(base64.b64encode(jpeg))
        assert wsp.frame_packet(frame_id, jpeg) == legacy
    # 头部单独验证：4 字节大端 frame_id
    assert wsp.frame_packet(1, b"") == b"\x00\x00\x00\x01"


def test_metadata_schedule_matches_fixtures():
    fx = _fixture()["metadata_schedule"]
    for fid_str, expected in fx["examples"].items():
        got = wsp.should_send_metadata(int(fid_str))
        assert got is expected, f"frame_id={fid_str}: 期望 {expected}, 实际 {got}"
    # 显式传入 interval 时不再读全局配置（Go 侧比对用同一规则）
    assert wsp.should_send_metadata(7, interval=7) is True
    assert wsp.should_send_metadata(8, interval=7) is False


def test_frame_metadata_message_matches_fixtures():
    for ex in _fixture()["frame_metadata_examples"]:
        got = wsp.frame_metadata_message(ex["result"], ex["mode"])
        assert got == ex["expect"], ex["note"]
    # faces 键只在 result 里有时透传（零售模式不得凭空多出 faces）
    plain = wsp.frame_metadata_message({"frame_id": 2}, "retail")
    assert "faces" not in plain
    assert plain["timestamp"] == 0 and plain["tracks"] == [] and plain["events"] == []


def test_source_status_message_matches_fixtures():
    for ex in _fixture()["source_status_examples"]:
        k = ex["kwargs"]
        got = wsp.source_status_message(k["state"], k.get("echo"), k.get("code"),
                                        k.get("message"), **k.get("extra", {}))
        assert got == ex["expect"], ex


def test_client_camera_busy_message_matches_fixture():
    assert wsp.CLIENT_CAMERA_BUSY_MESSAGE == _fixture()["messages"]["client_camera_busy"]


def test_ws_protocol_minimal_deps():
    """ws_protocol 必须能在最小依赖环境导入（CI 单测 job 里没有 cv2）。

    子进程验证：同进程内其他测试可能已把 cv2/fastapi 装进 sys.modules，测不出真相。
    """
    script = (
        "import sys, ws_protocol; "
        "bad = [m for m in ('cv2', 'fastapi') if m in sys.modules]; "
        "assert not bad, f'ws_protocol 拉起了重依赖: {bad}'; "
        "print('ok')"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run([sys.executable, "-c", script], cwd=str(PROJECT_ROOT),
                          env=env, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"


def test_stream_module_has_no_base64_frame_roundtrip():
    """静态守卫：stream.py 不再持有 base64 字符串帧；帧发送必经 ws_protocol.frame_packet。

    断言的是**源码结构**（AST），不是运行时行为 —— 目的是防回退。
    注意 b64decode 不做禁止：上行入口（浏览器推帧 inject_client_frame）的入参
    本来就是 base64 字符串（canvas.toDataURL 产物），那是协议另一侧，保留。
    """
    tree = ast.parse((PROJECT_ROOT / "api" / "routes" / "stream.py").read_text(encoding="utf-8"))

    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "_latest_frame_b64" not in names, "旧的 base64 帧全局量回来了"
    assert "_latest_frame_jpeg" in names, "未找到直存 JPEG 字节的全局量"

    called_attrs = {n.func.attr for n in ast.walk(tree)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "b64encode" not in called_attrs, "下行侧又出现了 base64 编码（应为直存 JPEG 字节）"

    called_names = {n.func.id for n in ast.walk(tree)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "frame_packet" in called_names, "帧发送未经过 ws_protocol.frame_packet"
