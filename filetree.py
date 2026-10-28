import os
import json

# ------------------------------------------------------------------
# Folders to exclude by name (common cache, version control, etc.)
# ------------------------------------------------------------------
EXCLUDED_NAMES = {
    '__pycache__', '.git', '.idea', '.vscode', 'node_modules',
    'site-packages', 'dist-packages', 'Lib', 'lib', 'lib64',
    'venv', '.venv', 'env', '.ENV',
}

def is_virtual_env(folder_path):
    """Return True if the folder contains a pyvenv.cfg file."""
    return os.path.isfile(os.path.join(folder_path, 'pyvenv.cfg'))

def get_sorted_items(path):
    """Return sorted list of (item, is_dir) for items not excluded."""
    try:
        items = os.listdir(path)
    except PermissionError:
        return [], []

    dirs = []
    files = []
    for item in items:
        if item in EXCLUDED_NAMES:
            continue
        full = os.path.join(path, item)
        if os.path.isdir(full):
            dirs.append(item)
        else:
            files.append(item)

    dirs.sort()
    files.sort()
    return dirs, files

def build_nested_structure(current_path):
    """Return nested dict (folders first, then files, sorted). Skip venv & excluded."""
    structure = {}
    dirs, files = get_sorted_items(current_path)

    for d in dirs:
        sub_path = os.path.join(current_path, d)
        # Skip entire folder if it is a virtual environment (contains pyvenv.cfg)
        if is_virtual_env(sub_path):
            continue
        structure[d] = build_nested_structure(sub_path)

    for f in files:
        structure[f] = ""   # JSON requires a value

    return structure

def write_text_tree(structure, file, indent=0):
    """Write clean indented tree (no extra characters, folders first)."""
    # Separate folders and files
    folders = [k for k, v in structure.items() if isinstance(v, dict)]
    files = [k for k, v in structure.items() if isinstance(v, str)]

    folders.sort()
    files.sort()

    for fld in folders:
        file.write('\t' * indent + fld + '\n')
        write_text_tree(structure[fld], file, indent + 1)

    for fil in files:
        file.write('\t' * indent + fil + '\n')

if __name__ == '__main__':
    current = os.path.abspath('.')
    print(f"📂 Scanning: {current}")
    print("🚫 Excluding: __pycache__, .git, node_modules, site-packages, lib, etc.")
    print("🚫 Also excluding ANY folder that contains a pyvenv.cfg file (virtual env)\n")

    nested = build_nested_structure(current)

    folder_name = os.path.basename(current)

    # JSON output
    json_file = f"{folder_name}.json"
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump(nested, f, indent=2, ensure_ascii=False)
    print(f"✅ JSON saved to: {json_file}")

    # Text tree output
    text_file = f"{folder_name}.txt"
    with open(text_file, 'w', encoding='utf-8') as f:
        write_text_tree(nested, f)
    print(f"✅ Clean tree saved to: {text_file}")