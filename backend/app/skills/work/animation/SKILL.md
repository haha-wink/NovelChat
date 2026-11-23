---
name: animation
description: 用户要一段动画/短片/动图：起一个后台渲染任务，先把进度告诉用户
modes: [work]
requires: [node, ffmpeg, chrome]
external: true
when:
  keywords: [动画, 短片, 视频, 动图, gif, GIF, MV, mv, 渲染, 歌词, 像素]
  action_words: [帮我, 给我, 做一个, 做一段, 做个, 做, 生成, 剪, 来一段, 出一段, 拍一段, 为这首歌]
  not_crisis: true
  base: 0.55
  bonus: 0.3
steps:
  - [animation_start, {prompt: $text, style: $style, lyrics_file: $lyrics_file, audio: $audio}]
---
长任务：起任务立刻返回，渲染在后台线程跑。

**不要**在回复里假装成片已经好了——说"正在做，好了发你"就够。
歌词/MV 类任务会停在"等确认分镜"，需要用户确认后才开始渲染。

风格：watercolor 水彩（默认）/ pixel 像素 / ink 水墨 / neon 赛博朋克 / film 胶片 /
noir 黑白 / pastel 小清新 / anime 动漫 / sunset 日落；用户说「水墨风 / 赛博朋克 / 胶片感」也能认。

只做一段：section=chorus（副歌、高潮、重复最多的那段）或 verse（主歌、第一段副歌之前），
不传就是整首。截取后歌词时间轴会平移到 0，配乐按起点自动裁，不要自己再剪一遍。

歌词来源：用户贴的歌词 / .lrc 优先；只给歌时自动读 ID3 USLT、MP4 ©lyr、FLAC 标签和同名 .lrc。
都没有就如实告诉用户「这首里没找到歌词，贴歌词或传 .lrc」，不要假装有。

依赖 node + ffmpeg + 无头 Chrome；缺任何一个时工具会如实返回缺什么。
