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

脚本会自动备份数据库、增量拉取代码、按需重建前端并重启服务。**你已添加的服务商、密钥、请求日志都不会受影响。**

---
