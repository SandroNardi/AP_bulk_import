## AP Claim Automation for Meraki

This repository contains two Python scripts that help validate a supplier-provided list of access points (APs) and then automatically claim and configure those APs in the Cisco Meraki Dashboard.

### Overview

- **`1_supplier_list_validation.py`**  
  Reads the supplier CSV report, cross-checks each AP serial against the Meraki Dashboard inventory, resolves network names, and prepares validated output files that are safe to claim.

- **`2_claim_devices.py`**  
  Takes the validated CSV produced in the previous step, bulk-claims devices into their target networks, and updates AP names, tags, and addresses in the Meraki Dashboard.

Both scripts use the Meraki Dashboard API and rely on an environment variable `MK_CSM_KEY` that must contain a valid API key with sufficient permissions. The API client is configured with rate-limit handling, automatic retries on 429 responses, and extended timeouts for large organizations.

### Prerequisites

- **Python** 3.8+ installed.
- **Dependencies** (install via `pip`):
  - `meraki`
  - `pandas`
- **Meraki Dashboard API key** stored in the environment variable:
  - `MK_CSM_KEY`

If `MK_CSM_KEY` is not set, both scripts will exit with an error.

### Input Data

The primary input file is:

- **`VCC - AP Report.csv`**  
  Expected columns (semicolon-separated, `;`):
  - `Shipment date`
  - `Network name`
  - `Serial number`

Column names are read from the header row (enclosed in quotes in the raw file, then stripped by the script).

### Script 1: Supplier List Validation (`1_supplier_list_validation.py`)

**Purpose:**  
Validate the supplier list against the Meraki inventory and networks, detect issues, and generate three categorized CSV outputs.

**High-level logic:**

1. **Configuration & setup**
   - Reads `VCC - AP Report.csv` using `pandas` with `;` as the separator.
   - Creates an output directory `validation_output` if it does not exist.
   - Instantiates a Meraki `DashboardAPI` client using `MK_CSM_KEY`.

2. **Dashboard discovery**
   - Fetches all organizations visible to the API key.
   - For each organization:
     - Retrieves all inventory devices and builds an `inventory_map` keyed by serial:
       - `orgId`, `orgName`, `networkId`.
       - Tracks networks whose devices carry a `connectivity` tag in `network_connectivity_map`.
       - Tracks networks whose devices carry a `diagnostic` tag in `network_diagnostic_map`.
     - Retrieves all networks and builds a `network_list` of `{orgId, orgName, netId, netName}`.

3. **Flexible network name matching & validation**
   - For each input row (shipment date, network name, serial number):
     - If the serial already exists in `inventory_map`:
       - Marks the row as **already added**.
       - If the device has a `networkId`, marks status as `good` and adds it to the **ignored** output (since it is already in a network).
       - If the device has no `networkId`, adds it to the **error** output with a message that it is not assigned to a network.
     - If the serial is **not** in inventory:
       - Attempts to find a matching network using `find_network_match`:
         - First tries a **case-insensitive exact match** on `netName`.
         - If none, tries a **prefix match** (`netName` starts with the CSV network name).
       - If exactly one match is found:
         - **Validates network name format** (`validate_and_extract_prefix`): expects `CC-REG-PID-Company Name` (e.g. 2-letter country code, 3-char region, partner ID). Invalid format → error output.
         - **Checks for mandatory `diagnostic` tag**: the network must have a device with the `diagnostic` tag. Missing → error output.
         - Uses `get_next_ap_number` to:
           - Inspect existing devices in that network.
           - Find the highest AP number in names like `CC-REG-PID-APXX-N`.
           - **Extract address** from any existing device in the network (cached per network) for later use.
           - Compute the next progressive AP number (`01`–`99`), cached per network.
         - Constructs the AP name: `CC-REG-PID-APXX-N` (using the validated prefix, not the full network name).
         - Validates:
           - AP name length must be `< 50` characters.
           - Progressive number must be `<= 99`.
         - If valid:
           - Marks status as `good`.
           - Adds organization, network, generated AP name, **Diagnostic Tag**, and **Address** to the **validated** output.
           - Marks `Connectivity` as `yes` and stores the connectivity tag if the network has a `connectivity` tag in `network_connectivity_map`.
       - If zero or multiple network matches are found:
         - Adds the row to the **error** output with an appropriate message:
           - `"network not found"` or `"potential network name overlap"`.

