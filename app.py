import os
import time
import json
import re
import subprocess
from flask import Flask, jsonify, request, Response
from flask_cors import CORS
from functools import wraps
from multiprocessing import Process
# CRITICAL: Import datetime and timedelta for robust time handling
from datetime import datetime, timedelta

# --- Try to import psutil for real-time memory usage (as requested) ---
try:
    import psutil
    PSUTIL_AVAILABLE = True
    print("psutil is available. Will attempt to use real memory metrics.")
except ImportError:
    PSUTIL_AVAILABLE = False
    print("psutil not found. Falling back to memory simulation.")

# ----------------------------------------------------
# --- SHARED CONFIGURATION (ADJUST THESE VALUES) ---
# ----------------------------------------------------
TARGETS_CONFIG_PATH = '/docker-storage/smokeping/config/Targets' # Local path to the targets file volume
RRD_DATA_PATH = '/docker-storage/smokeping/data' # Local path to the RRD data volume
SMOKEPING_CONTAINER_NAME = 'smokeping' # The name of your running Smokeping container
LOG_FILE_PATH = 'logs/activity_log.json' # Path for server-side activity log
# Command to gracefully reload Smokeping *inside* the container
SMOKEPING_RELOAD_COMMAND = ['pkill', '-f', '-HUP', '/usr/bin/perl /usr/s?bin/smokeping(_cgi)?']

BEARER_TOKEN = "a-secret-smokeping-token-12345"
SERVICE_ACCESS_ENABLED = True # Must be True to allow service control (docker exec/stop/start)

# --- RESTORED PLACEHOLDERS ---
# Placeholders used in index.html for dynamic content injection
TOKEN_PLACEHOLDER = 'INJECTED_BEARER_TOKEN_HERE'
API_URL_PLACEHOLDER = 'INJECTED_API_BASE_URL_HERE'
# -----------------------------

UI_PORT = 5000
API_PORT = 5001
HOST_IP = '192.168.10.2'
API_BASE_URL = f"http://{HOST_IP}:{API_PORT}/api"

# --- Flask App Initialization ---
ui_app = Flask(__name__, static_folder='static', template_folder='templates')
api_app = Flask(__name__)
CORS(ui_app)
CORS(api_app)

# --- Authentication Decorator ---
def auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({"status": "error", "error": "Authorization header missing or malformed"}), 401

        token = auth_header.split(' ')[1]
        if token != BEARER_TOKEN:
            return jsonify({"status": "error", "error": "Invalid token"}), 401

        return f(*args, **kwargs)
    return decorated

# ----------------------------------------------------
# --- LOG MANAGEMENT UTILITIES ---
# ----------------------------------------------------

def _load_logs():
    """
    Loads all logs from the JSON file. Catches JSONDecodeError upon corruption.
    """
    if not os.path.exists(LOG_FILE_PATH):
        return []
    try:
        with open(LOG_FILE_PATH, 'r') as f:
            logs = json.load(f)
            return [log for log in logs if isinstance(log, dict)]
    except json.JSONDecodeError as e:
        print(f"ERROR: Log file corruption detected in {LOG_FILE_PATH}: {e}")
        return []
    except FileNotFoundError:
        return []

def _save_logs(logs):
    """
    Saves logs to the JSON file using 'w' (write mode) to safely overwrite.
    """
    try:
        os.makedirs(os.path.dirname(LOG_FILE_PATH), exist_ok=True)
        with open(LOG_FILE_PATH, 'w') as f:
            json.dump(logs, f, indent=4)
        return True
    except IOError as e:
        print(f"Error saving log file: {e}")
        return False

def log_activity(message):
    """Adds a new entry (with epoch timestamp) to the log and saves it."""
    timestamp_epoch = time.time()

    new_entry = {
        "timestamp": timestamp_epoch,
        "message": message
    }

    logs = _load_logs()
    logs.insert(0, new_entry)

    _save_logs(logs)

    human_readable_time = time.strftime("[%Y-%m-%d %H:%M:%S]", time.localtime(timestamp_epoch))
    return f"{human_readable_time} {message}"

