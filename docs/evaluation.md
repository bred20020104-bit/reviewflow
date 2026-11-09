# 实验协议与评分

## 数据与划分

`benchmarks/corpus/` 包含30个自创小型 Python 项目（15类需求，每类缺陷/无缺陷各1）。前5类共10案例为 dev，后10类共20案例为 test；同源变体不能跨集合。

每个模型输入只限 `cases/<case-id>/app.py` 与该目录的 README 需求。**不要审查整个 benchmarks 目录**：`manifest.json`、`annotations.json`、生成器与参考补丁是评估资料，必须在模型可见范围外。案例 ID 后缀目前有规律，正式实验宜由实验协调者做盲化映射；不要将后缀规律告知被评估模型。`frozen-hashes.json` 固定文件字节；离线 harness 检查实际案例是否变更。

这些是人工构造的微型样例，不代表真实大型仓库分布，也不包含自动可执行的隐藏测试。真实实验前由另一位组员复核需求、标签和参考修复；保留复核记录。不要在宿主直接运行未知目标脚本。

## 对比组

- single：一个 general Reviewer，不独立复核。
- multi-no-verifier：Planner + correctness/security，不做 Verifier。
- multi：完整只读多角色流程。
- multi-no-retry：完整审查及修复，但 `run_repair(max_attempts=1)`，用于未来真实修复消融。

固定模型、版本、源码快照、输入权限、提示词版本和上下文规则。预算上限固定，同时报告实际消耗；不要把增加调用次数带来的改进直接归因于多 Agent 协作。先只用 dev 调整，冻结实现后再运行 test。至少保留一次失败/误报案例。

`benchmark-demo` 是离线实验流程彩排，使用 DemoBackend，不能发现真实缺陷。它输出 execution=simulated、快照、调用次数、状态、耗时和未覆盖路径；**不输出 precision/recall、token 或真实费用**，`evaluate` 拒绝这类数据。

## 人工根因匹配结果格式

真实运行后将候选与参考根因人工匹配；`truth_id=null` 表示误报。不要仅按行号相近自动匹配，也不要让模型自己的判定充当地面真值。单 Agent／无 Verifier 的 verdict 必须为 `unverified`；multi 为 accepted/rejected/unresolved。

```json
{
  "execution": "real",
  "mode": "multi",
  "split": "dev",
  "runs": [
    {
      "case_id": "case-01-a",
      "candidates": [
        {"finding_id": "correctness-1", "truth_id": "off-by-one", "verdict": "accepted"}
      ],
      "input_tokens": 1000,
      "output_tokens": 200,
      "cost_usd": "0.001",
      "seconds": 3.0,
      "repair_attempted": false,
      "repair_validated": false,
      "introduced_regression": false
    }
  ]
}
```

以上只是字段说明，数字不是实验结果，且完整文件必须包含所选 split 的每个案例；此单条示例会被拒绝为不完整。即使零候选也保留案例记录。读取范围、模型配置、原始报告与审阅者身份在脱敏实验记录中另存；评分器验证结构与一致性，但**无法证明手工输入的 execution=real 或费用是真的**。

## 指标定义

- TP：保留的候选首次正确匹配该案例一个参考根因。
- FP：保留的候选无根因匹配，或重复报告已匹配根因。重复单独计数，避免刷高 recall。
- FN：没有被保留候选命中的参考根因。
- precision = TP/(TP+FP)，没有预测时为0。
- recall = TP/参考根因数；没有参考缺陷时为 null。
- F1 = precision/recall 的调和均值；零分母为0。
- 无缺陷误报率：至少有一个保留误报的 clean 案例占全部 clean 案例比例；没有 clean 案例为 null。
- Verifier 正确驳回：rejected 且人工 truth_id=null；错误驳回：rejected 且匹配真实根因。
- unresolved 不算已确认问题，相关真实缺陷仍可能计 FN；必须同时展示 unresolved 数及原报告，不能隐藏。
- 修复成功率：实际尝试的案例中，独立验证成功且无新回归的比例；没有尝试为 null。
- Token、费用和时间求和；价格字段使用十进制字符串避免二进制浮点金额误差。

评分器拒绝重复案例/候选 ID、缺失案例、未知匹配、跨 split 同组、非有限时间、模拟结果和自相矛盾的修复成功声明。

## 记录表模板

| 组别 | 模型/配置摘要 | 样本数 | TP/FP/FN | P/R/F1 | clean误报率 | 正确/错误驳回 | 修复率 | token/费用/耗时 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| single | TODO | TODO | TODO | TODO | TODO | 不适用 | TODO | TODO |
| multi-no-verifier | TODO | TODO | TODO | TODO | TODO | 不适用 | TODO | TODO |
| multi | TODO | TODO | TODO | TODO | TODO | TODO | TODO | TODO |

在真实实验前全部保持 TODO，不把单元测试中用于验证计算公式的数字搬进报告。
