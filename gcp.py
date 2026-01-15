import getpass
import os
import shutil
import subprocess
import sys
import time
import traceback

try:
    from google.cloud import compute_v1
    from google.cloud import resourcemanager_v3
except ImportError:
    print("【错误】缺少必要的 Python 库。")
    print("请先在终端运行以下命令安装：")
    print("pip install google-cloud-compute google-cloud-resource-manager")
    sys.exit(1)

DEFAULT_PROJECT_ID = "pelagic-pod-432503-h1"
GITHUB_REPO = "fatekey/gcp_free"
GITHUB_BRANCH = "master"
GITHUB_RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}"
REMOTE_SCRIPT_URLS = {
    "apt": f"{GITHUB_RAW_BASE}/apt.sh",
    "dae": f"{GITHUB_RAW_BASE}/dae.sh",
}

REGION_OPTIONS = [
    {"name": "俄勒冈 (Oregon) [推荐]", "region": "us-west1", "default_zone": "us-west1-b"},
    {"name": "爱荷华 (Iowa)", "region": "us-central1", "default_zone": "us-central1-f"},
    {"name": "南卡罗来纳 (South Carolina)", "region": "us-east1", "default_zone": "us-east1-b"},
]

OS_IMAGE_OPTIONS = [
    {"name": "Debian 12 (Bookworm)", "project": "debian-cloud", "family": "debian-12"},
    {"name": "Ubuntu 22.04 LTS", "project": "ubuntu-os-cloud", "family": "ubuntu-2204-lts"},
]


def print_info(msg):
    print(f"[信息] {msg}")
    sys.stdout.flush()


def print_success(msg):
    print(f"\033[92m[成功] {msg}\033[0m")
    sys.stdout.flush()


def print_warning(msg):
    print(f"\033[93m[警告] {msg}\033[0m")
    sys.stdout.flush()


def select_from_list(items, prompt_text, label_fn):
    print(f"\n--- {prompt_text} ---")
    for i, item in enumerate(items):
        print(f"[{i+1}] {label_fn(item)}")
    while True:
        choice = input(f"请输入数字选择 (1-{len(items)}): ").strip()
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(items):
                return items[idx]
        print("输入无效，请重试。")


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
            return DEFAULT_PROJECT_ID

        print("\n--- 请选择目标项目 ---")
        for i, p in enumerate(active_projects):
            print(f"[{i+1}] {p.project_id} ({p.display_name})")

        while True:
            choice = input(f"请输入数字选择 (1-{len(active_projects)}): ").strip()
            if choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(active_projects):
                    selected = active_projects[idx].project_id
                    print_info(f"已选择项目: {selected}")
                    return selected
            print("输入无效，请重试。")
    except Exception as e:
        print_warning(f"无法列出项目: {e}。使用默认设置。")
        return DEFAULT_PROJECT_ID


def list_zones_for_region(project_id, region):
    zones_client = compute_v1.ZonesClient()
    zones = []
    for zone in zones_client.list(project=project_id):
        if zone.status != "UP":
            continue
        zone_region = zone.region.split("/")[-1] if zone.region else ""
        if zone_region == region:
            zones.append(zone.name)
    return sorted(zones)


def select_zone(project_id):
    region_config = select_from_list(REGION_OPTIONS, "请选择部署区域", lambda r: r["name"])
    region = region_config["region"]
    default_zone = region_config["default_zone"]

    print_info(f"正在获取 {region} 的可用区列表...")
    try:
        zones = list_zones_for_region(project_id, region)
    except Exception as e:
        print_warning(f"获取可用区失败: {e}。将使用默认可用区 {default_zone}。")
        return default_zone

    if not zones:
        print_warning(f"未获取到可用区列表，使用默认可用区 {default_zone}。")
        return default_zone

    return select_from_list(zones, f"请选择可用区 ({region})", lambda z: z)


def select_os_image():
    return select_from_list(OS_IMAGE_OPTIONS, "请选择操作系统", lambda o: o["name"])


