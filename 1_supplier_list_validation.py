import meraki
import pandas as pd
import os
import sys
import re
from datetime import datetime

# --- Configuration ---
INPUT_FILE = "VCC - AP Report.csv"
LOG_FILE = "list_validation.log"
OUTPUT_DIR = "validation_output"

API_KEY = os.getenv("MK_CSM_KEY")

if not API_KEY:
    print("Error: Meraki API key (MK_CSM_KEY) not found in environment variables.")
    sys.exit(1)

dashboard = meraki.DashboardAPI(
    api_key=API_KEY,
    suppress_logging=True,
    wait_on_rate_limit=True,      # Automatically sleep when a 429 occurs
    maximum_retries=5,            # Try up to 5 times before failing
    nginx_429_retry_wait_time=60, # Wait 60s if Nginx returns 429 without a timer
    retry_4xx_error=False,
    single_request_timeout=60
)

def write_log(summary_line, log_entries):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"--- Validation Run: {timestamp} ---\n")
        f.write(f"{summary_line}\n")
        for entry in log_entries:
            f.write(f"{entry}\n")
        f.write("-" * 50 + "\n\n")

def get_dashboard_data():
    inventory_map = {}
    network_list = []
    network_connectivity_map = {}
    network_diagnostic_map = {}
    
    try:
        print("Fetching Organizations and building tag maps...")
        organizations = dashboard.organizations.getOrganizations()
        
        for org in organizations:
            o_id = org['id']
            o_name = org['name']
            print(f"Processing Org: {o_name}...")
            
            inventory = dashboard.organizations.getOrganizationInventoryDevices(o_id, total_pages='all')
            for dev in inventory:
                serial = dev['serial']
                net_id = dev.get('networkId')
                tags = dev.get('tags', [])
                
                inventory_map[serial] = {
                    'orgId': o_id,
                    'orgName': o_name,
                    'networkId': net_id
                }
                
                if net_id:
                    for t in tags:
                        t_lower = t.lower()
                        if t_lower == "connectivity":
                            network_connectivity_map[net_id] = t
                        elif t_lower == "diagnostic":
                            network_diagnostic_map[net_id] = t
            
            networks = dashboard.organizations.getOrganizationNetworks(o_id, total_pages='all')
            for net in networks:
                network_list.append({
                    'orgId': o_id,
                    'orgName': o_name,
                    'netId': net['id'],
                    'netName': net['name']
                })
        
        return inventory_map, network_list, network_connectivity_map, network_diagnostic_map
    except meraki.APIError as e:
        print(f"Meraki API Error: {e}")
        sys.exit(1)

def validate_and_extract_prefix(full_net_name):
    parts = full_net_name.split('-')
    
    if len(parts) < 4:
        return None, "Format invalid. Expected: CC-REG-PID-Company Name"
    
    cc = parts[0]
    reg = parts[1]
    pid = parts[2]
    
    if not re.match(r"^[A-Za-z]{2}$", cc):
        return None, f"Invalid Country Code: {cc}"
    
    if not re.match(r"^[A-Za-z0-9]{3}$", reg):
        return None, f"Invalid Region Code: {reg}"
        
    if not re.match(r"^[A-Za-z0-9]+$", pid):
        return None, f"Invalid Partner ID: {pid}"

    return f"{cc}-{reg}-{pid}", None

def get_next_ap_number(network_id, naming_prefix, usage_cache, address_cache):
    """
    Returns: (next_number, address_found)
    """
    start_num = usage_cache.get(network_id, 0)
    # Try to get address from cache first
    found_address = address_cache.get(network_id, "")

    if start_num == 0:
        try:
            devices = dashboard.networks.getNetworkDevices(network_id)
            pattern = rf"^{re.escape(naming_prefix)}-AP(\d{{2}})-N$"
            existing_nums = []
            
            for d in devices:
                # 1. Check for progressive number
                name = d.get('name', '')
                match = re.match(pattern, name)
                if match:
                    existing_nums.append(int(match.group(1)))
                
                # 2. Check for Address (if we haven't found one yet)
                if not found_address:
                    addr = d.get('address', '').strip()
                    # Some devices might have 'null' or empty string
                    if addr and addr.lower() != 'none':
                        found_address = addr

            start_num = max(existing_nums) if existing_nums else 0
            
            # Update address cache with what we found (even if it's empty)
            address_cache[network_id] = found_address
            
        except:
            start_num = 0

    next_num = start_num + 1
    usage_cache[network_id] = next_num
    
    return next_num, found_address

def find_network_match(csv_name, network_list):
    csv_name_clean = csv_name.lower()
    
    exact_matches = [n for n in network_list if n['netName'].lower() == csv_name_clean]
    if len(exact_matches) == 1:
        return exact_matches
    
    partial_matches = [n for n in network_list if n['netName'].lower().startswith(csv_name_clean)]
    return partial_matches

