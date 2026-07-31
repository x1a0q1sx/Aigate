"""
AIGate 一键增量更新

从 GitHub 拉取最新代码并就地升级，全程不触碰你的数据：
  data/aigate.db  服务商 / 模型 / 密钥 / 日志   —— 只备份，不覆盖
  config.yaml     加密密钥 / 代理 / 登录密码     —— 完全不动
  data/archives/  日志归档                      —— 完全不动

执行流程：
  1. 备份数据库（SQLite 在线备份，网关运行中也安全）
  2. git pull 增量拉取（只下载变化的文件，不是整个项目）
  3. 对比变更：requirements 变了才装依赖，前端变了才重新构建
  4. 重启 PM2 进程

用法：
  python scripts/update.py              常规更新
  python scripts/update.py --check      只看有什么更新，不实际执行
  python scripts/update.py --stash      有本地未提交改动时自动暂存
  python scripts/update.py --no-restart 更新完不自动重启
  python scripts/update.py --rebuild    强制重建前端（即使前端没变化）
"""
import argparse
import os
import shutil
import subprocess
import sys
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IS_WIN = os.name == "nt"
KEEP_BACKUPS = 5
PM2_PROCESS = os.environ.get("AIGATE_PM2_NAME", "Aigate")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# ── 输出辅助 ────────────────────────────────────────────────────────────────
def step(msg):
    print(f"\n\033[36m▸ {msg}\033[0m" if not IS_WIN else f"\n▸ {msg}")


def ok(msg):
    print(f"  ✓ {msg}")


def warn(msg):
    print(f"  ! {msg}")


def die(msg, code=1):
    print(f"\n✗ {msg}\n")
    sys.exit(code)


def run(cmd, cwd=ROOT, capture=True, check=True):
    """执行命令。Windows 下 npm/npx 是 .cmd，必须走 shell。"""
    use_shell = IS_WIN and isinstance(cmd, str)
    if isinstance(cmd, str) and not use_shell:
        cmd = cmd.split()
    r = subprocess.run(
        cmd, cwd=str(cwd), shell=use_shell,
        capture_output=capture, text=True, encoding="utf-8", errors="replace",
    )
    if check and r.returncode != 0:
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        die(f"命令失败：{cmd}\n{out}")
    return r


def git(args, check=True):
    return run(f"git {args}" if IS_WIN else ["git"] + args.split(), check=check)


# ── 1. 前置检查 ─────────────────────────────────────────────────────────────
def preflight(auto_stash):
    step("环境检查")

    if not (ROOT / ".git").exists():
        die(
            "当前目录不是 git 仓库，无法增量更新。\n"
            "  首次改造：在项目上级目录执行\n"
            f"    git clone https://github.com/x1a0q1sx/Aigate.git aigate-new\n"
            f"  然后把旧目录的 data/ 和 config.yaml 复制进去即可。"
        )

    if not run("git remote get-url origin", check=False).stdout.strip():
        die("没有配置远端 origin，执行：git remote add origin <仓库地址>")
    ok("git 仓库正常")

    branch = git("rev-parse --abbrev-ref HEAD").stdout.strip()
    ok(f"当前分支：{branch}")

    # 注意用 rstrip 而非 strip：porcelain 格式前两位是状态码，
    # strip 会吃掉第一行的前导空格导致文件名被截断
    dirty = git("status --porcelain --untracked-files=no").stdout.rstrip()
    if dirty:
        files = [ln[3:] for ln in dirty.splitlines()]
        if auto_stash:
            git("stash push -m aigate-update-autostash")
            warn(f"已暂存 {len(files)} 个本地改动（恢复：git stash pop）")
        else:
            print("\n  检测到未提交的本地改动：")
            for f in files[:15]:
                print(f"    {f}")
            if len(files) > 15:
                print(f"    ... 另有 {len(files) - 15} 个")
            die(
                "请先处理本地改动，三选一：\n"
                "    git commit -am '说明'     提交它们\n"
                "    git checkout -- .          丢弃它们\n"
                "    python scripts/update.py --stash   自动暂存"
            )
    else:
        ok("工作区干净")

    return branch


