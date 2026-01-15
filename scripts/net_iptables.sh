#!/bin/bash

# ==========================================
# 流量监控自动部署脚本
# 功能：
# 1. 自动获取网卡，只监控出站流量 (TX)
# 2. 运行 check_traffic.sh 时终端显示精确流量，日志保留简略信息
# 3. 每月重置流量并删除旧的监控日志
# ==========================================

# 1. 检查 Root 权限
if [ "$(id -u)" -ne 0 ]; then
    echo "错误：请使用 root 权限运行此脚本。"
    exit 1
fi

# 2. 自动获取默认网卡名称
INTERFACE=$(ip route | grep default | awk '{print $5}' | head -n1)

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
# 尝试添加接口
if ! vnstat --add -i "$INTERFACE" 2>/dev/null; then
    echo "    (接口可能已存在，跳过添加)"
fi

systemctl enable vnstat
systemctl restart vnstat

# 等待服务启动并生成初始数据库
sleep 5
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

# 日志记录函数 (保持原格式)
log() {
    echo "\$(date '+%Y-%m-%d %H:%M:%S') - \$1" >> "\$LOG_FILE"
}

# 权限检查
if [ "\$(id -u)" -ne 0 ]; then
    echo "错误：需要 root 权限"
    exit 1
fi

# 获取流量数据 (强制使用 'b' 参数获取字节单位)
# 输出格式示例: 1;ens4;2026-01-15;RX_BYTES;TX_BYTES;...
VNSTAT_RAW=\$(vnstat -i "\$INTERFACE" --oneline b 2>/dev/null)

# 提取出站流量 (TX)，第 5 个字段
TX_BYTES=\$(echo "\$VNSTAT_RAW" | cut -d ';' -f 5)

# 如果获取失败或为空，默认为 0
if [[ -z "\$TX_BYTES" ]]; then
    TX_BYTES=0
fi

# 将字节转换为 GB (1 GB = 1073741824 Bytes)
TX_GB=\$(echo "scale=2; \$TX_BYTES / 1073741824" | bc)

# ==========================================
# 1. 终端直接输出 (显示精确数值)
# ==========================================
echo "========================================"
echo " 网卡接口    : \$INTERFACE"
echo " 当前时间    : \$(date '+%Y-%m-%d %H:%M:%S')"
echo " 精确出站(TX): \$TX_BYTES Bytes"
echo " 换算出站(TX): \$TX_GB GB"
echo " 流量上限    : \$LIMIT GB"
echo "========================================"

# ==========================================
# 2. 日志记录与限制逻辑 (保持简洁)
# ==========================================

log "当前出站流量: \$TX_GB GB (限制: \$LIMIT GB)"

# 检查是否超限
if [ \$(echo "\$TX_GB >= \$LIMIT" | bc) -eq 1 ]; then
    echo "状态: [警告] 流量已超限，正在应用防火墙规则..."
    log "警告：流量超出限制！正在执行封禁策略..."
    
    # 封禁策略
    iptables -F
    iptables -X
    iptables -P INPUT DROP
    iptables -P FORWARD DROP
    iptables -P OUTPUT ACCEPT
    
    # 放行规则
    iptables -A INPUT -p tcp --dport 22 -j ACCEPT
    iptables -A INPUT -i lo -j ACCEPT
    iptables -A OUTPUT -o lo -j ACCEPT
    
    log "网络已限制 (仅保留 SSH)。"
else
    echo "状态: [正常] 流量未超限。"
    log "流量正常。"
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

# 1. 删除旧的流量监控日志 (新增功能)
if [ -f "\$TRAFFIC_LOG" ]; then
    rm -f "\$TRAFFIC_LOG"
    log "已删除旧的流量监控日志: \$TRAFFIC_LOG"
else
    log "流量监控日志不存在，无需删除。"
fi

# 2. 重置防火墙规则
iptables -P INPUT ACCEPT
iptables -P OUTPUT ACCEPT
iptables -P FORWARD ACCEPT
iptables -F
iptables -X
log "防火墙规则已重置，限制已解除。"

# 3. 重置 vnStat 数据库
systemctl stop vnstat
vnstat --remove --force -i "\$INTERFACE"
vnstat --add -i "\$INTERFACE"
systemctl start vnstat

# 强制刷新一次数据以确保数据库建立
sleep 3
vnstat -i "\$INTERFACE" > /dev/null 2>&1

log "vnStat 数据库已重置 (接口: \$INTERFACE)。"
EOF

# 7. 赋予执行权限
chmod +x /root/check_traffic.sh
chmod +x /root/reset_network.sh

# 8. 设置定时任务
echo "--> 更新 Crontab 定时任务..."
crontab -l > /tmp/cron_bk 2>/dev/null

# 清理旧任务，防止重复
sed -i '/check_traffic.sh/d' /tmp/cron_bk
sed -i '/reset_network.sh/d' /tmp/cron_bk

# 添加新任务
# 每5分钟检查一次流量
echo "*/5 * * * * /root/check_traffic.sh" >> /tmp/cron_bk
# 每月1号 00:00 重置网络和日志
echo "0 0 1 * * /root/reset_network.sh" >> /tmp/cron_bk

crontab /tmp/cron_bk
rm /tmp/cron_bk

echo "=========================================="
echo " 安装完成！"
echo "=========================================="
echo "您可以手动运行以下命令查看精确流量："
echo "  bash /root/check_traffic.sh"
echo ""
echo "监控日志位置："
echo "  /var/log/traffic_monitor.log"
echo "=========================================="