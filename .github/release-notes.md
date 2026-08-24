### 开箱即用（推荐）

下载下方的 `aigate-*.zip`，解压后：

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml      # Windows: copy config.example.yaml config.yaml
python start.py
```

浏览器打开 <http://127.0.0.1:8000> ，默认密码 `aigate123`（首次登录后请修改）。

**压缩包已内置编译好的管理界面，无需安装 Node.js。** 首次启动会自动创建数据库并写入 3 个内置免费渠道（MiMo / OpenCode / AtomCode）。

### 从源码运行

```bash
git clone https://github.com/x1a0q1sx/Aigate.git
cd Aigate/client && npm install && npm run build && cd ..
pip install -r requirements.txt
python start.py
```

### 老用户升级

在项目目录直接执行：

```bash
python scripts/update.py
```

脚本会创建带 SHA-256 清单的恢复点，在数据库副本上验证迁移并运行测试，重启后执行健康检查。任一步失败会自动恢复旧代码、数据库、配置和前端。

本版新增“路由决策”页面，可查看 Auto 候选评分拆分、选中/跳过原因和完整 fallback 尝试链。

---
