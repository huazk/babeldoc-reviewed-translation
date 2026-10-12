# 本地文件回填实现

## 环境与启动

脚本需要 Python、BabelDOC（适配 0.6.4）、PyMuPDF、Pillow；图内 OCR 另需 `rapidocr_onnxruntime`。先用现有环境，不重装整个工具栈。`file_bridge.py --help` 和 `pdf_review.py --help` 不加载重型依赖，可先看参数。

`--python-path` 可重复传递，按命令中顺序赋予导入优先级；用于复用案例中的兼容包和 BabelDOC 包。不同版本先验证导入与 `TranslationConfig` 参数，不自动回退到旧版 pdf2zh。

示意命令（替换路径，PowerShell 中用 `&` 调用带空格的 Python 路径）：

```text
python file_bridge.py collect --input input.pdf --work work --assets assets
python file_bridge.py check --work work
python file_bridge.py render --input input.pdf --work work --assets assets
```

collect 的输出只是提取中间物，不可当译本交付。首次缺少字体或布局模型时可先运行 `file_bridge.py assets --work work --assets assets`，该步骤会从 BabelDOC 配置的来源下载公开资源。脚本没有翻译 API 调用；依赖库仍可能在缺少资源时尝试下载，不宣称由此实现网络隔离。

独立环境复用例子：

```text
python file_bridge.py collect --input input.pdf --work work --assets assets --python-path path/to/compat-packages --python-path path/to/babeldoc-packages --numpy-bytes-compat
```

Windows 子进程也需要在导入翻译缓存前设置缓存目录；适配器用仅在本次进程树内设置的 `REVIEW_BABELDOC_ASSETS` 传递它，避免子进程回落到不可写的用户缓存目录。中文文件验证可用 `python -X utf8`，避免系统默认 GBK 解码失败。

`--numpy-bytes-compat` 只在旧 PDF 解析代码用 NumPy `fromstring(bytes)` 的兼容性问题出现时使用。这是进程内补丁，不修改全局安装。

## 映射文件与检查

- `source-index.json`：BabelDOC 实际请求的原文字符串列表。
- `translations.json`：完整的 `{"source text": "中文译文"}` 对象。
- `keep.json`：保留不译原文的理由，如 `{"TCP": "协议缩写"}`；此项仍须出现在 translations 中并映射到自身。
- `manifest.json`：原文件 SHA-256、语言、选择页与关键排版配置，用来验证提取和回填一致。
- `requests-render.json`：实际回填请求；`missing-render.json` 必须为空。

保持原文键原样，不 trim、合并空格或重新抽取后按猜测编号匹配。使用 JSON 正确保存换行与控制字符，不通过不安全的 shell 拼接写入。脚本拒绝重复键、缺项、空译文、没有理由的原样保留和 `<bN>` 占位符损坏。

源文映射只对同一文件和同一配置有效。语言、页面或分段参数改变时新建工作目录重新 collect。不要用不同文档复用缓存。翻译器关闭译文缓存以确保修订生效。

默认参数取自已成功案例：关闭富文本翻译，关闭短行强拆分，跳过清理，最短片段 2 字符，不自动提取术语表，不回退同文译文。它们是可复现起点，不保证适合每份 PDF。脚本中的设置与 manifest 要一起更新。

公式/数值检查不能只比字符集合；最终还要看图。变量 `u/v/x` 和 `1/l/I` 很容易被 OCR 混淆；保持原数据，不把 OCR 当真值。

## 图内 OCR

```text
python pdf_review.py ocr --input original.pdf --out ocr.json
```

OCR 只检测原 PDF 的图片区域，输出原页坐标、候选英文、置信度、与原生文字层重叠标记。它不自动翻译或覆盖。纯扫描页可以作为大图片检测，但需要重新评估布局方案；大量正文都在图片里时，不能依靠普通文本回填宣称完成。旋转页会标记并跳过，先处理坐标变换或使用专用 OCR 路线。

原生文字重叠提示用于人工复核，不能简单把所有重叠项都丢弃，尤其是带 OCR 隐藏文字层的扫描件。识别结果中有公式、数值或图线干扰时，直接对照原图。

## 逐文档修版

在任务工作目录写一个可重复运行的补丁脚本，始终打开 BabelDOC 未修补的 mono 作为起点。OCR 标签和原生文字重排分别处理：

- 简单纯色图片背景：从原图估计背景色，绘制最小覆盖矩形，再插入中文。保留箭头和线条；复杂背景不要粗暴白块覆盖。
- 原生文字：先明确矩形内所有内容，用 `add_redact_annot` 和 `apply_redactions(images=0, graphics=0, text=0)` 删除旧文字而保留图像/矢量，再回填。核实当前 PyMuPDF 中这些参数的含义。
- 公式、表头、路径序列可能与正文共用矩形。清理前列出会受影响的内容；必要时完整恢复，不能只覆盖而忘了重画。
- 根据字体实际宽度安排换行并检验高度；不要默默裁掉尾行或把字号缩到无法阅读。多项目合段时恢复每条项目的完整含义。
- 字体使用支持目标语言的可嵌入字体，保存前 `subset_fonts()`。多轮修改后核对文本抽取与视觉效果，避免表面遮住但仍残留旧文字层。

页号和矩形必须根据当前文档观察得到，不可沿用案例的页 12、43 等坐标。

## 质检与双语输出

```text
python pdf_review.py qa --input final-mono.pdf --out qa
python pdf_review.py dual --input original.pdf --translated final-mono.pdf --out final-dual.pdf
python pdf_review.py verify --input original.pdf --translated final-mono.pdf --dual final-dual.pdf
```

qa 为所有页面生成 PNG、保持宽高比的联系表和字符统计。联系表用于发现问题；密集文字和公式须看单页。低中文字数只是复核信号，纯拓扑页可能正常。

dual 严格按原文第 1 页、译文第 1 页、原文第 2 页、译文第 2 页顺序构建。verify 核对页数、尺寸，以及默认首尾/中间代表页的像素一致性；`--all-pages` 可核对全部页。它只验证拼接和页序，不证明译文准确。

不要把源文件覆盖为输出，不输出测试或未校对版本作为成品。长任务保留映射、术语、修版记录和待检查页清单，让下一轮可以续做。
