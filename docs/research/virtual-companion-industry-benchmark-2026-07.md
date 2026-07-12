# 虚拟伴侣行业对标（2026-07）

## 结论

Girl-Agent 选择的是行业前沿方向。在公开可验证范围内，它在以下细分架构上可能已经超过多数普通角色聊天产品：事件溯源的显式情绪状态、负面情绪持续与衰减、用户/NPC/目标/世界共同影响情绪、关系损伤—承诺—机会—履约修复链，以及 prompt/校验/fallback 同源的表达计划。

但目前不能证明它的综合体验已经超过 Character.AI、Replika、Nomi、Kindroid 等头部产品。商业系统不公开内部实现；缺少公开资料不能反推对方没有类似能力。Girl-Agent 也尚缺跨产品盲测、数周真人纵向数据、大规模可靠性、多模态具身和用户留存证据。

## 对标判断

| 维度 | 当前判断 |
| --- | --- |
| 情绪架构可解释性、因果可回放 | 可能处于公开项目的前列 |
| 负面情绪、冒犯、边界与渐进修复 | 可能超过多数只做情绪标签或提示词调制的实现 |
| 虚拟世界/NPC 与情绪联动 | 有明显差异化，方向先进 |
| 长期记忆 | 架构严谨，但尚无公开基准证明优于头部产品 |
| 自然语言即时体验 | 尚无盲测，不能下领先结论 |
| 语音、视觉、Avatar、移动端完成度 | 落后于成熟商业产品 |
| 长期关系真实感 | 离线机制较强，真人证据不足 |
| 规模、留存、安全运营 | 尚未进入可比较阶段 |

## 一手资料依据

- 2025 年情感智能 agent 综述把多模态理解、认知评价、情绪映射、动态调制和表达列为完整情感智能的重要组成；Girl-Agent 的 appraisal—episode—process—expression 路径与该方向一致：[Intelligent Agents with Emotional Intelligence](https://arxiv.org/abs/2511.20657)。
- CoRE 工作指出，只做离散情绪识别不足，需要评价 LLM 是否依据 appraisal dimensions 形成连贯情绪推理；这支持本项目从关键词标签走向 agency、controllability、norm compatibility 等维度：[Do Machines Think Emotionally?](https://arxiv.org/abs/2508.05880)。
- Livia 公开方案结合模块化 agent、多模态情感识别、渐进记忆压缩和 AR 具身，说明前沿竞争不止文本情绪状态；Girl-Agent 在具身和真人实验方面仍有差距：[Livia](https://arxiv.org/abs/2509.05298)。
- AIVA 将多模态情绪感知、TTS 和动画 Avatar 结合，进一步说明语音/视觉表达属于综合体验的重要部分：[AIVA](https://arxiv.org/abs/2509.03212)。
- Replika 官方公开强调长期记忆、关系、情绪智能、语音/图像与数千万用户，但没有公开可复现的内部情绪动力学，因此只能比较功能和证据，不能比较未公开实现：[Replika 官网](https://replika.com/)、[Replika Ultra](https://help.replika.com/hc/en-us/articles/37292892831885-What-is-Replika-Ultra)。
- Character.AI 已公开 Story Memory、Facts、消息历史与 Memory Usage 等产品能力，证明头部产品的长期上下文体验仍在快速演进：[Smarter Memory for Smarter Chats](https://blog.character.ai/memory/)。

## 要证明“超过多数同行”还缺什么

1. 与至少四个头部陪伴产品做匿名、随机顺序的多轮盲测。
2. 覆盖冒犯、误会、修复、NPC 迁怒、长期压抑、热启动、事实回忆等固定剧本。
3. 报告人类感、情绪连续性、错误归因、模板察觉率、延迟、成本和偏好胜率及置信区间。
4. 跑公开长期记忆与社会对话基准，并与可复现 agent baseline 对测。
5. 至少进行数周真人纵向体验，观察过敏、恢复过快、机械重复和关系漂移。

在完成这些验证前，适合使用的表述是：**“在可回放情绪动力学、负面情绪和修复因果链这个细分方向上具有行业前沿特征。”** 不适合直接宣传“综合体验已经行业第一”。
