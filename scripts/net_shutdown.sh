#!/bin/bash

# ==========================================
# 流量监控自动部署脚本 (断网版)
# 功能：
# 1. 自动获取网卡，只监控出站流量 (TX)
# 2. 流量超标后直接关闭网卡 (ip link set down) -> SSH 会断开
# 3. 每月1号重置流量、删除旧日志并自动恢复网卡 (ip link set up)
# ==========================================

# 1. 检查 Root 权限
if [ "$(id -u)" -ne 0 ]; then
    echo "错误：请使用 root 权限运行此脚本。"
    exit 1
fi

# 2. 自动获取默认网卡名称
# 注意：如果网卡已经被 down 掉，ip route 可能查不到默认路由。
# 为了防止脚本重装时找不到网卡，我们优先尝试 ip route，
# 如果失败（比如当前就是断网状态），则尝试获取第一个非 lo 的网卡。
INTERFACE=$(ip route | grep default | awk '{print $5}' | head -n1)

if [ -z "$INTERFACE" ]; then
    # 备选方案：获取第一个非 loopback 的网卡
    INTERFACE=$(ip link show | awk -F': ' '/^[0-9]+: [^lo]/ {print $2}' | head -n1)
fi

if [ -z "$INTERFACE" ]; then
    echo "错误：无法自动检测到网卡名称，请手动修改脚本中的 INTERFACE 变量。"
    exit 1
fi

echo "--> 检测到当前主网卡为: $INTERFACE"

# 3. 安装依赖工具
echo "--> 正在更新软件源并安装工具..."
apt-get update -y
apt-get install vnstat bc -y

# 4. 配置并启动 vnStat
echo "--> 配置 vnStat..."
# 尝试添加接口 (如果接口已存在会报错但无害，忽略即可)
if ! vnstat --add -i "$INTERFACE" 2>/dev/null; then
    echo "    (接口可能已存在，跳过添加)"
fi

systemctl enable vnstat
systemctl restart vnstat

# 等待服务启动
sleep 5
# 强制更新一次数据库，确保有数据文件
vnstat -i "$INTERFACE" > /dev/null 2>&1

# 5. 生成监控脚本 (/root/check_traffic.sh)
echo "--> 生成监控脚本 /root/check_traffic.sh..."
cat > /root/check_traffic.sh <<EOF
#!/bin/bash

# 强制使用标准区域设置
export LC_ALL=C

# 配置
LOG_FILE="/var/log/traffic_monitor.log"
INTERFACE="$INTERFACE"
LIMIT=180

# 日志记录函数
log() {
    echo "\$(date '+%Y-%m-%d %H:%M:%S') - \$1" >> "\$LOG_FILE"
}

# 权限检查
if [ "\$(id -u)" -ne 0 ]; then
    echo "错误：需要 root 权限"
    exit 1
fi

# 检查网卡当前状态 (UP 或 DOWN)
# ip link show 输出中，UP 状态通常包含 "state UP" 或 "<...UP...>"
IS_UP=\$(ip link show "\$INTERFACE" | grep -q "UP" && echo "yes" || echo "no")

# 获取流量数据 (强制使用 'b' 参数获取字节单位)
# 即使网卡 down 了，vnstat 依然可以读取数据库中的历史数据
VNSTAT_RAW=\$(vnstat -i "\$INTERFACE" --oneline b 2>/dev/null)

# 提取出站流量 (TX)，第 5 个字段
TX_BYTES=\$(echo "\$VNSTAT_RAW" | cut -d ';' -f 5)

if [[ -z "\$TX_BYTES" ]]; then TX_BYTES=0; fi

# 转换为 GB
TX_GB=\$(echo "scale=2; \$TX_BYTES / 1073741824" | bc)

# ==========================================
# 1. 终端直接输出
# ==========================================
echo "========================================"
echo " 网卡接口    : \$INTERFACE"
echo " 当前状态    : \$([ "\$IS_UP" == "yes" ] && echo "在线 (UP)" || echo "已断开 (DOWN)")"
echo " 当前时间    : \$(date '+%Y-%m-%d %H:%M:%S')"
echo " 精确出站(TX): \$TX_BYTES Bytes"
echo " 换算出站(TX): \$TX_GB GB"
echo " 流量上限    : \$LIMIT GB"
echo "========================================"

