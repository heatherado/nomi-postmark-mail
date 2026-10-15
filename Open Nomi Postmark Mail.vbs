Option Explicit
Dim shell, folder, fso
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
shell.Run "pythonw " & Chr(34) & folder & "\app\Companion App.pyw" & Chr(34), 1, False
