#!/usr/bin/env bash
# oath-up / oath-down 共用的那部分。**source 进来用，不要直接执行。**
#
# 约定：一切相对路径按仓库根算，脚本先 cd 过去。配置只读 oath.toml —— 没有它就没有
# data_root，没有 data_root 就不知道 pidfile 在哪。
#
# **变量后面紧跟中文一定要写 `${VAR}`。** macOS 自带 bash 3.2 按字节认变量名，
# `$PID，日志` 会被当成一个叫 `PID，日志` 的变量，然后报「unbound variable」。
set -euo pipefail

OATH_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OATH_CONFIG="$OATH_ROOT/oath.toml"
OATH_ENV="$OATH_ROOT/scripts/oath-env.py"

#: 停 biz 最多等这么久。正常路径 SIGTERM 就够了，等不到才动手。
OATH_STOP_GRACE=10

oath_die() {
    echo "$@" >&2
    exit 1
}

oath_say() {
    echo "==> $*"
}

# 解释器直接用 venv 里的那个：不让 uv 留在进程树中间，否则 pidfile 记的是 uv 的 pid，
# 真正干活的 python 成了孙子，杀不到。
oath_python() {
    local candidate
    for candidate in "$OATH_ROOT/.venv/bin/python3" "$OATH_ROOT/.venv/bin/python"; do
        if [ -x "$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    done
    command -v uv >/dev/null 2>&1 || oath_die "没有 .venv，先跑 uv sync"
    uv run python -c 'import sys; print(sys.executable)'
}

# 把 oath-env.py show 的输出读进 REDIS_URL / REDIS_PORT / DATA_ROOT / HTTP_PORT。
oath_load_config() {
    [ -f "$OATH_CONFIG" ] || oath_die "没有 $OATH_CONFIG —— 先 cp oath.example.toml oath.toml 并改好"
    local parsed
    parsed="$(cd "$OATH_ROOT" && "$(oath_python)" "$OATH_ENV" show "$OATH_CONFIG")" || \
        oath_die "配置读不出来，先修 $OATH_CONFIG"
    eval "$parsed"
    RUN_DIR="$DATA_ROOT/run"
    PID_FILE="$RUN_DIR/oath.pid"
    LOG_FILE="$RUN_DIR/oath.log"
    REDIS_PID_FILE="$RUN_DIR/redis.pid"
}

oath_alive() {
    kill -0 "$1" 2>/dev/null
}

# pidfile 里的 pid 可能早就被系统回收给别的进程了。动手之前先确认它还是我们自己拉起来
# 的那个 —— 按 pid 盲杀是这类脚本最容易出的事故。
oath_pid_is() {
    local pid="$1" pattern="$2" command_line
    oath_alive "$pid" || return 1
    command_line="$(ps -o command= -p "$pid" 2>/dev/null || true)"
    case "$command_line" in
        *"$pattern"*) return 0 ;;
        *) return 1 ;;
    esac
}

# 这个进程组里还有没有我们的人。**pgid 是可以被系统回收再分配给别人的** —— 只有组里还
# 躺着至少一个 Oath 的进程，才敢往这个组上发信号。
oath_group_has_agent() {
    ps -o pgid=,command= -ax 2>/dev/null |
        awk -v group="$1" '$1 == group && /bootstrap\.agent\.process/ { found = 1 } END { exit !found }'
}

oath_wait_gone() {
    local pid="$1" deadline=$((SECONDS + OATH_STOP_GRACE))
    while oath_alive "$pid"; do
        [ "$SECONDS" -lt "$deadline" ] || return 1
        sleep 0.2
    done
    return 0
}

# biz 是 setsid 起来的新进程组组长，它拉起的 agent 与 supervisor 都在这个组里。整组一起
# 收，不用去猜 supervisor 的命令行 —— 那些是 multiprocessing 的 spawn 子进程，cmdline 上
# 根本没有能认出它们的东西。
#
# **pgid 一并写进 pidfile**：biz 被 kill -9 之后 pid 已经查不到了，但组还在，agent 与
# supervisor 还活着。只记 pid 的话这一轮就漏掉了它们。
oath_stop_biz() {
    if [ -f "$PID_FILE" ]; then
        local pid pgid
        read -r pid pgid <"$PID_FILE" || true
        rm -f "$PID_FILE"
        if [ -n "$pgid" ] && [ "$pgid" != "1" ] && oath_group_has_agent "$pgid"; then
            kill -TERM "-$pgid" 2>/dev/null || true
            local deadline=$((SECONDS + OATH_STOP_GRACE))
            while oath_group_has_agent "$pgid"; do
                [ "$SECONDS" -lt "$deadline" ] || break
                sleep 0.2
            done
            oath_group_has_agent "$pgid" && kill -KILL "-$pgid" 2>/dev/null || true
        elif [ -n "$pid" ]; then
            # 组里没人了，只剩 biz 自己。
            oath_pid_is "$pid" "bootstrap.biz.process" || \
                oath_say "pidfile 里的 $pid 已经不是 biz 了"
            oath_alive "$pid" && kill -TERM "$pid" 2>/dev/null || true
            oath_wait_gone "$pid" || kill -KILL "$pid" 2>/dev/null || true
        fi
    fi
    # 没有 pidfile 也照扫一遍。agent 的命令行认得出来；supervisor 只有进程组能认。
    pkill -TERM -f 'bootstrap\.agent\.process' 2>/dev/null || true
    sleep 0.5
    pkill -KILL -f 'bootstrap\.agent\.process' 2>/dev/null || true
}

oath_start_redis() {
    command -v redis-server >/dev/null 2>&1 || \
        oath_die "找不到 redis-server —— 队列是 Redis Streams，装一个（brew install redis）"
    if "$(oath_python)" "$OATH_ENV" listening 127.0.0.1 "$REDIS_PORT"; then
        oath_say "Redis 已经在 $REDIS_PORT 上，不动它"
        return 0
    fi
    mkdir -p "$RUN_DIR"
    # 不落盘、不追加：开发用的队列，重启本来就该是空的。要持久化就在 oath.toml 里换地址。
    redis-server \
        --port "$REDIS_PORT" \
        --bind 127.0.0.1 \
        --dir "$RUN_DIR" \
        --save '' \
        --appendonly no \
        --daemonize yes \
        --pidfile "$REDIS_PID_FILE" \
        --logfile "$RUN_DIR/redis.log"
    local deadline=$((SECONDS + 10))
    while ! "$(oath_python)" "$OATH_ENV" listening 127.0.0.1 "$REDIS_PORT"; do
        [ "$SECONDS" -lt "$deadline" ] || oath_die "Redis 没起来，看 $RUN_DIR/redis.log"
        sleep 0.2
    done
    oath_say "Redis 起在 $REDIS_PORT"
}

oath_stop_redis() {
    [ -f "$REDIS_PID_FILE" ] || {
        oath_say "Redis 不是脚本起的，不动它"
        return 0
    }
    local pid
    pid="$(cat "$REDIS_PID_FILE" 2>/dev/null || true)"
    rm -f "$REDIS_PID_FILE"
    [ -n "$pid" ] || return 0
    oath_pid_is "$pid" "redis-server" || {
        oath_say "pidfile 里的 $pid 已经不是 redis 了，跳过"
        return 0
    }
    kill -TERM "$pid" 2>/dev/null || true
    oath_wait_gone "$pid" || kill -KILL "$pid" 2>/dev/null || true
    oath_say "Redis 停了"
}
