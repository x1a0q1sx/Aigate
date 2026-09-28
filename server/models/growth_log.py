"""成长中心操作日志（Buddy 旅行 / 成长任务）。

形态对齐 checkin_logs（同为「操作日志 + 前端详情」模式）：
- 旅行：每次派发/领奖落一行，供「今日是否已处理」派生 + 历史展示
- 任务：每次接取落一行（记录接取了哪些任务码）

kind 六态：
  traveled   已派出旅行（detail 有 location_id）
  claimed    旅行礼物已领取（credit/energy 为本次所得）
  accepted   已接取成长任务（detail 有任务码列表）
  already    无需操作（旅行进行中 / 次数用完 / 无待接任务）—— **不是失败**
  inactive   本版无此活动（国际版实测：数据为空）
  failed     请求或解析失败（error 列有详情）
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Float, Index
from .base import Base


class GrowthLog(Base):
    __tablename__ = "growth_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    # manual（页面点按钮）/ scheduled（每日定时）/ startup_catchup（启动补跑）
    trigger = Column(String(20), default="manual")
    provider_code = Column(String(50), nullable=False)   # codebuddy_cn / codebuddy_intl
    owner = Column(String(100), default="__default")
    # 动作类别：travel（旅行）/ task（成长任务）—— 与 kind 组合成完整语义
    action = Column(String(20), default="travel")
    kind = Column(String(20), default="failed")          # 见模块 docstring 六态
    credit = Column(Float, nullable=True)                # 本次所得积分
    energy = Column(Float, nullable=True)                # 本次所得能量（旅行/任务奖励）
    location = Column(String(100), default="")           # 旅行地点名
    task_codes = Column(Text, nullable=True)             # 接取的任务码（JSON 数组）
    message = Column(String(500), default="")
    error = Column(Text, nullable=True)
    duration_ms = Column(Integer, default=0)

    __table_args__ = (
        Index("idx_growth_prov_owner_time", "provider_code", "owner", "created_at"),
        Index("idx_growth_action_time", "action", "created_at"),
    )

    def __repr__(self):
        return (f"<GrowthLog {self.provider_code}/{self.owner} {self.action}/{self.kind} "
                f"credit={self.credit}>")
