# OrbitServe 研究总结与实验报告

- `OrbitServe_Agent_研究总结与实验报告.docx`：可编辑报告，22页。
- 同名PDF：已逐页渲染检查的阅览版。
- `report_source.md`：完整文字与表格来源。
- `report_model.json`：可编辑的结构化排版来源。
- `figures/`：报告使用的6幅图；其中架构图为标注的AI概念图，3幅科学图来自真实像元，2幅性能图来自模型结果。
- `previews/`：封面、统计结果和真实产品页预览。
- `sources/`：冻结的报告数据、统计汇总、图件来源和排版核验。

## 重建DOCX

在本目录安装 `requirements-report.txt` 后执行：

```bash
python report_build.py --model report_model.json --output OrbitServe_Agent_研究总结与实验报告.docx
```

正文采用固定22磅行距，插图段落为单倍行距。DOCX指定中文宋体、英文Times New Roman；当前PDF渲染环境缺少这两种原字体，采用Noto Serif CJK SC和Liberation Serif。该替代已在报告第一页说明。在装有所需字体的Word环境打开DOCX并导出PDF，可生成精确字体版本。

## 重建科学与模型图

先从项目数据归档恢复真实缓存和最终结果，保留 `normal-final/` 与 `model-v2-replay/` 目录结构。在安装OrbitServe及其依赖的环境中执行：

```bash
python prepare_display_rgb.py --data-root /path/to/data
export ORBITSERVE_STUDY_ROOT=/path/to/semantic-runs
python build_figures.py
```

`prepare_display_rgb.py`只为报告重建前期RGB显示数组，不改变科学产品；其结果与本报告使用数组已逐值验证一致。原TIFF、NPZ与完整逐运行结果由数据归档提供，不复制进报告代码目录。统计图读取随包固定的 `sources/sweep_summary.json`。概念图保留原始图像文件，不由数值绘图脚本生成。

## 证据范围

实际科学批次为normal-final和focused-final，共45次合同通过、42次产品交付、3次正确缺数据。性能表使用v2同图回放，统计来自每族50个独立world的三产品配对；完整方法和负结果见报告。实际API评测由使用者本地配置端点、模型和密钥后进行。详见仓库 `docs/semantic_harness.md` 和 `docs/semantic_world_sweep.md`。
