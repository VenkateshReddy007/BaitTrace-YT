import os
import shutil
import re
from pathlib import Path

def main():
    root = Path(__file__).parent.resolve()
    
    # 1. Create directories
    src_dir = root / "src" / "baittrace"
    web_dir = root / "web"
    src_dir.mkdir(parents=True, exist_ok=True)
    web_dir.mkdir(parents=True, exist_ok=True)
    
    # 2. Move core files
    core_modules = [
        "main.py", "scheduler.py", "server.py", "discovery.py",
        "parser.py", "heuristics.py", "brain.py", "scoring.py",
        "pivot.py", "enricher.py", "extractor.py", "config.py"
    ]
    
    for mod in core_modules:
        src = root / mod
        if src.exists():
            shutil.move(str(src), str(src_dir / mod))
            print(f"Moved {mod} to src/baittrace/")
            
    # Create __init__.py
    (src_dir / "__init__.py").touch()
    
    # 3. Move dashboard
    dash = root / "dashboard.html"
    if dash.exists():
        shutil.move(str(dash), str(web_dir / "dashboard.html"))
        print("Moved dashboard.html to web/")
        
    # 4. Clean up stray files
    stray_test = root / "test_parser_evasion.py"
    if stray_test.exists():
        stray_test.unlink()
        print("Deleted stray test_parser_evasion.py at root")
        
    temp_bat = root / "run_tests_temp.bat"
    if temp_bat.exists():
        temp_bat.unlink()
        print("Deleted run_tests_temp.bat")
        
    # 5. Fix internal cross-module imports in src/baittrace/*.py
    import_pattern = re.compile(r'^(from|import)\s+(' + '|'.join([m[:-3] for m in core_modules]) + r')(\s+import|\s*$)', re.MULTILINE)
    
    for py_file in src_dir.glob("*.py"):
        if py_file.name == "__init__.py": continue
        content = py_file.read_text(encoding="utf-8")
        
        # Replace 'from module import X' with 'from .module import X'
        new_content = re.sub(
            r'^from (' + '|'.join([m[:-3] for m in core_modules]) + r') import ',
            r'from .\1 import ',
            content,
            flags=re.MULTILINE
        )
        
        # Replace 'import module' with 'from . import module'
        new_content = re.sub(
            r'^import (' + '|'.join([m[:-3] for m in core_modules]) + r')$',
            r'from . import \1',
            new_content,
            flags=re.MULTILINE
        )
        
        # Special case for server.py web path
        if py_file.name == "server.py":
            new_content = new_content.replace('open("dashboard.html"', 'open("web/dashboard.html"')
            new_content = new_content.replace('subprocess.Popen(["python", "main.py"', 'subprocess.Popen(["python", "-m", "baittrace.main"')
            new_content = new_content.replace('subprocess.Popen(["python", "scheduler.py"', 'subprocess.Popen(["python", "-m", "baittrace.scheduler"')
            
        if new_content != content:
            py_file.write_text(new_content, encoding="utf-8")
            print(f"Fixed imports in src/baittrace/{py_file.name}")
            
    # 6. Fix tests/*.py imports
    test_dir = root / "tests"
    if not (test_dir / "__init__.py").exists():
        (test_dir / "__init__.py").touch()
        
    for py_file in test_dir.glob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        # Replace 'from module import X' with 'from baittrace.module import X'
        new_content = re.sub(
            r'^from (' + '|'.join([m[:-3] for m in core_modules]) + r') import ',
            r'from baittrace.\1 import ',
            content,
            flags=re.MULTILINE
        )
        new_content = re.sub(
            r'^import (' + '|'.join([m[:-3] for m in core_modules]) + r')$',
            r'import baittrace.\1',
            new_content,
            flags=re.MULTILINE
        )
        if new_content != content:
            py_file.write_text(new_content, encoding="utf-8")
            print(f"Fixed imports in tests/{py_file.name}")
            
    # 7. Fix scripts/*.py imports
    scripts_dir = root / "scripts"
    for py_file in scripts_dir.glob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        new_content = re.sub(
            r'^from (' + '|'.join([m[:-3] for m in core_modules]) + r') import ',
            r'from baittrace.\1 import ',
            content,
            flags=re.MULTILINE
        )
        if new_content != content:
            py_file.write_text(new_content, encoding="utf-8")
            print(f"Fixed imports in scripts/{py_file.name}")
            
    # 8. Create pyproject.toml
    pyproject_toml = """[project]
name = "baittrace"
version = "2.0.0"
description = "Autonomous threat-intelligence sensor for YouTube comment-section scam detection"
requires-python = ">=3.10"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
"""
    (root / "pyproject.toml").write_text(pyproject_toml, encoding="utf-8")
    print("Created pyproject.toml")
    
    # 9. Update run_tests.bat
    run_tests_bat = root / "run_tests.bat"
    if run_tests_bat.exists():
        run_tests_bat.write_text("pip install -e .\npython -m pytest tests/ -v\n", encoding="utf-8")
        print("Updated run_tests.bat")
        
    print("All done! You can now run `pip install -e .` and `python -m pytest tests/ -v`.")

if __name__ == "__main__":
    main()
