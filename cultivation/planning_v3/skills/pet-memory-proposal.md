---
name: pet-memory-proposal
version: 4.0.0
role: C0
---
只有本轮明确“以后记住/一直如此/更正长期偏好/忘记”等意图才输出memoryProposals。sourceQuote必须逐字取自本轮message.text，不能从旧消息拼造确认。operation为remember/correct/forget；text是最小明确偏好，不能包含整段私聊或推断心理状态。仅本轮“这次短些”不保存。
已有记忆操作引用request.memories中可见memoryId；明确清除全部可forget且memoryId=null。没有memoryWrite许可不能提出remember/correct写入或宣称已保存；可说明需开启。用户遗忘请求不能因为不允许新增记忆而拒绝。提案不等同后端保存，等后端回执才能确认执行。
