import sys
import time
from google.cloud import compute_v1
from google.cloud import resourcemanager_v3

# --- 日志辅助 ---
def print_info(msg):
    print(f"[信息] {msg}")
    sys.stdout.flush()

def print_success(msg):
    print(f"\033[92m[成功] {msg}\033[0m") 
    sys.stdout.flush()

def print_warning(msg):
    print(f"\033[93m[警告] {msg}\033[0m") 
    sys.stdout.flush()

# --- 1. 选择项目 ---
def select_gcp_project():
    print_info("正在扫描您的项目列表...")
    try:
        client = resourcemanager_v3.ProjectsClient()
        request = resourcemanager_v3.SearchProjectsRequest(query="")
        page_result = client.search_projects(request=request)
        
        active_projects = []
        for project in page_result:
            if project.state == resourcemanager_v3.Project.State.ACTIVE:
                active_projects.append(project)

        if not active_projects:
            print_warning("未找到活跃的项目。将尝试使用默认环境项目。")
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
        print_warning(f"无法列出项目: {e}。使用默认设置。")
        return "pelagic-pod-432503-h1"

# --- 2. 选择实例 ---
def list_and_select_instance(project_id):
    instance_client = compute_v1.InstancesClient()
    request = compute_v1.AggregatedListInstancesRequest(project=project_id)
    
    print_info(f"正在扫描项目 {project_id} 中的实例...")
    
    available_instances = []
    
    for zone_path, response in instance_client.aggregated_list(request=request):
        if response.instances:
            for instance in response.instances:
                zone_short = zone_path.split('/')[-1]
                available_instances.append({
                    "name": instance.name,
                    "zone": zone_short,
                    "status": instance.status,
                    "cpu_platform": instance.cpu_platform
                })
    
    if not available_instances:
        print_warning("该项目中没有任何实例！")
        sys.exit(0)

    print("\n--- 请选择要刷 CPU 的虚拟机 ---")
    for i, inst in enumerate(available_instances):
        status_color = "\033[92m" if inst['status'] == "RUNNING" else "\033[91m"
        print(f"[{i+1}] {inst['name']:<20} | 区域: {inst['zone']:<15} | 状态: {status_color}{inst['status']}\033[0m | 当前CPU: {inst['cpu_platform']}")

    while True:
        choice = input(f"请输入数字选择 (1-{len(available_instances)}): ").strip()
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(available_instances):
                return available_instances[idx]
        print("输入无效，请重试。")

# --- 3. 等待操作 ---
def wait_for_operation(project_id, zone, operation_name):
    operation_client = compute_v1.ZoneOperationsClient()
    return operation_client.wait(project=project_id, zone=zone, operation=operation_name)

# --- 4. 核心逻辑 ---
def reroll_cpu_loop(project_id, instance_info):
    instance_name = instance_info['name']
    zone = instance_info['zone']
    
    instance_client = compute_v1.InstancesClient()
    attempt_counter = 1

    print_info(f"目标实例: {instance_name} ({zone})")
    print_info("目标: 只要 CPU 包含 'AMD' 即停止。")
    
    while True:
        print("\n" + "="*50)
        print_info(f"第 {attempt_counter} 次尝试...")

        # === 第一步：确保开机 ===
        current_inst = instance_client.get(project=project_id, zone=zone, instance=instance_name)
        if current_inst.status != "RUNNING":
            print_info(f"正在启动虚拟机 {instance_name}...")
            op = instance_client.start(project=project_id, zone=zone, instance=instance_name)
            wait_for_operation(project_id, zone, op.name)
            print_info("虚拟机已通电，正在等待系统初始化...")
        
        # === 第二步：耐心等待 CPU 信息同步 (最多 2 分钟) ===
        current_platform = "Unknown CPU Platform"
        # 增加到 60 次重试，每次 2 秒 = 120 秒等待时间
        max_retries = 60 
        
        for i in range(max_retries):
            # 获取最新状态
            current_inst = instance_client.get(project=project_id, zone=zone, instance=instance_name)
            
            # 1. 检查机器是否还活着
            if current_inst.status != "RUNNING":
                print_warning(f"检测到虚拟机状态异常变为: {current_inst.status}。跳过本次检测。")
                current_platform = "Instability Detected"
                break
            
            # 2. 检查 CPU 是否已识别
            current_platform = current_inst.cpu_platform
            if current_platform and current_platform != "Unknown CPU Platform":
                break # 获取成功，跳出等待
            
            # 3. 继续等待
            if (i+1) % 5 == 0: # 每5次打印一条日志，避免刷屏
                print_info(f"正在等待 CPU 元数据同步... ({i+1}/{max_retries}) - 机器正在启动中")
            time.sleep(2)
        
        # === 第三步：判断结果 ===
        if current_platform == "Unknown CPU Platform":
            print_warning("超时：等待 2 分钟后仍无法获取 CPU 信息。")
            # 即使超时，我们也得重试，因为未知的状态无法使用
        else:
            print_info(f"检测到 CPU: {current_platform}")

        # 只要包含 AMD (不区分大小写)
        if "AMD" in str(current_platform).upper(): 
            print_success(f"恭喜！已成功刷到目标 CPU: {current_platform}")
            print_info("脚本执行完毕。")
            break
        else:
            print_warning(f"结果不满意 ({current_platform})。准备重置...")
            
            print_info(f"正在关停虚拟机 {instance_name}...")
            op = instance_client.stop(project=project_id, zone=zone, instance=instance_name)
            wait_for_operation(project_id, zone, op.name)
            
            attempt_counter += 1
            time.sleep(2)

if __name__ == "__main__":
    try:
        selected_project = select_gcp_project()
        target_instance = list_and_select_instance(selected_project)
        reroll_cpu_loop(selected_project, target_instance)
    except KeyboardInterrupt:
        print("\n[用户终止] 脚本已停止。")
    except Exception as e:
        print(f"\n[错误] 发生异常: {e}")