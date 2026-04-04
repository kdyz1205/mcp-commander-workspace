"""Real-time system vitals monitor — CPU & disk."""

import json
import os
import psutil

VITALS_PATH = os.path.join(os.path.dirname(__file__), "..", "..", ".auth", "vitals.json")


def collect_vitals() -> dict:
    cpu_percent = psutil.cpu_percent(interval=1)
    disk = psutil.disk_usage("/")
    disk_free_gb = round(disk.free / (1024 ** 3), 2)
    return {"cpu_percent": cpu_percent, "disk_free_gb": disk_free_gb}


def run():
    vitals_path = os.path.normpath(VITALS_PATH)
    os.makedirs(os.path.dirname(vitals_path), exist_ok=True)

    # Read existing vitals.json if present, merge new fields
    data = {}
    if os.path.exists(vitals_path):
        with open(vitals_path, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                data = {}

    data.update(collect_vitals())

    with open(vitals_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"[vitals] cpu={data['cpu_percent']}%  disk_free={data['disk_free_gb']}GB  -> {vitals_path}")
    return data


if __name__ == "__main__":
    run()