4. **Outputs & logging**
   - Writes a summary and per-row messages to `list_validation.log`.
   - Produces up to three CSV files in `validation_output` (semicolon-separated):
     - `*_ignored_inventory.csv` – serials already present and correctly assigned in Meraki.
     - `*_error_validation.csv` – rows with missing/ambiguous networks, inventory problems, or naming issues.
     - `*_validated_upload.csv` – rows that passed all checks and are ready to be claimed.
   - Each output uses a consistent set of columns, including:
     - `Status`, `Already Added`, `Shipment Date`, `Input Network Name`, `Serial Number`,
       `Org ID`, `Org Name`, `Network ID`, `Full Network Name`,
       `Connectivity`, `Connectivity Tag`, `Diagnostic Tag`, `Address`, `AP Name`, `Messages`.

**How to run:**

```bash
python 1_supplier_list_validation.py
```

After successful execution, use the generated `*_validated_upload.csv` as input (renamed or copied as needed) for the second script.

### Script 2: Claim Devices (`2_claim_devices.py`)

**Purpose:**  
Take a validated list of APs and automate bulk claiming, naming, tagging, and address assignment of devices in the Meraki Dashboard.

**Input:**

- **`validated_upload.csv`**  
  Semicolon-separated, with at least the following columns:
  - `Serial Number`
  - `Status`
  - `Already Added`
  - `Network ID`
  - `Full Network Name`
  - `Org ID`
  - `Org Name`
  - `AP Name`
  - `Connectivity`
  - `Connectivity Tag`
  - `Diagnostic Tag`
  - `Address`

Typically, this file is derived from `*_validated_upload.csv` produced by the first script.

**High-level logic:**

1. **Configuration & setup**
   - Ensures `claimed_lists` directory exists.
   - Logs a new execution header to `claim_device.log`.
   - Instantiates a Meraki `DashboardAPI` client using `MK_CSM_KEY`.

2. **Pre-processing validation**
   - Loads `validated_upload.csv` using `pandas` with `;` as the separator.
   - Strips whitespace from string columns and adds a `line_number` column (CSV line index).
   - **Validation 1 – duplicate serials:**
     - Detects duplicate `Serial Number` values.
     - Logs each duplicate as an `[ERROR]` with its line number.
   - **Validation 2 – status and already added:**
     - Requires `Status` to be `good` and `Already Added` to be `false` (case-insensitive).
     - Any row that fails this is logged as an `[ERROR]`.
   - If any critical validation errors exist, the script stops processing and treats the run as failed.

3. **Claiming & updating devices**
   - Groups rows by `Network ID`, `Full Network Name`, `Org ID`, `Org Name`.
   - For each group (i.e. for each target network):
     - Collects the list of serials to claim.
     - **Step A – bulk claim:**
       - Calls `dashboard.networks.claimNetworkDevices` with `addAtomically=True`.
       - On success, logs the claimed serials and increments totals.
       - If Meraki returns errors per serial, logs them and tracks failed serials.
       - If the batch claim fails entirely, logs the failure and marks all serials in the group as failed for this run.
     - **Step B – update names, tags, and addresses:**
       - For each successfully claimed serial:
         - Builds a tag list starting with `["NEW-AP"]`, then appends `Diagnostic Tag` from the CSV (if present).
         - If `Connectivity` is `yes` and `Connectivity Tag` is present, appends that tag as well.
         - Uses `AP Name` as the target device name.
         - Uses `Address` from the CSV when available; if present, also sets `moveMapMarker=True` so the device appears on the map.
         - Calls `dashboard.devices.updateDevice` to set `name`, `tags`, and optionally `address`.
         - Logs success or failure for each device.
       - Displays a simple console progress bar while updating devices in a group.

4. **Finalization & file rotation**
   - Prints a summary:
     - Total devices to process.
     - Number successfully claimed.
     - Number successfully updated.
   - Renames and moves the input file into `claimed_lists`:
     - If any critical error occurred during validation or processing:
       - Renamed as `error_claimed_process_log_<timestamp>.csv`.
     - Otherwise:
       - Renamed as `claimed_process_log_<timestamp>.csv`.

**How to run:**

```bash
python 2_claim_devices.py
```

Ensure that `validated_upload.csv` exists in the repository root and has been produced/verified by the first script.

### Logging

- **`list_validation.log`**  
  - Contains per-run sections for `1_supplier_list_validation.py`.
  - Summarizes how many lines were already added and logs warnings/errors for each problematic row.

- **`claim_device.log`**  
  - Contains timestamped messages for each execution of `2_claim_devices.py`.
  - Logs validation errors, claim attempts, and name/tag/address update results.

### Security Notes

- The Meraki API key is read from the `MK_CSM_KEY` environment variable and is **never** hard-coded in the scripts.
- Make sure you do **not** commit or share your API key or any files that may contain it.

### License

This project is licensed under the **MIT License**. See the `LICENSE` file for full details.

