---
name: suggest
description: 求助：给一条今天就能做的小步骤
modes: [chat, work]
when:
  intent: [seek_help]
  not_crisis: true
  base: 0.8
steps:
  - [small_step, {topic: $topic}]
---
只给**一条**最小的、今天就能做的步骤，不要列清单。
