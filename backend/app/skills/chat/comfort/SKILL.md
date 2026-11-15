---
name: comfort
description: 用户在倾诉：先接住情绪，需要时可以带一个呼吸引导
modes: [chat]
when:
  intent: [vent]
  not_crisis: true
  base: 0.9
steps:
  - [comfort_phrase, {emotion: $emotion}]
  - [breathing_guide, {seconds: 4, rounds: 3}]
---
用户正在倾诉，先承认他的感受，不要急着给方案。

素材里的安慰句和呼吸引导可以自然带出一两句，
不要整段照念，也不要一次给太多建议。
