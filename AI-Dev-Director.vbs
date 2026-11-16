' AI DEV DIRECTOR - Windowless Launcher (avoids SmartScreen .exe warning)
' Double-click this file (or create a Desktop shortcut to it) to launch AI DEV DIRECTOR.
Option Explicit

Dim fso, shell, rootDir, launcherPy, exePath, pywExe, cmd, rc
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

rootDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = rootDir
launcherPy = fso.BuildPath(rootDir, "launcher.py")
exePath = fso.BuildPath(rootDir, "AI-Dev-Director.exe")

If Not fso.FileExists(launcherPy) Then
    If fso.FileExists(exePath) Then
        shell.Run """" & exePath & """", 1, False
        WScript.Quit 0
    End If
    MsgBox "launcher.py not found in: " & rootDir, 16, "AI DEV DIRECTOR"
    WScript.Quit 1
End If

pywExe = ""
If fso.FileExists(fso.BuildPath(rootDir, ".venv\Scripts\pythonw.exe")) Then
    pywExe = fso.BuildPath(rootDir, ".venv\Scripts\pythonw.exe")
ElseIf fso.FileExists(fso.BuildPath(rootDir, "venv\Scripts\pythonw.exe")) Then
    pywExe = fso.BuildPath(rootDir, "venv\Scripts\pythonw.exe")
ElseIf shell.Run("cmd /c where pythonw >nul 2>&1", 0, True) = 0 Then
    pywExe = "pythonw"
ElseIf shell.Run("cmd /c where pyw >nul 2>&1", 0, True) = 0 Then
    pywExe = "pyw"
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

MsgBox "Python (pythonw/pyw) was not found in PATH. Please install Python 3.11+ or run AI-Dev-Director.exe.", 16, "AI DEV DIRECTOR"
WScript.Quit 1
