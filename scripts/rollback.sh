#!/usr/bin/env bash
# rollback.sh — 用 deploy.sh 留下的快照回滚代码并重启机器人。
#
# 只回滚代码：快照里只有 KirikoBot/*.py、templates/、static/。
# 不恢复、也不触碰 robot.db*、.env、backups/、stickers/、shared_files/、
# llbot_config/ —— 这些属于线上运行时数据。
# 注意：回滚会覆盖快照里的文件，但不会删除坏部署新增、且不在快照里的文件。
#
# 用法：scripts/rollback.sh <快照时间戳|--latest> [--plan] [--yes]
#       scripts/rollback.sh            # 列出可用快照
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER="${SERVER:-root@47.108.183.60}"
REMOTE_DIR="${REMOTE_DIR:-/root/kirikobot}"
SERVICE="${SERVICE:-my-robot}"
CONTAINER="${CONTAINER:-kirikobot}"
HEALTH_URL="${HEALTH_URL:-http://localhost:5000/healthz}"
SSH_OPTS=(-F "${SSH_CONFIG:-$HOME/.ssh/config}" -i "${SSH_KEY:-$HOME/.ssh/Fedora.pem}"
          -o BatchMode=yes -o ConnectTimeout=10)
BACKUP_ROOT="$REMOTE_DIR/.deploy-backup"

# shellcheck source=scripts/_common.sh
source "$REPO_ROOT/scripts/_common.sh"

PLAN=0 YES=0 TARGET=""
for arg in "$@"; do
    case "$arg" in
        --plan) PLAN=1 ;;
        --yes|-y) YES=1 ;;
        --latest) TARGET="--latest" ;;
        -h|--help) TARGET="" ;;
        -*) err "未知参数: $arg"; exit 2 ;;
        *) TARGET="$arg" ;;
    esac
done

list_snapshots() {
    echo "可用快照（$SERVER:$BACKUP_ROOT）:"
    remote "ls -1t '$BACKUP_ROOT' 2>/dev/null | head -20" || true
}

if [[ -z "$TARGET" ]]; then
    list_snapshots
    exit 0
fi
if [[ "$TARGET" == "--latest" ]]; then
    TARGET="$(remote "ls -1t '$BACKUP_ROOT' 2>/dev/null | head -1")"
    [[ -n "$TARGET" ]] || { err "没有任何快照"; exit 1; }
fi
if [[ ! "$TARGET" =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    err "时间戳格式不对（应为 YYYYmmdd-HHMMSS）: $TARGET"
    list_snapshots
    exit 2
fi

SNAPSHOT="$BACKUP_ROOT/$TARGET/code.tgz"

if (( PLAN )); then
    echo "# 目标: $SERVER:$REMOTE_DIR"
    echo "# 快照: $SNAPSHOT"
    echo "# 校验: test -s + tar -tzf"
    echo "# 解包: tar -xzf $SNAPSHOT -C $REMOTE_DIR（只含代码）"
    echo "# 重启: (ssh $SERVER) cd $REMOTE_DIR && docker compose restart $SERVICE"
    echo "# 验收: healthz=$HEALTH_URL、docker inspect 健康状态、logs 无 traceback"
    exit 0
fi

log "校验快照 $SNAPSHOT"
remote "test -s '$SNAPSHOT' && tar -tzf '$SNAPSHOT' >/dev/null" \
    || { err "快照不存在或已损坏: $SNAPSHOT"; list_snapshots; exit 1; }

if (( ! YES )); then
    printf '将用快照 %s 覆盖远端代码并重启 %s，继续？[y/N] ' "$TARGET" "$SERVICE"
    read -r reply
    [[ "$reply" =~ ^[Yy]$ ]] || { log "已取消"; exit 1; }
fi

log "解包快照（只覆盖代码，不动数据库/密钥/贴图）"
remote "tar -xzf '$SNAPSHOT' -C '$REMOTE_DIR'"

SINCE="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
restart_service
if verify_after_restart "$SINCE"; then
    log "回滚完成: $TARGET"
    exit 0
fi
err "回滚后验收仍失败，需要人工介入"
err "快照留在 $SNAPSHOT；容器请检查: docker logs $CONTAINER"
exit 2
