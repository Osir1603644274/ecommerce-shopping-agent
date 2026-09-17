# V10 最终证据与结论

结论：`CONTROL_HOLD / FORMAL_NOT_EXECUTED`。

V10 只把 nonce 全新的唯一候选 Skill 移到当前授权可视化工作区，并冻结两阶段合同。
唯一 loader control 中模型仍正确返回 `POOL_WORKFLOW`，但 Windows CLI 的
`--sandbox read-only` 策略同时拒绝绝对路径与相对路径的 `Get-Content`；因此正文读取事件为 0、
attestation 不匹配、存在禁用事件。控制门未过，48 次正式调用按预注册未执行。

V9/V10 共同证明：当前探针可以观察到隐式路由倾向，但无法在该 Windows 只读沙箱合同下证明
候选正文被读取。继续创建版本不会修复平台能力，故停止迭代。显式 Skill、本地 STDIO MCP、
联合字节一致性与真实 ES+BGE 核心继续沿用 V2/V4.1 的有界 ACCEPT；隐式默认触发继续 HOLD。