# --- Configuration Parsing Logic (UNCHANGED) ---
def parse_smokeping_config(raw_config):
    """Parses the raw Smokeping Targets config file into a hierarchical JSON structure."""
    parsed_config = {"Targets": {"type": "group", "name": "Targets", "Children": []}}
    current_level = [parsed_config["Targets"]]
    last_node = parsed_config["Targets"]
    lines = raw_config.splitlines()
    for line in lines:
        line = line.strip()
        group_match = re.match(r'^(\++)\s*(\S+)\s*$', line)
        if group_match:
            level = len(group_match.group(1))
            name = group_match.group(2)
            while len(current_level) > level:
                current_level.pop()
            parent_node = current_level[-1]
            new_node = {"type": "group", "name": name, "Children": []}
            if 'Children' not in parent_node:
                parent_node['Children'] = []
            parent_node['Children'].append(new_node)
            current_level.append(new_node)
            last_node = new_node
            continue
        host_match = re.match(r'^\s*host\s*=\s*(\S+)$', line)
        if host_match:
            address = host_match.group(1)
            if last_node and last_node['name'] != 'Targets':
                last_node['type'] = 'host'
                last_node['host'] = address
                if 'Children' in last_node:
                    del last_node['Children']
            continue
        prop_match = re.match(r'^\s*(\w+)\s*=\s*(\S.+)$', line)
        if prop_match:
            key = prop_match.group(1).strip()
            value = prop_match.group(2).strip().strip('"')
            if last_node and last_node['name'] != 'Targets':
                last_node[key] = value
    return parsed_config

# ----------------------------------------------------
# --- API ROUTES (LOG DATE/TIME FIXED) ---
# ----------------------------------------------------

@api_app.route('/api/log', methods=['GET'])
@auth_required
def get_log():
    """
    Returns the log history, filtered to only show entries from the last 24 hours,
    with robust epoch-to-string conversion to handle future timestamps.
    """
    all_logs = _load_logs()

    cutoff_time = time.time() - (24 * 60 * 60)

    recent_logs = [
        log for log in all_logs
        if log.get("timestamp", 0) >= cutoff_time
    ]

    formatted_logs = []
    for log in recent_logs:
        log_timestamp_epoch = log.get("timestamp", 0)
        message = log.get('message', 'No Message')

        # Use current time as fallback if timestamp is missing
        if log_timestamp_epoch == 0:
            log_timestamp_epoch = time.time()

        try:
            # FIX: Use datetime.fromtimestamp() and catch specific time errors.
            # This handles future timestamps (like 1760982133) more reliably
            # and prevents silent failure caused by OverflowError.
            dt_object = datetime.fromtimestamp(log_timestamp_epoch)
            timestamp_str = dt_object.strftime("[%Y-%m-%d %H:%M:%S]")

        except (ValueError, OverflowError):
            # If conversion fails (usually due to a very large future epoch on 32-bit systems)
            # we format it using the date the log was written to the file (time.time()),
            # but mark it as an error to alert the user.
            print(f"WARNING: Log timestamp {log_timestamp_epoch} failed conversion (OverflowError).")
            # We must provide *some* time string for the front-end format
            timestamp_str = f"[INVALID TIME {time.strftime('%H:%M:%S')}]"

        formatted_logs.append(f"{timestamp_str} {message}")

    return jsonify({
        "status": "success",
        "log_history": formatted_logs
    })

@api_app.route('/api/log', methods=['POST'])
@auth_required
def post_log():
    """Adds a new log entry via API request from the frontend."""
    data = request.get_json()
    message = data.get('message')
    if not message:
        return jsonify({"status": "error", "error": "Missing 'message' in request body"}), 400

    logged_entry = log_activity(message)

    return jsonify({
        "status": "success",
        "message": "Log entry recorded.",
        "entry": logged_entry
    })

# --- Existing Endpoints (UNCHANGED) ---

@api_app.route('/api/config', methods=['GET'])
@auth_required
def get_config():
    try:
        with open(TARGETS_CONFIG_PATH, 'r') as f:
            raw_config = f.read()

        parsed_config = parse_smokeping_config(raw_config)

        return jsonify({
            "status": "success",
            "raw_config": raw_config,
            "parsed_config": parsed_config
        })

    except FileNotFoundError:
        error_msg = f"Configuration file not found at expected path: {TARGETS_CONFIG_PATH}"
        print(f"ERROR: {error_msg}")
        return jsonify({
            "status": "error",
            "error": error_msg
        }), 404
    except Exception as e:
        print(f"Exception during config load: {e}")
        return jsonify({
            "status": "error",
            "error": f"Error loading config: {e}"
        }), 500

