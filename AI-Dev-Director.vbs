' AI DEV DIRECTOR - Windowless Launcher (avoids SmartScreen .exe warning)
' Double-click this file (or create a Desktop shortcut to it) to launch AI DEV DIRECTOR.
Option Explicit

Dim fso, shell, rootDir, launcherPy, exePath, reqPath
Dim venvDir, venvPy, venvPyw, sysPy, pywExe, cmd, rc
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

' Always unblock AI-Dev-Director.exe first so SmartScreen / Explorer Zone.Identifier is cleared
If fso.FileExists(exePath) Then
    On Error Resume Next
    shell.Run "powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -Command ""Unblock-File -LiteralPath '" & exePath & "' -ErrorAction SilentlyContinue""", 0, True
    On Error GoTo 0
End If

Function SafeRun(cmdStr, winStyle, waitOnReturn)
    On Error Resume Next
    Err.Clear
    SafeRun = shell.Run(cmdStr, winStyle, waitOnReturn)
    If Err.Number <> 0 Then
        SafeRun = -1
        Err.Clear
    End If
    On Error GoTo 0
End Function

Function HasDeps(pyBin)
    Dim checkCmd
    If InStr(pyBin, " ") > 0 And Left(pyBin, 3) <> "py " Then
        checkCmd = "cmd.exe /c """"" & pyBin & """ -c ""import pydantic,yaml,psutil,click"" >nul 2>&1"""
    Else
        checkCmd = "cmd.exe /c """ & pyBin & " -c ""import pydantic,yaml,psutil,click"" >nul 2>&1"""
    End If
    HasDeps = (SafeRun(checkCmd, 0, True) = 0)
End Function

pywExe = ""
If fso.FileExists(venvPyw) And HasDeps(venvPy) Then
    pywExe = """" & venvPyw & """"
ElseIf fso.FileExists(fso.BuildPath(rootDir, "venv\Scripts\pythonw.exe")) And HasDeps(fso.BuildPath(rootDir, "venv\Scripts\python.exe")) Then
    pywExe = """" & fso.BuildPath(rootDir, "venv\Scripts\pythonw.exe") & """"
ElseIf SafeRun("cmd.exe /c where pythonw >nul 2>&1", 0, True) = 0 And HasDeps("python") Then
    pywExe = "pythonw"
ElseIf SafeRun("cmd.exe /c where pyw >nul 2>&1", 0, True) = 0 And HasDeps("py -3") Then
    pywExe = "pyw"
End If

' Auto-bootstrap .venv and install requirements.txt if Python is available but deps are missing
If pywExe = "" And fso.FileExists(reqPath) And fso.FileExists(launcherPy) Then
    sysPy = ""
    If SafeRun("cmd.exe /c where py >nul 2>&1", 0, True) = 0 Then
        sysPy = "py -3"
    ElseIf SafeRun("cmd.exe /c where python >nul 2>&1", 0, True) = 0 Then
        sysPy = "python"
    End If
    If sysPy <> "" Then
        If Not fso.FileExists(venvPy) Then
            rc = SafeRun("cmd.exe /c """ & sysPy & " -m venv """ & venvDir & """ >nul 2>&1""", 0, True)
        End If
        If fso.FileExists(venvPy) Then
            rc = SafeRun("cmd.exe /c """"" & venvPy & """ -m pip install --disable-pip-version-check -q -r """ & reqPath & """ >nul 2>&1""", 0, True)
            If rc = 0 And fso.FileExists(venvPyw) Then
                pywExe = """" & venvPyw & """"
            End If
        End If
    End If
End If

If pywExe <> "" And fso.FileExists(launcherPy) Then
    cmd = pywExe & " """ & launcherPy & """"
    If SafeRun(cmd, 0, False) = 0 Then
        WScript.Quit 0
    End If
End If

' Fallback to standalone AI-Dev-Director.exe (already unblocked above)
If fso.FileExists(exePath) Then
    SafeRun """" & exePath & """", 1, False
    WScript.Quit 0
End If

MsgBox "AI-Dev-Director.exe 또는 Python 환경을 찾을 수 없습니다.", 16, "AI DEV DIRECTOR"
WScript.Quit 1
