"""上传安全约束测试（台账 B11）。

被修的错误（三件事叠在一起就很危险，因为**视频源是可以被播放的**）：
1. **无大小限制** —— 无限写入 `data/videos`，把磁盘写满即可拖垮整个服务；
2. **无内容校验** —— 只看扩展名，改个后缀就能把任意文件塞进来当视频源；
3. **同名静默覆盖** —— 同名上传直接把已有文件替换掉（演示视频被换掉也无从察觉）。
另外补上：该端点此前**没有权限门禁**（任意已登录账号都能传）。

这里测三个纯函数；端点的 HTTP 行为在集成验证里覆盖。
临时目录用 `os.makedirs` 创建（`tempfile.mkdtemp` 的 0o700 在受限令牌下不可写，踩过）。
"""
import os
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from video_sources import (  # noqa: E402
    SourceError,
    detect_video_container,
    resolve_name_conflict,
    safe_upload_name,
)

ALLOWED = [".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".wmv"]


def _tmpdir(name: str) -> Path:
    d = PROJECT_ROOT / "data" / "_tmp_tests" / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------- 内容校验（魔数） ----------------

def test_detects_common_containers():
    assert detect_video_container(b"\x00\x00\x00\x20ftypisom....") == "mp4/mov"
    assert detect_video_container(b"RIFF\x00\x00\x00\x00AVI LIST") == "avi"
    assert detect_video_container(b"\x1a\x45\xdf\xa3\x01\x00\x00\x00") == "mkv/webm"
    assert detect_video_container(b"FLV\x01\x05\x00\x00\x00") == "flv"
    assert detect_video_container(b"\x30\x26\xb2\x75\x8e\x66\xcf\x11\xa6\xd9") == "asf/wmv"


def test_rejects_non_video_content():
    """这才是这条校验的意义：改后缀也进不来。"""
    for bad in (b"", b"plain text pretending to be mp4", b"PK\x03\x04zip",
                b"\x7fELF\x02\x01\x01", b"%PDF-1.7", b"\x89PNG\r\n\x1a\n"):
        assert detect_video_container(bad) is None, bad


# ---------------- 文件名净化 ----------------

def test_strips_path_traversal():
    assert safe_upload_name("../../etc/passwd.mp4", ALLOWED) == "passwd.mp4"
    assert safe_upload_name(r"..\..\windows\system32\evil.mp4", ALLOWED) == "evil.mp4"
    assert safe_upload_name("/abs/path/demo.mp4", ALLOWED) == "demo.mp4"


def test_neutralizes_dangerous_chars_and_control_chars():
    got = safe_upload_name('a<b>c:d"e|f?g*h.mp4', ALLOWED)
    assert not any(ch in got for ch in '<>:"|?*'), got
    got2 = safe_upload_name("bad\x00\x1fname.mp4", ALLOWED)
    assert "\x00" not in got2 and "\x1f" not in got2, repr(got2)


def test_extension_whitelist_and_case_folding():
    assert safe_upload_name("DEMO.MP4", ALLOWED) == "DEMO.mp4", "扩展名应统一小写"
    try:
        safe_upload_name("evil.exe", ALLOWED)
        raise AssertionError("不该放行 .exe")
    except SourceError as e:
        assert e.code == "unsupported_type", e.code


def test_empty_or_extension_only_names_rejected():
    for bad in ("", "   ", "/", "....mp4", ".mp4"):
        try:
            safe_upload_name(bad, ALLOWED)
            raise AssertionError(f"不该放行 {bad!r}")
        except SourceError as e:
            assert e.code in ("bad_request", "unsupported_type"), (bad, e.code)


# ---------------- 同名冲突 ----------------

def test_no_conflict_returns_target():
    d = _tmpdir("upload_nc1")
    try:
        assert resolve_name_conflict(d, "demo.mp4") == d / "demo.mp4"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_rename_policy_never_overwrites():
    d = _tmpdir("upload_nc2")
    try:
        (d / "demo.mp4").write_bytes(b"original")
        p1 = resolve_name_conflict(d, "demo.mp4", "rename")
        assert p1 == d / "demo_1.mp4", p1
        p1.write_bytes(b"second")
        p2 = resolve_name_conflict(d, "demo.mp4", "rename")
        assert p2 == d / "demo_2.mp4", p2
        # 原文件内容必须**原封不动**（这就是"静默覆盖"被修掉的证据）
        assert (d / "demo.mp4").read_bytes() == b"original"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_reject_policy_raises_conflict():
    d = _tmpdir("upload_nc3")
    try:
        (d / "demo.mp4").write_bytes(b"x")
        try:
            resolve_name_conflict(d, "demo.mp4", "reject")
            raise AssertionError("reject 策略下应当报冲突")
        except SourceError as e:
            assert e.code == "name_conflict", e.code
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_overwrite_policy_keeps_legacy_behavior():
    """逃生门：需要旧行为时可显式选 overwrite（默认不是它）。"""
    d = _tmpdir("upload_nc4")
    try:
        (d / "demo.mp4").write_bytes(b"x")
        assert resolve_name_conflict(d, "demo.mp4", "overwrite") == d / "demo.mp4"
    finally:
        shutil.rmtree(d, ignore_errors=True)