@api_app.route('/api/config', methods=['POST'])
@auth_required
def save_config():
    data = request.get_json()
    new_raw_config = data.get('raw_config')

    if not new_raw_config:
        return jsonify({"status": "error", "error": "Missing raw_config in request body"}), 400

    try:
        with open(TARGETS_CONFIG_PATH, 'w') as f:
            f.write(new_raw_config)

        log_activity("Configuration saved successfully.")

        message = "Configuration saved successfully. Use /service/restart to apply changes."

        return jsonify({
            "status": "success",
            "message": message,
            "raw_config": new_raw_config
        })

    except IOError as e:
        log_activity(f"ERROR: Failed to write configuration file: {e}")
        return jsonify({"status": "error", "error": f"Failed to write configuration file: {e}"}), 500
    except Exception as e:
        log_activity(f"ERROR: An unexpected error occurred while saving config: {e}")
        return jsonify({"status": "error", "error": f"An unexpected error occurred: {e}"}), 500


@api_app.route('/api/service/restart', methods=['POST'])
@auth_required
def restart_smokeping():
    if not SERVICE_ACCESS_ENABLED:
        return jsonify({
            "status": "error",
            "error": "Service access is disabled in app.py configuration."
        }), 503

    try:
        command = ['docker', 'exec', SMOKEPING_CONTAINER_NAME] + SMOKEPING_RELOAD_COMMAND

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True
        )

        log_activity("Smokeping service reloaded successfully via HUP signal.")

        print(f"Smokeping HUP reload output: {result.stdout.strip()}")
        return jsonify({
            "status": "success",
            "message": "Smokeping service reloaded via HUP signal (docker exec).",
            "details": result.stdout.strip()
        })
    except subprocess.CalledProcessError as e:
        error_msg = f"Smokeping reload failed (docker exec error): {e.stderr.strip()}"
        log_activity(f"ERROR: {error_msg}")
        print(error_msg)
        return jsonify({
            "status": "error",
            "error": "Failed to execute docker exec reload command. Check container name and docker socket access.",
            "details": e.stderr.strip()
        }), 500
    except Exception as e:
        error_msg = f"An unexpected error occurred during service control: {e}"
        log_activity(f"ERROR: {error_msg}")
        return jsonify({
            "status": "error",
            "error": error_msg
        }), 500


@api_app.route('/api/rrd/delete', methods=['POST'])
@auth_required
def delete_rrd_data():
    if not SERVICE_ACCESS_ENABLED:
        return jsonify({
            "status": "error",
            "error": "Service access is disabled in app.py configuration."
        }), 503

    data = request.get_json()
    target_path = data.get('target_path')
    if not target_path or not target_path.startswith('Targets > '):
        return jsonify({"status": "error", "error": "Invalid target_path for RRD deletion."}), 400

    rrd_relative_path = target_path.replace('Targets > ', '').replace(' > ', '/')
    rrd_full_path = os.path.join(RRD_DATA_PATH, rrd_relative_path)

    # CRITICAL: Prevent path traversal
    if not os.path.abspath(rrd_full_path).startswith(os.path.abspath(RRD_DATA_PATH)):
        log_activity(f"SECURITY ALERT: Path traversal attempt detected for path: {rrd_full_path}")
        return jsonify({"status": "error", "error": "Path traversal attempt detected."}), 400

    deletion_message = ""

    try:
        # 1. Stop container
        print(f"Stopping container {SMOKEPING_CONTAINER_NAME}...")
        subprocess.run(['docker', 'stop', SMOKEPING_CONTAINER_NAME], check=True, capture_output=True, timeout=10)
        time.sleep(2)

        # 2. Delete RRD files
        if os.path.exists(rrd_full_path):
            subprocess.run(
                ['rm', '-rf', rrd_full_path],
                capture_output=True,
                text=True,
                check=True
            )
            deletion_message = f"RRD directory '{rrd_relative_path}' deleted from host volume."
        else:
            deletion_message = f"RRD directory '{rrd_relative_path}' not found, already clean."

        # 3. Start container
        print(f"Starting container {SMOKEPING_CONTAINER_NAME}...")
        subprocess.run(['docker', 'start', SMOKEPING_CONTAINER_NAME], check=True, capture_output=True, timeout=10)
        time.sleep(2)

        log_activity(f"RRD cleanup successful for '{target_path}'. Smokeping stopped and restarted by RRD delete routine.")


        return jsonify({
            "status": "success",
            "message": f"Container stopped, RRD cleaned up on volume, and container started. {deletion_message}",
            "path_checked": rrd_relative_path
        })

    except subprocess.CalledProcessError as e:
        # Attempt to restart the container on failure
        try:
            subprocess.run(['docker', 'start', SMOKEPING_CONTAINER_NAME], check=False)
        except Exception:
            pass

        error_msg = f"RRD deletion process failed (subprocess error): {e.stderr.strip()}"
        log_activity(f"ERROR: {error_msg}")
        print(error_msg)

        return jsonify({
            "status": "error",
            "error": 'RRD cleanup failed. Check docker permissions, container name, and host file permissions.',
            "details": e.stderr.strip()
        }), 500
    except Exception as e:
        error_msg = f"An unexpected error occurred during RRD delete: {e}"
        log_activity(f"ERROR: {error_msg}")
        return jsonify({
            "status": "error",
            "error": error_msg
        }), 500


