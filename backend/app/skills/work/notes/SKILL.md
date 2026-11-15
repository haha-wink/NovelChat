---
name: notes
description: 把一段杂乱的话整理成纪要（结论 / 待办 / 问题）
modes: [work]
when:
  keywords: [整理, 纪要, 总结一下, 归纳, 记录, 复盘]
  not_crisis: true
  base: 0.65
steps:
  - [note_digest, {text: $text}]
---
输出三段：结论、待办、未决问题。没有的段落直接省略，不要硬凑。
