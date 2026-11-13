#!/usr/bin/env bash
# deploy.sh — 把本地 KirikoBot 代码同步到阿里云并重启机器人。
#
# 安全边界（硬编码，没有关闭开关）：
#   * 不删除远端任何文件：rsync 不加删除参数
#   * 只同步白名单：KirikoBot/*.py、templates/、static/，其余一律不同步
#   * 永不覆盖 robot.db*、.env、.dashboard_password、backups/、stickers/、
#     shared_files/、llbot_config/、carside_picture/
#   * 不重建镜像：requirements.txt 与远端不一致时直接中止
#   * 部署前打 tar 快照到 /root/kirikobot/.deploy-backup/<ts>/code.tgz
#   * 重启后验收 healthz=ok、容器 healthy、日志无 traceback；失败打印回滚命令
#
# 用法：scripts/deploy.sh [--dry-run] [--plan] [--yes] [--allow-dirty] [--allow-branch]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_SRC="${LOCAL_SRC:-$REPO_ROOT/KirikoBot}"
SERVER="${SERVER:-root@47.108.183.60}"
REMOTE_DIR="${REMOTE_DIR:-/root/kirikobot}"
REMOTE_SRC="$REMOTE_DIR/KirikoBot"
SERVICE="${SERVICE:-my-robot}"
CONTAINER="${CONTAINER:-kirikobot}"
HEALTH_URL="${HEALTH_URL:-http://localhost:5000/healthz}"
SSH_OPTS=(-F "${SSH_CONFIG:-$HOME/.ssh/config}" -i "${SSH_KEY:-$HOME/.ssh/Fedora.pem}"
          -o BatchMode=yes -o ConnectTimeout=10)
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"

# shellcheck source=scripts/_common.sh
source "$REPO_ROOT/scripts/_common.sh"

DRY_RUN=0 PLAN=0 YES=0 ALLOW_DIRTY=0 ALLOW_BRANCH=0
usage() {
    sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}
while (( $# )); do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --plan) PLAN=1 ;;
        --yes|-y) YES=1 ;;
        --allow-dirty) ALLOW_DIRTY=1 ;;
        --allow-branch) ALLOW_BRANCH=1 ;;
        -h|--help) usage 0 ;;
        *) err "未知参数: $1"; usage 2 ;;
    esac
    shift
done

# ── 白名单 / 保护名单 ────────────────────────────────────────────────
# 目录：这些内容属于线上运行时数据，只允许留在服务器上。
PROTECTED_DIRS=(stickers backups shared_files carside_picture llbot_config __pycache__ .pytest_cache)
# 文件：密钥与数据库，同样绝不参与同步。
PROTECTED_FILES=(.env .dashboard_password 'robot.db*' '*.db-wal' '*.db-shm' '*.log')

RSYNC_ARGS=(-a --numeric-ids --human-readable --itemize-changes --prune-empty-dirs)
for d in "${PROTECTED_DIRS[@]}"; do
    RSYNC_ARGS+=("--exclude=/$d/")
done
for f in "${PROTECTED_FILES[@]}"; do
    RSYNC_ARGS+=("--exclude=/$f")
done
# 白名单：只放行顶层 *.py 与前端资源，最后一条把其余全部挡掉。
RSYNC_ARGS+=(--include='/templates/***' --include='/static/***' --include='/*.py' --include='/requirements.txt' --exclude='*')

# 只同步 git 跟踪的文件：未提交/未跟踪的 WIP（如 dsh_plugins.py、tts_*.py）
# 一律不上线。删除文件不会被同步删除（本流程不删除远端文件），需人工处理。
TRACKED_LIST="$(mktemp)"
trap 'rm -f "$TRACKED_LIST"' EXIT
git -C "$REPO_ROOT" ls-files -- \
    'KirikoBot/*.py' 'KirikoBot/templates' 'KirikoBot/static' 'KirikoBot/requirements.txt' \
    | sed 's|^KirikoBot/||' > "$TRACKED_LIST"
[[ -s "$TRACKED_LIST" ]] || { err "git 跟踪文件列表为空，拒绝部署"; exit 1; }

RSYNC_CMD=(rsync "${RSYNC_ARGS[@]}" --files-from="$TRACKED_LIST" \
    -e "ssh ${SSH_OPTS[*]}" "$LOCAL_SRC/" "$SERVER:$REMOTE_SRC/")

TAR_EXCLUDES=()
for d in "${PROTECTED_DIRS[@]}"; do
    TAR_EXCLUDES+=("--exclude=KirikoBot/$d")
done
for f in "${PROTECTED_FILES[@]}"; do
    TAR_EXCLUDES+=("--exclude=KirikoBot/$f")
done
# 远端 shell 会二次展开参数，这里先逐个做 shell 转义。
TAR_EXCLUDES_Q=""
for e in "${TAR_EXCLUDES[@]}"; do
    TAR_EXCLUDES_Q+=" $(printf '%q' "$e")"
done

