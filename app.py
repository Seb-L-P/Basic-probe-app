import os
import sqlite3
import yaml
import subprocess
import datetime
import threading
import time
from flask import Flask, jsonify, request, send_file

# Paths
CONFIG_PATH = "config.yaml"
DB_PATH = "monitoring.db"

# Load config
with open(CONFIG_PATH) as f:
    config = yaml.safe_load(f)

INTERVAL = config.get("interval_seconds", 10)
HOSTS = config.get("hosts", [])

# Initialize database
def init_db():
    conn = sqlite3.connect(DB_PATH)
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

def probe_host(address):
    try:
        proc = subprocess.run(["ping", "-c", "1", "-W", "1", address],
                              capture_output=True, text=True, timeout=3)
        output = proc.stdout
        if proc.returncode == 0:
            for part in output.split():
                if "time=" in part:
                    val = part.split("time=")[1]
                    if val.endswith("ms"):
                        val = val[:-2]
                    return float(val)
        return None
    except Exception:
        return None

def insert_probe(host_name, host_address, latency, success):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO probes (host_name, host_address, timestamp, latency_ms, success)
    VALUES (?, ?, ?, ?, ?)
    """, (host_name, host_address, datetime.datetime.utcnow(), latency, success))
    conn.commit()
    conn.close()

def probing_loop():
    while True:
        for h in HOSTS:
            name = h.get("name")
            address = h.get("address")
            latency = probe_host(address)
            success = 1 if latency is not None else 0
            insert_probe(name, address, latency if success else None, success)
        time.sleep(INTERVAL)

# Start probes
init_db()
thread = threading.Thread(target=probing_loop, daemon=True)
thread.start()

# Flask app
app = Flask(__name__)

@app.route("/status")
def status():
    conn = sqlite3.connect(DB_PATH)
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
        if row:
            ts, latency, success = row
            result.append({
                "name": name,
                "address": address,
                "timestamp": ts,
                "latency_ms": latency,
                "up": bool(success)
            })
        else:
            result.append({
                "name": name,
                "address": address,
                "timestamp": None,
                "latency_ms": None,
                "up": False
            })
    conn.close()
    return jsonify(result)

@app.route("/history")
def history():
    host_name = request.args.get("name")
    minutes = int(request.args.get("minutes", "60"))
    if not host_name:
        return jsonify({"error": "missing name parameter"}), 400
    since = datetime.datetime.utcnow() - datetime.timedelta(minutes=minutes)
    conn = sqlite3.connect(DB_PATH)
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
            "timestamp": ts,
            "latency_ms": latency,
            "up": bool(success)
        })
    conn.close()
    return jsonify({
        "name": host_name,
        "since": since.isoformat() + "Z",
        "data": data
    })

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time_utc": datetime.datetime.utcnow().isoformat() + "Z"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
