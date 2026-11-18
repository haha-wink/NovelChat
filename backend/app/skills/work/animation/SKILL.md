---
name: animation
description: 用户要一段动画/短片/动图：起一个后台渲染任务，先把进度告诉用户
modes: [work]
requires: [node, ffmpeg, chrome]
external: true
when:
  keywords: [动画, 短片, 视频, 动图, gif, GIF, MV, mv, 渲染, 歌词]
  action_words: [帮我做, 给我做, 做一个, 做一段, 生成, 剪一段, 做动画, 做成视频]
  not_crisis: true
  base: 0.55
  bonus: 0.3
steps:
  - [animation_start, {prompt: $text, style: $style, lyrics_file: $lyrics_file, audio: $audio}]
---
长任务：起任务立刻返回，渲染在后台线程跑。

**不要**在回复里假装成片已经好了——说"正在做，好了发你"就够。
歌词/MV 类任务会停在"等确认分镜"，需要用户确认后才开始渲染。

依赖 node + ffmpeg + 无头 Chrome；缺任何一个时工具会如实返回缺什么。
