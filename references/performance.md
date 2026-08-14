# 广告扫描性能基准

`scripts/benchmark_scan.py` 使用固定生成器测试两种画像：叙事为主（每 128 个段落一个明确外部标记）和高候选密度（每 4 个段落一个明确外部标记）。每个段落含字母唯一标记，避免数字归一化后意外折叠。

基准在独立 Python 子进程中运行。耗时覆盖候选扫描、生产用 candidate fingerprint 和 anchor ID；峰值内存是包含输入生成、扫描、身份和候选哈希的进程 peak RSS。候选哈希本身不计入耗时。`boundary` 是默认支持模式；`all` 是显式深扫模式，不适用 60 秒和 1 GiB 的默认模式目标。

快速回归：

```powershell
python scripts/benchmark_scan.py --profile ci --scope both --workload both --repeat 3 --baseline tests/performance/scan_baseline_ci.json
```

完整矩阵回归：

```powershell
python scripts/benchmark_scan.py --profile full --scope both --workload both --repeat 3 --baseline tests/performance/scan_baseline_full.json
```

仅在人工确认并接受性能或语义变化后冻结新基线：

```powershell
python scripts/benchmark_scan.py --profile full --scope both --workload both --repeat 3 --freeze-baseline tests/performance/scan_baseline_full.json
```

所有环境都比较输入哈希、有序候选哈希和候选集合哈希。`ci` 基线只作语义门禁，避免小样本和共享 CI 硬件产生计时假失败。完整基线且机器、Python 和 CPU 元数据完全相同时，会执行中位耗时相对冻结基线不超过 15% 的检查。固定 runner 必须同时传入 `--require-comparable-baseline`；环境不匹配或比较仅为 semantic-only 时命令会失败，而不是静默跳过 15% 门禁。完整基线还要求 40 MB `boundary` 中位耗时不超过 60 秒、peak RSS 不超过 1 GiB，且 `T(40 MB) / T(20 MB) <= 2.6`。

GitHub 托管 runner 每次运行硬件可能不同，因此普通 CI 的 `performance` 作业执行完整 `boundary` 画像，强制语义、60 秒、1 GiB 和扩展比目标；15% 相对回归由带 `cml-performance` 标签的固定自托管 runner 在手动 `run_fixed_performance` 工作流中执行。基线只应在有意接受生成器、候选语义或经复核的性能变化时重新冻结。
