import sys
import os
import traceback
from google.cloud import compute_v1
from google.cloud import resourcemanager_v3

# --- 辅助函数：选择项目 ---
def select_gcp_project():
    print("\n正在扫描您的项目列表...")
    try:
        client = resourcemanager_v3.ProjectsClient()
        request = resourcemanager_v3.SearchProjectsRequest(query="")
        page_result = client.search_projects(request=request)
        
        active_projects = []
        for project in page_result:
            if project.state == resourcemanager_v3.Project.State.ACTIVE:
                active_projects.append(project)

        if not active_projects:
            print("【注意】未找到活跃的项目。将尝试使用默认环境项目。")
            return "pelagic-pod-432503-h1"

        print("\n--- 请选择目标项目 ---")
        for i, p in enumerate(active_projects):
            print(f"[{i+1}] {p.project_id} ({p.display_name})")

        while True:
            choice = input(f"请输入数字选择 (1-{len(active_projects)}): ").strip()
            if choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(active_projects):
                    return active_projects[idx].project_id
            print("输入无效，请重试。")
    except Exception as e:
        print(f"无法列出项目: {e}。使用默认设置。")
        return "pelagic-pod-432503-h1"

# --- 功能 1: 列出当前项目的所有实例 ---
def list_all_instances(project_id):
    instance_client = compute_v1.InstancesClient()
    request = compute_v1.AggregatedListInstancesRequest(project=project_id)
    
    print(f"\n正在扫描项目 {project_id} 中的实例...")
    
    found_any = False
    agg_response = instance_client.aggregated_list(request=request)
    
    all_instances = []
    
    # 修正: 直接遍历 Pager
    for zone, response in agg_response:
        if response.instances:
            for instance in response.instances:
                found_any = True
                zone_short = zone.split('/')[-1]
                network = instance.network_interfaces[0].network.split('/')[-1]
                internal_ip = instance.network_interfaces[0].network_i_p
                print(f" - [实例] {instance.name:<20} | 区域: {zone_short:<15} | 网络: {network} | 内网IP: {internal_ip}")
                all_instances.append(instance)
    
    if not found_any:
        print("未在该项目中找到任何实例。")
    
    return all_instances

# --- 功能 2: 读取 cdnip.txt ---
def read_cdn_ips(filename="cdnip.txt"):
    if not os.path.exists(filename):
        print(f"【错误】找不到文件: {filename}")
        print("请在脚本同目录下创建该文件，并填入IP段。")
        return []
    
    ip_list = []
    with open(filename, 'r', encoding='utf-8') as f:
        for line in f:
            clean_line = line.strip()
            if clean_line:
                ip = clean_line.split()[0]
                ip_list.append(ip)
    
    print(f"已从 {filename} 读取到 {len(ip_list)} 个 IP 段。")
    return ip_list

# --- 辅助：设置协议字段 (带自动重试/调试) ---
def set_protocol_field(config_object, value):
    """
    尝试设置协议字段，解决 naming convention 的混乱问题。
    优先尝试 ip_protocol (全小写)，这是最标准的 proto-plus 转换。
    """
    try:
        # 尝试 1: 全小写 (最可能的修复)
        config_object.ip_protocol = value
    except AttributeError:
        try:
            # 尝试 2: I_p_protocol (罕见但存在)
            config_object.I_p_protocol = value
        except AttributeError:
            print(f"\n【调试信息】无法设置协议字段。对象 '{type(config_object).__name__}' 的有效属性如下:")
            print([d for d in dir(config_object) if not d.startswith('_')])
            raise

# --- 功能 3: 添加允许所有入站规则 ---
def add_allow_all_ingress(project_id, network="global/networks/default"):
    firewall_client = compute_v1.FirewallsClient()
    rule_name = "allow-all-ingress-custom"
    
    print(f"\n正在创建入站规则: {rule_name} ...")
    
    firewall_rule = compute_v1.Firewall()
    firewall_rule.name = rule_name
    firewall_rule.direction = "INGRESS"
    firewall_rule.network = network
    firewall_rule.priority = 1000
    firewall_rule.source_ranges = ["0.0.0.0/0"]
    
    allow_config = compute_v1.Allowed()
    
    # === 关键修正: 使用辅助函数设置 ip_protocol ===
    set_protocol_field(allow_config, "all")
    
    firewall_rule.allowed = [allow_config]

    try:
        operation = firewall_client.insert(project=project_id, firewall_resource=firewall_rule)
        print("正在应用规则...")
        operation_client = compute_v1.GlobalOperationsClient()
        operation_client.wait(project=project_id, operation=operation.name)
        print("【成功】已添加允许所有入站连接的规则。")
    except Exception as e:
        if "already exists" in str(e):
            print(f"【跳过】规则 {rule_name} 已存在。")
        else:
            print(f"【失败】{e}")
            traceback.print_exc()

# --- 功能 4: 添加拒绝 CDN 出站规则 ---
def add_deny_cdn_egress(project_id, ip_ranges, network="global/networks/default"):
    if not ip_ranges:
        print("IP 列表为空，跳过创建拒绝规则。")
        return

    firewall_client = compute_v1.FirewallsClient()
    rule_name = "deny-cdn-egress-custom"
    
    print(f"\n正在创建出站拒绝规则: {rule_name} ...")
    
    firewall_rule = compute_v1.Firewall()
    firewall_rule.name = rule_name
    firewall_rule.direction = "EGRESS"
    firewall_rule.network = network
    firewall_rule.priority = 900
    firewall_rule.destination_ranges = ip_ranges
    
    deny_config = compute_v1.Denied()
    
    # === 关键修正: 使用辅助函数设置 ip_protocol ===
    set_protocol_field(deny_config, "all")
    
    firewall_rule.denied = [deny_config]

    try:
        operation = firewall_client.insert(project=project_id, firewall_resource=firewall_rule)
        print("正在应用规则...")
        operation_client = compute_v1.GlobalOperationsClient()
        operation_client.wait(project=project_id, operation=operation.name)
        print(f"【成功】已添加拒绝规则，共拦截 {len(ip_ranges)} 个 IP 段。")
    except Exception as e:
        if "already exists" in str(e):
            print(f"【跳过】规则 {rule_name} 已存在。")
        else:
            print(f"【失败】{e}")
            traceback.print_exc()

# --- 主逻辑 ---
if __name__ == "__main__":
    # 1. 选择项目
    project_id = select_gcp_project()
    
    # 2. 列出机器
    instances = list_all_instances(project_id)
    
    target_network = "global/networks/default"
    
    print("\n------------------------------------------------")
    print("防火墙规则管理菜单")
    print("------------------------------------------------")

    # 3. 询问是否添加入站规则
    choice_in = input("\n[1/2] 是否添加【允许所有入站连接 (0.0.0.0/0)】规则? (y/n): ").strip().lower()
    if choice_in == 'y':
        add_allow_all_ingress(project_id, target_network)
    else:
        print("已跳过入站规则配置。")

    # 4. 询问是否添加出站拒绝规则
    choice_out = input("\n[2/2] 是否添加【拒绝对 cdnip.txt 中 IP 的出站连接】规则? (y/n): ").strip().lower()
    if choice_out == 'y':
        ips = read_cdn_ips()
        if ips:
            if len(ips) > 256:
                print(f"【警告】IP 数量 ({len(ips)}) 超过 GCP 单条规则上限 (256)。")
                print("脚本将只取前 256 个 IP。")
                ips = ips[:256]
            
            add_deny_cdn_egress(project_id, ips, target_network)
    else:
        print("已跳过出站规则配置。")

    print("\n所有操作完成。")