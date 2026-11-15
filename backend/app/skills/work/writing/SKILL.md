---
name: writing
description: 要写东西时给结构和语气建议（邮件 / 周报 / 报告 / 文案 / 通知）
modes: [work]
when:
  keywords: [写, 帮我写, 文案, 邮件, 周报, 报告, 通知, 措辞, 改一下]
  action_words: [帮我写, 写一份, 写个, 起草, 改一下, 润色, 措辞]
  not_crisis: true
  base: 0.6
  bonus: 0.25
steps:
  - [writing_outline, {text: $text}]
---
给**结构和要点**，不要一次性把整篇文章写出来——先对齐框架，再填内容。
