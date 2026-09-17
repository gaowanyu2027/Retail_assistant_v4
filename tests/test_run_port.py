"""启动端口自检测试（run.py）。

用**真实的临时 HTTP 服务**验证探测逻辑，而不是 mock —— 因为这个检查的价值
就在于"真的去问一次"，mock 掉就失去意义了（E11 的教训：只试 bind 判断不出来）。
"""
import http.server
import socket
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import run as run_mod  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):                                  # noqa: N802
        if self.path.startswith("/api/health"):
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):                         # 静音
        pass


def _serve(port: int):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _HealthHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def test_detects_existing_service():
    port = _free_port()
    srv = _serve(port)
    try:
        desc = run_mod.probe_existing_service("127.0.0.1", port, timeout=1.0)
        assert desc, "已有服务在响应时必须能探测到"
        assert "/api/health" in desc
    finally:
        srv.shutdown()


def test_free_port_reports_nothing():
    port = _free_port()
    assert run_mod.probe_existing_service("127.0.0.1", port, timeout=0.5) is None
    assert run_mod.port_bindable("127.0.0.1", port) is True


def test_check_port_available_exits_on_conflict():
    """占用时应 sys.exit(2)，而不是继续启动。"""
    port = _free_port()
    srv = _serve(port)
    try:
        try:
            run_mod.check_port_available("127.0.0.1", port, allow_conflict=False)
            raise AssertionError("端口被占时应当 sys.exit(2)")
        except SystemExit as e:
            assert e.code == 2, f"退出码应为 2，实际 {e.code}"
        # 显式允许冲突时不应退出
        run_mod.check_port_available("127.0.0.1", port, allow_conflict=True)
    finally:
        srv.shutdown()


def test_occupied_port_is_not_bindable():
    """被真实占用（无 HTTP 应答）时，port_bindable 应为 False。"""
    # ⚠ 必须绑**同一个地址**（127.0.0.1）来制造占用：Windows 上 0.0.0.0 被占时
    #   仍允许绑 127.0.0.1（地址可并存），那样测不出"不可绑定"。
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert run_mod.port_bindable("127.0.0.1", port) is False
        assert run_mod.probe_existing_service("127.0.0.1", port, timeout=0.5) is None
    finally:
        s.close()