def main():
    exec_timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    try:
        df_input = pd.read_csv(INPUT_FILE, sep=';')
        df_input.columns = [c.strip('"') for c in df_input.columns]
    except FileNotFoundError:
        print(f"Error: {INPUT_FILE} not found.")
        return

    inventory_map, network_list, connectivity_map, diagnostic_map = get_dashboard_data()
    
    # Caches
    network_usage_counter = {}
    network_address_cache = {} # New cache for addresses

    ignored_list, error_list, validated_list, log_entries = [], [], [], []
    already_added_count = 0
    
    print("Validating data with flexible network name matching, prefix enforcement, and address fetching...")
    
    for index, row in df_input.iterrows():
        line_num = index + 1
        csv_date = row.get('Shipment date', '')
        csv_net_raw = str(row.get('Network name', '')).strip('"')
        csv_serial = str(row.get('Serial number', '')).strip('"')

        res = {
            'Status': 'bad',
            'Already Added': 'false',
            'Shipment Date': csv_date,
            'Input Network Name': csv_net_raw,
            'Serial Number': csv_serial,
            'Org ID': '', 'Org Name': '', 'Network ID': '', 'Full Network Name': '',
            'Connectivity': 'no', 'Connectivity Tag': '', 
            'Diagnostic Tag': '', 
            'Address': '', # New Field
            'AP Name': '', 'Messages': ''
        }

        # Check Inventory First
        if csv_serial in inventory_map:
            inv_data = inventory_map[csv_serial]
            res['Already Added'] = 'true'
            already_added_count += 1
            res['Org ID'], res['Org Name'] = inv_data['orgId'], inv_data['orgName']
            
            if inv_data['networkId']:
                res['Status'] = 'good'
                res['Network ID'] = inv_data['networkId']
                net_info = next((n for n in network_list if n['netId'] == inv_data['networkId']), None)
                res['Full Network Name'] = net_info['netName'] if net_info else "Unknown"
                res['Messages'] = "device already successfully added"
                ignored_list.append(res)
            else:
                res['Messages'] = "serial in inventory but not assigned to a network."
                error_list.append(res)
                log_entries.append(f"[WARN][Line {line_num}] Serial: {csv_serial} - Not assigned.")
        
        else:
            matches = find_network_match(csv_net_raw, network_list)
            
            if len(matches) == 1:
                match = matches[0]
                net_id, net_name = match['netId'], match['netName']
                
                # 1. Validate Name Format
                ap_prefix, error_msg = validate_and_extract_prefix(net_name)
                
                if not ap_prefix:
                    res['Messages'] = error_msg
                    res['Full Network Name'] = net_name
                    error_list.append(res)
                    log_entries.append(f"[ERROR][Line {line_num}] Serial: {csv_serial}, Net: {net_name} -> {error_msg}")
                    continue 

                # 2. MANDATORY CHECK: Diagnostic Tag
                diag_tag = diagnostic_map.get(net_id)
                if not diag_tag:
                    res['Messages'] = "Network missing mandatory 'diagnostic' tag"
                    res['Full Network Name'] = net_name
                    error_list.append(res)
                    log_entries.append(f"[ERROR][Line {line_num}] Serial: {csv_serial}, Net: {net_name} -> Missing Diagnostic Tag")
                    continue

                # 3. Calculate AP Number AND Fetch Address
                prog_num, net_address = get_next_ap_number(net_id, ap_prefix, network_usage_counter, network_address_cache)
                
                # Check Address
                if not net_address:
                    log_entries.append(f"[WARN][Line {line_num}] Serial: {csv_serial}, Net: {net_name} -> No existing address found in network to copy.")
                
                ap_name = f"{ap_prefix}-AP{str(prog_num).zfill(2)}-N"
                
                if len(ap_name) >= 50:
                    res['Messages'] = f"AP Name too long ({len(ap_name)} chars)"
                    error_list.append(res)
                elif prog_num > 99:
                    res['Messages'] = "Progressive number exceeds 99"
                    error_list.append(res)
                else:
                    res.update({
                        'Status': 'good', 
                        'Org ID': match['orgId'], 
                        'Org Name': match['orgName'],
                        'Network ID': net_id, 
                        'Full Network Name': net_name, 
                        'AP Name': ap_name,
                        'Diagnostic Tag': diag_tag,
                        'Address': net_address # Populate Address
                    })
                    
                    conn_tag = connectivity_map.get(net_id)
                    if conn_tag:
                        res['Connectivity'], res['Connectivity Tag'] = 'yes', conn_tag
                    
                    validated_list.append(res)
                    log_entries.append(f"[INFO][Line {line_num}] Serial: {csv_serial} -> {ap_name}")
            else:
                res['Messages'] = "potential network name overlap" if len(matches) > 1 else "network not found"
                error_list.append(res)
                log_entries.append(f"[WARN][Line {line_num}] Serial: {csv_serial}, Input: {csv_net_raw}, Msg: {res['Messages']}")

    summary_line = f"Summary: {already_added_count} lines marked as already added."
    write_log(summary_line, log_entries)

    # Added 'Address' to columns
    cols = [
        'Status', 'Already Added', 'Shipment Date', 'Input Network Name', 'Serial Number', 
        'Org ID', 'Org Name', 'Network ID', 'Full Network Name', 
        'Connectivity', 'Connectivity Tag', 'Diagnostic Tag', 'Address', 'AP Name', 'Messages'
    ]

    file_configs = [
        (ignored_list, f"{exec_timestamp}_ignored_inventory.csv"),
        (error_list, f"{exec_timestamp}_error_validation.csv"),
        (validated_list, f"{exec_timestamp}_validated_upload.csv")
    ]

    for data, filename in file_configs:
        if data:
            pd.DataFrame(data)[cols].to_csv(os.path.join(OUTPUT_DIR, filename), index=False, sep=';')
            print(f"Generated: {filename}")

if __name__ == "__main__":
    main()