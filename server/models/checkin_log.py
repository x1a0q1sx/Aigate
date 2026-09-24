"""签到日志：每次签到（手动/定时/启动补签）落一行，签到页据此派生「今日状态」。

形态对齐 model_refresh_logs（同为「操作日志 + 前端详情」模式）：
- 「今日是否已签」= 查当天是否有该 (provider_code, owner) 的终态行（claimed/already_claimed）
- 不做双写：状态从日志派生，避免两处数据打架

kind 四态（端口自 Jet-Hub 的 ClaimOutcome 语义）：
  claimed          领取成功（credit 为本次所得）
  already_claimed  今天已领（**不是失败**，UI 不得标红/不得显示 0 积分当失败）
  inactive         活动未开启/无领取资格（上游有接口但当前不可领）
  unsupported      上游未提供签到接口（能力矩阵为 False）
  failed           请求或解析失败（error 列有详情）
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Float, Boolean, Index
from .base import Base


class CheckinLog(Base):
    __tablename__ = "checkin_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    # manual（页面点按钮）/ scheduled（每日定时）/ startup_catchup（启动补签）
    trigger = Column(String(20), default="manual")
    provider_code = Column(String(50), nullable=False)   # codebuddy_cn / codebuddy_intl / qoder / u1s1
    owner = Column(String(100), default="__default")     # 账号名，与 oauth_tokens.owner 对应
    kind = Column(String(20), default="failed")          # 见模块 docstring 五态
    credit = Column(Float, nullable=True)                # 本次所得积分（已领/未开启为 NULL）
    streak_days = Column(Integer, nullable=True)         # 连续签到天数（CodeBuddy 有）
    total_credits = Column(Float, nullable=True)         # 累计积分（CodeBuddy 状态端点有）
    activity_name = Column(String(200), default="")      # 活动名（如「高校新生攻略」）
    message = Column(String(500), default="")            # 人话说明
    error = Column(Text, nullable=True)                  # 失败详情
    upstream_code = Column(Integer, nullable=True)       # 上游业务码（CodeBuddy 10001 等）
    duration_ms = Column(Integer, default=0)

    __table_args__ = (
        # 签到页按 (provider, owner, 当天) 查今日状态；历史页按时间倒序
        Index("idx_checkin_prov_owner_time", "provider_code", "owner", "created_at"),
    )

    def __repr__(self):
        return (f"<CheckinLog {self.provider_code}/{self.owner} {self.kind} "
                f"credit={self.credit}>")
