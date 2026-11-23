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
  - [animation_start, {prompt: $text, style: $style, lyrics_file: $lyrics_file, audio: $audio, conversation_id: $conversation_id}]
---
长任务：起任务立刻返回，渲染在后台线程跑。

**不要**在回复里假装成片已经好了——说"正在做，好了发你"就够。
歌词/MV 类任务**不再等确认**：起了任务就直接渲染，进度看右侧任务栏；画质可随时换。
任务做不了 / 缺条件时，直说缺什么、为什么做不了，不要假装在做；
一次做不完的大任务拆成几个小任务分别起，做完一个再说下一个。
同一个需求已经起过 3 次、画面没变化时，先停下来说明卡住了，换风格 / 换镜头 / 拆段，
不要重复起同款；真要改已渲好的成片，在任务栏点 720p / 1080p 重渲，或说明这次改哪一处。

风格：watercolor 水彩（默认）/ pixel 像素 / ink 水墨 / neon 赛博朋克 / film 胶片 /
noir 黑白 / pastel 小清新 / anime 动漫 / sunset 日落；用户说「水墨风 / 赛博朋克 / 胶片感」也能认。

只做一段：section=chorus（副歌、高潮、重复最多的那段）或 verse（主歌、第一段副歌之前），
不传就是整首。截取后歌词时间轴会平移到 0，配乐按起点自动裁，不要自己再剪一遍。

歌词来源：用户贴的歌词 / .lrc 优先；只给歌时自动读 ID3 USLT、MP4 ©lyr、FLAC 标签和同名 .lrc。
都没有就如实告诉用户「这首里没找到歌词，贴歌词或传 .lrc」，不要假装有。

画面会按需求里的元素（雨 / 夜 / 车站 / 伞 / 山 / 海 / 城市…）和歌词句子的关键词决定构图、
天气与配色；非歌词任务会按标点拆成几个镜头。
具体到“人物长相 / 精确动作 / 指定文字排版”这类做不到的约束，直接告诉用户做不到或只能近似，
不要含糊成“已按你的要求做了”。

依赖 node + ffmpeg + 无头 Chrome；缺任何一个时工具会如实返回缺什么。
