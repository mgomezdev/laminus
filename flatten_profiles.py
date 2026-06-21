"""Recursively flatten OrcaSlicer system profiles into standalone user profiles.

OrcaSlicer's built-in profiles use an 'inherits' chain that the CLI cannot resolve
at runtime for user presets. This script walks the chain and writes a fully merged,
self-contained JSON ready to drop into /config/user/default/<type>/.
"""
import json
import os
import sys

PROFILES_BASE_DIR = "/opt/orcaslicer/resources/profiles"


def find_file_by_name(name, search_dir):
    filename = f"{name}.json"
    for root, dirs, files in os.walk(search_dir):
        if filename in files:
            return os.path.join(root, filename)
    return None


def flatten_profile(filepath, _visited=None):
    # Fix #7: track visited paths to detect circular inheritance and avoid RecursionError
    if _visited is None:
        _visited = set()
    real_path = os.path.realpath(filepath)
    if real_path in _visited:
        raise ValueError(f"Circular inheritance detected: '{filepath}' has already been visited.")
    _visited.add(real_path)

    print(f"Loading {filepath}...")
    with open(filepath, "r") as f:
        data = json.load(f)

    if "inherits" in data:
        parent_name = data["inherits"]
        print(f"  Inherits from: {parent_name}")
        parent_path = find_file_by_name(parent_name, PROFILES_BASE_DIR)

        if parent_path:
            parent_data = flatten_profile(parent_path, _visited)
            merged = parent_data.copy()
            merged.update(data)
            data = merged
            data.pop("inherits", None)
        else:
            print(f"  WARNING: Parent profile '{parent_name}' not found.")

    return data


def run_flatten(src, tgt, profile_type):
    if not os.path.exists(src):
        print(f"Error: Source profile '{src}' does not exist.")
        return False

    os.makedirs(os.path.dirname(tgt), exist_ok=True)

    try:
        profile_data = flatten_profile(src)
        profile_data["from"] = "user"
        profile_data["compatible_printers"] = ["", "Creality Ender-3 0.4 nozzle"]
        profile_data["compatible_printers_condition"] = ""

        if profile_type == "machine":
            profile_data["layer_change_gcode"] = "G92 E0"

        with open(tgt, "w") as f:
            json.dump(profile_data, f, indent=4)
        print(f"Successfully saved flattened profile: {tgt}")
        return True
    except ValueError as e:
        print(f"Profile error: {e}")
        return False
    except Exception as e:
        print(f"Failed to flatten profile: {e}")
        return False


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python3 flatten_profiles.py <src_path> <tgt_path> <machine|process|filament>")
        print("\nExample (run inside container):")
        print("  python3 flatten_profiles.py \\")
        print('    "/opt/orcaslicer/resources/profiles/Creality/machine/Creality Ender-3 0.4 nozzle.json" \\')
        print('    "/config/user/default/machine/Creality Ender-3 0.4 nozzle.json" \\')
        print('    "machine"')
        sys.exit(1)

    src_path = sys.argv[1]
    tgt_path = sys.argv[2]
    p_type = sys.argv[3]

    success = run_flatten(src_path, tgt_path, p_type)
    sys.exit(0 if success else 1)
