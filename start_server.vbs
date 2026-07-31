' MediaSaver 静默启动脚本
' 双击运行，无控制台窗口
' 放到 shell:startup 可实现开机自启

Dim shell, fso
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' 切换到脚本所在目录
shell.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)

' 优先用打包版，其次用 Python
If fso.FileExists("dist\MediaSaver.exe") Then
    shell.Run "dist\MediaSaver.exe", 0, False
ElseIf fso.FileExists("MediaSaver.exe") Then
    shell.Run "MediaSaver.exe", 0, False
Else
    shell.Run "python app.py", 0, False
End If
