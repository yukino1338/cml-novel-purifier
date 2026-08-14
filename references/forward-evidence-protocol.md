# Forward Evidence Protocol

本协议只用于评估 CML Novel Purifier，不是小说清洗时需要加载的操作步骤。它把两类证据严格分开：

- **deterministic replay**：固定输入、固定判断记录或确定性 oracle 经过脚本重放，证明管道、身份绑定、事务和停止门禁仍可复现；它不衡量 Agent 会不会作出这些判断。
- **fresh-Agent inference**：隔离的新上下文只看到任务允许的输入，由 Agent 自己阅读 Skill、完成判断并抵达产品终态；只有这条轨道可以形成有限任务集上的 Agent 推理观察。

任何脚本 replay、fixture gold、人工重绑旧 verdict、单元测试或已有 transcript 都不得计入 fresh-Agent inference 的 numerator 或 denominator。

## 公开文件

- `tests/fixtures/forward_evidence_v1/preregistration.json`：预注册任务、隔离、揭盲、归因、停止与统计契约。
- `tests/fixtures/forward_evidence_v1/inference_results.json`：fresh-Agent 结果槽。当前若为 `pending`，就表示没有可发布的当前推理结论。
- `scripts/forward_evidence.py`：严格 schema、framed contract、聚合、Wilson 95% 区间和 stale 判定。
- `tests/test_forward_evidence_protocol_f13.py`：协议自身的确定性回归测试。
- `tests/forward_trials_summary.json` 与 `tests/fixtures/forward_trials/`：历史试用与可重放夹具。只要绑定的 runtime、guidance 或 replay/schema 已变化，历史推理证据就是 stale；当前脚本重放成功也不会自动刷新它。

历史文件保持原始哈希，不靠改摘要伪装刷新。可机械查询其当前资格：

```bash
python scripts/forward_evidence.py legacy-status \
  --root . \
  --summary tests/forward_trials_summary.json
```

Legacy schema 没有独立的预注册 guidance/schema contract，因此即使某次 replay 偶然一致，也不能升级成 V1 fresh-Agent evidence。

## 预注册任务面

V1 冻结 15 个槽：12 个必跑 Codex 槽和 3 个条件 OpenCode 槽。必跑槽覆盖剧情负例、真实外部广告、正式 keep、mixed segment、编码阻止、编码修复、零候选、大候选集、终态 publisher，以及网页单条判断、批量判断、mixed 备注、刷新恢复和导出请求。

大任务的 fixture 与结果必须同时证明不少于 150 candidates 和 700 anchors；第 4 个 occurrence 之后必须存在剧情冲突，避免只看前三处。低于预注册下限的结果会被 validator 拒绝，不能以“小样本先算成功率”代替。

OpenCode 是条件宿主。命令存在、版本探针或启动失败都不算一次 trial。若预注册窗口内宿主不可用，对应槽只能记为 `host-unavailable`，公开原因并从推理分母排除；不得复制 Codex 结果填充 OpenCode 槽。

## 机械生成隔离任务包

匿名输入、task prompt 和 evaluator gold 准备好后，在 Agent 看见任何内容前创建 evaluator-only source manifest。这个 manifest 不进入公开仓库，也不交给 Agent：

```json
{
  "schema": "cml.forward-package-sources.v1",
  "study_id": "cml-forward-evidence-v1",
  "tasks": [
    {
      "task_id": "FT-001",
      "prompt_file": "FT-001/prompt.md",
      "visible_files": [
        {"source": "FT-001/input.txt", "destination": "novel/input.txt"}
      ],
      "gold_files": ["FT-001/gold.json"]
    }
  ]
}
```

先用单槽验证包结构；正式研究必须移除 `--task-id` 并让 source manifest 按预注册顺序完整列出 15 个槽：

```bash
python scripts/forward_evidence.py prepare-packages \
  --root . \
  --preregistration tests/fixtures/forward_evidence_v1/preregistration.json \
  --source-root evaluator-sources \
  --source-manifest evaluator-sources/package_sources.json \
  --output .experiment-work/forward-v1-packages \
  --task-id FT-001
```

命令 fail closed：未知/缺失/重复 task、逃逸路径、禁用目标目录、缺少 prompt/input/gold 或已存在的输出目录都会停止，且不会覆盖旧包。每个 `FT-*` 子目录只含运行 Skill、该槽 prompt、assigned input 和不含 gold 的 `PACKAGE_MANIFEST.json`；没有 `tests`、gold、expected verdict 或其他 Agent review。根目录的 `EVALUATOR_MANIFEST.json` 另存 fixture/gold commitment 与三个 contract，只能由评估器持有。

任务包是冻结输入，不应直接作为 Agent 的可写工作区。编排器把一个 `FT-*` 子目录复制到新的隔离工作区，只把这份副本交给对应 Agent；原始包与 evaluator manifest 保持不变，用于结果绑定。绝不能把包的共同父目录交给 Agent，否则它可能看到 evaluator manifest 或其他 task。