# ── plan：只打印将执行的命令，不连服务器 ─────────────────────────────
if (( PLAN )); then
    echo "# 目标: $SERVER:$REMOTE_SRC"
    echo "# 快照: $REMOTE_DIR/.deploy-backup/<ts>/code.tgz"
    echo "# 保护: ${PROTECTED_DIRS[*]} / ${PROTECTED_FILES[*]}"
    echo "# 本次只同步 git 跟踪的 $(wc -l < "$TRACKED_LIST") 个文件（未跟踪 WIP 不上线）"
    echo "# rsync（不删除远端文件）:"
    printf '  '; printf '%q ' "${RSYNC_CMD[@]}"; echo
    echo "# 快照:"
    printf '  tar -czf %q -C %q%s KirikoBot\n' \
        "$REMOTE_DIR/.deploy-backup/<ts>/code.tgz" "$REMOTE_DIR" "$TAR_EXCLUDES_Q"
    echo "# 重启:"
    printf '  (ssh %s) cd %q && docker compose restart %q\n' "$SERVER" "$REMOTE_DIR" "$SERVICE"
    echo "# 验收: healthz=$HEALTH_URL、docker inspect 健康状态、logs 无 traceback"
    exit 0
fi

# ── 本地前置检查 ────────────────────────────────────────────────────
if (( ! ALLOW_DIRTY )); then
    if ! git -C "$REPO_ROOT" diff --quiet || ! git -C "$REPO_ROOT" diff --cached --quiet; then
        err "工作区有未提交改动；确认无误后加 --allow-dirty，或先提交"
        exit 1
    fi
fi
CURRENT_BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD)"
if (( ! ALLOW_BRANCH )) && [[ "$CURRENT_BRANCH" != "$DEPLOY_BRANCH" ]]; then
    err "当前分支 $CURRENT_BRANCH ≠ $DEPLOY_BRANCH；合并后再部署，或加 --allow-branch"
    exit 1
fi
[[ -d "$LOCAL_SRC" ]] || { err "本地目录不存在: $LOCAL_SRC"; exit 1; }

# ── 远端前置检查（只读） ────────────────────────────────────────────
log "检查 $SERVER:$REMOTE_SRC"
remote "test -w '$REMOTE_SRC'" || { err "远端目录不可写: $REMOTE_SRC"; exit 1; }
remote "cd '$REMOTE_DIR' && docker compose config --services" | grep -qx "$SERVICE" \
    || { err "远端 compose 里没有服务 $SERVICE"; exit 1; }
if [[ -f "$LOCAL_SRC/requirements.txt" ]]; then
    REMOTE_REQ=$(remote "cat '$REMOTE_SRC/requirements.txt' 2>/dev/null || true")
    if ! diff -q <(printf '%s\n' "$REMOTE_REQ") "$LOCAL_SRC/requirements.txt" >/dev/null 2>&1; then
        ADDED=$(requirements_added_count "$LOCAL_SRC/requirements.txt" "$REMOTE_REQ")
        if [[ "$ADDED" != "0" ]]; then
            err "本地 requirements.txt 有 $ADDED 行远端不存在的依赖：重启不会安装它们"
            err "本流程禁止重建镜像；请先人工确认依赖已装进镜像，再部署代码"
            exit 1
        fi
        log "requirements.txt 仅删除了远端已有依赖，将一并同步（不需要重建）"
    fi
fi

# ── dry-run：只让 rsync 列出差异，不写任何东西 ──────────────────────
if (( DRY_RUN )); then
    log "dry-run：只列出 rsync 将同步的文件"
    rsync -n "${RSYNC_ARGS[@]}" --files-from="$TRACKED_LIST" \
        -e "ssh ${SSH_OPTS[*]}" "$LOCAL_SRC/" "$SERVER:$REMOTE_SRC/"
    exit 0
fi

# ── 人工确认 ────────────────────────────────────────────────────────
if (( ! YES )); then
    printf '将部署 %s 到 %s:%s 并重启 %s，继续？[y/N] ' \
        "$CURRENT_BRANCH" "$SERVER" "$REMOTE_SRC" "$SERVICE"
    read -r reply
    [[ "$reply" =~ ^[Yy]$ ]] || { log "已取消"; exit 1; }
fi

# ── 快照（先于任何写入） ────────────────────────────────────────────
TS="$(date '+%Y%m%d-%H%M%S')"
SNAPSHOT_DIR="$REMOTE_DIR/.deploy-backup/$TS"
SNAPSHOT="$SNAPSHOT_DIR/code.tgz"
log "打快照 $SNAPSHOT"
remote "set -e; mkdir -p '$SNAPSHOT_DIR'; tar -czf '$SNAPSHOT' -C '$REMOTE_DIR'$TAR_EXCLUDES_Q KirikoBot; test -s '$SNAPSHOT'; tar -tzf '$SNAPSHOT' >/dev/null"
log "快照完成: $(remote "du -h '$SNAPSHOT' | cut -f1")"

# ── 同步代码 ────────────────────────────────────────────────────────
log "同步代码（不删除远端文件）"
"${RSYNC_CMD[@]}"

# ── 重启 + 验收 ─────────────────────────────────────────────────────
SINCE="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
restart_service
if verify_after_restart "$SINCE"; then
    log "部署完成，快照: $SNAPSHOT"
    exit 0
fi
err "部署后验收失败"
err "回滚命令: $REPO_ROOT/scripts/rollback.sh $TS"
exit 1