# ── 2. 备份数据库 ───────────────────────────────────────────────────────────
def backup_db():
    step("备份数据库")
    src = ROOT / "data" / "aigate.db"
    if not src.exists():
        warn("尚无数据库文件，跳过备份")
        return None

    bdir = ROOT / "data" / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    dst = bdir / f"aigate-{datetime.now():%Y%m%d-%H%M%S}.db"

    # SQLite 在线备份 API：网关正在运行也能安全备份，且会带上 WAL 里未落盘的数据
    try:
        s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
        d = sqlite3.connect(str(dst))
        s.backup(d)
        d.close()
        s.close()
    except Exception as e:
        die(f"数据库备份失败，已中止更新（不敢在没有备份的情况下动你的数据）：{e}")

    ok(f"已备份 → {dst.relative_to(ROOT)}  ({dst.stat().st_size / 1048576:.1f} MB)")

    olds = sorted(bdir.glob("aigate-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in olds[KEEP_BACKUPS:]:
        p.unlink(missing_ok=True)
        print(f"    清理旧备份 {p.name}")
    return dst


# ── 3. 拉取更新 ─────────────────────────────────────────────────────────────
def pull(branch, dry_run):
    step("拉取远端更新")
    git("fetch origin")

    before = git("rev-parse HEAD").stdout.strip()
    remote = f"origin/{branch}"
    if git(f"rev-parse --verify {remote}", check=False).returncode != 0:
        die(f"远端不存在分支 {remote}")

    ahead = git(f"rev-list --count {remote}..HEAD").stdout.strip()
    behind = git(f"rev-list --count HEAD..{remote}").stdout.strip()

    if behind == "0":
        ok("已是最新版本")
        return before, before, []

    print(f"\n  远端有 {behind} 个新提交：")
    for ln in git(f"log --oneline --no-decorate HEAD..{remote}").stdout.strip().splitlines()[:15]:
        print(f"    {ln}")

    files = git(f"diff --name-only HEAD..{remote}").stdout.strip().splitlines()
    print(f"\n  将更新 {len(files)} 个文件")

    if dry_run:
        for f in files[:30]:
            print(f"    {f}")
        if len(files) > 30:
            print(f"    ... 另有 {len(files) - 30} 个")
        return before, before, files

    if ahead != "0":
        warn(f"本地有 {ahead} 个未推送提交，使用 rebase 方式合并")
        r = git(f"pull --rebase origin {branch}", check=False)
        if r.returncode != 0:
            die(
                "rebase 出现冲突，更新已中止（你的数据完好无损）。\n"
                "  解决冲突后 git rebase --continue，或 git rebase --abort 放弃。\n"
                f"{(r.stdout or '') + (r.stderr or '')}"
            )
    else:
        git(f"merge --ff-only {remote}")

    after = git("rev-parse HEAD").stdout.strip()
    ok(f"已更新到 {after[:8]}")
    return before, after, files


# ── 4. 按需安装依赖 / 重建前端 ──────────────────────────────────────────────
def sync_python_deps(changed):
    if "requirements.txt" not in changed:
        return False
    step("Python 依赖有变化，安装中")
    run(f'"{sys.executable}" -m pip install -r requirements.txt --quiet', capture=False)
    ok("依赖已更新")
    return True


def build_frontend(changed, force):
    touched = [f for f in changed if f.startswith("client/") and not f.startswith("client/dist")]
    dist_ok = (ROOT / "client" / "dist" / "index.html").exists()

    if not force and not touched and dist_ok:
        return False

    step("重建前端界面")
    client = ROOT / "client"

    if not (client / "node_modules").exists():
        print("    首次构建，安装前端依赖（可能需要几分钟）...")
        run("npm install", cwd=client, capture=False)
    elif any(f.endswith("package-lock.json") or f.endswith("package.json") for f in touched):
        print("    依赖清单有变化，重新安装...")
        run("npm install", cwd=client, capture=False)

    # 坑一：npm 的可选依赖机制在 Windows 上经常漏装 rollup 原生模块
    if IS_WIN:
        native = client / "node_modules" / "@rollup" / "rollup-win32-x64-msvc"
        if not native.exists():
            warn("缺少 rollup 原生模块，补装中")
            run("npm install @rollup/rollup-win32-x64-msvc --no-save", cwd=client,
                capture=False, check=False)

    # 坑二：部分环境的删除保护会拦截 vite 清空 dist，先手动清再关掉 emptyOutDir
    dist = client / "dist"
    shutil.rmtree(dist / "assets", ignore_errors=True)
    (dist / "index.html").unlink(missing_ok=True)

    r = run("npx vite build --emptyOutDir=false", cwd=client, capture=True, check=False)
    if not (dist / "index.html").exists():
        die(f"前端构建失败：\n{(r.stdout or '') + (r.stderr or '')}")

    ok("前端已重建")
    return True


# ── 5. 重启服务 ─────────────────────────────────────────────────────────────
def restart():
    step("重启网关")
    pm2 = os.environ.get("AIGATE_PM2") or shutil.which("pm2")
    if not pm2:
        warn(f"未找到 pm2，请手动重启（或设置环境变量 AIGATE_PM2 指向 pm2 路径）")
        return False

    r = run(f'"{pm2}" restart {PM2_PROCESS}', check=False)
    if r.returncode != 0:
        warn(f"PM2 进程 {PM2_PROCESS} 重启失败，请手动重启：\n{(r.stdout or '') + (r.stderr or '')}")
        return False
    ok(f"PM2 进程 {PM2_PROCESS} 已重启")
    return True


# ── 主流程 ──────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="AIGate 一键增量更新")
    ap.add_argument("--check", action="store_true", help="只查看更新内容，不实际执行")
    ap.add_argument("--stash", action="store_true", help="自动暂存本地未提交改动")
    ap.add_argument("--no-restart", action="store_true", help="更新后不重启服务")
    ap.add_argument("--rebuild", action="store_true", help="强制重建前端")
    args = ap.parse_args()

    print("=" * 62)
    print("  AIGate 增量更新   （data/ 与 config.yaml 全程不受影响）")
    print("=" * 62)

    branch = preflight(args.stash)

    if args.check:
        pull(branch, dry_run=True)
        print("\n  这是预览模式，未做任何改动。去掉 --check 即可执行。\n")
        return

    backup = backup_db()
    before, after, changed = pull(branch, dry_run=False)

    if before == after and not args.rebuild:
        print("\n  无需更新。\n")
        return

    deps = sync_python_deps(changed)
    fe = build_frontend(changed, args.rebuild)
    backend = any(f.startswith(("server/", "start.py")) for f in changed)

    restarted = False
    if not args.no_restart and (backend or deps or fe):
        restarted = restart()

    print("\n" + "=" * 62)
    print("  更新完成")
    print("=" * 62)
    print(f"  代码      {before[:8]} → {after[:8]}（{len(changed)} 个文件）")
    print(f"  Python依赖 {'已更新' if deps else '无变化'}")
    print(f"  前端界面   {'已重建' if fe else '无变化'}")
    print(f"  服务       {'已重启' if restarted else '未重启（无需或需手动）'}")
    if backup:
        print(f"  数据备份   {backup.relative_to(ROOT)}")
    print(f"  你的数据   服务商 / 密钥 / 日志 / 配置 均未改动")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(130)