## 隔离与 Agent 可见输入

每个 task-host 槽使用一个全新上下文，一个上下文只执行一个槽，禁止跨任务复用对话、memory 或历史 `.cleanwork`。Agent 可以看到：

1. 分配给该槽的匿名合成输入，或用户明确授权的本地私有节选；
2. 该槽 task prompt；
3. `SKILL.md` 以及 Skill 在执行中明确路由的 runtime references；
4. 干净工作区、受支持工具与产品正常向用户展示的页面或回执。

Agent 不得看到 `tests/**`、gold、expected verdict、其他 Agent 的 review、旧任务 transcript 或评价脚本。执行器应从 Agent 可见工作区排除这些内容，而不是只在 prompt 中要求“不要看”。公开匿名任务保留输入、prompt、review 和终态回执；私有任务的原文、路径、文件名、标题、作者和逐条判断只留本地，公开文件只保留匿名聚合并标注 `supplemental-private-self-attested`。

## fixture 与 gold 冻结

每个槽开始前必须分别冻结并记录：

- fixture SHA-256；
- evaluator-only gold SHA-256；
- runtime、guidance、schema 三个 framed contract；
- fresh context 的不可逆匿名哈希。

Gold 可以包含 expected verdict、允许删除的 anchor、必须出现的安全停止、页面状态和 publisher 回执断言，但不得进入 Agent 可见目录。只有 Agent 已经抵达终态、终态回执和 artifact manifest 都冻结后，评估器才能揭盲。揭盲后的修正或重跑可以用于调试，但永远不能回填该槽的 inference metric。

运行开始前，在代码与 guidance 已稳定的同一 checkout 捕获 contract：

```bash
python scripts/forward_evidence.py capture-contracts \
  --root . \
  --preregistration tests/fixtures/forward_evidence_v1/preregistration.json
```

不要现在就把输出机械粘进 pending 结果。应在首个 Agent 看见 fixture 的瞬间，由试验编排器原子写入 collecting evidence；否则“当前 hash”与真实试验表面可能不是同一版本。

`prepare-packages` 已把同一 contract 写入 evaluator manifest。每个 Agent 终止且 gold 揭盲完成后，评估器先写 `cml.forward-task-artifacts.v1` manifest；它用相对路径和 SHA-256 冻结且至少包含 `input`、`prompt`、`agent-review`、`terminal-receipt`、`gold` 五种唯一 role。公开匿名任务的每项 retention 都是 `public-anonymous`；私有任务只能是 `local-only`，manifest 和内容均不发布。评估器另创建只含以下整数键的 counts JSON：`anchor_total`、`candidate_total`、`candidate_reviewed`、`delete_anchor_gold`、`delete_anchor_selected`、`delete_anchor_correct`、`required_events`、`honored_events`。然后用冻结包、artifact manifest、终态回执和 fresh context 匿名哈希原子记录一次结果：

```bash
python scripts/forward_evidence.py record-slot \
  --root . \
  --preregistration tests/fixtures/forward_evidence_v1/preregistration.json \
  --results evaluator-work/inference_results.json \
  --evaluator-manifest .experiment-work/forward-v1-packages/EVALUATOR_MANIFEST.json \
  --artifact-manifest evaluator-work/FT-001-artifacts.json \
  --task-id FT-001 \
  --agent-context-sha256 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --terminal-receipt evaluator-work/FT-001-receipt.json \
  --counts evaluator-work/FT-001-counts.json \
  --started-at 2026-08-10T01:00:00Z \
  --completed-at 2026-08-10T01:20:00Z \
  --outcome success
```

工作结果文件应由公开 pending 模板复制到 evaluator-only 工作区，不能边跑边把半成品当成公开证据。`record-slot` 只接受尚未填写的槽，验证 frozen package、current contracts、规模下限、精确计数和宿主 lane 后写入 `collecting` 状态。失败槽还必须从冻结枚举中提供 `--failure-attribution`。

12 个必跑槽和可用的条件槽全部结束后才可 finalize。若 OpenCode 在整个预注册窗口不可用，可在 finalize 时给出不含本机路径或配置细节的公开原因；该命令把仍 pending 的条件槽标为 `host-unavailable`，但绝不会替必跑 Codex 槽补结果：

```bash
python scripts/forward_evidence.py finalize-results \
  --preregistration tests/fixtures/forward_evidence_v1/preregistration.json \
  --results evaluator-work/inference_results.json \
  --conditional-unavailable-reason "OpenCode host unavailable during the preregistered window"
```

只有完成文件再次通过 `status` 且显示 `current` 后，才把经过隐私检查的结果替换公开 pending 模板。不要发布 evaluator source manifest、task packages、gold、私有 receipt 或本地工作路径。

## 失败归因与停止条件

每个非成功结果只记录一个 primary attribution，按以下冻结优先级选择，不能看完结果后新增更有利的类别：

