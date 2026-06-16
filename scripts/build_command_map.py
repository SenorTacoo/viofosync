#!/usr/bin/env python3
"""Build ``viofosync_lib/data/command_map.json`` from the VIOFO app's
``device-cmd-manager.db``.

The Camera-control feature needs a semantic map for the Novatek netapp HTTP
protocol (command id -> key/options), which the bare protocol doesn't provide.
That mapping is derived from the official VIOFO Android app, which ships a
SQLite asset ``assets/device-cmd-manager.db``.

This script reformats only the *factual API data* (command ids, English keys,
descriptions and option enumerations) into a plain JSON file we can ship and
diff. It does not copy any app code or resources. We do not redistribute the
``.db`` itself.

To regenerate (e.g. for a newer app version, or to add a model):

    # obtain the app's base APK, then:
    unzip -o com.viofo.dashcam.apk 'assets/device-cmd-manager.db' -d /tmp/viofo
    python3 scripts/build_command_map.py \
        --db /tmp/viofo/assets/device-cmd-manager.db \
        --out viofosync_lib/data/command_map.json

Only the A329S has been validated against real hardware; other models are
reformatted as-is and untested.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3


def build(db_path: str) -> dict:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    models = [r[0] for r in db.execute(
        "SELECT DISTINCT DEVICE_MODEL FROM CMD_DEVICE_MANAGER ORDER BY DEVICE_MODEL")]
    out: dict = {
        "_provenance": (
            "Command ids, keys and option enumerations reformatted from the "
            "VIOFO dashcam Android app asset (device-cmd-manager.db). Factual "
            "API data only. A329S validated against hardware; other models "
            "reformatted as-is and untested. See scripts/build_command_map.py."
        ),
        "models": {},
    }
    for m in models:
        commands: dict = {}
        rows = db.execute(
            "SELECT m.CMD cmd, m.CMD_KEY key, m.DESCRIPTION descr "
            "FROM CMD_MANAGER m JOIN CMD_DEVICE_MANAGER d ON d.CMD_ID=m._ID "
            "WHERE d.DEVICE_MODEL=? GROUP BY m.CMD, m.CMD_KEY "
            "ORDER BY CAST(m.CMD AS INTEGER)", (m,))
        for r in rows:
            opts = [
                {"index": int(o["_INDEX"]), "value": o["_VALUE"],
                 "camera_tag": o["CAMERA_TAG"]}
                for o in db.execute(
                    "SELECT o._INDEX, o._VALUE, o.CAMERA_TAG "
                    "FROM DASHCAM_MENU_OPTION_INFO o "
                    "JOIN CMD_MANAGER m ON o.CMD_ID=m._ID "
                    "WHERE o.DEVICE_MODEL=? AND m.CMD=? "
                    "ORDER BY CAST(o._INDEX AS INTEGER)", (m, r["cmd"]))
                if str(o["_INDEX"]).lstrip("-").isdigit()]
            commands.setdefault(str(r["cmd"]), {
                "key": r["key"], "description": r["descr"], "options": opts})
        out["models"][m] = {"commands": commands}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="path to device-cmd-manager.db")
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(__file__), "..", "viofosync_lib", "data",
        "command_map.json"))
    args = ap.parse_args()
    data = build(args.db)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"wrote {args.out}: {len(data['models'])} models")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
