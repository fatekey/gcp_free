import requests
import ipaddress

def get_gcp_ips_merged():
    url = "https://www.gstatic.com/ipranges/cloud.json"
    
    # 俄勒冈 (us-west1), 爱荷华 (us-central1), 南卡罗来纳 (us-east1)
    target_regions = {"us-west1", "us-central1", "us-east1"}
    
    print(f"正在获取并计算合并 IP 段...")
    
    try:
        response = requests.get(url)
        data = response.json()
        
        # 1. 收集所有目标区域的 IPv4 对象
        networks = []
        for prefix in data.get("prefixes", []):
            if prefix.get("scope") in target_regions:
                if "ipv4Prefix" in prefix:
                    # 将字符串转换为 IPv4Network 对象
                    net = ipaddress.IPv4Network(prefix["ipv4Prefix"])
                    networks.append(net)
        
        # 2. 核心步骤：合并相邻网段 (collapse_addresses)
        # 这个函数会自动去重，并将相邻/包含的网段合并为最大的 CIDR
        merged_networks = list(ipaddress.collapse_addresses(networks))
        
        print(f"原始段数: {len(networks)} -> 合并后段数: {len(merged_networks)}\n")
        
        # 3. 输出结果
        for net in merged_networks:
            print(str(net))

    except Exception as e:
        print(f"发生错误: {e}")

if __name__ == "__main__":
    get_gcp_ips_merged()