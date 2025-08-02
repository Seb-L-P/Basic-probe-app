import os
import sqlite3
import yaml
import subprocess
import datetime
import threading
import time
import logging
from flask import Flask, jsonify, request, send_file, send_from_directory

# ---------- logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------- timezone-aware + sqlite adapters ----------
UTC = datetime.timezone.utc

def adapt_datetime(dt: datetime.datetime):
    return dt.isoformat()

def convert_datetime(s: bytes):
    return datetime.datetime.fromisoformat(s.decode())

sqlite3.register_adapter(datetime.datetime, adapt_datetime)
sqlite3.register_converter("timestamp", convert_datetime)
sqlite3.register_converter("DATETIME", convert_datetime)

# ---------- paths / config ----------
CONFIG_PATH = "config.yaml"
DB_PATH = "monitoring.db"

def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

config = load_config()
INTERVAL = config.get("interval_seconds", 10)
HOSTS = config.get("hosts", [])

# ---------- database helpers ----------
def get_conn():
    return sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS probes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        host_name TEXT,
        host_address TEXT,
        timestamp DATETIME,
        latency_ms REAL,
        success INTEGER
    )
    """)
    conn.commit()
    conn.close()

def insert_probe(host_name, host_address, latency, success):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO probes (host_name, host_address, timestamp, latency_ms, success)
    VALUES (?, ?, ?, ?, ?)
    """, (host_name, host_address, datetime.datetime.now(UTC), latency, success))
    conn.commit()
    conn.close()

# ---------- probing ----------
def probe_host(address):
    try:
        proc = subprocess.run(
            ["ping", "-c", "1", "-W", "1", address],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if proc.returncode == 0:
            for part in proc.stdout.split():
                if "time=" in part:
                    val = part.split("time=")[1]
                    if val.endswith("ms"):
                        val = val[:-2]
                    try:
                        return float(val)
                    except ValueError:
                        continue
        return None
    except Exception as e:
        logging.debug("Probe exception for %s: %s", address, e)
        return None

def probing_loop():
    global config, HOSTS, INTERVAL
    while True:
        try:
            new_cfg = load_config()
            if new_cfg != config:
                logging.info("Config change detected, reloading")
                config = new_cfg
                HOSTS = config.get("hosts", [])
                INTERVAL = config.get("interval_seconds", INTERVAL)
        except Exception as e:
            logging.warning("Failed to reload config: %s", e)

        for h in HOSTS:
            name = h.get("name")
            address = h.get("address")
            latency = probe_host(address)
            success = 1 if latency is not None else 0
            insert_probe(name, address, latency if success else None, success)
            logging.debug("Probed %s (%s): up=%s latency=%s", name, address, bool(success), latency)
        time.sleep(INTERVAL)

# ---------- Flask app ----------
app = Flask(__name__)

@app.route("/status")
def status():
    conn = get_conn()
    cur = conn.cursor()
    result = []
    for h in HOSTS:
        name = h.get("name")
        address = h.get("address")
        cur.execute("""
        SELECT timestamp, latency_ms, success FROM probes
        WHERE host_name=? ORDER BY timestamp DESC LIMIT 1
        """, (name,))
        row = cur.fetchone()
        uptime_60m = None
        since = datetime.datetime.now(UTC) - datetime.timedelta(minutes=60)
        cur.execute("""
            SELECT COUNT(*), SUM(success) FROM probes
            WHERE host_name=? AND timestamp >= ?
        """, (name, since))
        total, succ = cur.fetchone() or (0, 0)
        if total and total > 0:
            try:
                uptime_60m = 100.0 * (succ or 0) / total
            except Exception:
                uptime_60m = None

        if row:
            ts, latency, success = row
            result.append({
                "name": name,
                "address": address,
                "timestamp": ts.isoformat() + "Z" if isinstance(ts, datetime.datetime) else ts,
                "latency_ms": latency,
                "up": bool(success),
                "uptime_60m": uptime_60m,
            })
        else:
            result.append({
                "name": name,
                "address": address,
                "timestamp": None,
                "latency_ms": None,
                "up": False,
                "uptime_60m": uptime_60m,
            })
    conn.close()
    return jsonify(result)

@app.route("/history")
def history():
    host_name = request.args.get("name")
    minutes = int(request.args.get("minutes", "60"))
    if not host_name:
        return jsonify({"error": "missing name parameter"}), 400
    since = datetime.datetime.now(UTC) - datetime.timedelta(minutes=minutes)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT timestamp, latency_ms, success FROM probes
    WHERE host_name=? AND timestamp >= ?
    ORDER BY timestamp
    """, (host_name, since))
    data = []
    for row in cur.fetchall():
        ts, latency, success = row
        data.append({
            "timestamp": ts.isoformat() + "Z" if isinstance(ts, datetime.datetime) else ts,
            "latency_ms": latency,
            "up": bool(success)
        })
    conn.close()
    return jsonify({
        "name": host_name,
        "since": since.isoformat() + "Z",
        "data": data
    })

@app.route("/config")
def get_config():
    return send_file(CONFIG_PATH, mimetype="text/yaml")

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time_utc": datetime.datetime.now(UTC).isoformat() + "Z"})

@app.route("/")
def dashboard():
    frontend_path = os.path.join("web", "dashboard.html")
    if os.path.exists(frontend_path):
        return send_from_directory("web", "dashboard.html")
    return "<p>Dashboard not found. Create web/dashboard.html</p>", 404

# ---------- bootstrap ----------
if __name__ == "__main__":
    init_db()
    thread = threading.Thread(target=probing_loop, daemon=True)
    thread.start()
    port = int(os.environ.get("PORT", "5000"))
    logging.info("Starting app on port %s", port)
    app.run(host="0.0.0.0", port=port)
