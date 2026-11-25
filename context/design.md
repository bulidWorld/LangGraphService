# 模型设计

## ACTIVITY 模型
 @models/activity.schema.yaml

## RUN FINISH Interupt 模型
 @models/approve.schema.yaml

## approve Decision 模型
 @models/approve-resume-schema.yaml

## mcp tool 模型
@models/mtap-tool.schema.yaml

# 模型之间的关系：

- mcp tool模型里定义了是否需要审批以及审批的元数据
- ACTIVITY中的approve 模型 以 mcp tool模型为基础设计，引用 mcp tool 模型的字段定义
- [approve Decision] 是客户端回复的payload，与 [ACTIVITY中的approve] 保持强一致，根据这个来设计

## 业务流
- 在业务流上，先从[mcp tool 模型]获取需要审批的工具
- graph执行过程中，如果执行到的工具，定义为需要审批，则进入中断
- 进入中断后返回中断数据模型[RUN FINISH Interupt 模型]
- 用户对审批进行操作，基于AG-UI 返回审批模型[approve Decision 模型]

#### 业务流 v2
- 根据用户意图分析问题
- 判断Pod是否故障
- 如果是Pod故障，查询Pod信息
- 根据Pod提供的信息，获取Pod的源码仓库
- 把源码仓库发送给 a2a agent， 调用A接口
- a2a agent 干活，分析问题
- a2a agent 给出答案
- 怎么保留 a2a agent的输出结果？作为tool message 吗？user message 吗？
- a2a的答案分几种：
  + 问题原因
  + 修改方案
- human in loop 同意a2a的方案，要求修改代码
- 调用 B接口进行修改

新增工具：查询pod的状态
