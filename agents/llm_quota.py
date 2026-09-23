"""LLM 日配额判定（成本护栏）。

## 为什么会有这个模块

公网可访问的问答接口**每次调用都要花 token**，而模型平台**没有"单 Key 额度上限/预算上限"
这类硬开关**（控制台只提供"按 Key 查看用量"）。所以"演示账号被爬/被刷"的钱只能自己兜：

1. **账户里只留少量余额** —— 余额本身就是硬上限（最简单，零代码）；
2. **本模块 + API 层闸门** —— 按"账号"和"全局"两个维度限制**每日提问数**，超限直接 429、不打 LLM。

默认两个上限都是 `0 = 不限`（不影响本机开发），演示前在 `.env` 里打开即可。

## 为什么把判定写成纯函数

`check_quota` 不碰数据库、不碰网络、不看全局状态 —— 计数由调用方（API 层）查好再传进来。
这样：① 单测可以直接覆盖边界（第十九次/第二十次、账号先于全局等）；② 将来换计数存储（Redis 等）
也不用改判定逻辑。
"""


def check_quota(used_user: int, used_global: int, per_user: int, global_limit: int) -> tuple[bool, str]:
    """判断是否放行本次提问。

    参数都是"**已经用掉的**次数"（不含本次）：调用方在**写入历史之前**查计数 ——
    于是 `per_user=20` 的语义是"这一天最多得到 20 个回答"，第 21 次被拒。

    返回 `(是否放行, 拒绝原因)`；放行时原因为空串。
    """
    if per_user > 0 and used_user >= per_user:
        return False, (
            f"今天的问答额度已用完（每个账号 {per_user} 次/天）。"
            "额度按自然日重置；如需更多，请用管理员账号调整配额。"
        )
    if global_limit > 0 and used_global >= global_limit:
        return False, (
            f"今天的整体问答额度已用完（全站 {global_limit} 次/天）。"
            "这是演示环境的成本护栏，明天自动恢复。"
        )
    return True, ""


def quota_enabled(per_user: int, global_limit: int) -> bool:
    """两个上限都为 0 ⇒ 闸门关闭（此时 API 层连一次计数查询都不做）。"""
    return per_user > 0 or global_limit > 0
