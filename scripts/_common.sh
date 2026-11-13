#!/usr/bin/env bash
# _common.sh — deploy.sh / rollback.sh 共用的远程执行与部署验收。
#
# 调用方必须先设置：SERVER、REMOTE_DIR、SSH_OPTS、CONTAINER、HEALTH_URL。
# 只读约定：这里所有函数只做「读」和「重启服务」，不碰 robot.db / .env /
# backups / stickers / shared_files / llbot_config。

remote() {
    # 在远端执行一条命令（字符串）。调用方负责引号。
    ssh "${SSH_OPTS[@]}" "$SERVER" "$1"
}

log() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
err() { printf '[%s] ✗ %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }

# 验收等待时长（秒）。测试里压到 0，避免为了等超时把用例拖到几分钟。
HEALTHZ_WAIT="${HEALTHZ_WAIT:-60}"
HEALTHY_WAIT="${HEALTHY_WAIT:-120}"

# 轮询 healthz，最多等 $2 秒（默认 60）。
wait_healthz() {
    local url=$1 deadline=$((SECONDS + ${2:-60}))
    while (( SECONDS < deadline )); do
        if remote "curl -fsS --max-time 3 '$url'" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    return 1
}

# 轮询容器健康状态，最多等 $1 秒（默认 120）。
wait_healthy() {
    local deadline=$((SECONDS + ${1:-120})) status="unknown"
    while (( SECONDS < deadline )); do
        status=$(remote "docker inspect --format '{{.State.Health.Status}}' '$CONTAINER'" 2>/dev/null || true)
        [[ "$status" == "healthy" ]] && return 0
        sleep 3
    done
    err "容器健康状态=${status:-unknown}"
    return 1
}

# 重启以来的日志里不允许出现 traceback（大小写不敏感）。
# fail-closed：ssh 失败、容器不在跑、日志输出为空或不可解析，一律判 FAIL ——
# 「查不到」绝不能当成「没问题」。
check_no_traceback() {
    local since=$1 running output count
    if ! running=$(remote "docker inspect --format '{{.State.Running}}' '$CONTAINER'"); then
        err "无法确认容器状态（ssh/docker 失败），按 FAIL 处理"
        return 1
    fi
    if [[ "$(printf '%s' "$running" | tr -d '[:space:]')" != "true" ]]; then
        err "容器未在运行（docker inspect Running=${running:-空}），按 FAIL 处理"
        return 1
    fi
    if ! output=$(remote "docker logs --since '$since' '$CONTAINER' 2>&1 | grep -ci traceback || true"); then
        err "无法读取容器日志（ssh/docker 失败），按 FAIL 处理"
        return 1
    fi
    count=$(printf '%s' "$output" | tr -d '[:space:]')
    if [[ ! "$count" =~ ^[0-9]+$ ]]; then
        err "日志检查输出不可解析（${count:-空}），按 FAIL 处理"
        return 1
    fi
    if (( count > 0 )); then
        err "日志出现 $count 处 traceback"
        remote "docker logs --since '$since' '$CONTAINER' 2>&1 | grep -i -m3 -A3 traceback" >&2 || true
        return 1
    fi
    return 0
}

# 重启后验收：healthz=ok、容器 healthy、日志无 traceback。
# 用法：verify_after_restart <since-rfc3339>
verify_after_restart() {
    local since=$1 failed=0
    if wait_healthz "$HEALTH_URL" "$HEALTHZ_WAIT"; then
        log "healthz=ok"
    else
        err "healthz 未通过（$HEALTH_URL）"
        failed=1
    fi
    if wait_healthy "$HEALTHY_WAIT"; then
        log "容器 healthy"
    else
        failed=1
    fi
    if check_no_traceback "$since"; then
        log "日志无 traceback"
    else
        failed=1
    fi
    return "$failed"
}

# 统计本地 requirements 里「远端没有」的依赖行数（新增依赖需要重建镜像）。
# 只做文本比较，不联网、不装包。
requirements_added_count() {
    local local_file=$1 remote_content=$2
    awk 'NR==FNR { seen[$0] = 1; next } !seen[$0] && NF' \
        <(printf '%s\n' "$remote_content") "$local_file" | wc -l
}

# 重启服务（不重建镜像）。调用方在重启前自己取 since 时间戳。
restart_service() {
    log "重启 ${SERVICE}（docker compose restart，不重建）"
    remote "cd '$REMOTE_DIR' && docker compose restart '$SERVICE'" >/dev/null
}
