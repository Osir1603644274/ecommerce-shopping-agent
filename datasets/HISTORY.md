# 历史、外部基准和运行原件

这些材料不进入当前电商数据底座列表，但已有实验原件不能按“当前不用”直接销毁。

| 类别 | 位置 | 定位 |
| --- | --- | --- |
| KuaiSearch 旧 r1—r6、手机旧扩展、旧标注阶段等 | [本轮实体归档](../data/_archive/2026-09-08/README.md) | 历史证据，旧路径兼容；完整映射在迁移清单 |
| ESCI | [外部排序实验](../agent/evaluation/external_public_esci_task1_20260902_attempt001)；[后续确认材料](../agent/evaluation/external_public_esci_task1_confirmation_v2) | 外部独立基准，退出当前电商主线；不删除历史结果 |
| Yelp / 商家 RAG | [知识材料](../agent/knowledge_data)、历史说明（本地材料：`../docs/archive/merchant-rag`） | 暂退出当前电商主线；遗留模块可能仍有代码依赖，未迁移或删除 |
| 其他能力与失败尝试 | [Agent evaluation](../agent/evaluation)、evaluation（本地材料：`../evaluation`）、[权威索引](../docs/CURRENT_PROJECT_AUTHORITY.md) | 封存尝试、回归 fixture、恢复/交易测试不等于重复商品数据；保持原位 |
| 本机真人 Query 留存 | `.runtime/web-query-intake/queries.sqlite3`、`.runtime/used-phone-demo-439/web-query-intake.sqlite3` | 持续变化的留存数据库，混有自动化数据；不能整体当作真人 benchmark，未移动 |
| 原始下载 / 其他领域 | raw（本地材料：`../data/raw`）、processed（本地材料：`../data/processed`） | 来源及遗留领域材料；本轮未证明其多余，不按日期删除 |

本次清理是“当前实体集中＋历史实体归档＋取消多余日常入口”。永久删除数据文件 0；没有把字节相同但被不同冻结包依赖的文件随意去重，也没有声称释放磁盘容量。
