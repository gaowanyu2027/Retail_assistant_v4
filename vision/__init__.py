"""视频数据面（S3：会话状态、事件日志与处理线程的宿处）。

- `vision.session` —— 会话/观看者/事件日志（谁在看、看到哪、停不停服务都在这里）；
- `vision.pipeline` —— 处理线程（读帧 → 推理 → 发布），零售与表情两种模式。

对外的两种入口都建在这两个模块之上（都不含会话逻辑）：
- `api/routes/stream.py` —— 传统 WS 适配器（浏览器直连 Python，或 Go 边缘回退时用）；
- `api/routes/vision_internal.py` —— 给 Go 边缘的内部端点（上行帧流 / 动作转发）。
"""
