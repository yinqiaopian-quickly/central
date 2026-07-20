import shutil
import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
RELEASE_DIR = BASE_DIR / "release"
PY38 = BASE_DIR / ".tools" / "Python38" / "python.exe"


def ensure_legacy_python():
    if sys.version_info[:2] == (3, 8):
        return
    if PY38.exists():
        subprocess.run([str(PY38), str(Path(__file__).resolve())], cwd=BASE_DIR, check=True)
        raise SystemExit(0)
    raise SystemExit(
        "This project should be packaged with Python 3.8 for old Windows compatibility. "
        "Install Python 3.8 under .tools/Python38 or run this script with Python 3.8."
    )


def run_pyinstaller(name, entry):
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--windowed",
            "--name",
            name,
            entry,
        ],
        cwd=BASE_DIR,
        check=True,
    )


def copy_file(source, destination_dir):
    destination_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination_dir / source.name)


def copy_tree(source, destination):
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def main():
    ensure_legacy_python()
    run_pyinstaller("被控端Agent", "agent_app.py")
    run_pyinstaller("主控端", "controller_app.py")

    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    copy_file(BASE_DIR / "dist" / "被控端Agent.exe", RELEASE_DIR)
    copy_file(BASE_DIR / "dist" / "主控端.exe", RELEASE_DIR)
    copy_file(BASE_DIR / "agent_config.json", RELEASE_DIR)
    copy_file(BASE_DIR / "hosts.txt", RELEASE_DIR)
    copy_file(BASE_DIR / "README.md", RELEASE_DIR)
    copy_tree(BASE_DIR / "scripts", RELEASE_DIR / "scripts")

    print(f"Built release package: {RELEASE_DIR}")


if __name__ == "__main__":
    main()