def create_instance(project_id, zone, os_config, instance_name="free-tier-vm"):
    instance_client = compute_v1.InstancesClient()
    images_client = compute_v1.ImagesClient()

    print(f"\n[开始] 正在 {project_id} 项目中准备资源...")
    print(f"可用区: {zone}")
    print(f"系统: {os_config['name']}")

    try:
        image_response = images_client.get_from_family(
            project=os_config["project"],
            family=os_config["family"],
        )
        source_disk_image = image_response.self_link

        disk = compute_v1.AttachedDisk()
        disk.boot = True
        disk.auto_delete = True
        initialize_params = compute_v1.AttachedDiskInitializeParams()
        initialize_params.source_image = source_disk_image
        initialize_params.disk_size_gb = 30
        initialize_params.disk_type = f"zones/{zone}/diskTypes/pd-standard"
        disk.initialize_params = initialize_params

        network_interface = compute_v1.NetworkInterface()
        network_interface.name = "global/networks/default"

        access_config = compute_v1.AccessConfig()
        access_config.name = "External NAT"
        access_config.type_ = compute_v1.AccessConfig.Type.ONE_TO_ONE_NAT.name
        access_config.network_tier = compute_v1.AccessConfig.NetworkTier.STANDARD.name
        network_interface.access_configs = [access_config]

        instance = compute_v1.Instance()
        instance.name = instance_name
        instance.machine_type = f"zones/{zone}/machineTypes/e2-micro"
        instance.disks = [disk]
        instance.network_interfaces = [network_interface]

        tags = compute_v1.Tags()
        tags.items = ["http-server", "https-server"]
        instance.tags = tags

        print("配置组装完成，正在向 Google Cloud 发送创建请求...")
        operation = instance_client.insert(
            project=project_id,
            zone=zone,
            instance_resource=instance,
        )

        print("请求已发送，正在等待操作完成... (约 30-60 秒)")
        operation_client = compute_v1.ZoneOperationsClient()
        operation = operation_client.wait(
            project=project_id,
            zone=zone,
            operation=operation.name,
        )

        if operation.error:
            print("创建失败:", operation.error)
        else:
            print_success(f"实例 '{instance_name}' 已创建！")
            try:
                inst_info = instance_client.get(project=project_id, zone=zone, instance=instance_name)
                ip = inst_info.network_interfaces[0].access_configs[0].nat_i_p
                print(f"外部 IP 地址: {ip}")
            except Exception:
                pass
            print("请前往 GCP 控制台查看详情。")

    except Exception as e:
        print(f"\n[失败] 操作中止: {e}")
        traceback.print_exc()


def list_instances(project_id):
    instance_client = compute_v1.InstancesClient()
    request = compute_v1.AggregatedListInstancesRequest(project=project_id)

    print_info(f"正在扫描项目 {project_id} 中的实例...")

    instances = []
    for zone_path, response in instance_client.aggregated_list(request=request):
        if not response.instances:
            continue
        zone_short = zone_path.split("/")[-1]
        for instance in response.instances:
            network = None
            internal_ip = "-"
            external_ip = "-"
            if instance.network_interfaces:
                network = instance.network_interfaces[0].network
                internal_ip = instance.network_interfaces[0].network_i_p
                access_configs = instance.network_interfaces[0].access_configs
                if access_configs:
                    external_ip = access_configs[0].nat_i_p or "-"
            instances.append(
                {
                    "name": instance.name,
                    "zone": zone_short,
                    "status": instance.status,
                    "cpu_platform": instance.cpu_platform or "Unknown CPU Platform",
                    "network": network or "global/networks/default",
                    "internal_ip": internal_ip,
                    "external_ip": external_ip,
                }
            )
    return instances


def select_instance(project_id):
    instances = list_instances(project_id)
    if not instances:
        print_warning("该项目中没有任何实例！")
        return None

    print("\n--- 请选择目标服务器 ---")
    for i, inst in enumerate(instances):
        status_color = "\033[92m" if inst["status"] == "RUNNING" else "\033[91m"
        network_short = inst["network"].split("/")[-1] if inst["network"] else "-"
        print(
            f"[{i+1}] {inst['name']:<20} | 区域: {inst['zone']:<15} | 状态: "
            f"{status_color}{inst['status']}\033[0m | 网络: {network_short} | 内网IP: "
            f"{inst['internal_ip']} | 外网IP: {inst['external_ip']} | CPU: {inst['cpu_platform']}"
        )

    while True:
        choice = input(f"请输入数字选择 (1-{len(instances)}): ").strip()
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(instances):
                return instances[idx]
        print("输入无效，请重试。")


def wait_for_operation(project_id, zone, operation_name):
    operation_client = compute_v1.ZoneOperationsClient()
    return operation_client.wait(project=project_id, zone=zone, operation=operation_name)


