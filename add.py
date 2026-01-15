import sys
import traceback

# --- 尝试导入必要的库 ---
try:
    from google.cloud import compute_v1
    from google.cloud import resourcemanager_v3
except ImportError as e:
    print("【错误】缺少必要的 Python 库。")
    print("请先在终端运行以下命令安装：")
    print("pip install google-cloud-compute google-cloud-resource-manager")
    sys.exit(1)

# --- 配置常量 ---
REGIONS = {
    "1": {"zone": "us-west1-b", "name": "俄勒冈 (Oregon) [推荐]", "desc": "us-west1-b"},
    "2": {"zone": "us-central1-f", "name": "爱荷华 (Iowa)", "desc": "us-central1-f"},
    "3": {"zone": "us-east1-b", "name": "南卡罗来纳 (South Carolina)", "desc": "us-east1-b"}
}

OS_IMAGES = {
    "1": {"project": "debian-cloud", "family": "debian-12", "name": "Debian 12 (Bookworm)"},
    "2": {"project": "ubuntu-os-cloud", "family": "ubuntu-2204-lts", "name": "Ubuntu 22.04 LTS"}
}

def get_user_choice(options, prompt_text):
    print(f"\n--- {prompt_text} ---")
    for key, val in options.items():
        print(f"[{key}] {val['name']}")
    
    while True:
        choice = input("请输入数字选择: ").strip()
        if choice in options:
            return options[choice]
        print("输入无效，请输入列表中的数字。")

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
            print(f"[{i+1}] {p.project_id}  ({p.display_name})")

        while True:
            choice = input(f"请输入数字选择 (1-{len(active_projects)}): ").strip()
            if choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(active_projects):
                    selected = active_projects[idx]
                    print(f"已选择项目: {selected.project_id}")
                    return selected.project_id
            print("输入无效，请重试。")

    except Exception as e:
        print(f"【警告】获取项目列表遇到问题: {e}")
        print("将尝试直接使用您当前环境的项目 ID。")
        return "pelagic-pod-432503-h1" 

def create_instance(project_id, zone_config, os_config, instance_name="free-tier-vm"):
    zone = zone_config['zone']
    instance_client = compute_v1.InstancesClient()
    images_client = compute_v1.ImagesClient()

    print(f"\n[开始] 正在 {project_id} 项目中准备资源...")
    print(f"区域: {zone_config['name']}")
    print(f"系统: {os_config['name']}")

    try:
        # 1. 获取镜像
        image_response = images_client.get_from_family(
            project=os_config['project'], 
            family=os_config['family']
        )
        source_disk_image = image_response.self_link

        # 2. 配置磁盘
        disk = compute_v1.AttachedDisk()
        disk.boot = True
        disk.auto_delete = True
        initialize_params = compute_v1.AttachedDiskInitializeParams()
        initialize_params.source_image = source_disk_image
        initialize_params.disk_size_gb = 30
        initialize_params.disk_type = f"zones/{zone}/diskTypes/pd-standard"
        disk.initialize_params = initialize_params

        # 3. 配置网络
        network_interface = compute_v1.NetworkInterface()
        network_interface.name = "global/networks/default"
        
        access_config = compute_v1.AccessConfig()
        access_config.name = "External NAT"
        
        # === 关键修正点 ===
        # 必须使用 .name 获取字符串值，不能直接传对象
        access_config.type_ = compute_v1.AccessConfig.Type.ONE_TO_ONE_NAT.name 
        access_config.network_tier = compute_v1.AccessConfig.NetworkTier.STANDARD.name
        # =================
        
        network_interface.access_configs = [access_config]

        # 4. 组装实例
        instance = compute_v1.Instance()
        instance.name = instance_name
        instance.machine_type = f"zones/{zone}/machineTypes/e2-micro"
        instance.disks = [disk]
        instance.network_interfaces = [network_interface]

        tags = compute_v1.Tags()
        tags.items = ["http-server", "https-server"]
        instance.tags = tags

        # 5. 发送请求
        print(f"配置组装完成，正在向 Google Cloud 发送创建请求...")
        operation = instance_client.insert(
            project=project_id,
            zone=zone,
            instance_resource=instance
        )
        
        print("请求已发送，正在等待操作完成... (约 30-60 秒)")
        operation_client = compute_v1.ZoneOperationsClient()
        operation = operation_client.wait(
            project=project_id,
            zone=zone,
            operation=operation.name
        )

        if operation.error:
            print("创建失败:", operation.error)
        else:
            print(f"\n[成功] 实例 '{instance_name}' 已创建！")
            # 尝试打印IP
            try:
                inst_info = instance_client.get(project=project_id, zone=zone, instance=instance_name)
                ip = inst_info.network_interfaces[0].access_configs[0].nat_i_p
                print(f"外部 IP 地址: {ip}")
            except:
                pass
            print("请前往 GCP 控制台查看详情。")

    except Exception as e:
        print(f"\n[失败] 操作中止: {e}")
        # 打印详细错误栈，方便排查其他问题
        traceback.print_exc()

if __name__ == "__main__":
    # 1. 自动选择项目
    selected_project_id = select_gcp_project()
    
    # 2. 选择区域
    selected_region = get_user_choice(REGIONS, "请选择部署区域")
    
    # 3. 选择系统
    selected_os = get_user_choice(OS_IMAGES, "请选择操作系统")

    # 4. 执行
    create_instance(selected_project_id, selected_region, selected_os)