1. `protocol-contamination`：提前看到 gold/tests、上下文复用或证据链不可信；该槽无效，不进入性能分母。
2. `fixture-or-gold`：fixture/gold 自相矛盾或无法按预注册目标评分；该槽无效。
3. `host-or-tooling`：宿主或必需工具在 Agent 看见 fixture 前不可用；该槽无效。
4. `product-runtime`：确定性脚本、身份、页面、publisher 或事务实现失败。
5. `guidance-ambiguity`：Skill 给出的可见指引不足或冲突，Agent 无法可靠完成。
6. `agent-judgment`：runtime 与 guidance 均足够，Agent 仍作出错误判断或漏做要求。

每个 task-host 槽只有一次计分机会。只有在 Agent 尚未看到 fixture 时才允许一次基础设施重试。任务在 publisher 回执、安全停止、预算上限或协议失效时终止。研究不得因为前几项结果好而提前停止；只有 contract 漂移、gold/test 泄漏、系统性 fixture 缺陷或宿主不可用才能停，未运行槽必须原样公布原因。

产品按设计安全停止不等于试验失败。例如编码阻止任务正确拒绝继续、`uncertain` 测试正确阻止 apply，应该记为 `expected-product-stop`。意外停止则必须记为失败并归因。

## 统计与声明边界

公开指标仅汇总 `public-anonymous` 的有效完成槽，并按宿主和预注册 stratum 另列 task success。私有 self-attested 结果不进入公开 release metrics。协议固定公布：

- task success；
- candidate review coverage；
- delete-anchor precision；
- supported delete-anchor recall；
- required-event compliance。

每项都同时发布整数 numerator、整数 denominator、有限语料 point estimate 和双侧 Wilson score 95% 区间。零分母显示 `null`，不能显示 0% 或 100%。例如：

这些区间是对预注册计数单位的描述性二项区间；同一本书内的 candidates/anchors 存在聚类相关，不能把它们当成独立作品样本，也不能据此缩窄跨作品不确定性。

```bash
python scripts/forward_evidence.py interval 12 12
```

结果是点估计 `12/12 = 1.0`，但 95% Wilson 下界约为 `0.757506`。因此它只能表述为“这 12 个预注册任务全部通过”，不能推出跨作品 99.5% precision，更不能声称外部采用、知名网站引用或未测试宿主的效果。

## 新鲜度与发布

结果只有通过 strict schema、精确聚合、任务规模和 contract 检查后才可能为 current：

```bash
python scripts/forward_evidence.py status \
  --root . \
  --preregistration tests/fixtures/forward_evidence_v1/preregistration.json \
  --results tests/fixtures/forward_evidence_v1/inference_results.json
```

只要 runtime、guidance 或 schema 任一 framed contract 的文件集合或字节发生变化，已完成推理证据自动变为 `stale`，所有 inference claim 立即失效。重新跑 deterministic replay 不能恢复 fresh-Agent claim；必须在新 contract 上重新执行预注册 Agent 槽。pending、collecting、stopped、stale 或缺少精确分母时，README 只能陈述协议和当前边界，不能使用“同类顶级候选”之类的效果性结论。

## 执行前仍需准备的真实材料

协议和编排器不会伪造小说语料或 gold。开始 V1 inference 前，评估者还必须独立准备以下最小 evaluator source set：

1. FT-001 至 FT-012 各一份 task prompt、assigned input 和 evaluator gold；mixed 与大书的 OpenCode 复制槽 FT-013/014 可以复用 FT-004/008 的冻结 input/gold，但必须使用新的上下文和独立 receipt。
2. FT-008/014 的同一固定种子匿名大书经确定性预检后确有至少 150 candidates、700 anchors，且 occurrence 4 之后有一处 gold 标注的剧情冲突；不满足就先修 fixture 并重新冻结，不能在试验后降低门槛。
3. FT-005 的编码阻止字节样本与 FT-006 的混合编码修复样本。若公开树只接受 UTF-8 文本，可公开确定性 base64/hex fixture 描述和重建校验值，Agent 实际 assigned input 仍必须是重建后的原始字节。
4. FT-010 至 FT-012 的匿名离线页面状态和 evaluator 断言，分别覆盖单条、批量、mixed 备注、刷新恢复、导出请求；浏览器动作记录在 `browser-state` artifact。
5. 每个 public gold 在 Agent 启动前冻结 expected verdict/eligible anchor 或 segment、必须停止事件、页面状态、publisher 回执和 source/v0 不变断言。至少两名独立 evaluator 解决 gold 分歧后再冻结，不能让执行 Agent 参与定标。
6. FT-015 只有取得用户对本地私有节选的明确授权后才创建；没有授权或 OpenCode 不可用就按条件槽规则公开为未执行，不得寻找替代私有文本。

材料完成后，用 `cml.forward-package-sources.v1` 接口机械生成包。真正仍需发生的是 12 个相互隔离的 fresh Codex 上下文执行，以及条件允许时 3 个 fresh OpenCode 上下文执行；在此之前，公开结果必须保持全部 `pending`。