def reroll_cpu_loop(project_id, instance_info):
    instance_name = instance_info["name"]
    zone = instance_info["zone"]

    instance_client = compute_v1.InstancesClient()
    attempt_counter = 1

    print_info(f"目标实例: {instance_name} ({zone})")
    print_info("目标: 只要 CPU 包含 'AMD' 即停止。")

    while True:
        print("\n" + "=" * 50)
        print_info(f"第 {attempt_counter} 次尝试...")

        current_inst = instance_client.get(project=project_id, zone=zone, instance=instance_name)
        if current_inst.status != "RUNNING":
            print_info(f"正在启动虚拟机 {instance_name}...")
            op = instance_client.start(project=project_id, zone=zone, instance=instance_name)
            wait_for_operation(project_id, zone, op.name)
            print_info("虚拟机已通电，正在等待系统初始化...")

        current_platform = "Unknown CPU Platform"
        max_retries = 60

        for i in range(max_retries):
            current_inst = instance_client.get(project=project_id, zone=zone, instance=instance_name)

            if current_inst.status != "RUNNING":
                print_warning(f"检测到虚拟机状态异常变为: {current_inst.status}。跳过本次检测。")
                current_platform = "Instability Detected"
                break

            current_platform = current_inst.cpu_platform
            if current_platform and current_platform != "Unknown CPU Platform":
                break

            if (i + 1) % 5 == 0:
                print_info(f"正在等待 CPU 元数据同步... ({i+1}/{max_retries}) - 机器正在启动中")
            time.sleep(2)

        if current_platform == "Unknown CPU Platform":
            print_warning("超时：等待 2 分钟后仍无法获取 CPU 信息。")
        else:
            print_info(f"检测到 CPU: {current_platform}")

        if "AMD" in str(current_platform).upper():
            print_success(f"恭喜！已成功刷到目标 CPU: {current_platform}")
            print_info("脚本执行完毕。")
            break

        print_warning(f"结果不满意 ({current_platform})。准备重置...")
        print_info(f"正在关停虚拟机 {instance_name}...")
        op = instance_client.stop(project=project_id, zone=zone, instance=instance_name)
        wait_for_operation(project_id, zone, op.name)
        attempt_counter += 1
        time.sleep(2)


def read_cdn_ips(filename="cdnip.txt"):
    if not os.path.exists(filename):
        print(f"【错误】找不到文件: {filename}")
        print("请在脚本同目录下创建该文件，并填入IP段。")
        return []

    ip_list = []
    with open(filename, "r", encoding="utf-8") as f:
        for line in f:
            clean_line = line.strip()
            if clean_line:
                ip = clean_line.split()[0]
                ip_list.append(ip)

    print(f"已从 {filename} 读取到 {len(ip_list)} 个 IP 段。")
    return ip_list


def set_protocol_field(config_object, value):
    try:
        config_object.ip_protocol = value
    except AttributeError:
        try:
            config_object.I_p_protocol = value
        except AttributeError:
            print(f"\n【调试信息】无法设置协议字段。对象 '{type(config_object).__name__}' 的有效属性如下:")
            print([d for d in dir(config_object) if not d.startswith("_")])
            raise


def add_allow_all_ingress(project_id, network):
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
    set_protocol_field(allow_config, "all")
    firewall_rule.allowed = [allow_config]

    try:
        operation = firewall_client.insert(project=project_id, firewall_resource=firewall_rule)
        print("正在应用规则...")
        operation_client = compute_v1.GlobalOperationsClient()
        operation_client.wait(project=project_id, operation=operation.name)
        print_success("已添加允许所有入站连接的规则。")
    except Exception as e:
        if "already exists" in str(e):
            print_warning(f"规则 {rule_name} 已存在。")
        else:
            print(f"【失败】{e}")
            traceback.print_exc()


def add_deny_cdn_egress(project_id, ip_ranges, network):
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
    set_protocol_field(deny_config, "all")
    firewall_rule.denied = [deny_config]

    try:
        operation = firewall_client.insert(project=project_id, firewall_resource=firewall_rule)
        print("正在应用规则...")
        operation_client = compute_v1.GlobalOperationsClient()
        operation_client.wait(project=project_id, operation=operation.name)
        print_success(f"已添加拒绝规则，共拦截 {len(ip_ranges)} 个 IP 段。")
    except Exception as e:
        if "already exists" in str(e):
            print_warning(f"规则 {rule_name} 已存在。")
        else:
            print(f"【失败】{e}")
            traceback.print_exc()


