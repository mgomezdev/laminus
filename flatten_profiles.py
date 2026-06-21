# Utility script to recursively flatten OrcaSlicer system profiles to make them standalone user profiles
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

def flatten_profile(filepath):
    print(f"Loading {filepath}...")
    with open(filepath, 'r') as f:
        data = json.load(f)
        
    if "inherits" in data:
        parent_name = data["inherits"]
        print(f"  Inherits from: {parent_name}")
        parent_path = find_file_by_name(parent_name, PROFILES_BASE_DIR)
        
        if parent_path:
            # Recursively flatten parent
            parent_data = flatten_profile(parent_path)
            # Merge parent and child (child overrides parent)
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
        profile_data["compatible_printers"] = ["", "Creality Ender-3 0.4 nozzle"] # Workaround for CLI compatibility
        profile_data["compatible_printers_condition"] = ""
        
        # Specific overrides
        if profile_type == "machine":
            profile_data["layer_change_gcode"] = "G92 E0" # Reset extruder position at layer change to satisfy validator
            
        with open(tgt, 'w') as f:
            json.dump(profile_data, f, indent=4)
        print(f"Successfully saved flattened profile: {tgt}")
        return True
    except Exception as e:
        print(f"Failed to flatten profile: {e}")
        return False

if __name__ == "__main__":
    # Example usage inside the container:
    # docker exec orcaslicer-api python3 /workspace/flatten_profiles.py <src_path> <tgt_path> <type: machine|process|filament>
    if len(sys.argv) < 4:
        print("Usage: python3 flatten_profiles.py <src_path> <tgt_path> <machine|process|filament>")
        print("\nExample (run inside container):")
        print("  python3 flatten_profiles.py \\")
        print("    \"/opt/orcaslicer/resources/profiles/Creality/machine/Creality Ender-3 0.4 nozzle.json\" \\")
        print("    \"/config/user/default/machine/Creality Ender-3 0.4 nozzle.json\" \\")
        print("    \"machine\"")
        sys.exit(1)
        
    src_path = sys.argv[1]
    tgt_path = sys.argv[2]
    p_type = sys.argv[3]
    
    success = run_flatten(src_path, tgt_path, p_type)
    sys.exit(0 if success else 1)
