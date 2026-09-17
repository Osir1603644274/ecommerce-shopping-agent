# Redis Cluster 有界实验最终证据与结论

## 结论

`HOLD_REDIS_CLUSTER`

不改变当前生产默认。该结论只覆盖同一台 Windows 主机上的 Redis 5.0.14.1：
独立单节点对比 3 master + 3 replica Cluster，不外推到多主机或生产容量。

## 原 attempt 保全

`attempt001` 在 CLI 报告 16384 槽已覆盖后过早发出首条 Cluster 命令，节点尚未全部
收敛并完成副本同步，因 `ClusterDownError` 终止。该 attempt 仅有 `started.json` 与
失败说明，未产出业务或性能结论，未覆盖、未重跑。

独立 `attempt001_remediation001` 只增加了集群稳定等待并使用新端口，其余预注册负载与门不变。

## remediation 结果

- 业务不变量：全部通过；每个活动库存最终为 0、成功集合为 100、成功返回为 100，超卖为 0。
- 单热点吞吐中位数：单节点 2863.69 ops/s，Cluster 2658.05 ops/s，变化 -7.18%。
- 48 Key 吞吐中位数：单节点 9281.06 ops/s，Cluster 7839.83 ops/s，变化 -15.53%。
- 直接访问错误节点真实观察到 `MOVED`；Cluster-aware 客户端完成透明路由。
- 停止 Key 所属 master 后，副本在 15 秒门内接管，故障前 marker 可读。
- 进程内存快照：负载前单节点约 49.7 MB；6 节点 Cluster 合计约 228.9 MB（不含单节点对照）。

## 裁决解释

Cluster 的正确性与故障转移门通过，但预注册要求多 Key 吞吐至少高于单节点 10%；实测为
-15.53%，因此不能接受当前单机 Cluster 方案。该结果符合 Cluster 的边界：单热点仍由单个
slot/master 串行承载；同机多节点还增加路由、连接、进程与复制开销。只有出现单节点容量、
内存或故障域的真实瓶颈，并在多主机环境重做容量与故障实验后，才应重新评估。

## 禁止外推

本实验不证明多主机吞吐、网络分区下可用性、异步复制零丢失、与 MySQL 的跨系统 exactly-once，
也不授权改变默认部署。