def configure_firewall(project_id, network):
    print("\n------------------------------------------------")
    print("防火墙规则管理菜单")
    print("------------------------------------------------")
    print(f"目标网络: {network}")

    choice_in = input("\n[1/2] 是否添加【允许所有入站连接 (0.0.0.0/0)】规则? (y/n): ").strip().lower()
    if choice_in == "y":
        add_allow_all_ingress(project_id, network)
    else:
        print("已跳过入站规则配置。")

    choice_out = input("\n[2/2] 是否添加【拒绝对 cdnip.txt 中 IP 的出站连接】规则? (y/n): ").strip().lower()
    if choice_out == "y":
        ips = read_cdn_ips()
        if ips:
            if len(ips) > 256:
                print(f"【警告】IP 数量 ({len(ips)}) 超过 GCP 单条规则上限 (256)。")
                print("脚本将只取前 256 个 IP。")
                ips = ips[:256]

            add_deny_cdn_egress(project_id, ips, network)
    else:
        print("已跳过出站规则配置。")

    print("\n所有操作完成。")


def pick_remote_method():
    has_gcloud = shutil.which("gcloud") is not None
    has_ssh = shutil.which("ssh") is not None

    if not has_gcloud and not has_ssh:
        print_warning("本机未发现 gcloud 或 ssh，无法执行远程脚本。")
        return None

    if has_gcloud:
        choice = input("是否使用 gcloud compute ssh 远程执行? (Y/n): ").strip().lower()
        if choice in ("", "y", "yes"):
            return {"method": "gcloud"}

    if not has_ssh:
        print_warning("未找到 ssh 命令，无法继续。")
        return None

    default_user = getpass.getuser()
    ssh_user = input(f"请输入 SSH 用户名 (默认 {default_user}): ").strip() or default_user
    ssh_port = input("请输入 SSH 端口 (默认 22): ").strip() or "22"
    ssh_key = input("请输入 SSH 私钥路径 (留空表示使用默认密钥): ").strip()
    return {"method": "ssh", "user": ssh_user, "port": ssh_port, "key": ssh_key}


def build_remote_download_command(script_url):
    return (
        "set -e;"
        "if command -v curl >/dev/null 2>&1; then DL=\"curl -fsSL\";"
        "elif command -v wget >/dev/null 2>&1; then DL=\"wget -qO-\";"
        "else echo \"error: curl or wget not found\"; exit 1; fi;"
        "tmp=$(mktemp /tmp/gcp_free.XXXXXX.sh);"
        f"$DL \"{script_url}\" > \"$tmp\";"
        "sudo bash \"$tmp\";"
        "rm -f \"$tmp\""
    )


def build_remote_exec_command(project_id, instance_info, remote_config, remote_command):
    instance_name = instance_info["name"]
    zone = instance_info["zone"]
    method = remote_config.get("method")

    if method == "gcloud":
        return [
            "gcloud",
            "compute",
            "ssh",
            instance_name,
            "--project",
            project_id,
            "--zone",
            zone,
            "--command",
            remote_command,
        ]
    if method == "ssh":
        host = instance_info.get("external_ip")
        if not host or host == "-":
            print_warning("该实例没有外网 IP，无法使用 SSH 直连。")
            return None
        cmd = ["ssh"]
        port = remote_config.get("port")
        if port:
            cmd += ["-p", str(port)]
        key_path = remote_config.get("key")
        if key_path:
            cmd += ["-i", key_path]
        cmd += [f"{remote_config.get('user')}@{host}", remote_command]
        return cmd

    print_warning("远程执行方式未设置。")
    return None


def build_remote_upload_command(project_id, instance_info, remote_config, local_path, remote_path):
    instance_name = instance_info["name"]
    zone = instance_info["zone"]
    method = remote_config.get("method")

    if method == "gcloud":
        return [
            "gcloud",
            "compute",
            "scp",
            local_path,
            f"{instance_name}:{remote_path}",
            "--project",
            project_id,
            "--zone",
            zone,
        ]
    if method == "ssh":
        if shutil.which("scp") is None:
            print_warning("未找到 scp 命令，无法上传文件。")
            return None
        host = instance_info.get("external_ip")
        if not host or host == "-":
            print_warning("该实例没有外网 IP，无法使用 SSH 直连。")
            return None
        cmd = ["scp"]
        port = remote_config.get("port")
        if port:
            cmd += ["-P", str(port)]
        key_path = remote_config.get("key")
        if key_path:
            cmd += ["-i", key_path]
        cmd += [local_path, f"{remote_config.get('user')}@{host}:{remote_path}"]
        return cmd

    print_warning("远程执行方式未设置。")
    return None


