#!/usr/bin/env python3
"""
AIGate 一键启动脚本
智能 LLM 聚合网关
"""
import os
import sys
import warnings
# 在导入任何 SQLAlchemy 模块之前，抑制 aiosqlite 连接池的 GC 泄漏告警
warnings.filterwarnings("ignore", message=r".*non-checked-in connection.*", category=Warning)
import uvicorn
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
# 确保工作目录是项目根目录
project_root = Path(__file__).parent.resolve()
os.chdir(project_root)
sys.path.insert(0, str(project_root))
# 确保数据目录存在
Path("./data").mkdir(exist_ok=True)
# 检查配置
config_path = Path("config.yaml")
if not config_path.exists():
    print("ℹ️  首次启动，创建默认配置...")
from server.config import get_config
config = get_config()
print("\n📋 AIGate 配置:")
print(f"   监听: {config.server.host}:{config.server.port}")
print(f"   数据库: {config.database.path}")
print(f"   健康探测间隔: {config.health_check.interval_minutes} 分钟")
print(f"   最大回退次数: {config.auto_router.max_fallbacks}")
print()
if not config.security.encryption_key:
    print("⚠️  警告: 加密密钥未生成，启动会自动生成，请备份!")
    print()
print("🚀 启动 AIGate...\n")
uvicorn.run(
    "server.main:app",
    host=config.server.host,
    port=config.server.port,
    reload=False,
    workers=1
)