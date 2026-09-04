"""统一构造行程写入状态提示，避免模型把建议误述为已完成。"""


def format_itinerary_commitment_status(committed: bool) -> str:
    """返回供各 Agent 使用的本次请求行程写入状态。"""
    if committed:
        return (
            "【最高优先级：本次行程写入状态】\n"
            "系统确认：本次请求已经成功写入过 CurrentItinerary。\n"
            "只能陈述系统已经实际完成的行程写入；不得把其他建议、候选方案或未执行操作说成已修改、"
            "已更新或已保存。"
        )
    return (
        "【最高优先级：本次行程写入状态】\n"
        "系统确认：本次请求尚未成功写入 CurrentItinerary。\n"
        "不得声称已经修改、更新、保存、加入或删除行程。只能说明建议、候选方案，或等待用户确认的状态。"
    )
