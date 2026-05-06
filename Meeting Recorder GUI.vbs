Set shell = CreateObject("Shell.Application")
Set fso = CreateObject("Scripting.FileSystemObject")

appDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonExe = appDir & "\.venv\Scripts\pythonw.exe"
scriptPath = appDir & "\meeting_recorder_gui.py"

If Not fso.FileExists(pythonExe) Then
  MsgBox "Python virtual environment launcher not found:" & vbCrLf & pythonExe, vbCritical, "Meeting Recorder"
  WScript.Quit 1
End If

shell.ShellExecute pythonExe, """" & scriptPath & """", appDir, "runas", 1
