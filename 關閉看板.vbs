Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "taskkill /F /IM python.exe", 0, True
MsgBox "看板已關閉", 64, "CPBL 看板"
