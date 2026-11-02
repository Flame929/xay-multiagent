-- 「AI 团队看板」通知小应用
-- 有待发的通知（~/.xay/notify/*.msg）时：逐条弹出系统通知，并记住最后一条对应的看板地址。
-- 没有待发通知时（也就是用户点击了通知、系统唤起本应用）：确保看板服务在运行，然后在浏览器里打开那个地址。
-- .msg 文件共 4 行：标题、副标题、正文、看板地址。

on run
	set q to (POSIX path of (path to home folder)) & ".xay/notify"
	set sentAny to false
	repeat
		set names to paragraphs of (do shell script "mkdir -p " & quoted form of q & "; cd " & quoted form of q & " && ls -1 | grep '\\.msg$' || true")
		if names is {} or names = {""} then exit repeat
		repeat with n in names
			set p to q & "/" & n
			try
				set t to paragraphs of (read (POSIX file p) as «class utf8»)
				display notification (item 3 of t) with title (item 1 of t) subtitle (item 2 of t)
				do shell script "printf %s " & quoted form of (item 4 of t) & " > " & quoted form of (q & "/last_url")
			end try
			do shell script "rm -f " & quoted form of p
			set sentAny to true
		end repeat
		-- 刚处理完时可能又有新通知写进来，再看一轮
		delay 0.5
	end repeat
	if sentAny then return
	set u to do shell script "cat " & quoted form of (q & "/last_url") & " 2>/dev/null || echo http://127.0.0.1:7878/"
	do shell script "export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin; curl -s -m 1 -o /dev/null http://127.0.0.1:7878/ || { (nohup python3 \"$HOME/.agents/skills/xay-multiagent-workflow/scripts/xay_watch.py\" >/dev/null 2>&1 &); sleep 1.5; }"
	open location u
end run
