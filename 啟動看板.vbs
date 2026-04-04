Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "python C:\Users\sospp\Desktop\claude\cpbl-dashboard\server.py", 0, False
WScript.Sleep 2000
WshShell.Run "http://127.0.0.1:8080", 1, False
