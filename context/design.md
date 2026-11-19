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
