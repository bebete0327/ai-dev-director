' AI DEV DIRECTOR - Windowless Launcher (avoids SmartScreen .exe warning)
' Double-click this file (or create a Desktop shortcut to it) to launch AI DEV DIRECTOR.
Option Explicit

Dim fso, shell, rootDir, launcherPy, exePath, reqPath
Dim venvDir, venvPy, venvPyw, sysPy, sysPyw, pywExe, cmd, rc
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

rootDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = rootDir
launcherPy = fso.BuildPath(rootDir, "launcher.py")
exePath = fso.BuildPath(rootDir, "AI-Dev-Director.exe")
reqPath = fso.BuildPath(rootDir, "requirements.txt")
venvDir = fso.BuildPath(rootDir, ".venv")
venvPy = fso.BuildPath(venvDir, "Scripts\python.exe")
venvPyw = fso.BuildPath(venvDir, "Scripts\pythonw.exe")

If Not fso.FileExists(launcherPy) Then
    If fso.FileExists(exePath) Then
        shell.Run """" & exePath & """", 1, False
        WScript.Quit 0
    End If
    MsgBox "launcher.py not found in: " & rootDir, 16, "AI DEV DIRECTOR"
    WScript.Quit 1
End If

Function HasDeps(pyBin)
    Dim checkCmd
    If InStr(pyBin, " ") > 0 Then
        checkCmd = """" & pyBin & """ -c ""import pydantic,yaml,psutil,click"""
    Else
        checkCmd = pyBin & " -c ""import pydantic,yaml,psutil,click"""
    End If
    HasDeps = (shell.Run(checkCmd, 0, True) = 0)
End Function

pywExe = ""
If fso.FileExists(venvPyw) And HasDeps(venvPy) Then
    pywExe = venvPyw
ElseIf fso.FileExists(fso.BuildPath(rootDir, "venv\Scripts\pythonw.exe")) And HasDeps(fso.BuildPath(rootDir, "venv\Scripts\python.exe")) Then
    pywExe = fso.BuildPath(rootDir, "venv\Scripts\pythonw.exe")
ElseIf shell.Run("cmd /c where pythonw >nul 2>&1", 0, True) = 0 And HasDeps("python") Then
    pywExe = "pythonw"
ElseIf shell.Run("cmd /c where pyw >nul 2>&1", 0, True) = 0 And HasDeps("py -3") Then
    pywExe = "pyw"
End If

' Auto-bootstrap .venv and install requirements.txt on first run if deps are missing
If pywExe = "" And fso.FileExists(reqPath) Then
    sysPy = ""
    If shell.Run("cmd /c where python >nul 2>&1", 0, True) = 0 Then
        sysPy = "python"
    ElseIf shell.Run("cmd /c where py >nul 2>&1", 0, True) = 0 Then
        sysPy = "py -3"
    End If
    If sysPy <> "" Then
        If Not fso.FileExists(venvPy) Then
            rc = shell.Run(sysPy & " -m venv """ & venvDir & """", 0, True)
        End If
        If fso.FileExists(venvPy) Then
            rc = shell.Run("""" & venvPy & """ -m pip install --disable-pip-version-check -q -r """ & reqPath & """", 0, True)
            If rc = 0 And fso.FileExists(venvPyw) Then
                pywExe = venvPyw
            End If
        End If
    End If
End If

If pywExe <> "" Then
    If InStr(pywExe, " ") > 0 Then
        cmd = """" & pywExe & """ """ & launcherPy & """"
    Else
        cmd = pywExe & " """ & launcherPy & """"
    End If
    shell.Run cmd, 0, False
    WScript.Quit 0
End If

If fso.FileExists(exePath) Then
    shell.Run """" & exePath & """", 1, False
    WScript.Quit 0
End If

MsgBox "Failed to initialize Python dependencies. Please run: pip install -r requirements.txt", 16, "AI DEV DIRECTOR"
WScript.Quit 1