# ==========================================
# 2. 判断逻辑
# ==========================================

log "当前出站流量: \$TX_GB GB (限制: \$LIMIT GB) [状态: \$([ "\$IS_UP" == "yes" ] && echo "UP" || echo "DOWN")]"

# 检查是否超限
if [ \$(echo "\$TX_GB >= \$LIMIT" | bc) -eq 1 ]; then
    
    # 如果当前还是 UP 状态，才执行关闭操作，避免重复执行
    if [ "\$IS_UP" == "yes" ]; then
        echo "状态: [警告] 流量已超限，正在关闭网卡..."
        log "警告：流量超出限制！执行断网操作 (ip link set \$INTERFACE down)..."
        
        # === 核心动作：关闭网卡 ===
        ip link set "\$INTERFACE" down
        
        log "网卡已关闭。SSH 连接将中断。"
    else
        echo "状态: [已封禁] 流量超限，网卡保持关闭状态。"
    fi
else
    echo "状态: [正常] 流量未超限。"
    # 如果因为某种原因网卡是 down 的但流量没超标（例如误操作），可以在这里加逻辑自动恢复
    # 但为了安全起见，这里不自动恢复，只在重置脚本里恢复。
fi
EOF

# 6. 生成重置脚本 (/root/reset_network.sh)
echo "--> 生成重置脚本 /root/reset_network.sh..."
cat > /root/reset_network.sh <<EOF
#!/bin/bash

RESET_LOG="/var/log/network_reset.log"
TRAFFIC_LOG="/var/log/traffic_monitor.log"
INTERFACE="$INTERFACE"

log() {
    echo "\$(date '+%Y-%m-%d %H:%M:%S') - \$1" >> "\$RESET_LOG"
}

log "开始执行每月网络重置..."

# 1. 恢复网卡 (核心动作)
log "正在恢复网卡接口 (\$INTERFACE)..."
ip link set "\$INTERFACE" up
sleep 5
log "网卡已尝试启动。"

# 2. 删除旧的流量监控日志
if [ -f "\$TRAFFIC_LOG" ]; then
    rm -f "\$TRAFFIC_LOG"
    log "已删除旧的流量监控日志。"
fi

# 3. 清理 iptables (防止用户之前用过 iptables 版本残留)
iptables -F
iptables -X
iptables -P INPUT ACCEPT
iptables -P OUTPUT ACCEPT
iptables -P FORWARD ACCEPT

# 4. 重置 vnStat 数据库
systemctl stop vnstat
vnstat --remove --force -i "\$INTERFACE"
vnstat --add -i "\$INTERFACE"
systemctl start vnstat

# 强制刷新
sleep 3
vnstat -i "\$INTERFACE" > /dev/null 2>&1

log "vnStat 数据库已重置，网络已恢复。"
EOF

# 7. 赋予执行权限
chmod +x /root/check_traffic.sh
chmod +x /root/reset_network.sh

# 8. 设置定时任务
echo "--> 更新 Crontab 定时任务..."
crontab -l > /tmp/cron_bk 2>/dev/null

# 清理旧任务
sed -i '/check_traffic.sh/d' /tmp/cron_bk
sed -i '/reset_network.sh/d' /tmp/cron_bk

# 添加新任务
# 每5分钟检查一次流量
echo "*/5 * * * * /root/check_traffic.sh" >> /tmp/cron_bk
# 每月1号 00:00 重置
echo "0 0 1 * * /root/reset_network.sh" >> /tmp/cron_bk

crontab /tmp/cron_bk
rm /tmp/cron_bk

echo "=========================================="
echo " 安装完成！(Hard Shutdown Mode)"
echo "=========================================="
echo " 警告：当流量超标时，将执行 'ip link set $INTERFACE down'。"
echo " 这将导致 SSH 立即断开，直到下月 1 号自动恢复。"
echo "=========================================="
echo "手动查看流量: bash /root/check_traffic.sh"
echo "日志文件位置: /var/log/traffic_monitor.log"
echo "=========================================="