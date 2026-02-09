import meraki
import os
import pandas as pd
import sys
from datetime import datetime

# --- Configuration ---
INPUT_FILE = "validated_upload.csv"
LOG_FILE = "claim_device.log"
CLAIMED_DIR = "claimed_lists"
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

def write_log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{timestamp}] {message}\n")

def write_log_header():
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"--- NEW EXECUTION START: {timestamp} ---\n")
        f.write(f"{'='*60}\n")

def progress_bar(current, total, prefix='Progress'):
    percent = float(current) * 100 / total
    bar_length = 20
    filled_length = int(bar_length * current // total)
    bar = '=' * filled_length + '-' * (bar_length - filled_length)
    sys.stdout.write(f"\r{prefix}: |{bar}| {percent:.1f}% Complete")
    sys.stdout.flush()
    if current == total:
        sys.stdout.write('\n')

def main():
    os.makedirs(CLAIMED_DIR, exist_ok=True)
    write_log_header()
    
    if not os.path.exists(INPUT_FILE):
        error_msg = f"[ERROR] File not found: {INPUT_FILE}."
        print(error_msg)
        write_log(error_msg)
        return

    critical_error = False
    
    try:
        # 1. Load data
        df = pd.read_csv(INPUT_FILE, sep=';')
        df = df.apply(lambda x: x.str.strip() if x.dtype == "object" else x)
        df['line_number'] = df.index + 2

        # --- VALIDATION 1: Check for Duplicate Serials ---
        duplicate_mask = df.duplicated(subset=['Serial Number'], keep=False)
        if duplicate_mask.any():
            critical_error = True
            print("Error: Duplicate serial numbers found in file. Check log.")
            for _, row in df[duplicate_mask].iterrows():
                write_log(f"[ERROR] Line {row['line_number']}: Duplicate serial '{row['Serial Number']}' found.")

        # --- VALIDATION 2: Check for Invalid Status or Already Added ---
        invalid_mask = (df['Status'].astype(str).str.lower() != 'good') | \
                       (df['Already Added'].astype(str).str.lower() != 'false')
        
        if invalid_mask.any():
            critical_error = True
            print("Error: File contains invalid status or already added devices. Check log.")
            for _, row in df[invalid_mask].iterrows():
                write_log(f"[ERROR] Line {row['line_number']}: Serial '{row['Serial Number']}' has invalid status '{row['Status']}' or Already Added is '{row['Already Added']}'.")

        if critical_error:
            raise Exception("Input file failed pre-processing validation.")

        # --- START PROCESSING ---
        total_to_process = len(df)
        print(f"Found {total_to_process} devices to process.")

        grouped = df.groupby(['Network ID', 'Full Network Name', 'Org ID', 'Org Name'])

        total_claimed = 0
        total_updated = 0
        failed_serials = []

        for (net_id, net_name, org_id, org_name), group in grouped:
            serials_in_group = group['Serial Number'].astype(str).tolist()
            num_aps = len(serials_in_group)

            print(f"\nProcessing {num_aps} AP(s) for network: {net_name}")
            write_log(f"[INFO] Attempting claim in network {net_name} ({net_id})")

            # --- STEP A: BULK CLAIM ---
            claimed_successfully = []
            try:
                response = dashboard.networks.claimNetworkDevices(net_id, serials=serials_in_group, addAtomically=True)
                claimed_successfully = response.get('serials', [])
                
                if claimed_successfully:
                    write_log(f"[INFO] Claim successful for: {', '.join(claimed_successfully)}")
                    total_claimed += len(claimed_successfully)
                
                if response.get('errors'):
                    for err in response['errors']:
                        err_serial = err.get('serial', 'Unknown')
                        err_msg = ", ".join(err.get('errors', []))
                        ln = df[df['Serial Number'] == err_serial]['line_number'].values[0]
                        write_log(f"\t[WARN] Line {ln}:  Claim failed for {err_serial}: {err_msg}")
                        failed_serials.append(f"Line {ln}: {err_serial}")

            except meraki.APIError as e:
                write_log(f"[WARN] Batch claim failed for network {net_id}: {e.message}")
                for s in serials_in_group:
                    failed_serials.append(f"Line {df[df['Serial Number']==s]['line_number'].values[0]}: {s}")
                continue

            # --- STEP B: UPDATE NAME, TAGS & ADDRESS ---
            if claimed_successfully:
                print(f"Updating names, tags, and addresses...")
                for i, serial in enumerate(claimed_successfully):
                    row = group[group['Serial Number'] == serial].iloc[0]
                    
                    # 1. Prepare Tags
                    tags_to_apply = ["NEW-AP"] 
                    
                    diag_tag = row.get('Diagnostic Tag')
                    if pd.notna(diag_tag) and str(diag_tag).lower() != 'nan' and str(diag_tag).strip() != '':
                        tags_to_apply.append(str(diag_tag))

                    if str(row.get('Connectivity', '')).lower() == 'yes':
                        c_tag = row.get('Connectivity Tag')
                        if pd.notna(c_tag) and str(c_tag).lower() != 'nan' and str(c_tag).strip() != '':
                            tags_to_apply.append(str(c_tag))

                    # 2. Prepare Name
                    target_name = str(row.get('AP Name', ''))

                    # 3. Prepare Address (New Logic)
                    raw_addr = row.get('Address')
                    target_address = None
                    if pd.notna(raw_addr) and str(raw_addr).lower() != 'nan' and str(raw_addr).strip() != '':
                        target_address = str(raw_addr).strip()

                    try:
                        # Construct Update Parameters
                        update_params = {
                            'serial': serial,
                            'name': target_name,
                            'tags': tags_to_apply
                        }

                        # Add Address parameters only if address exists
                        if target_address:
                            update_params['address'] = target_address
                            update_params['moveMapMarker'] = True

                        # Perform Update
                        dashboard.devices.updateDevice(**update_params)
                        
                        log_msg = f"\t[INFO] Line {row['line_number']}: {serial} updated. Name: {target_name}, Tags: {tags_to_apply}"
                        if target_address:
                            log_msg += f", Address: {target_address}"
                        write_log(log_msg)
                        
                        total_updated += 1
                    except meraki.APIError as e:
                        write_log(f"\t[WARN] Line {row['line_number']}: {serial} update failed: {e.message}")
                        failed_serials.append(f"Line {row['line_number']}: {serial}")
                    
                    progress_bar(i + 1, len(claimed_successfully), prefix='Updating')

        print(f"\nDONE: {total_claimed} claimed, {total_updated} updated out of {total_to_process}.")

    except Exception as e:
        print(f"Process stopped: {e}")
        critical_error = True

    # 6. Rename and Move File
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    prefix = "error_claimed_" if critical_error else "claimed_"
    new_filename = f"{prefix}process_log_{timestamp}.csv"
    
    try:
        os.rename(INPUT_FILE, os.path.join(CLAIMED_DIR, new_filename))
        print(f"Input file moved to {CLAIMED_DIR}/{new_filename}")
    except Exception as e:
        print(f"Error moving file: {e}")

if __name__ == "__main__":
    main()