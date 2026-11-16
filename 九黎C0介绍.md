# 灵宠九黎 · C0

从 2026-11-16 正式运行的灵宠算法和风格库提取。C0 负责对话及意图判断，U0 在长会话中滚动摘要，C1 在涉及规划事实、偏好提案或紧急回复时复核候选。

| 目录 | 内容 |
| --- | --- |
| `cultivation/pet_chat/workflow.py` | 正式版本的对话、摘要、证据校验和编辑导航工作流 |
| `cultivation/phase4/` | 请求、候选、输出、读取许可及失效检查 |
| `cultivation/planning_v3/` | 模型调用、作业执行、恢复及系统技能 Markdown |
| `style/` | 主性格、表达偏好、场景卡及用户自定义风格的选择与组合 |

**线上配置差异。** C0 的人格技能指定“俏皮嘴硬”。产品后端的风格目录仍列出四种主性格，目录默认值为“温和细腻”。本仓库保留两侧源码；命令行示例显式选择“俏皮嘴硬”。

## 本地运行

使用 Python 3.12，在仓库根目录执行。模型地址为兼容 Chat Completions 的完整 HTTPS 请求地址。正式环境使用的模型名为 `deepseek-v4.1-flash`。将你自己的凭据通过环境变量 `PET_MODEL_API_KEY` 提供给进程。

~~~powershell
python -m pip install -r requirements.txt
$env:PET_MODEL_URL = 'https://你的模型服务域名/v1/chat/completions'
$env:PET_MODEL_NAME = 'deepseek-v4.1-flash'
python -m cultivation --text '练了一晚上，想歇会儿。'
~~~

也可以用 `python -m cultivation --request request.json` 读取符合 `PetInput` 的完整请求。调用方负责提供已授权的事实、对话历史和有效修订号。命令行示例每次用临时数据库运行一轮，结束后清理。首次使用 tiktoken 的 o200k_base 编码需要可用的词表缓存或联网下载。

## 规划导航

当用户要编辑一个已授权且能唯一定位的目标时，结果中的 `editGoalId` 标识目标。单项安排可返回 `editAction=editGoal`，调整整份计划可返回 `editAction=refinePlan`。前端应在当前对话轮次打开相应页面。产品后端在发布前仍需校验账号归属、许可修订和目标可编辑状态；实际修改由用户确认。无法确定目标时，C0 可先追问。

## 验证

`python -m unittest -v test_pet_agent` 使用受控模型输出验证 C0、C1、风格选择和导航回执。真实模型调用可用上面的本地命令验证。此仓库包含灵宠链路和独立运行入口，产品账号、前端和整站部署由主项目维护。
