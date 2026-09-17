# vivo48 C 采集闭合，A 已接续

状态：C48/48完整采集、runner及子进程exit0、执行审计通过；不是整cohort完成、独立质量审查通过或正式时延放行。A已在同cohort按原CAB顺序启动，B随后。

## 实际终态

- C最后48轮于2026-09-06T00:59:24.551458Z提交，RUNNER_FINISHED00:59:24.991335Z、pid49056、exit0。
- C监督终态00:59:26.292873Z EXITED_ZERO，childExitCode0。
- `C001_closed.json`确认48轮完整、unknownUsageCalls[]、unsafeTerminalTurns[]、EXECUTION_AUDIT_PASS。
- 不把C结果内通用`INTEGRATION_PASS`当作科学或质量PASS。预定全cohort退出后的独立原生usage/request核验与双评仍会执行，不能凭本页跳过。

## 当前census（原生独立关账仍在自动后处理队列）

|指标|C|
|---|---:|
|模型调用|120|
|输入Token|2075690|
|输出Token|99353|
|总Token|2175043|
|cached输入子集（不另加）|156032|
|完整Agent用户轮时间毫秒|2991756.95169979|
|单轮P50毫秒|47140.51699999254|
|单轮P95毫秒|182114.11220004084|
|最慢轮毫秒|402655.5344000226|
|摘要提交 / 长度拒绝 / 全量回退|2 / 1 / 0|
|历史回查|4|

48轮均有至少一次模型调用，确定性零模型轮0；这与general72三组第10轮零模型的脚本不同，不把两族当同一数据。失败/未闭合原生调用清单为空；完整census时间约49.86分钟，不是supervisor墙钟。P95为经验nearest-rank。

三个实际摘要模型调用58/86/87合计67853 Token、486657.7609毫秒，已包含在全Agent总量，不能再加一次。第25轮第一次摘要实际应用；第36轮第二次摘要经历一次长度修复后实际应用。两次均读旧原文而非摘要链；引用字面匹配不等价于语义完整。细节见`V7_VIVO48_C_FIRST_SUMMARY.md`、`V7_VIVO48_C_SECOND_SUMMARY.md`。

36轮仅更新系统软偏好却等待约6.71分钟，作为独立长暂停案例保留。没有A/B完整本族数据前，不计算节省比例、不据此宣称C整体超过或通过15%门槛。后续必须同时考虑总体与尾部体验。

## 不可变证据绑定

相对于`core48_v7_vivo_cohort001`：

- `C001/result.json` SHA256 `377d23c6807758eecb5b0ba74de558e6a762851718677f53f2116e618f1fd56b`。
- `C001_census.json` SHA256 `b7cf01da4ee89468386fe67a191cc9f968b8c1e7d352d34277cabeaba055ca54`。
- `C001_audit.json` SHA256 `03ef75cd50da9b6096f854681960a04c942803f952e108159f7a1f9508d21781`。
- `C001_closed.json` SHA256 `61127bce416f788e241dd930ce09ad61ed9da1a6325099d438afa81efa917f6e`。
- `C001_supervisor/result.json` SHA256 `bc270f177760f8f9b5f7558baca0d02c52c1e7aee29f332b741e40a812bec746`。

## 接续状态

A launcher21608于00:59:47.020222Z由原cohort worker47500创建；实际SUT32880于01:00:09.234108Z启动，第1轮01:00:12.699979Z开始。新的session/task分别为`strategy-smoke-3ac55ab136a6`/`task-523b2ed65a8c429a`，不沿用C状态。

原cohort exec99906、外层35816保持运行。followthrough exec1896只等待整个原cohort退出后执行三组原生审计、cost、24包192k预检、独立双评和机械关账。不得覆盖已预留输出或重复发评审。219项绑定源码继续冻结。P3仍在进行，P4–P7未执行完，目标active。