@api_app.route('/api/metrics', methods=['GET'])
@auth_required
def get_metrics():
    # Load averages
    load_avg = os.getloadavg() if hasattr(os, 'getloadavg') else (0.1, 0.15, 0.2)
    memory_used_mb = 100.0

    # File sizes
    config_size = os.path.getsize(TARGETS_CONFIG_PATH) if os.path.exists(TARGETS_CONFIG_PATH) else 0
    log_size = os.path.getsize(LOG_FILE_PATH) if os.path.exists(LOG_FILE_PATH) else 0

    if PSUTIL_AVAILABLE:
        try:
            memory = psutil.virtual_memory()
            memory_used_mb = (memory.total - memory.available) / (1024 * 1024)
        except Exception:
            pass

    return jsonify({
        "status": "success",
        "metrics": {
            "load_avg": {
                "1m": load_avg[0],
                "5m": load_avg[1],
                "15m": load_avg[2]
            },
            "memory_used_mb": memory_used_mb,
            "config_size": config_size,
            "log_size": log_size
        }
    })


# --- Server Start Functions (UNCHANGED) ---

def start_api_server():
    """Function to run the API server on its dedicated port."""
    print(f"Starting API Server on port {API_PORT}...")
    api_app.run(debug=False, host='192.168.10.2', port=API_PORT)


@ui_app.route('/')
def serve_ui():
    """Serves the index.html content with injected variables."""
    try:
        with open('index.html', 'r') as f:
            html_content = f.read()

        injected_html = html_content.replace(TOKEN_PLACEHOLDER, BEARER_TOKEN)
        injected_html = injected_html.replace(API_URL_PLACEHOLDER, API_BASE_URL)

        return Response(injected_html, mimetype='text/html')
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": f"Error serving index.html: {e}"
        }), 500


def start_ui_server():
    """Function to run the UI server on its dedicated port."""
    print(f"Starting UI Server on port {UI_PORT}...")
    ui_app.run(debug=False, host='192.168.10.2', port=UI_PORT)


# --- Main Manager ---

if __name__ == '__main__':
    # --- LOG FILE INITIALIZATION/CHECK ---
    current_epoch = time.time()

    initial_logs = [
        {
            "timestamp": current_epoch,
            "message": "Dashboard manager initialized and API started."
        },
        {
            "timestamp": current_epoch - 1.5,
            "message": "Log file successfully initialized."
        },
    ]

    if not os.path.exists(LOG_FILE_PATH) or os.stat(LOG_FILE_PATH).st_size == 0:
        print(f"Log file not found or is empty. Initializing at {LOG_FILE_PATH}.")

        os.makedirs(os.path.dirname(LOG_FILE_PATH), exist_ok=True)

        try:
            with open(LOG_FILE_PATH, 'w') as f:
                json.dump(initial_logs, f, indent=4)
        except Exception as e:
            print(f"Failed to initialize log file: {e}")

    # --- END LOG FILE INITIALIZATION ---

    api_process = Process(target=start_api_server)
    ui_process = Process(target=start_ui_server)

    api_process.start()
    ui_process.start()

    print("\n--- Dashboard Manager Initialized ---")
    print(f"Web UI URL: http://{HOST_IP}:{UI_PORT}")
    print(f"API Base URL: {API_BASE_URL}")
    print(f"Service Access Enabled: {SERVICE_ACCESS_ENABLED}")
    print(f"Targets Path: {TARGETS_CONFIG_PATH}")
    print(f"RRD Data Path: {RRD_DATA_PATH}")
    print(f"Log File Path: {LOG_FILE_PATH}")
    print("-------------------------------------\n")

    try:
        while True:
            if not api_process.is_alive() or not ui_process.is_alive():
                print("One or more server processes have terminated unexpectedly.")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nManager shut down by user.")
    finally:
        api_process.terminate()
        ui_process.terminate()
        api_process.join()
        ui_process.join()
        print("All processes terminated.")