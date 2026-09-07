# 贡献指南

感谢关注 AIGate！欢迎以任何形式贡献：报告 Bug、提需求、改代码、完善文档。

## 开发环境

```bash
# 后端（Python 3.10+）
python -m venv venv
venv\Scripts\activate        # Windows
source venv/bin/activate     # Linux/macOS
pip install -r requirements.txt

# 前端（Node 18+）
cd client && npm install

# 启动（后端 8000 端口，前端 dev 5173 代理到 8000）
python start.py
cd client && npm run dev
```

## 提交前检查

```bash
python -m pytest tests/ -q        # 全部测试须通过（当前 56+ 项）
python -m py_compile server/**/*.py
cd client && npm run build        # 前端改动必须能构建
```

## 约定

- 提交信息格式：`type(scope): 摘要`，type 取 feat/fix/perf/refactor/docs/test/chore
- 不要把 `config.yaml`、`data/`、真实密钥提交进仓库（有提交钩子拦截，请勿 --no-verify 绕过真实密钥）
- 数据库结构变更：在 `server/db.py` 的迁移列表追加幂等语句（CREATE/ALTER 需 try-except 兼容 PG），不要依赖 alembic
- 新功能尽量带测试；协议相关的改动参考 `tests/test_protocol_fixes.py` 的 fixture 风格
- 涉及三协议入口（/v1/chat/completions、/v1/responses、/v1/messages）的改动，务必保持契约兼容

## 分支与发布

- `main` 为稳定分支，功能开发请 fork 或开分支后 PR
- 版本发布由维护者打 tag，Release 附 CHANGELOG 摘要
