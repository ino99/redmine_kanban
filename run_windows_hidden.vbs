Option Explicit

Dim shell
Dim fileSystem
Dim scriptDir
Dim command

Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")

scriptDir = fileSystem.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = scriptDir

command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " _
    & Chr(34) & scriptDir & "\run_windows.ps1" & Chr(34) & " -OpenBrowser"

shell.Run command, 0, False
