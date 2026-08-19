"""
本地 KWS 唤醒词封装 — sherpa-onnx KeywordSpotter
"""
from pathlib import Path

from config.settings import (
    KWS_MODEL_DIR,
    KWS_KEYWORDS_FILE,
    KWS_KEYWORD,
    KWS_THRESHOLD,
    KWS_SCORE,
    SHERPA_ONNX_PROVIDER,
)


class WakeWordSpotter:
    """基于 sherpa-onnx 的流式关键词唤醒器。"""

    def __init__(self, model_dir: Path | None = None, num_threads: int = 2):
        import sherpa_onnx

        model_dir = Path(model_dir or KWS_MODEL_DIR)
        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(model_dir / "tokens.txt"),
            encoder=str(model_dir / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            decoder=str(model_dir / "decoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
            joiner=str(model_dir / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            keywords_file=str(Path(KWS_KEYWORDS_FILE)),
            num_threads=num_threads,
            keywords_score=KWS_SCORE,
            keywords_threshold=KWS_THRESHOLD,
            provider=SHERPA_ONNX_PROVIDER,
        )
        self._default_keywords = KWS_KEYWORD

    def create_stream(self, keywords: str | None = None):
        return self._spotter.create_stream(keywords or self._default_keywords)

    def is_ready(self, stream) -> bool:
        return self._spotter.is_ready(stream)

    def decode_stream(self, stream):
        self._spotter.decode_stream(stream)

    def get_result(self, stream) -> str:
        return self._spotter.get_result(stream)

    def reset_stream(self, stream):
        self._spotter.reset_stream(stream)