def run_remote_script(project_id, instance_info, script_key, remote_config):
    script_url = REMOTE_SCRIPT_URLS.get(script_key)
    if not script_url:
        print_warning("未知的脚本类型，无法执行。")
        return False
    remote_command = build_remote_download_command(script_url)
    cmd = build_remote_exec_command(project_id, instance_info, remote_config, remote_command)
    if not cmd:
        return False

    print_info(f"正在远程执行脚本: {script_url}")
    try:
        result = subprocess.run(cmd)
        if result.returncode == 0:
            print_success("远程脚本执行完成。")
            return True
        print_warning(f"远程脚本执行失败，退出码: {result.returncode}")
        return False
    except Exception as e:
        print_warning(f"远程执行失败: {e}")
        return False


def deploy_dae_config(project_id, instance_info, remote_config):
    local_config = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.dae")
    if not os.path.isfile(local_config):
        print_warning(f"找不到本地配置文件: {local_config}")
        return False

    remote_tmp = "/tmp/config.dae"
    upload_cmd = build_remote_upload_command(
        project_id,
        instance_info,
        remote_config,
        local_config,
        remote_tmp,
    )
    if not upload_cmd:
        return False

    print_info("正在上传 config.dae ...")
    try:
        result = subprocess.run(upload_cmd)
        if result.returncode != 0:
            print_warning(f"上传失败，退出码: {result.returncode}")
            return False
    except Exception as e:
        print_warning(f"上传失败: {e}")
        return False

    remote_command = (
        "set -e;"
        "sudo mkdir -p /usr/local/etc/dae;"
        "sudo cp /tmp/config.dae /usr/local/etc/dae/config.dae;"
        "sudo chmod 600 /usr/local/etc/dae/config.dae;"
        "sudo systemctl enable dae;"
        "sudo systemctl restart dae;"
        "rm -f /tmp/config.dae"
    )
    exec_cmd = build_remote_exec_command(project_id, instance_info, remote_config, remote_command)
    if not exec_cmd:
        return False

    print_info("正在应用配置并重启 dae ...")
    try:
        result = subprocess.run(exec_cmd)
        if result.returncode == 0:
            print_success("配置已更新并重启 dae。")
            return True
        print_warning(f"配置应用失败，退出码: {result.returncode}")
        return False
    except Exception as e:
        print_warning(f"配置应用失败: {e}")
        return False


def main():
    print("GCP 免费服务器多功能管理工具")
    project_id = select_gcp_project()
    current_instance = None
    remote_config = None

    while True:
        print("\n================================================")
        print(f"当前项目: {project_id}")
        if current_instance:
            print(f"当前服务器: {current_instance['name']} ({current_instance['zone']})")
        else:
            print("当前服务器: 未选择")
        print("------------------------------------------------")
        print("[1] 新建免费实例")
        print("[2] 选择服务器")
        print("[3] 刷 AMD CPU (当前服务器)")
        print("[4] 配置防火墙规则")
        print("[5] 换源")
        print("[6] 安装 dae")
        print("[7] 上传 config.dae 并启用 dae")
        print("[0] 退出")
        choice = input("请输入数字选择: ").strip()

        if choice == "1":
            zone = select_zone(project_id)
            os_config = select_os_image()
            create_instance(project_id, zone, os_config)
        elif choice == "2":
            current_instance = select_instance(project_id)
        elif choice == "3":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                reroll_cpu_loop(project_id, current_instance)
        elif choice == "4":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                network = current_instance.get("network") or "global/networks/default"
                configure_firewall(project_id, network)
        elif choice == "5":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                if not remote_config:
                    remote_config = pick_remote_method()
                if remote_config:
                    run_remote_script(project_id, current_instance, "apt", remote_config)
        elif choice == "6":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                if not remote_config:
                    remote_config = pick_remote_method()
                if remote_config:
                    run_remote_script(project_id, current_instance, "dae", remote_config)
        elif choice == "7":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                if not remote_config:
                    remote_config = pick_remote_method()
                if remote_config:
                    deploy_dae_config(project_id, current_instance, remote_config)
        elif choice == "0":
            print("已退出。")
            break
        else:
            print("输入无效，请重试。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[用户终止] 脚本已停止。")
    except Exception as e:
        print(f"\n[错误] 发生异常: {e}")
        traceback.print_exc()
