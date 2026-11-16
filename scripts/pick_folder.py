"""Native folder dialog (tkinter; Windows verified, macOS/Linux unverified) for the 8081 console. A browser never reveals a folder's absolute path, so the local
companion server runs this helper and reads the choice from stdout: one JSON line {"path": "..."} ("" = cancelled)."""
import json
import sys


def _powershell_pick_folder(initial: str = "") -> str:
    import os
    import subprocess
    if os.name != "nt":
        return ""
    ps_script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$f.Description = 'AI DEV DIRECTOR - 작업할 폴더 선택'; "
        "$f.ShowNewFolderButton = $true; "
        + (f"if (Test-Path -LiteralPath {json.dumps(initial)}) {{ $f.SelectedPath = {json.dumps(initial)} }}; " if initial else "")
        + "if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($f.SelectedPath) }"
    )
    kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    res = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-Command", ps_script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        **kwargs,
    )
    return (res.stdout or "").strip() if res.returncode == 0 else ""


def main() -> int:
    initial = sys.argv[1] if len(sys.argv) > 1 else ""
    chosen = ""
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)          # the dialog must not open behind the browser
        root.update()
        chosen = filedialog.askdirectory(parent=root, initialdir=initial or None, mustexist=True,
                                         title="AI DEV DIRECTOR - 작업할 폴더 선택") or ""
        root.destroy()
    except Exception as exc:
        if sys.platform == "win32":
            chosen = _powershell_pick_folder(initial)
        else:
            sys.stderr.write(f"tkinter unavailable: {exc}\n")
            return 1
    sys.stdout.buffer.write(json.dumps({"path": chosen or ""}).encode("ascii"))   # ensure_ascii: safe on any codepage
    return 0


if __name__ == "__main__":
    sys.exit(main())
