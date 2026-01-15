#!/bin/bash

# 检查 root 权限
if [ "$EUID" -ne 0 ]; then 
    echo "错误: 请以 root 用户运行此脚本"
    exit 1
fi

SOURCE_FILE="/etc/apt/sources.list.d/debian.sources"

echo "=== 正在换源 ==="

# 1. 备份
if [ -f "$SOURCE_FILE" ]; then
    cp "$SOURCE_FILE" "${SOURCE_FILE}.bak_fix_$(date +%Y%m%d_%H%M%S)"
fi

# 2. 写入混合配置
# 主源 (Main): 继续使用 MIT (mirrors.mit.edu)
# 安全源 (Security): UC Berkeley (mirrors.ocf.berkeley.edu)

cat > "$SOURCE_FILE" <<EOF
Types: deb deb-src
URIs: http://mirrors.mit.edu/debian
Suites: bookworm bookworm-updates bookworm-backports
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb deb-src
URIs: http://mirrors.ocf.berkeley.edu/debian-security
Suites: bookworm-security
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF

# 3. 清理并更新
echo "-> 清理缓存..."
rm -rf /var/lib/apt/lists/*

echo "-> 正在更新源..."
apt update

if [ $? -eq 0 ]; then
    echo "=== 完美！所有源均已连接成功，且未使用 Fastly CDN ==="
else
    echo "=== 仍然有错误，请检查网络或尝试其他镜像 ==="
fi