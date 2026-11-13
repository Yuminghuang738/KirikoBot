"""部署脚本的静态约束与离线 plan 验收。

这些测试只读脚本本身，不连服务器、不执行 rsync。
锁住群 notice 里写死的部署安全边界：不删远端文件、不重建镜像、
先打快照、保护数据库/密钥/数据目录、重启后三项验收。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
DEPLOY = SCRIPTS / "deploy.sh"
ROLLBACK = SCRIPTS / "rollback.sh"
COMMON = SCRIPTS / "_common.sh"

BASH = shutil.which("bash")

# 线上运行时数据 / 密钥：任何部署路径都不许碰。
PROTECTED = (
    "robot.db",
    ".env",
    ".dashboard_password",
    "backups",
    "stickers",
    "shared_files",
    "llbot_config",
    "carside_picture",
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", [DEPLOY, ROLLBACK, COMMON])
def test_scripts_exist_and_parse(path: Path) -> None:
    assert path.is_file(), f"缺少 {path}"
    if BASH is None:
        pytest.skip("环境里没有 bash")
    proc = subprocess.run([BASH, "-n", str(path)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_deploy_never_deletes_remote_files() -> None:
    for path in (DEPLOY, ROLLBACK, COMMON):
        assert "--delete" not in read(path), f"{path.name} 出现了删除远端文件的参数"


def test_deploy_never_rebuilds_image() -> None:
    text = read(DEPLOY)
    assert "up --build" not in text
    # 部署只允许 restart，不允许重建。
    assert "restart_service" in text
    assert "docker compose up" not in text


def test_protected_paths_are_excluded() -> None:
    text = read(DEPLOY)
    for name in PROTECTED:
        assert name in text, f"deploy.sh 没有排除 {name}"


def test_snapshot_happens_before_sync() -> None:
    text = read(DEPLOY)
    snapshot_at = text.index('log "打快照')
    sync_at = text.index('log "同步代码')
    assert snapshot_at < sync_at
    assert ".deploy-backup" in text
    assert "tar -czf" in text
    assert "tar -tzf" in text  # 快照要校验完整性


def test_restart_then_three_acceptance_checks() -> None:
    common = read(COMMON)
    assert "docker compose restart" in common
    assert "healthz" in common
    assert '"healthy"' in common
    assert "traceback" in common
    assert "verify_after_restart" in common


def test_rollback_uses_snapshot_and_verifies() -> None:
    text = read(ROLLBACK)
    assert "tar -xzf" in text
    assert ".deploy-backup" in text
    assert "verify_after_restart" in text
    # 回滚不得顺手恢复数据库/密钥。
    assert "robot.db" in text  # 文档明确说明不碰


def test_deploy_plan_runs_offline() -> None:
    proc = subprocess.run(
        [BASH, str(DEPLOY), "--plan", "--allow-branch", "--allow-dirty"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "SERVER": "invalid.invalid"},
        cwd=str(REPO),
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "rsync" in out
    assert "--exclude" in out
    assert "restart" in out
    assert "--delete" not in out
    assert "--files-from" in out


def test_rollback_plan_runs_offline() -> None:
    proc = subprocess.run(
        [BASH, str(ROLLBACK), "20260101-000000", "--plan"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "SERVER": "invalid.invalid"},
        cwd=str(REPO),
    )
    assert proc.returncode == 0, proc.stderr
    assert "code.tgz" in proc.stdout
    assert "restart" in proc.stdout


def test_deploy_requires_clean_and_main_by_default() -> None:
    text = read(DEPLOY)
    assert "diff --quiet" in text
    assert "--allow-dirty" in text
    assert "DEPLOY_BRANCH" in text
    assert re.search(r"REPO_ROOT.*git", text, re.S)


def test_requirements_added_count_distinguishes_add_and_remove(tmp_path) -> None:
    local = tmp_path / "requirements.txt"
    local.write_text("flask>=3.0\nimagehash>=4.3\n", encoding="utf-8")
    cmd = (
        f'source "{COMMON}"; '
        f'requirements_added_count "$LOCAL_REQ" "$REMOTE_REQ"'
    )
    # 远端多一行（本地只是删依赖）→ 0 行新增，可以同步。
    proc = subprocess.run(
        [BASH, "-c", cmd], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "LOCAL_REQ": str(local),
             "REMOTE_REQ": "flask>=3.0\nimagehash>=4.3\nbeautifulsoup4>=4.12\n"},
    )
    assert proc.stdout.strip() == "0", proc.stderr
    # 本地多一行（新增依赖）→ 1 行新增，必须中止。
    proc = subprocess.run(
        [BASH, "-c", cmd], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "LOCAL_REQ": str(local),
             "REMOTE_REQ": "flask>=3.0\n"},
    )
    assert proc.stdout.strip() == "1", proc.stderr


def test_plan_syncs_only_git_tracked_files() -> None:
    proc = subprocess.run(
        [BASH, str(DEPLOY), "--plan", "--allow-branch", "--allow-dirty"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "SERVER": "invalid.invalid"},
        cwd=str(REPO),
    )
    assert proc.returncode == 0, proc.stderr
    match = re.search(r"只同步 git 跟踪的 (\d+) 个文件", proc.stdout)
    assert match, proc.stdout
    tracked = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "--", "KirikoBot/*.py",
         "KirikoBot/templates", "KirikoBot/static", "KirikoBot/requirements.txt"],
        capture_output=True, text=True,
    ).stdout.splitlines()
    assert int(match.group(1)) == len(tracked)
    # 未跟踪的 WIP 绝不能被部署。
    assert "dsh_plugins.py" not in proc.stdout
    assert "tts_service.py" not in proc.stdout
