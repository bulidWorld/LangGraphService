# MCP 注册工具规范
- 每个工具必须提供扩展元数据：
```
 "security": {
    "riskLevel": "high",
    "approvalMode": "conditional",
    "approvalRole": "ops_admin",
    "defaultAction": "require_approval"
  }

完整内容：
{
  "serverId": "kubernetes-mcp",
  "toolName": "restart_pod",
  "security": {
    "riskLevel": "high",
    "approvalMode": "conditional",
    "approvalRole": "ops_admin",
    "defaultAction": "require_approval"
  }
}
```